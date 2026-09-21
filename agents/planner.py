"""
Planner Agent — 两阶段规划：
  Phase 1: 模型从候选景点中选出最适合用户的 3-4 个（并排序）
  Phase 2: Python 计算精确时间/费用，模型只写 notes + summary
"""

import json
import logging

from openai import OpenAI

from user_profile import UserProfile, TravelStyle

logger = logging.getLogger(__name__)


# ── Phase 1 工具：选景点 ──────────────────────────────────────

SELECT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "select_attractions",
            "description": (
                "从候选景点中挑选最适合这位用户的 3-4 个，按建议游览顺序排列。"
                "综合考虑：用户兴趣、旅行风格、体力、是否开车、RAG 游记建议。"
                "距离远（>60km）的景点需独占一天，不要和其他景点混排。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "按游览顺序排列的景点名称列表（3-4 个，名称须与候选列表完全一致）",
                    },
                    "reason": {
                        "type": "string",
                        "description": "一句话说明选择逻辑（为什么这几个适合这位用户）",
                    },
                },
                "required": ["selected_names", "reason"],
            },
        },
    }
]


# ── Phase 2 工具：写注释 ──────────────────────────────────────

ANNOTATE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "annotate_itinerary",
            "description": "为已排好时间的行程每一站写一句推荐理由或注意事项，并写一句行程总结",
            "parameters": {
                "type": "object",
                "properties": {
                    "annotations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name":  {"type": "string", "description": "景点名称（与行程一致）"},
                                "notes": {"type": "string", "description": "一句推荐理由或注意事项"},
                            },
                            "required": ["name", "notes"],
                        },
                    },
                    "summary": {
                        "type": "string",
                        "description": "50 字以内的今日行程亮点总结",
                    },
                },
                "required": ["annotations", "summary"],
            },
        },
    }
]


# ── Phase 1：模型选景点 ──────────────────────────────────────

def _build_select_messages(
    profile: UserProfile,
    candidates: list[dict],
    weather: dict,
    rag_context: str,
    user_input: str,
    departure_name: str = "",
    departure_km: float = 2.0,
) -> list[dict]:
    system_msg = profile.to_prompt_prefix()

    style_hint = {
        TravelStyle.BUDGET:   "穷游：优先免费或低价景点，避开高消费酒庄品酒",
        TravelStyle.MIDRANGE: "适中：兼顾体验与价格，可以安排 1 个付费博物馆或酒庄",
        TravelStyle.LUXURY:   "精致：优先体验独特、高品质的景点，不在意门票价格",
    }[profile.style]

    depart_str = departure_name or "Rochester市区"
    to_downtown_min = max(5, int(departure_km / 70 * 60)) if profile.can_drive else max(15, int(departure_km / 25 * 60))
    transport_hint = (
        f"用户{'有车' if profile.can_drive else '没有车'}，出行半径 {profile.transport_radius_km}km。"
        + ("超过 60km 的景点需要独占一天，不要和其他景点混排。" if profile.can_drive else "请只选步行/Uber 可达的景点。")
        + f" 用户从【{depart_str}】出发，到市中心景点约 {to_downtown_min} 分钟。"
    )

    candidate_lines = "\n".join(
        f"- {a['name']} | {a.get('category','')} | 距市中心 {a.get('distance_km',0):.0f}km"
        f" | 类型: {a.get('cost_type','free_attraction')} | {a.get('description','')}"
        for a in candidates
    )

    user_msg = (
        f"今日天气：{weather.get('temp_c','?')}°C，{weather.get('description','未知')}。{weather.get('advisory','')}\n\n"
        f"旅行风格提示：{style_hint}\n"
        f"交通提示：{transport_hint}\n\n"
        f"## 候选景点（共 {len(candidates)} 个）\n{candidate_lines}\n\n"
        f"## 参考游记\n{rag_context or '无'}\n\n"
        f"## 用户需求\n{user_input}\n\n"
        "请调用 select_attractions，从候选景点中选出最适合这位用户的 3-4 个，按建议游览顺序排列。"
    )

    messages = [{"role": "system", "content": system_msg}]
    if rag_context:
        messages.append({
            "role": "system",
            "content": f"以下是真实旅行者的游记，规划时可借鉴其路线和风格：\n{rag_context}",
        })
    messages.append({"role": "user", "content": user_msg})
    return messages


async def _select_attractions(
    client: OpenAI,
    profile: UserProfile,
    candidates: list[dict],
    weather: dict,
    rag_context: str,
    user_input: str,
    departure_name: str = "",
    departure_km: float = 2.0,
) -> list[str]:
    """Phase 1：让模型从候选列表里选 3-4 个景点并排序，返回名称列表。"""
    messages = _build_select_messages(profile, candidates, weather, rag_context, user_input, departure_name, departure_km)

    # 选景点不需要太多 token，不走 streaming
    resp = client.chat.completions.create(
        model="Qwen/Qwen2.5-3B-Instruct-AWQ",
        messages=messages,
        tools=SELECT_TOOL,
        tool_choice={"type": "function", "function": {"name": "select_attractions"}},
        temperature=0.7,
        top_p=0.9,
        max_tokens=256,
    )

    args_str = resp.choices[0].message.tool_calls[0].function.arguments
    args = json.loads(args_str)
    selected = args.get("selected_names", [])
    reason = args.get("reason", "")
    logger.info(f"Model selected {len(selected)} attractions: {selected} | reason: {reason}")

    # 校验：过滤掉不在候选列表中的名称
    candidate_names = {a["name"] for a in candidates}
    valid = [n for n in selected if n in candidate_names]
    if not valid:
        logger.warning("Model returned no valid names, falling back to nearest 3")
        valid = [a["name"] for a in candidates[:3]]

    return valid[:4]  # 最多 4 个


# ── Phase 2：模型写 notes + summary ─────────────────────────

def _build_annotate_messages(
    profile: UserProfile,
    schedule: list[dict],
    weather: dict,
    rag_context: str,
    user_input: str,
) -> list[dict]:
    system_msg = profile.to_prompt_prefix()

    # 排除出发地条目，模型不需要为它写 notes
    schedule_items = [s for s in schedule if s.get("category") != "departure"]

    # 把每个景点的 RAG 片段直接嵌入 schedule 行，模型无需跨文档关联
    schedule_lines = []
    for s in schedule_items:
        line = (
            f"{s['start_time']}–{s['end_time']} | {s['name']} | ${s['cost_usd']}/人"
            + (f" | 前往下一站 {s['travel_min_to_next']}min" if s.get("travel_min_to_next") else "")
        )
        snippet = s.get("rag_snippet", "")
        if snippet:
            line += f"\n  [参考] {snippet}"
        elif s.get("category") != "food":
            # 精确查找没命中：明确告诉模型这一站没有资料，避免它去引用别人的 [参考]
            line += "\n  [无参考资料]"
        schedule_lines.append(line)
    schedule_text = "\n".join(schedule_lines)

    has_any_ref = any(s.get("rag_snippet") for s in schedule_items)

    user_msg = (
        f"今日天气：{weather.get('temp_c','?')}°C，{weather.get('description','未知')}\n\n"
        f"## 今日行程（时间和费用已由系统确定）\n{schedule_text}\n\n"
        f"## 用户需求\n{user_input}\n\n"
        "请调用 annotate_itinerary，为每一站写一句推荐理由或注意事项，并写一句行程总结。"
        + (
            "带 [参考] 的站，notes 必须引用该站自己 [参考] 里的具体事实"
            "（景点特色、规模、特别之处），不得引用其他站的参考内容。"
            if has_any_ref else ""
        )
        + "标为 [无参考资料] 的站，只写通用的时间/交通/准备提示，"
        "不要编造门票、规模、历史等具体细节。"
        "每条 notes 不超过 60 字。午餐可以推荐附近餐厅类型。"
    )

    messages = [{"role": "system", "content": system_msg}]
    messages.append({"role": "user", "content": user_msg})
    return messages


async def _annotate_itinerary(
    client: OpenAI,
    profile: UserProfile,
    schedule: list[dict],
    weather: dict,
    rag_context: str,
    user_input: str,
    on_token: callable = None,
) -> dict:
    """Phase 2：模型为每站写 notes + summary，流式传输。"""
    messages = _build_annotate_messages(profile, schedule, weather, rag_context, user_input)

    stream = client.chat.completions.create(
        model="Qwen/Qwen2.5-3B-Instruct-AWQ",
        messages=messages,
        tools=ANNOTATE_TOOL,
        tool_choice={"type": "function", "function": {"name": "annotate_itinerary"}},
        temperature=0.7,
        top_p=0.9,
        stream=True,
    )

    accumulated_args = ""
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.tool_calls:
            piece = delta.tool_calls[0].function.arguments or ""
            accumulated_args += piece
            if on_token:
                on_token(accumulated_args)

    if not accumulated_args:
        logger.warning("annotate_itinerary: no tool call received")
        return {"annotations": [], "summary": ""}

    return json.loads(accumulated_args)


# ── 主入口 ───────────────────────────────────────────────────

async def run_planner(
    client: OpenAI,
    profile: UserProfile,
    user_input: str,
    tool_results: dict,
    rag_context: str = "",
    on_token: callable = None,
    on_status: callable = None,
) -> dict:
    """
    两阶段规划：
      1. 模型选景点（select_attractions）
      2. Python 算时间/费用（_build_schedule）
      3. 模型写 notes/summary（annotate_itinerary）
    返回与旧版兼容的 activities + total_cost_usd + summary 结构。
    """
    from agents.orchestrator import repair_schedule_to_budget

    candidates = tool_results.get("candidates", [])
    weather = tool_results.get("weather", {})

    if not candidates:
        logger.warning("No candidates from orchestrator")
        return {"error": "no_candidates", "raw": ""}

    departure_name = tool_results.get("departure_name", profile.departure or "Rochester")
    departure_km   = tool_results.get("departure_km", 2.0)

    # Phase 1: 模型选景点
    if on_status: on_status("🤔 AI 正在选择最适合的景点…")
    selected_names = await _select_attractions(
        client, profile, candidates, weather, rag_context, user_input,
        departure_name=departure_name, departure_km=departure_km,
    )

    # Phase 2: Python 算时间/费用
    if on_status: on_status(f"🗓️ 已选：{' / '.join(selected_names)}，计算时间和费用…")
    name_to_attraction = {a["name"]: a for a in candidates}
    selected_attractions = [name_to_attraction[n] for n in selected_names if n in name_to_attraction]

    # 排程 + 确定性预算修复。放在写 notes 之前——否则模型会为一份即将被丢弃的
    # 行程写批注，那次调用纯属浪费。
    selected_attractions, schedule, total_cost, dropped = repair_schedule_to_budget(
        selected_attractions,
        profile.budget_usd,
        start_time=profile.start_time,
        end_time=profile.end_time,
        has_car=profile.can_drive,
        style=profile.style,
        departure_name=tool_results.get("departure_name", profile.departure or "Rochester"),
        departure_km=tool_results.get("departure_km", 2.0),
    )

    # 把计算好的 schedule 写回 tool_results，供 Verifier 使用
    tool_results["schedule"] = schedule
    tool_results["total_cost_usd"] = total_cost

    logger.info(f"Schedule built: {len(schedule)} slots, total ${total_cost:.0f}")

    # Phase 3: 每个景点单独检索 RAG，把片段嵌入 schedule 条目
    try:
        from rag.retriever import NoteRetriever
        retriever = NoteRetriever()
        hits = 0
        for slot in schedule:
            if slot.get("category") in ("departure", "food"):
                continue
            # 精确查找：拿得到就是这个景点自己的资料，拿不到就不给片段
            chunks = retriever.retrieve_by_poi(slot["name"], top_k=1)
            if chunks:
                slot["rag_snippet"] = chunks[0]["chunk_text"][:200].replace("\n", " ").strip()
                hits += 1
            else:
                logger.info(f"No corpus entry for {slot['name']!r} — annotating without reference")
        logger.info(f"Per-attraction RAG snippets: {hits}/{len(selected_names)} slots matched")
    except Exception as e:
        logger.warning(f"Per-attraction RAG failed: {e}")

    if on_status: on_status("✍️ AI 正在撰写每站说明…")
    annotation = await _annotate_itinerary(
        client, profile, schedule, weather, "", user_input, on_token=on_token
    )

    notes_by_name = {a["name"]: a.get("notes", "") for a in annotation.get("annotations", [])}

    activities = []
    for s in schedule:
        act = dict(s)
        act["notes"] = notes_by_name.get(s["name"], "")
        activities.append(act)

    plan = {
        "activities": activities,
        "total_cost_usd": total_cost,
        "summary": annotation.get("summary", ""),
    }
    if dropped:
        plan["advisories"] = [
            f"💰 为控制在 ${profile.budget_usd:.0f} 预算内，已移除：{'、'.join(dropped)}"
        ]
    return plan


def format_itinerary(plan: dict) -> str:
    """把结构化行程格式化为用户可读的文字。"""
    if "error" in plan:
        return plan.get("raw", "行程规划失败，请重试。")

    lines = ["## 今日行程\n"]
    for act in plan.get("activities", []):
        travel = act.get("travel_min_to_next", 0)
        transport = act.get("transport_to_next", "")
        travel_note = f"（{transport}，约 {travel}min）" if travel else ""

        # 出发地条目：特殊渲染
        if act.get("category") == "departure":
            lines.append(
                f"📍 **出发地：{act['name']}**\n"
                f"{f'> ↓ 前往第一站 {travel_note}' if travel_note else ''}\n"
            )
            continue

        lines.append(
            f"**{act['start_time']}–{act['end_time']}** {act['name']}\n"
            f"> 费用：${act['cost_usd']:.0f}/人 ｜ {act.get('notes', '')}\n"
            f"{f'> ↓ 前往下一站 {travel_note}' if travel_note else ''}\n"
        )

    lines.append(f"\n**全天预计费用：${plan.get('total_cost_usd', 0):.0f}/人**")
    lines.append(f"\n{plan.get('summary', '')}")
    return "\n".join(lines)
