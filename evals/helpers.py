"""共享辅助函数：构造 profile、同步跑 pipeline、把结构化行程转成 judge 可读的文本。"""

import asyncio

from agents.planner import format_itinerary
from pipeline import PipelineResult, run_pipeline
from user_profile import FitnessLevel, GroupType, Interest, Transport, TravelStyle, UserProfile


def make_profile(**overrides) -> UserProfile:
    defaults = dict(
        transport=Transport.OWN_CAR,
        style=TravelStyle.BUDGET,
        group=GroupType.COUPLE,
        group_size=2,
        fitness=FitnessLevel.MODERATE,
        interests=[Interest.NATURE, Interest.OUTDOOR],
        budget_usd=80,
        start_time="09:00",
        end_time="19:00",
    )
    defaults.update(overrides)
    return UserProfile(**defaults)


def run_pipeline_sync(profile: UserProfile, user_input: str, existing_plan: str = "") -> PipelineResult:
    """DeepEval 测试函数保持同步，唯一的异步调用在这里用 asyncio.run 隔离。"""
    return asyncio.run(run_pipeline(profile, user_input, existing_plan))


def itinerary_text(result: PipelineResult) -> str:
    """PipelineResult.itinerary 对 FULL_DAY_PLAN/MODIFY_PLAN 是 dict，其余是 str。"""
    if isinstance(result.itinerary, dict):
        return format_itinerary(result.itinerary)
    return result.itinerary or ""


def collect_rag_snippets(result: PipelineResult) -> list[str]:
    if not isinstance(result.itinerary, dict):
        return []
    return [
        a["rag_snippet"]
        for a in result.itinerary.get("activities", [])
        if a.get("rag_snippet")
    ]


def collect_notes_text(result: PipelineResult) -> str:
    if not isinstance(result.itinerary, dict):
        return ""
    return "\n".join(
        a.get("notes", "")
        for a in result.itinerary.get("activities", [])
        if a.get("notes")
    )
