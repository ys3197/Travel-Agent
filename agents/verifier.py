"""
Verifier Agent — 对 Planner 输出做硬约束校验（纯规则，不调 LLM）

校验项：
1. 时间可行性：活动时间是否有重叠，是否超出结束时间
2. 预算：总费用是否超出用户预算
3. 行程连贯性：相邻活动间是否留有足够交通时间
"""

import logging
from datetime import datetime, timedelta

from user_profile import UserProfile

logger = logging.getLogger(__name__)


def _to_minutes(time_str: str) -> int:
    """'09:30' → 570"""
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def check_time_feasibility(activities: list[dict], profile: UserProfile) -> tuple[bool, str]:
    """检查时间安排是否可行。出发地条目跳过（start==end，不参与时间校验）。"""
    acts = [a for a in activities if a.get("category") != "departure"]
    if not acts:
        return False, "行程为空"

    start_limit = _to_minutes(profile.start_time)
    end_limit = _to_minutes(profile.end_time)
    activities = acts  # shadow with filtered list

    prev_end = None
    for i, act in enumerate(activities):
        try:
            act_start = _to_minutes(act["start_time"])
            act_end = _to_minutes(act["end_time"])
        except (KeyError, ValueError):
            return False, f"活动 '{act.get('name', i)}' 时间格式错误"

        if act_start < start_limit:
            return False, f"'{act['name']}' 开始时间 {act['start_time']} 早于出发时间 {profile.start_time}"

        if act_end > end_limit:
            return False, f"'{act['name']}' 结束时间 {act['end_time']} 晚于计划结束时间 {profile.end_time}"

        if act_end <= act_start:
            return False, f"'{act['name']}' 结束时间不得早于开始时间"

        if prev_end is not None:
            travel_min = activities[i-1].get("travel_min_to_next", 0)
            earliest_start = prev_end + travel_min
            if act_start < earliest_start:
                gap = act_start - prev_end
                return False, (
                    f"'{act['name']}' 开始时间过早：上一活动结束后需要 {travel_min}min 交通，"
                    f"但只留了 {gap}min"
                )

            buffer = act_start - prev_end - travel_min
            if buffer < 10:
                logger.warning(f"活动 '{act['name']}' 前缓冲时间只有 {buffer}min，行程较紧")

        prev_end = act_end

    return True, "时间安排合理"


def check_budget(plan: dict, budget_usd: float) -> tuple[bool, str]:
    """检查费用是否超预算。"""
    total = plan.get("total_cost_usd", 0)
    if total <= budget_usd:
        return True, f"费用 ${total:.0f} 在预算 ${budget_usd:.0f} 以内"
    overrun = total - budget_usd
    # 找出最贵的活动，给 Planner 具体反馈
    activities = plan.get("activities", [])
    costly = sorted(activities, key=lambda x: x.get("cost_usd", 0), reverse=True)
    suggestion = ""
    if costly:
        suggestion = f"建议删除或替换：{costly[0]['name']}（${costly[0].get('cost_usd', 0):.0f}/人）"
    return False, f"预算超出 ${overrun:.0f}（共 ${total:.0f}，预算 ${budget_usd:.0f}）。{suggestion}"


def check_accessibility(activities: list[dict], profile: UserProfile) -> tuple[bool, str, list[str]]:
    """
    无障碍与携宠检查，依据景点表里的结构化属性。

    返回 (ok, 阻断原因, 提示列表)。三态语义：
      False → 确知不满足，直接判定失败
      True  → 确知满足
      None  → 未知。不阻断行程，但提示用户自行确认——语料里没有真实的无障碍
              信息，把"未知"当"不可达"会把候选池清空，当"可达"则是撒谎。

    早先这里是去 act["notes"] 里 grep "steep"/"stairs"，但 notes 是 Planner
    第二阶段由模型写的中文推荐语：既不会出现这些英文词，又等于拿模型自己的
    输出去校验模型自己的选择。
    """
    if not profile.accessible and not profile.pet_friendly:
        return True, "", []

    blocking: list[str] = []
    unknown: list[str] = []

    for act in activities:
        if act.get("category") in ("departure", "food"):
            continue
        name = act.get("name", "")

        if profile.accessible:
            wc = act.get("wheelchair_accessible")
            if wc is False:
                blocking.append(f"'{name}' 无轮椅通道")
            elif wc is None:
                unknown.append(name)

        if profile.pet_friendly:
            pet = act.get("pet_friendly")
            if pet is False:
                blocking.append(f"'{name}' 不允许携带宠物")
            elif pet is None and name not in unknown:
                unknown.append(name)

    hints = []
    if unknown:
        hints.append(
            f"⚠️ 以下站点的无障碍/携宠信息未收录，建议出行前致电确认：{'、'.join(unknown)}"
        )

    if blocking:
        return False, "；".join(blocking), hints
    return True, "", hints


def run_verifier(plan: dict, profile: UserProfile) -> tuple[bool, str]:
    """
    综合验证。返回 (ok, feedback)。
    feedback 用于传回 Planner 做修正。
    通过时若有"信息未知"类提示，会挂到 plan["advisories"] 上带给用户，
    但不作为失败理由——未知不等于不满足。
    """
    if "error" in plan:
        return False, "Planner 未返回有效行程，请重新生成"

    activities = plan.get("activities", [])

    time_ok, time_msg = check_time_feasibility(activities, profile)
    if not time_ok:
        return False, f"[时间问题] {time_msg}"

    budget_ok, budget_msg = check_budget(plan, profile.budget_usd)
    if not budget_ok:
        return False, f"[预算问题] {budget_msg}"

    access_ok, access_msg, access_hints = check_accessibility(activities, profile)
    if access_hints:
        plan.setdefault("advisories", []).extend(access_hints)
    if not access_ok:
        return False, f"[无障碍/宠物问题] {access_msg}"

    logger.info(f"Verifier PASS: {len(activities)} activities, ${plan.get('total_cost_usd','?')}")
    return True, "验证通过"
