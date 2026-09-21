"""
Orchestrator Agent — 并行拉取 weather + POI 候选，返回候选列表给 Planner 选择。
时间/费用计算在 _build_schedule() 里完成（Python 保证正确性），由 Planner 在模型选定景点后调用。
"""

import asyncio
import logging
from datetime import datetime, timedelta

from agents.tools.poi import search_attractions
from agents.tools.weather import get_weather
from user_profile import UserProfile, Interest, TravelStyle

logger = logging.getLogger(__name__)

INTEREST_MAP = {
    Interest.NATURE:  "nature",
    Interest.WINERY:  "winery",
    Interest.FOOD:    "food",
    Interest.HISTORY: "history",
    Interest.ARTS:    "arts",
    Interest.OUTDOOR: "outdoor",
}

AVG_SPEED_KMH = 70

MIN_VISIT_MIN = {
    "state_park":      150,
    "museum":           90,
    "winery_tasting":   90,
    "free_attraction":  45,
    "historic_site":    60,
}

LUNCH_DURATION_MIN = 60


def _drive_min(distance_km: float) -> int:
    return max(10, int(distance_km / AVG_SPEED_KMH * 60))


def _departure_km(departure_name: str) -> float:
    """估算出发地距 Rochester 市中心的距离（km）。"""
    name = departure_name.lower()
    if any(kw in name for kw in ["university of rochester", "u of r", "river campus", "rochester u"]):
        return 3.5
    if any(kw in name for kw in ["pittsford", "victor", "henrietta", "brighton"]):
        return 10.0
    if any(kw in name for kw in ["downtown", "market", "main st", "midtown"]):
        return 0.5
    return 2.0   # 默认：市区内某处


def _add_minutes(time_str: str, minutes: int) -> str:
    h, m = map(int, time_str.split(":"))
    dt = datetime(2000, 1, 1, h, m) + timedelta(minutes=minutes)
    return dt.strftime("%H:%M")


def _to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _build_schedule(
    attractions: list[dict],
    start_time: str,
    end_time: str,
    has_car: bool,
    style: TravelStyle,
    departure_name: str = "University of Rochester",
    departure_km: float = 3.5,   # U of R is ~3.5km from downtown Rochester
) -> list[dict]:
    """
    接收模型选定并排序的景点列表，计算精确时间槽，插入午餐。
    首站从出发地（默认 U of R）出发，计入初始交通时间。
    """
    style_costs = {
        TravelStyle.BUDGET:   {"state_park": 10, "museum": 0,  "winery_tasting": 0,  "free_attraction": 0, "historic_site": 0},
        TravelStyle.MIDRANGE: {"state_park": 10, "museum": 17, "winery_tasting": 20, "free_attraction": 0, "historic_site": 8},
        TravelStyle.LUXURY:   {"state_park": 10, "museum": 20, "winery_tasting": 35, "free_attraction": 0, "historic_site": 15},
    }[style]

    lunch_cost = {TravelStyle.BUDGET: 15, TravelStyle.MIDRANGE: 25, TravelStyle.LUXURY: 45}[style]

    schedule = []
    cursor = _to_minutes(start_time)
    end_total = _to_minutes(end_time)

    # ── 出发地条目 ───────────────────────────────────────────
    if attractions:
        first_dist = attractions[0].get("distance_km", 0)
        # U of R is departure_km from downtown; add to get distance from U of R to first attraction
        initial_dist = first_dist + departure_km
        if has_car:
            initial_drive = _drive_min(initial_dist)
        else:
            # Uber: slower in urban traffic
            initial_drive = max(15, int(initial_dist / 25 * 60))

        schedule.append({
            "name": departure_name,
            "start_time": start_time,
            "end_time": start_time,
            "cost_usd": 0,
            "category": "departure",
            "travel_min_to_next": initial_drive,
            "transport_to_next": "drive" if has_car else "Uber/Lyft",
            "notes": "出发地点",
        })
        cursor += initial_drive

    lunch_inserted = False
    prev_drive_min = 0   # drive from previous attraction to current (0 for first)

    for i, a in enumerate(attractions):
        # 逐景点的建议时长优先；缺失时退回按费用类型估算
        visit_min = a.get("typical_visit_min") or MIN_VISIT_MIN.get(
            a.get("cost_type", "free_attraction"), 60
        )

        # 检查是否还塞得下：前往 + 午餐（若未安排）+ 游览
        lunch_buffer = LUNCH_DURATION_MIN if not lunch_inserted else 0
        if cursor + prev_drive_min + lunch_buffer + visit_min > end_total:
            break

        # 前进交通时间
        cursor += prev_drive_min

        # 到了 11:30 就插午餐（吃完再去下一个）
        if not lunch_inserted and cursor >= _to_minutes("11:30"):
            schedule.append({
                "name": "午餐",
                "start_time": _add_minutes("00:00", cursor),
                "end_time": _add_minutes("00:00", cursor + LUNCH_DURATION_MIN),
                "cost_usd": lunch_cost,
                "category": "food",
                "travel_min_to_next": 0,
                "transport_to_next": "",
                "notes": "",
            })
            cursor += LUNCH_DURATION_MIN   # ← 只加午餐时长，不再重复加 drive_min
            lunch_inserted = True

        end_slot = cursor + visit_min
        if end_slot > end_total:
            break

        # 下一站交通时间（用下一景点与市中心的距离估算）
        next_drive = (
            _drive_min(attractions[i + 1].get("distance_km", 0))
            if i + 1 < len(attractions) else 0
        )

        schedule.append({
            "name": a["name"],
            "start_time": _add_minutes("00:00", cursor),
            "end_time": _add_minutes("00:00", end_slot),
            "cost_usd": style_costs.get(a.get("cost_type", "free_attraction"), 0),
            "category": a.get("category", ""),
            "travel_min_to_next": next_drive,
            "transport_to_next": "drive" if has_car else "walk/Uber",
            "notes": "",
            # 透传景点属性供 Verifier 校验——不能靠事后去 grep 模型自己写的 notes
            "wheelchair_accessible": a.get("wheelchair_accessible"),
            "pet_friendly": a.get("pet_friendly"),
            "indoor": a.get("indoor"),
            "distance_km": a.get("distance_km"),
        })

        cursor = end_slot + 5   # 5min 缓冲
        prev_drive_min = next_drive

        if len([s for s in schedule if s["name"] not in ("午餐", departure_name)
                and s.get("category") != "departure"]) >= 4:
            break

    # 若还没插午餐，在行程结束后加上
    non_depart = [s for s in schedule if s.get("category") != "departure"]
    if not lunch_inserted and non_depart:
        last_end = _to_minutes(non_depart[-1]["end_time"])
        if last_end + LUNCH_DURATION_MIN <= end_total:
            schedule.append({
                "name": "午餐",
                "start_time": non_depart[-1]["end_time"],
                "end_time": _add_minutes(non_depart[-1]["end_time"], LUNCH_DURATION_MIN),
                "cost_usd": lunch_cost,
                "category": "food",
                "travel_min_to_next": 0,
                "transport_to_next": "",
                "notes": "",
            })

    # 最后一条非出发地条目不应有 travel_min_to_next（没有下一站了）
    real_slots = [s for s in schedule if s.get("category") not in ("departure", "food")]
    if real_slots:
        real_slots[-1]["travel_min_to_next"] = 0
        real_slots[-1]["transport_to_next"] = ""

    return schedule


def repair_schedule_to_budget(
    attractions: list[dict],
    budget_usd: float,
    **build_kwargs,
) -> tuple[list[dict], list[dict], float, list[str]]:
    """
    超预算的确定性修复：反复丢掉最贵的一站并重排，直到进预算或无从再丢。

    费用是 _build_schedule 用 Python 算出来的确定值，不是模型猜的——所以超预算
    根本不需要让 LLM 重新规划。原先的做法是整条流水线重跑（含两次 LLM 调用），
    而且 Phase 1 是 temperature=0.7 的重新选择，并不保证会避开那个贵的景点。

    返回 (保留的景点, schedule, 总费用, 被丢掉的站名)。
    若剩余站点全部免费仍超预算（例如仅午餐就超了），原样返回交给 Verifier 报错。
    """
    remaining = list(attractions)
    dropped: list[str] = []

    while True:
        schedule = _build_schedule(remaining, **build_kwargs)
        total = sum(s["cost_usd"] for s in schedule)
        if total <= budget_usd or not remaining:
            return remaining, schedule, total, dropped

        cost_by_name = {
            s["name"]: s["cost_usd"]
            for s in schedule
            if s.get("category") not in ("departure", "food")
        }
        if not cost_by_name or max(cost_by_name.values()) <= 0:
            # 能丢的都是免费站点，丢了也降不下来——不做无谓删减
            logger.info(f"Budget repair stopped: ${total:.0f} > ${budget_usd:.0f} but all stops are free")
            return remaining, schedule, total, dropped

        victim = max(cost_by_name, key=cost_by_name.get)
        dropped.append(victim)
        remaining = [a for a in remaining if a["name"] != victim]
        logger.info(
            f"Budget repair: dropped {victim!r} (${cost_by_name[victim]:.0f}), "
            f"total was ${total:.0f} / budget ${budget_usd:.0f}"
        )


async def run_orchestrator(
    profile: UserProfile,
    date: str | None = None,
) -> dict:
    """
    并行拉取天气 + POI 候选，过滤后返回候选列表。
    不预排行程——由 Planner 让模型选景点后再调用 _build_schedule()。
    """
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    interest_strs = [INTEREST_MAP[i] for i in profile.interests]

    logger.info("Orchestrator: fetching weather + POI in parallel")

    weather_task = asyncio.create_task(get_weather(target_date))
    poi_task = asyncio.create_task(
        search_attractions(
            interests=interest_strs,
            radius_km=profile.transport_radius_km,
            max_results=15,
        )
    )
    weather, raw_attractions = await asyncio.gather(weather_task, poi_task)

    # 天气不好时按 indoor 属性过滤，而不是按 category。
    # category=="outdoor" 会误伤室内场馆（蹦床馆类目是 outdoor 但显然不怕下雨），
    # 也漏掉真正露天的 nature 类景点。indoor 是逐条核实过的事实字段。
    if not weather.get("is_outdoor_ok"):
        before = len(raw_attractions)
        raw_attractions = [a for a in raw_attractions if a.get("indoor") is not False]
        logger.info(f"Weather not outdoor-friendly: {before} → {len(raw_attractions)} candidates")

    # 无障碍是硬约束：明确标注不可达的直接排除。
    # 注意 None 表示"未知"而非"可达"——未知的保留在候选里，由 Verifier 提示用户自行确认。
    if profile.accessible:
        before = len(raw_attractions)
        raw_attractions = [a for a in raw_attractions if a.get("wheelchair_accessible") is not False]
        unknown = sum(1 for a in raw_attractions if a.get("wheelchair_accessible") is None)
        logger.info(f"Accessibility filter: {before} → {len(raw_attractions)} ({unknown} unknown)")

    raw_attractions.sort(key=lambda a: a.get("distance_km", 0))

    # Enrich candidates with real descriptions from RAG (Wikipedia / visitrochester)
    try:
        from rag.retriever import NoteRetriever
        retriever = NoteRetriever()
        hits = 0
        for attr in raw_attractions:
            if attr.get("description"):
                continue
            # 按景点名精确查找，查不到就留空——不要拿别的景点的段落来填
            chunks = retriever.retrieve_by_poi(attr["name"], top_k=1)
            if chunks:
                snippet = chunks[0]["chunk_text"][:150].replace("\n", " ").strip()
                attr["description"] = snippet
                hits += 1
        logger.info(f"RAG description enrichment: {hits}/{len(raw_attractions)} candidates matched")
    except Exception as e:
        logger.warning(f"RAG description enrichment failed: {e}")

    departure_name = profile.departure or "Rochester"
    logger.info(f"Orchestrator: {len(raw_attractions)} candidates after filter, departure={departure_name!r}")

    return {
        "date": target_date,
        "weather": weather,
        "candidates": raw_attractions,
        "departure_name": departure_name,
        "departure_km": _departure_km(departure_name),
    }
