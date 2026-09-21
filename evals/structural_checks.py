"""
结构性检查 —— 纯断言，不调 LLM，不需要 ANTHROPIC_API_KEY。

原先这些条目和"景点是否符合兴趣""notes 是否空洞"一起打包在 metrics.py 的
attraction_fit_metric 里，由 GEval 给一个模糊分。两个问题：

1. 能确定性判定的东西不该花钱让模糊的东西去判——景点数、总费用、notes 字数
   都是一行断言的事，判错率还低于 judge。
2. 六件事揉成一个分，掉下来不知道是哪件坏了。拆成独立原子检查后，
   失败信息直接指向具体哪一条。

这些检查零成本，应该在 judge 之前先跑：结构不对就不必再烧 judge 的 token。
"""

from dataclasses import dataclass

from user_profile import UserProfile

# _build_schedule 里非景点的条目
NON_STOP_CATEGORIES = ("departure", "food")

MAX_STOPS = 4          # planner._select_attractions 取 valid[:4]
MIN_STOPS = 3          # select_attractions 工具要求 3-4 个
MAX_NOTE_CHARS = 60    # _build_annotate_messages 的 prompt 明确要求


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str

    def __bool__(self) -> bool:
        return self.passed


def _stops(plan: dict) -> list[dict]:
    return [
        a for a in plan.get("activities", [])
        if a.get("category") not in NON_STOP_CATEGORIES
    ]


def _budget_trimmed(plan: dict) -> bool:
    """行程是否因预算被 repair_schedule_to_budget 削减过。"""
    return any("预算" in a for a in plan.get("advisories", []))


def check_stop_count(plan: dict) -> CheckResult:
    """
    上限恒为 4。下限通常是 3，但预算修复可能合法地把行程削到 3 站以下——
    此时不算失败，只要系统在 advisories 里说明了原因。
    """
    n = len(_stops(plan))
    if n > MAX_STOPS:
        return CheckResult("stop_count", False, f"{n} 站，超过上限 {MAX_STOPS}")
    if n < MIN_STOPS and not _budget_trimmed(plan):
        return CheckResult("stop_count", False, f"只有 {n} 站且未说明原因（预期 {MIN_STOPS}-{MAX_STOPS}）")
    if n == 0:
        return CheckResult("stop_count", False, "行程为空")
    return CheckResult("stop_count", True, f"{n} 站")


def check_within_budget(plan: dict, profile: UserProfile) -> CheckResult:
    total = plan.get("total_cost_usd", 0)
    ok = total <= profile.budget_usd
    return CheckResult(
        "within_budget", ok,
        f"${total:.0f} / 预算 ${profile.budget_usd:.0f}",
    )


def check_within_radius(plan: dict, profile: UserProfile) -> CheckResult:
    """所有站点都应在画像声明的出行半径内（距离字段由 _build_schedule 透传）。"""
    over = [
        (a["name"], a["distance_km"])
        for a in _stops(plan)
        if a.get("distance_km") is not None and a["distance_km"] > profile.transport_radius_km
    ]
    if over:
        detail = "；".join(f"{n} {d:.0f}km" for n, d in over)
        return CheckResult("within_radius", False, f"超出 {profile.transport_radius_km}km：{detail}")
    return CheckResult("within_radius", True, f"全部在 {profile.transport_radius_km}km 内")


def check_note_length(plan: dict) -> CheckResult:
    over = [
        (a["name"], len(a.get("notes", "")))
        for a in _stops(plan)
        if len(a.get("notes", "")) > MAX_NOTE_CHARS
    ]
    if over:
        detail = "；".join(f"{n} {c}字" for n, c in over)
        return CheckResult("note_length", False, f"超过 {MAX_NOTE_CHARS} 字：{detail}")
    return CheckResult("note_length", True, f"均不超过 {MAX_NOTE_CHARS} 字")


def check_accessibility_respected(plan: dict, profile: UserProfile) -> CheckResult:
    """
    画像声明需要无障碍时，行程里不得出现**确知**不可达的站点。
    wheelchair_accessible 为 None 是"未知"，不算违规——景点表里绝大多数是 null，
    把未知当违规会让这条检查永远失败。
    """
    if not profile.accessible:
        return CheckResult("accessibility", True, "画像无此需求")
    bad = [a["name"] for a in _stops(plan) if a.get("wheelchair_accessible") is False]
    if bad:
        return CheckResult("accessibility", False, f"含确知不可达站点：{'、'.join(bad)}")
    unknown = sum(1 for a in _stops(plan) if a.get("wheelchair_accessible") is None)
    return CheckResult("accessibility", True, f"无确知不可达站点（{unknown} 站信息未知）")


def check_schedule_monotonic(plan: dict) -> CheckResult:
    """时间必须单调递增——Verifier 也查，这里作为排程器的回归护栏。"""
    prev_end = None
    for a in _stops(plan):
        start, end = a.get("start_time"), a.get("end_time")
        if not start or not end:
            return CheckResult("schedule_monotonic", False, f"{a['name']} 缺时间字段")
        if end <= start:
            return CheckResult("schedule_monotonic", False, f"{a['name']} 结束不晚于开始")
        if prev_end is not None and start < prev_end:
            return CheckResult("schedule_monotonic", False, f"{a['name']} 与上一站时间重叠")
        prev_end = end
    return CheckResult("schedule_monotonic", True, "时间顺序正常")


def run_structural_checks(plan: dict, profile: UserProfile) -> list[CheckResult]:
    return [
        check_stop_count(plan),
        check_within_budget(plan, profile),
        check_within_radius(plan, profile),
        check_note_length(plan),
        check_accessibility_respected(plan, profile),
        check_schedule_monotonic(plan),
    ]


def format_failures(results: list[CheckResult]) -> str:
    failed = [r for r in results if not r.passed]
    if not failed:
        return ""
    return "\n".join(f"  ✗ {r.name}: {r.detail}" for r in failed)
