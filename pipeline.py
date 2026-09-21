"""
Travel Agent Pipeline — 带 Pilot 路由的多 Agent 编排

流程：
  UserProfile → Pilot (intent classification) → 对应 Agent 路径 → Verifier → 结果
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from openai import OpenAI

from user_profile import UserProfile

logger = logging.getLogger(__name__)

VLLM_BASE_URL = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-3B-Instruct-AWQ"
LATENCY_SLA_SEC = 60.0


class Intent(str, Enum):
    FULL_DAY_PLAN = "full_day_plan"       # 规划完整一日行程
    ATTRACTION_QUERY = "attraction_query" # 查询特定景点信息
    BUDGET_CHECK = "budget_check"         # 检查预算是否合理
    MODIFY_PLAN = "modify_plan"           # 修改已有行程


@dataclass
class PipelineResult:
    intent: Intent
    itinerary: str = ""
    warnings: list[str] = field(default_factory=list)
    latency_sec: float = 0.0
    retries: int = 0


# ── Pilot：意图分类 ───────────────────────────────────────

INTENT_RULES = [
    (Intent.BUDGET_CHECK,     ["预算", "超了", "贵", "划算", "多少钱", "花费"]),
    (Intent.ATTRACTION_QUERY, ["怎么去", "在哪", "开放时间", "门票", "介绍一下", "推荐"]),
    (Intent.MODIFY_PLAN,      ["改成", "换成", "删掉", "加上", "调整", "修改"]),
]


def classify_intent(user_input: str, has_existing_plan: bool = False) -> Intent:
    """
    轻量规则分类，避免为意图判断消耗 LLM 推理。
    规则未命中时默认走完整规划流程。
    """
    for intent, keywords in INTENT_RULES:
        if any(kw in user_input for kw in keywords):
            # modify_plan 还需要有已有行程
            if intent == Intent.MODIFY_PLAN and not has_existing_plan:
                continue
            return intent
    return Intent.FULL_DAY_PLAN


# ── LLM 客户端 ────────────────────────────────────────────

def make_client() -> OpenAI:
    return OpenAI(base_url=VLLM_BASE_URL, api_key="dummy")


def llm_chat(
    client: OpenAI,
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
) -> Any:
    kwargs: dict = dict(model=MODEL, messages=messages)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    return client.chat.completions.create(**kwargs)


# ── 各 Agent 路径 ─────────────────────────────────────────

async def run_full_day_plan(
    client: OpenAI,
    profile: UserProfile,
    user_input: str,
    rag_context: str,
    deadline: float,
    on_token: callable = None,
    on_status: callable = None,
    date: str | None = None,
) -> tuple[str, list[str]]:
    """完整一日行程规划：并行调工具 → Planner → Verifier（最多2次重试）。"""
    from agents.orchestrator import run_orchestrator
    from agents.planner import run_planner
    from agents.verifier import run_verifier

    if on_status: on_status("📡 获取天气和景点数据…")
    tool_results = await run_orchestrator(profile, date=date)

    warnings = []
    plan = {}
    last_feedback = None
    for attempt in range(3):
        if time.time() > deadline:
            warnings.append(f"⚠️ 超过 {LATENCY_SLA_SEC}s SLA，返回当前最优结果")
            break

        # run_planner: model selects attractions, Python calculates schedule, model writes notes
        plan = await run_planner(client, profile, user_input, tool_results, rag_context,
                                 on_token=on_token, on_status=on_status)
        if "error" in plan:
            break

        ok, feedback = run_verifier(plan, profile)  # type: ignore[arg-type]

        if ok:
            break

        warnings.append(f"第{attempt+1}次验证失败：{feedback}")

        # 同样的失败重复出现，说明重跑解决不了——再试两次只是白烧两次 LLM 调用。
        # 预算超支现在已由 repair_schedule_to_budget 确定性修复，不会走到这里；
        # 能走到这里的多半是排程器或数据的问题，换个 temperature 也变不出来。
        if feedback == last_feedback:
            logger.info(f"Verifier feedback unchanged, stopping retries: {feedback}")
            warnings.append("重试未能解决该问题，返回当前结果")
            break
        last_feedback = feedback

        user_input = f"{user_input}\n\n[修正要求] {feedback}"

    # Verifier 的"信息未知"类提示：不阻断行程，但要让用户看到
    if isinstance(plan, dict):
        warnings.extend(plan.get("advisories", []))

    return plan, warnings


async def run_attraction_query(
    client: OpenAI,
    profile: UserProfile,
    user_input: str,
) -> tuple[str, list[str]]:
    """只查询特定景点，不生成完整行程。"""
    from agents.tools.poi import search_attractions

    city = "Rochester, NY"
    results = await search_attractions(city, query=user_input, radius_km=profile.transport_radius_km)
    if not results:
        return "未找到相关景点信息。", []

    lines = [f"- **{r['name']}**：{r.get('description', '')}（距市中心约{r.get('distance_km', '?')}km）"
             for r in results[:5]]
    return "\n".join(lines), []


async def run_budget_check(
    client: OpenAI,
    profile: UserProfile,
    user_input: str,
    existing_plan: str,
) -> tuple[str, list[str]]:
    """检查已有行程是否超预算。"""
    from agents.verifier import check_budget

    ok, detail = check_budget(existing_plan, profile.budget_usd)
    if ok:
        return f"✅ 预算充足。{detail}", []
    return f"❌ 预算超出。{detail}", [detail]


async def run_modify_plan(
    client: OpenAI,
    profile: UserProfile,
    user_input: str,
    existing_plan: str,
    rag_context: str,
    deadline: float,
) -> tuple[str, list[str]]:
    """在已有行程基础上做局部修改。"""
    from agents.planner import run_planner
    from agents.verifier import run_verifier

    modify_input = f"已有行程：\n{existing_plan}\n\n用户要求修改：{user_input}"
    plan = await run_planner(client, profile, modify_input, {}, rag_context)
    ok, feedback = run_verifier(plan, profile)
    warnings = [] if ok else [f"修改后验证：{feedback}"]
    return plan, warnings


# ── 主入口 ────────────────────────────────────────────────

async def run_pipeline(
    profile: UserProfile,
    user_input: str,
    existing_plan: str = "",
    on_token: callable = None,
    on_status: callable = None,
    date: str | None = None,
) -> PipelineResult:
    t0 = time.time()
    deadline = t0 + LATENCY_SLA_SEC

    # Pilot 分类
    intent = classify_intent(user_input, has_existing_plan=bool(existing_plan))
    logger.info(f"Intent: {intent.value} | Input: {user_input[:60]}")

    # RAG 检索（full_day_plan 和 modify_plan 才需要）
    rag_context = ""
    if intent in (Intent.FULL_DAY_PLAN, Intent.MODIFY_PLAN):
        from rag.retriever import NoteRetriever
        retriever = NoteRetriever()
        chunks = retriever.retrieve(user_input, top_k=5, style_tags=None)
        rag_context = retriever.format_context(chunks)

    client = make_client()
    warnings: list[str] = []
    result_text = ""

    if intent == Intent.FULL_DAY_PLAN:
        result_text, warnings = await run_full_day_plan(
            client, profile, user_input, rag_context, deadline,
            on_token=on_token, on_status=on_status, date=date,
        )
    elif intent == Intent.ATTRACTION_QUERY:
        result_text, warnings = await run_attraction_query(client, profile, user_input)
    elif intent == Intent.BUDGET_CHECK:
        result_text, warnings = await run_budget_check(client, profile, user_input, existing_plan)
    elif intent == Intent.MODIFY_PLAN:
        result_text, warnings = await run_modify_plan(
            client, profile, user_input, existing_plan, rag_context, deadline
        )

    latency = round(time.time() - t0, 2)
    logger.info(f"Pipeline done in {latency}s | Intent: {intent.value} | Warnings: {len(warnings)}")

    return PipelineResult(
        intent=intent,
        itinerary=result_text,
        warnings=warnings,
        latency_sec=latency,
    )


if __name__ == "__main__":
    import asyncio
    from user_profile import collect_profile

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    profile = collect_profile()
    result = asyncio.run(run_pipeline(profile, "帮我规划一天的行程"))
    print(result.itinerary)
    for w in result.warnings:
        print(w)
