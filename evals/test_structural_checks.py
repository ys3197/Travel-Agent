"""
结构性检查的自测 —— 纯断言，不需要 vLLM 也不需要 ANTHROPIC_API_KEY。
和 test_verifier_unit.py 一样属于"随时该跑"的那一档。
"""

import pytest

from evals.helpers import make_profile
from user_profile import Transport
from evals.structural_checks import (
    check_accessibility_respected,
    check_note_length,
    check_schedule_monotonic,
    check_stop_count,
    check_within_budget,
    check_within_radius,
    format_failures,
    run_structural_checks,
)


def _stop(name, start="09:00", end="10:00", cost=0, notes="", **attrs):
    return {
        "name": name, "start_time": start, "end_time": end,
        "cost_usd": cost, "category": "attraction", "notes": notes, **attrs,
    }


def _plan(stops, total=None, advisories=None):
    plan = {
        "activities": [
            {"name": "出发地", "category": "departure", "start_time": "09:00", "end_time": "09:00"},
            *stops,
            {"name": "午餐", "category": "food", "start_time": "12:00", "end_time": "13:00", "cost_usd": 15},
        ],
        "total_cost_usd": total if total is not None else sum(s["cost_usd"] for s in stops),
    }
    if advisories:
        plan["advisories"] = advisories
    return plan


# ── stop_count ────────────────────────────────────────────────

def test_stop_count_accepts_three_to_four():
    plan = _plan([_stop(f"P{i}", f"{9+i}:00", f"{10+i}:00") for i in range(3)])
    assert check_stop_count(plan).passed


def test_stop_count_rejects_more_than_four():
    plan = _plan([_stop(f"P{i}", f"{9+i}:00", f"{10+i}:00") for i in range(5)])
    r = check_stop_count(plan)
    assert not r.passed and "超过上限" in r.detail


def test_stop_count_rejects_too_few_without_explanation():
    plan = _plan([_stop("P1")])
    r = check_stop_count(plan)
    assert not r.passed and "未说明原因" in r.detail


def test_stop_count_allows_too_few_when_budget_trimmed():
    """预算修复削减行程是合法的——只要 advisories 里说明了。"""
    plan = _plan([_stop("P1")], advisories=["💰 为控制在 $30 预算内，已移除：某酒庄"])
    assert check_stop_count(plan).passed


# ── budget / radius ───────────────────────────────────────────

def test_within_budget():
    profile = make_profile(budget_usd=50)
    assert check_within_budget(_plan([_stop("P1", cost=20)], total=35), profile).passed
    assert not check_within_budget(_plan([_stop("P1", cost=60)], total=75), profile).passed


def test_within_radius_flags_out_of_range_stop():
    profile = make_profile(transport=Transport.UBER_LYFT)   # 半径 40km
    plan = _plan([_stop("远景点", distance_km=120), _stop("近景点", "10:30", "11:30", distance_km=5)])
    r = check_within_radius(plan, profile)
    assert not r.passed and "远景点" in r.detail


def test_within_radius_ignores_missing_distance():
    """距离字段缺失时不误报——老数据可能没有这个字段。"""
    profile = make_profile(transport=Transport.UBER_LYFT)
    assert check_within_radius(_plan([_stop("P1")]), profile).passed


# ── notes ─────────────────────────────────────────────────────

def test_note_length_flags_overlong_note():
    plan = _plan([_stop("P1", notes="很" * 61)])
    r = check_note_length(plan)
    assert not r.passed and "61字" in r.detail


def test_note_length_passes_at_limit():
    assert check_note_length(_plan([_stop("P1", notes="很" * 60)])).passed


# ── accessibility ─────────────────────────────────────────────

def test_accessibility_blocks_known_inaccessible():
    profile = make_profile(accessible=True)
    plan = _plan([_stop("陡坡步道", wheelchair_accessible=False)])
    r = check_accessibility_respected(plan, profile)
    assert not r.passed and "陡坡步道" in r.detail


def test_accessibility_tolerates_unknown():
    """None = 未知。景点表里几乎全是 null，当成违规这条检查会永远失败。"""
    profile = make_profile(accessible=True)
    assert check_accessibility_respected(_plan([_stop("P1")]), profile).passed


def test_accessibility_skipped_when_not_needed():
    profile = make_profile()
    plan = _plan([_stop("陡坡步道", wheelchair_accessible=False)])
    assert check_accessibility_respected(plan, profile).passed


# ── schedule ──────────────────────────────────────────────────

def test_schedule_monotonic_flags_overlap():
    plan = _plan([_stop("A", "09:00", "11:00"), _stop("B", "10:00", "12:00")])
    r = check_schedule_monotonic(plan)
    assert not r.passed and "重叠" in r.detail


def test_schedule_monotonic_flags_inverted_slot():
    plan = _plan([_stop("A", "11:00", "09:00")])
    assert not check_schedule_monotonic(plan).passed


# ── 汇总 ──────────────────────────────────────────────────────

def test_run_all_passes_on_clean_plan():
    profile = make_profile(budget_usd=100, transport=Transport.OWN_CAR)   # 半径 150km
    plan = _plan([
        _stop("A", "09:00", "10:30", 10, notes="不错", distance_km=20),
        _stop("B", "11:00", "12:00", 17, notes="也不错", distance_km=30),
        _stop("C", "13:30", "15:00", 0, notes="免费", distance_km=8),
    ])
    results = run_structural_checks(plan, profile)
    assert all(r.passed for r in results), format_failures(results)


def test_failures_are_individually_attributable():
    """一次失败要能指到具体是哪条检查坏了——这正是从 GEval 拆出来的理由。"""
    profile = make_profile(budget_usd=10, transport=Transport.PUBLIC)     # 半径 10km
    plan = _plan([_stop("远而贵", cost=80, notes="很" * 80, distance_km=99)], total=95)
    failed = {r.name for r in run_structural_checks(plan, profile) if not r.passed}
    assert failed == {"stop_count", "within_budget", "within_radius", "note_length"}
