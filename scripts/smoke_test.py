"""
端到端测试 — Travel Agent Pipeline

测试场景：
  1. 穷游自驾，自然风光
  2. 适中消费，打车，酒庄+美食
  3. 家庭出行，亲子友好
  4. 预算超限（验证 Verifier 能抓到）
  5. 景点查询（不触发完整规划）
  6. 修改行程（modify_plan 路径）

用法:
    vllm serve Qwen/Qwen2.5-3B-Instruct-AWQ --port 8000 \
        --enable-auto-tool-choice --tool-call-parser hermes \
        --gpu-memory-utilization 0.85 --max-model-len 4096
    python scripts/smoke_test.py
"""

import asyncio
import logging

from user_profile import UserProfile, Transport, TravelStyle, GroupType, FitnessLevel, Interest
from pipeline import run_pipeline
from agents.planner import format_itinerary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


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


async def run_test(name: str, profile: UserProfile, user_input: str, existing_plan: str = ""):
    print(f"\n{'='*60}")
    print(f"[TEST] {name}")
    print(f"Input: {user_input}")
    print(f"Style: {profile.style.value} | Transport: {profile.transport.value} | Budget: ${profile.budget_usd}")
    print("=" * 60)

    result = await run_pipeline(profile, user_input, existing_plan)

    print(f"Intent:  {result.intent.value}")
    print(f"Latency: {result.latency_sec}s")

    if result.warnings:
        print("Warnings:")
        for w in result.warnings:
            print(f"  ⚠ {w}")

    if isinstance(result.itinerary, dict):
        print(format_itinerary(result.itinerary))
    else:
        print(result.itinerary)

    print(f"\n{'─'*60}")
    return result


async def main():
    # ── Case 1: 穷游自驾，自然风光 ──
    await run_test(
        "穷游自驾 — 自然风光",
        make_profile(
            style=TravelStyle.BUDGET,
            transport=Transport.OWN_CAR,
            interests=[Interest.NATURE, Interest.OUTDOOR],
            budget_usd=60,
            fitness=FitnessLevel.ACTIVE,
        ),
        "帮我规划一天的自然风光行程，喜欢徒步和瀑布",
    )

    # ── Case 2: 适中消费，Uber出行，酒庄+美食 ──
    await run_test(
        "适中消费 — 打车出行，酒庄美食",
        make_profile(
            style=TravelStyle.MIDRANGE,
            transport=Transport.UBER_LYFT,
            interests=[Interest.WINERY, Interest.FOOD],
            budget_usd=150,
            fitness=FitnessLevel.EASY,
        ),
        "想去酒庄品酒，顺便吃点好的，不想开车",
    )

    # ── Case 3: 家庭出行，有小孩 ──
    await run_test(
        "家庭出行 — 亲子友好",
        make_profile(
            style=TravelStyle.MIDRANGE,
            transport=Transport.OWN_CAR,
            group=GroupType.FAMILY,
            group_size=4,
            interests=[Interest.NATURE, Interest.ARTS],
            budget_usd=200,
            fitness=FitnessLevel.EASY,
            has_kids=True,
            kids_age_min=6,
        ),
        "带两个小孩（6岁和9岁）出去玩，要适合小孩的地方",
    )

    # ── Case 4: 预算超限测试（Verifier 应拦截） ──
    await run_test(
        "预算超限 — Verifier 拦截测试",
        make_profile(
            style=TravelStyle.LUXURY,
            transport=Transport.OWN_CAR,
            interests=[Interest.WINERY, Interest.ARTS, Interest.FOOD],
            budget_usd=30,  # 极低预算，故意触发超限
        ),
        "帮我规划一天精致游，去酒庄、博物馆、好餐厅",
    )

    # ── Case 5: 景点查询（不触发完整规划）──
    await run_test(
        "景点查询 — attraction_query 路径",
        make_profile(),
        "莱奇沃斯州立公园怎么去，门票多少",
    )

    # ── Case 6: 修改行程 ──
    existing = """09:00-11:30 High Falls
11:45-13:30 George Eastman Museum $17
14:00-16:00 Ontario Beach Park"""

    await run_test(
        "修改行程 — modify_plan 路径",
        make_profile(interests=[Interest.NATURE, Interest.WINERY]),
        "把下午改成去酒庄品酒",
        existing_plan=existing,
    )


if __name__ == "__main__":
    asyncio.run(main())
