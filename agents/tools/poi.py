"""
POI 工具 — OpenStreetMap Overpass API
查询 Rochester 及周边景点，按交通方式限制活动半径。

Rochester 中心: lat=43.1566, lon=-77.6088
"""

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

import httpx

from cache import result_cache

logger = logging.getLogger(__name__)

ROCHESTER_LAT = 43.1566
ROCHESTER_LON = -77.6088
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# 兴趣类型 → Overpass 查询标签
INTEREST_TAGS: dict[str, list[tuple[str, str]]] = {
    "nature":   [("leisure", "nature_reserve"), ("natural", "waterfall"),
                 ("leisure", "park"), ("boundary", "national_park")],
    "winery":   [("craft", "winery"), ("amenity", "winery"),
                 ("tourism", "wine_cellar")],
    "food":     [("amenity", "restaurant"), ("amenity", "food_court"),
                 ("amenity", "marketplace")],
    "history":  [("historic", "monument"), ("historic", "building"),
                 ("tourism", "museum"), ("historic", "memorial")],
    "arts":     [("tourism", "museum"), ("tourism", "gallery"),
                 ("amenity", "arts_centre")],
    "outdoor":  [("leisure", "hiking"), ("route", "hiking"),
                 ("leisure", "swimming_area"), ("sport", "climbing")],
}

# 历史遗留：这批数据已迁入 agents/tools/poi_table.json（curated=true），
# 保留字面量仅供参照和灾难恢复，运行时不再使用它。
_LEGACY_KNOWN_ATTRACTIONS: list[dict] = [
    {"name": "Letchworth State Park",          "lat": 42.5706, "lon": -78.0517, "category": "nature",   "cost_type": "state_park",      "distance_km": 65},
    {"name": "Watkins Glen State Park",         "lat": 42.3776, "lon": -76.8719, "category": "nature",   "cost_type": "state_park",      "distance_km": 110},
    {"name": "George Eastman Museum",           "lat": 43.1548, "lon": -77.5982, "category": "history",  "cost_type": "museum",          "distance_km": 3},
    {"name": "Strong National Museum of Play",  "lat": 43.1499, "lon": -77.5998, "category": "arts",     "cost_type": "museum",          "distance_km": 4},
    {"name": "High Falls Rochester",            "lat": 43.1578, "lon": -77.6150, "category": "nature",   "cost_type": "free_attraction", "distance_km": 1},
    {"name": "Ontario Beach Park",              "lat": 43.2637, "lon": -77.6069, "category": "nature",   "cost_type": "free_attraction", "distance_km": 13},
    {"name": "Seneca Lake Wine Trail",          "lat": 42.7007, "lon": -76.9307, "category": "winery",   "cost_type": "winery_tasting",  "distance_km": 90},
    {"name": "Keuka Lake Wineries",             "lat": 42.6134, "lon": -77.0975, "category": "winery",   "cost_type": "winery_tasting",  "distance_km": 80},
    {"name": "Taughannock Falls",               "lat": 42.5370, "lon": -76.5993, "category": "nature",   "cost_type": "state_park",      "distance_km": 120},
    {"name": "Genesee Valley Park",             "lat": 43.1166, "lon": -77.6133, "category": "outdoor",  "cost_type": "free_attraction", "distance_km": 5},
    {"name": "Canandaigua Lake",                "lat": 42.8890, "lon": -77.2927, "category": "nature",   "cost_type": "free_attraction", "distance_km": 40},
    {"name": "Bristol Mountain",                "lat": 42.7306, "lon": -77.3672, "category": "outdoor",  "cost_type": "free_attraction", "distance_km": 55},
    {"name": "Corning Museum of Glass",         "lat": 42.1442, "lon": -77.0553, "category": "arts",     "cost_type": "museum",          "distance_km": 130},
    {"name": "Rochester Public Market",         "lat": 43.1568, "lon": -77.5960, "category": "food",     "cost_type": "free_attraction", "distance_km": 3},
]


# ── 景点表：单一事实来源 ──────────────────────────────────────
# agents/tools/poi_table.json 同时收录手工整理的景点和从语料（rag/data/raw/*.jsonl）
# 抽出来的实体。骨架由 `python -m rag.build_poi_table` 生成，属性字段手工校正——
# 判错了就直接改 JSON，不要去改推断规则。
#
# wheelchair_accessible / pet_friendly 目前几乎全是 null：语料里没有任何真实的
# 无障碍信息（出现的 "accessible" 全是"门槛低""公有土地"之类的别义）。
# null 表示"未知"，语义上不同于 False——Verifier 对二者的处理也不同。
_TABLE_PATH = Path(__file__).parent / "poi_table.json"

# 进入候选池必须齐备的字段
_REQUIRED_FIELDS = ("name", "lat", "lon", "category", "cost_type", "distance_km")
# 供下游（排程、校验）使用的属性字段，缺失时为 None
_ATTR_FIELDS = ("indoor", "kid_friendly", "wheelchair_accessible",
                "pet_friendly", "typical_visit_min", "season_note")


def _load_attractions() -> list[dict]:
    if not _TABLE_PATH.exists():
        logger.error(f"{_TABLE_PATH.name} 不存在，候选池为空。运行 python -m rag.build_poi_table")
        return []
    try:
        rows = json.loads(_TABLE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"{_TABLE_PATH.name} 读取失败: {e}")
        return []

    out = []
    for r in rows:
        if not r.get("enabled"):
            continue
        missing = [f for f in _REQUIRED_FIELDS if r.get(f) is None]
        if missing:
            logger.warning(f"poi_table: {r.get('name')!r} 缺字段 {missing}，跳过")
            continue
        # 只取 pipeline 实际用到的字段，不把 *_source / note 之类元信息带进 prompt
        out.append(
            {f: r[f] for f in _REQUIRED_FIELDS}
            | {f: r.get(f) for f in _ATTR_FIELDS}
            | {"curated": bool(r.get("curated"))}
        )
    return out


ALL_ATTRACTIONS: list[dict] = _load_attractions()
# 向后兼容：build_poi_table 用它做去重
KNOWN_ATTRACTIONS: list[dict] = [a for a in ALL_ATTRACTIONS if a["curated"]] or _LEGACY_KNOWN_ATTRACTIONS
logger.info(
    f"POI pool: {len(ALL_ATTRACTIONS)} attractions "
    f"({sum(1 for a in ALL_ATTRACTIONS if a['curated'])} curated), "
    f"{sum(1 for a in ALL_ATTRACTIONS if a['wheelchair_accessible'] is None)} with unknown accessibility"
)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


async def search_attractions(
    query: str = "",
    interests: list[str] | None = None,
    radius_km: float = 150,
    max_results: int = 8,
) -> list[dict]:
    """
    查询 Rochester 周边景点。
    interests: ["nature", "winery", ...] 按兴趣过滤
    radius_km: 活动半径，来自 UserProfile.transport_radius_km
    """
    cache_key = ("poi", str(sorted(interests or [])), str(int(radius_km)))
    cached = result_cache.get(*cache_key)
    if cached:
        return cached

    # 先从预置数据 + 语料扩充候选中筛选
    results = []
    for attr in ALL_ATTRACTIONS:
        dist = attr.get("distance_km", _haversine_km(
            ROCHESTER_LAT, ROCHESTER_LON, attr["lat"], attr["lon"]
        ))
        if dist > radius_km:
            continue
        if interests and attr.get("category") not in interests:
            continue
        if query and query.lower() not in attr["name"].lower() and query.lower() not in attr.get("description", "").lower():
            continue
        results.append({**attr, "distance_km": round(dist, 1), "source": "known"})

    # 按距离排序
    results.sort(key=lambda x: x["distance_km"])

    # 如果预置数据不够，补充 Overpass 查询
    if len(results) < max_results and interests:
        osm_results = await _query_overpass(interests, radius_km)
        existing_names = {r["name"] for r in results}
        for r in osm_results:
            if r["name"] not in existing_names:
                results.append(r)
                if len(results) >= max_results:
                    break

    result_cache.set(*cache_key, value=results[:max_results], ttl_sec=3600)
    return results[:max_results]


async def _query_overpass(interests: list[str], radius_km: float) -> list[dict]:
    tags = []
    for interest in interests:
        tags.extend(INTEREST_TAGS.get(interest, []))

    if not tags:
        return []

    radius_m = int(radius_km * 1000)
    tag_queries = "\n".join(
        f'  node["{k}"="{v}"](around:{radius_m},{ROCHESTER_LAT},{ROCHESTER_LON});'
        for k, v in tags[:6]  # 限制查询数量
    )
    query = f"[out:json][timeout:20];\n(\n{tag_queries}\n);\nout body 20;"

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            data = resp.json()

        results = []
        for el in data.get("elements", []):
            tags_data = el.get("tags", {})
            name = tags_data.get("name") or tags_data.get("name:en") or tags_data.get("name:zh")
            if not name:
                continue
            lat, lon = el.get("lat", ROCHESTER_LAT), el.get("lon", ROCHESTER_LON)
            dist = _haversine_km(ROCHESTER_LAT, ROCHESTER_LON, lat, lon)
            results.append({
                "name": name,
                "lat": lat, "lon": lon,
                "distance_km": round(dist, 1),
                "description": tags_data.get("description", ""),
                "source": "osm",
            })
        return sorted(results, key=lambda x: x["distance_km"])
    except Exception:
        return []
