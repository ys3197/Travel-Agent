"""
Eval 黄金场景集 —— 迁移自 test_travel_agent.py 的 6 个手工场景，
新增 2 个覆盖 UserProfile 里此前完全没被测到的分支（无障碍、公共交通）。
"""

from dataclasses import dataclass

from pipeline import Intent
from user_profile import FitnessLevel, GroupType, Interest, Transport, TravelStyle


@dataclass
class GoldenCase:
    name: str
    profile_kwargs: dict
    user_input: str
    existing_plan: str = ""
    expected_intent: Intent = Intent.FULL_DAY_PLAN
    expect_budget_overrun_guard: bool = False


_MODIFY_EXISTING_PLAN = """09:00-11:30 High Falls
11:45-13:30 George Eastman Museum $17
14:00-16:00 Ontario Beach Park"""


GOLDEN_CASES = [
    GoldenCase(
        name="穷游自驾_自然风光",
        profile_kwargs=dict(
            style=TravelStyle.BUDGET,
            transport=Transport.OWN_CAR,
            interests=[Interest.NATURE, Interest.OUTDOOR],
            budget_usd=60,
            fitness=FitnessLevel.ACTIVE,
        ),
        user_input="帮我规划一天的自然风光行程，喜欢徒步和瀑布",
    ),
    GoldenCase(
        name="适中消费_酒庄美食",
        profile_kwargs=dict(
            style=TravelStyle.MIDRANGE,
            transport=Transport.UBER_LYFT,
            interests=[Interest.WINERY, Interest.FOOD],
            budget_usd=150,
            fitness=FitnessLevel.EASY,
        ),
        user_input="想去酒庄品酒，顺便吃点好的，不想开车",
    ),
    GoldenCase(
        name="家庭亲子",
        profile_kwargs=dict(
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
        user_input="带两个小孩（6岁和9岁）出去玩，要适合小孩的地方",
    ),
    GoldenCase(
        name="预算超限_Verifier拦截",
        profile_kwargs=dict(
            style=TravelStyle.LUXURY,
            transport=Transport.OWN_CAR,
            interests=[Interest.WINERY, Interest.ARTS, Interest.FOOD],
            budget_usd=30,
        ),
        user_input="帮我规划一天精致游，去酒庄、博物馆、好餐厅",
        expect_budget_overrun_guard=True,
    ),
    GoldenCase(
        name="景点查询",
        profile_kwargs={},
        user_input="莱奇沃斯州立公园怎么去，门票多少",
        expected_intent=Intent.ATTRACTION_QUERY,
    ),
    GoldenCase(
        name="修改行程",
        profile_kwargs=dict(interests=[Interest.NATURE, Interest.WINERY]),
        user_input="把下午改成去酒庄品酒",
        existing_plan=_MODIFY_EXISTING_PLAN,
        expected_intent=Intent.MODIFY_PLAN,
    ),
    GoldenCase(
        name="无障碍_轮椅",
        profile_kwargs=dict(
            accessible=True,
            interests=[Interest.NATURE, Interest.ARTS],
            budget_usd=100,
        ),
        user_input="帮我规划一天行程，我需要轮椅无障碍设施",
    ),
    GoldenCase(
        name="公共交通_预算",
        profile_kwargs=dict(
            transport=Transport.PUBLIC,
            style=TravelStyle.BUDGET,
            interests=[Interest.HISTORY, Interest.ARTS],
            budget_usd=50,
        ),
        user_input="不开车，坐公交玩一天，喜欢历史文化",
    ),
]
