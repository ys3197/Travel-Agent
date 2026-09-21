"""
从已爬取的语料构建候选景点表 —— 把 rag/data/raw/*.jsonl 里的实体接进 POI 候选池。

背景：语料有 45 个实体，而 agents/tools/poi.py 的 KNOWN_ATTRACTIONS 只有 14 个。
其余实体有完整资料却永远进不了候选池——实体表和语料本该是同一份东西的两面。

产物是 agents/tools/poi_extra.json，一份**可以手工校正**的数据文件：
坐标、类别、费用类型都带 *_source 字段标明来源，enabled=false 的条目附 note 说明原因。
推断错了就直接改 JSON，不要改这个脚本的启发式规则。

用法:
    python -m rag.build_poi_table            # 增量：只地理编码新增条目
    python -m rag.build_poi_table --refresh  # 全量重建（会重新请求 Nominatim）
"""

import argparse
import json
import logging
import math
import re
import sys
import time
from pathlib import Path

import httpx

from rag.poi_names import POI_ALIASES, normalize_poi

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

RAW_DIR = Path(__file__).parent / "data" / "raw"
OUT_PATH = Path(__file__).parent.parent / "agents" / "tools" / "poi_table.json"

# 手工维护的属性字段：新条目建为 None，重跑时**绝不覆盖**已有值
ATTR_FIELDS = ("indoor", "kid_friendly", "wheelchair_accessible",
               "pet_friendly", "typical_visit_min", "season_note")

ROCHESTER_LAT, ROCHESTER_LON = 43.1566, -77.6088
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "travel-agent-poi-table/1.0 (offline data build)"
MAX_DISTANCE_KM = 200          # 超出此范围视为地理编码失败（多半匹配到了别的地方）

# ── 排除：不是「可以在行程里停留的地点」的实体 ────────────────────
# 语料里混了泛指条目（Wikipedia 的城市/湖区总览）、活动、以及服务类商家。
GENERIC_TITLES = {
    "Rochester, New York", "Finger Lakes", "Seneca Lake", "Keuka Lake",
    "Charlotte, Rochester, New York",
}
NOT_A_PLACE_PATTERNS = [
    (r"\bfestival\b",            "活动而非地点"),
    (r"\bseries\b",              "系列演出而非地点"),
    (r"\btours?\b.*\bllc\b",     "旅游服务商而非地点"),
    (r"\bgame\b",                "线上/线下活动而非地点"),
]

# ── 类别推断：按顺序匹配，先命中者胜 ──────────────────────────────
# 语料 tags 只有 {free, history, family, winery, nature}，覆盖不到 arts/food/outdoor，
# 所以标题关键词优先，tags 兜底。
CATEGORY_RULES = [
    ("winery",  [r"winery", r"\bwine\b", r"vineyard", r"cider", r"brewery", r"distiller"]),
    ("arts",    [r"museum of (glass|play)", r"\bgallery\b", r"arts? cent(er|re)",
                 r"\btheat(er|re)\b", r"\bdance\b", r"school of music", r"\bcmac\b",
                 r"artisan"]),
    ("history", [r"historical", r"\bhistoric\b", r"lighthouse", r"\bvillage\b",
                 r"\bmuseum\b", r"\bheritage\b"]),
    ("food",    [r"\bmarket\b", r"restaurant", r"\bkitchen\b", r"\bfood\b",
                 r"buster", r"\bhoney\b"]),
    ("outdoor", [r"\bpark\b", r"\btrail\b", r"\bgolf\b", r"\bgym\b", r"climb",
                 r"mountain", r"adventure", r"trampoline", r"\bmarina\b", r"\bbeach\b"]),
    ("nature",  [r"\bfalls\b", r"\bcreek\b", r"\blake\b", r"\bgorge\b", r"\bnature\b",
                 r"animal", r"\bwoods\b"]),
]

# ── 费用类型推断 → 对应 orchestrator.MIN_VISIT_MIN / style_costs 的键 ──
COST_RULES = [
    ("state_park",      [r"state park"]),
    ("winery_tasting",  [r"winery", r"\bwine\b", r"vineyard", r"cider", r"distiller"]),
    ("museum",          [r"\bmuseum\b", r"\bgallery\b"]),
    ("historic_site",   [r"historical", r"\bhistoric\b", r"lighthouse", r"\bvillage\b"]),
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _extract_address(raw_text: str) -> tuple[str, str] | None:
    """
    VisitRochester 条目的地址通常独占一行：'<街道> <城市>, NY <邮编>'。
    返回 (清洗后的完整地址, 城市)。Suite/Floor/单元号会被剥掉——Nominatim 会因此匹配失败。
    """
    for line in (l.strip() for l in raw_text.splitlines()):
        m = re.search(r"^(.*?),?\s*([A-Z][A-Za-z.' ]+),\s*NY\s*(\d{5})(?:-\d{4})?$", line)
        if not m or len(line) > 120:
            continue
        street = re.sub(r",?\s*(suite|ste\.?|floor|fl\.?|unit|#)\s*\S+", "", m.group(1), flags=re.I)
        city = m.group(2).strip()
        street = re.sub(r"\s+", " ", street).strip(" ,")
        return f"{street}, {city}, NY {m.group(3)}", city
    return None


def _infer(rules: list[tuple[str, list[str]]], text: str, default: str) -> tuple[str, str]:
    """按规则表推断，返回 (值, 依据)。"""
    low = text.lower()
    for value, patterns in rules:
        for p in patterns:
            if re.search(p, low):
                return value, f"rule:{p}"
    return default, "default"


def _not_a_place(title: str) -> str | None:
    low = title.lower()
    for pattern, reason in NOT_A_PLACE_PATTERNS:
        if re.search(pattern, low):
            return reason
    return None


def load_entities() -> list[dict]:
    """读取全部原始语料，按 title 去重（同名取正文更长的那条）。"""
    by_title: dict[str, dict] = {}
    for path in sorted(RAW_DIR.glob("*.jsonl")):
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            t = d.get("title")
            if not t:
                continue
            if t not in by_title or len(d.get("raw_text", "")) > len(by_title[t].get("raw_text", "")):
                by_title[t] = d
    return list(by_title.values())


def _geocode_once(client: httpx.Client, query: str) -> tuple[float, float] | None:
    try:
        resp = client.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        hits = resp.json()
    except Exception as e:
        logger.warning(f"geocode error for {query!r}: {e}")
        return None
    time.sleep(1.1)                       # Nominatim 限速：1 req/s
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def geocode(client: httpx.Client, title: str, addr: tuple[str, str] | None) -> tuple[tuple[float, float] | None, str]:
    """
    依次尝试：完整地址 → 名称+所在城市 → 名称+州。
    返回 (坐标, 实际生效的查询串)。按名称查时不硬编 Rochester——
    语料里有 120km 外的景点，写死城市反而会匹配到错误的地点。
    """
    queries = []
    if addr:
        queries.append(addr[0])
        queries.append(f"{title}, {addr[1]}, NY")
    queries.append(f"{title}, New York State")

    for q in queries:
        coords = _geocode_once(client, q)
        if coords:
            return coords, q
    return None, queries[0]


def build(refresh: bool = False) -> list[dict]:
    from agents.tools.poi import KNOWN_ATTRACTIONS

    # 用归一化名去重：语料里的 'Taughannock Falls State Park' / 'The Strong National
    # Museum of Play' 其实就是 KNOWN_ATTRACTIONS 里的条目，只是写法不同。
    known_norm = {normalize_poi(a["name"]) for a in KNOWN_ATTRACTIONS}
    known_norm |= {normalize_poi(v) for v in POI_ALIASES.values()}
    generic_norm = {normalize_poi(t) for t in GENERIC_TITLES}

    # 已有的表永远原样保留——里面有手工校正过的属性和 curated 条目。
    # --refresh 只影响"没见过的新条目要不要重新地理编码"，不会推翻已有数据。
    cached: dict[str, dict] = {}
    if OUT_PATH.exists():
        cached = {r["name"]: r for r in json.loads(OUT_PATH.read_text(encoding="utf-8"))}
        logger.info(f"Preserving {len(cached)} existing rows in {OUT_PATH.name}")

    # 表里已有的行一律原样带过来——包括 curated 条目和所有手工校正过的属性。
    # 这个循环只负责**发现新实体**，绝不重建已有数据。
    rows: list[dict] = list(cached.values())
    client = httpx.Client()

    for ent in sorted(load_entities(), key=lambda d: d["title"]):
        title = ent["title"]

        if title in cached:          # 已在表中，上面已带过
            continue

        norm = normalize_poi(title)
        if norm in known_norm:
            logger.info(f"skip  {title}  (已是表内某条目，或是其别名)")
            continue
        if norm in generic_norm:
            logger.info(f"skip  {title}  (泛指条目，非具体景点)")
            continue

        tags = ent.get("tags", [])

        # 类别优先从**标题**推断。正文会把话带偏——'Genesee Country Village &
        # Museum' 的介绍里提到 brewery，用全文推断会被判成 winery。
        category, cat_src = _infer(CATEGORY_RULES, title, default="")
        if not category:
            for t in ("winery", "nature", "history"):      # tags 兜底
                if t in tags:
                    category, cat_src = t, f"tag:{t}"
                    break
        if not category:
            category, cat_src = _infer(CATEGORY_RULES, ent.get("raw_text", "")[:400], default="")
            cat_src = f"text:{cat_src}" if category else ""
        if not category:
            category, cat_src = "history", "fallback"

        cost_type, cost_src = _infer(COST_RULES, title, default="free_attraction")

        addr = _extract_address(ent.get("raw_text", ""))
        coords, query = geocode(client, title, addr)

        row: dict = {
            "name": title,
            "category": category,
            "cost_type": cost_type,
            # 属性字段留空待人工填写——语料里没有可靠依据推断这些
            **{f: None for f in ATTR_FIELDS},
            "enabled": True,
            "curated": False,
            "source": ent.get("source", ""),
            "url": ent.get("url", ""),
            "tags": tags,
            "category_source": cat_src,
            "cost_type_source": cost_src,
            "geocode_query": query,
        }

        reason = _not_a_place(title)
        if reason:
            row.update(enabled=False, note=reason)

        if coords is None:
            row.update(enabled=False, note=row.get("note") or "地理编码失败，缺坐标",
                       lat=None, lon=None, distance_km=None)
        else:
            lat, lon = coords
            dist = round(_haversine_km(ROCHESTER_LAT, ROCHESTER_LON, lat, lon), 1)
            row.update(lat=round(lat, 4), lon=round(lon, 4), distance_km=dist)
            if dist > MAX_DISTANCE_KM:
                row.update(enabled=False,
                           note=row.get("note") or f"地理编码结果距 Rochester {dist}km，疑似匹配错误")

        flag = "  " if row["enabled"] else "!!"
        logger.info(f"{flag} {title[:40]:42} {category:8} {cost_type:15} "
                    f"{row.get('distance_km')}km  {row.get('note','')}")
        rows.append(row)

    client.close()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，全量重建")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent.parent))
    rows = build(refresh=args.refresh)

    OUT_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    on = sum(1 for r in rows if r["enabled"])
    logger.info(f"\nWrote {OUT_PATH} — {on} enabled / {len(rows)} total")
    logger.info("推断错了就直接改这个 JSON（enabled / category / cost_type / lat / lon）")


if __name__ == "__main__":
    main()
