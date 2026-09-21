"""
Verifier 护栏回归测试 —— 纯规则，不调 LLM，不需要 vLLM 服务器或 ANTHROPIC_API_KEY。
直接调用 agents/verifier.py 的函数，手造 plan dict，是本套件里跑得最快、最该常跑的部分。
"""

from agents.orchestrator import repair_schedule_to_budget
from agents.verifier import check_budget, run_verifier
from user_profile import TravelStyle
from evals.helpers import make_profile


def _activity(name, start, end, cost, travel_min_to_next=0, notes="", **attrs):
    """
    attrs 传景点表里的结构化属性，如 wheelchair_accessible=False。
    不传即为 None——语义是"未知"，与 False 不同。
    """
    return {
        "name": name,
        "start_time": start,
        "end_time": end,
        "cost_usd": cost,
        "travel_min_to_next": travel_min_to_next,
        "category": "attraction",
        "notes": notes,
        **attrs,
    }


def test_check_budget_catches_overrun_and_names_costliest_activity():
    plan = {
        "activities": [
            _activity("Genesee Brew House", "09:00", "10:30", 20),
            _activity("Casa Larga Vineyards", "11:00", "13:00", 45),
        ],
        "total_cost_usd": 65,
    }
    ok, msg = check_budget(plan, budget_usd=30)

    assert ok is False
    assert "预算超出" in msg
    assert "Casa Larga Vineyards" in msg


def test_check_budget_passes_within_limit():
    plan = {
        "activities": [_activity("High Falls", "09:00", "10:00", 0)],
        "total_cost_usd": 0,
    }
    ok, _ = check_budget(plan, budget_usd=60)

    assert ok is True


def test_run_verifier_flags_budget_before_accessibility():
    # 短路顺序：时间 -> 预算 -> 无障碍。这里两个问题都存在，预算应先被捕获。
    profile = make_profile(budget_usd=30, accessible=True)
    plan = {
        "activities": [
            _activity(
                "Casa Larga Vineyards", "09:00", "10:30", 45,
                travel_min_to_next=15, notes="steep trail only",
            ),
        ],
        "total_cost_usd": 45,
    }
    ok, feedback = run_verifier(plan, profile)

    assert ok is False
    assert feedback.startswith("[预算问题]")


def test_run_verifier_blocks_when_attraction_is_known_inaccessible():
    """wheelchair_accessible=False 是确知的事实约束，必须拦截。"""
    profile = make_profile(budget_usd=100, accessible=True)
    plan = {
        "activities": [
            _activity("High Falls Gorge Trail", "09:00", "10:30", 0,
                      travel_min_to_next=15, wheelchair_accessible=False),
        ],
        "total_cost_usd": 0,
    }
    ok, feedback = run_verifier(plan, profile)

    assert ok is False
    assert feedback.startswith("[无障碍/宠物问题]")
    assert "High Falls Gorge Trail" in feedback


def test_run_verifier_advises_but_does_not_block_when_accessibility_unknown():
    """
    未知 ≠ 不可达。景点表里 wheelchair_accessible 绝大多数是 null（语料里没有
    真实的无障碍信息），把未知当不可达会直接清空候选池，当可达则是在撒谎。
    正确处理是放行 + 提示用户自行确认。
    """
    profile = make_profile(budget_usd=100, accessible=True)
    plan = {
        "activities": [
            _activity("Genesee Valley Park", "09:00", "10:30", 0),   # 未传 = None
        ],
        "total_cost_usd": 0,
    }
    ok, feedback = run_verifier(plan, profile)

    assert ok is True
    assert any("Genesee Valley Park" in a for a in plan["advisories"])


def test_run_verifier_no_advisory_when_accessibility_is_known_good():
    profile = make_profile(budget_usd=100, accessible=True)
    plan = {
        "activities": [
            _activity("George Eastman Museum", "09:00", "10:30", 17,
                      wheelchair_accessible=True),
        ],
        "total_cost_usd": 17,
    }
    ok, feedback = run_verifier(plan, profile)

    assert ok is True
    assert plan.get("advisories", []) == []


def test_run_verifier_ignores_accessibility_when_profile_does_not_need_it():
    """画像没有无障碍需求时，不该因为景点属性未知就冒出提示。"""
    profile = make_profile(budget_usd=100)
    plan = {
        "activities": [_activity("Genesee Valley Park", "09:00", "10:30", 0)],
        "total_cost_usd": 0,
    }
    ok, _ = run_verifier(plan, profile)

    assert ok is True
    assert plan.get("advisories", []) == []


def test_run_verifier_passes_clean_plan():
    profile = make_profile(budget_usd=100)
    plan = {
        "activities": [
            _activity("High Falls", "09:00", "10:30", 0, travel_min_to_next=15),
            _activity("George Eastman Museum", "10:45", "12:30", 17),
        ],
        "total_cost_usd": 17,
    }
    ok, feedback = run_verifier(plan, profile)

    assert ok is True
    assert feedback == "验证通过"


# ── 预算的确定性修复（不调 LLM，见 orchestrator.repair_schedule_to_budget）──

def _attraction(name, cost_type, distance_km=10, visit_min=60):
    return {"name": name, "cost_type": cost_type, "category": "arts",
            "distance_km": distance_km, "typical_visit_min": visit_min}


def _repair(pool, budget, style=TravelStyle.LUXURY):
    return repair_schedule_to_budget(
        pool, budget, start_time="09:00", end_time="19:00", has_car=True,
        style=style, departure_name="University of Rochester", departure_km=3.5,
    )


def test_budget_repair_is_noop_when_within_budget():
    pool = [_attraction("A", "museum"), _attraction("B", "free_attraction")]
    kept, _, total, dropped = _repair(pool, budget=500)

    assert dropped == []
    assert len(kept) == 2


def test_budget_repair_drops_costliest_stop_first():
    pool = [
        _attraction("便宜博物馆", "museum"),          # $20
        _attraction("贵酒庄", "winery_tasting"),      # $35
        _attraction("免费公园", "free_attraction"),   # $0
    ]
    kept, _, total, dropped = _repair(pool, budget=80)

    assert dropped == ["贵酒庄"]
    assert total <= 80
    assert "贵酒庄" not in [a["name"] for a in kept]


def test_budget_repair_drops_repeatedly_until_within_budget():
    pool = [
        _attraction("博物馆", "museum"),
        _attraction("酒庄", "winery_tasting"),
        _attraction("免费公园", "free_attraction"),
    ]
    _, _, total, dropped = _repair(pool, budget=50)

    assert dropped == ["酒庄", "博物馆"]
    assert total <= 50


def test_budget_repair_stops_when_remaining_stops_are_all_free():
    """
    只剩免费站点却仍超预算（午餐本身就超了）时必须停手——
    继续删只会把行程清空，且并不能降低费用。交给 Verifier 如实报错。
    """
    pool = [_attraction("免费公园", "free_attraction")]
    kept, _, total, dropped = _repair(pool, budget=5)

    assert dropped == []
    assert kept == pool
    assert total > 5          # 留给 Verifier 判失败
