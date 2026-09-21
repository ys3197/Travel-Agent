"""
预算估算工具 — 基于旅行风格和活动类型估算费用（USD）

Rochester 本地物价参考（2026年）：
- 州立公园停车费: $10/车
- 主要博物馆: $15-20/人
- 酒庄品酒: $15-30/人
- 普通餐厅午餐: $15-25/人
- 街边/快餐: $8-12/人
- Uber 市区内: $10-20/程
- Uber 郊外（如手指湖）: $60-120/程
"""

from user_profile import TravelStyle

# 活动类型 → (穷游, 适中, 精致) 估算费用 USD/人
COST_TABLE: dict[str, tuple[float, float, float]] = {
    # 景点
    "state_park":        (10, 10, 10),    # 停车费，人人一样
    "museum":            (0, 17, 20),     # 穷游可找免费日
    "winery_tasting":    (0, 20, 35),     # 穷游跳过或找免费样品
    "historic_site":     (0, 8, 15),
    "free_attraction":   (0, 0, 0),
    # 餐饮
    "breakfast":         (0, 10, 20),     # 穷游自备
    "lunch":             (10, 18, 35),
    "dinner":            (15, 30, 60),
    "coffee_snack":      (3, 6, 12),
    # 交通
    "uber_city":         (15, 15, 20),    # 市区单程
    "uber_suburbs":      (60, 70, 90),    # 郊外单程
    "gas_day":           (20, 20, 20),    # 自驾一天油费估算
    "parking":           (5, 10, 15),
}


def estimate_activity_cost(
    activity_type: str,
    style: TravelStyle,
    num_people: int = 1,
) -> float:
    """估算单个活动的费用（总计，非人均）。"""
    row = COST_TABLE.get(activity_type, (10, 15, 25))
    idx = {TravelStyle.BUDGET: 0, TravelStyle.MIDRANGE: 1, TravelStyle.LUXURY: 2}[style]
    per_person = row[idx]

    # 停车费、油费按车算，不乘人数
    if activity_type in ("state_park", "gas_day", "parking"):
        return per_person
    return per_person * num_people


def estimate_day_budget(
    activities: list[dict],
    style: TravelStyle,
    num_people: int,
    has_car: bool,
) -> dict:
    """
    估算一整天的费用明细。
    activities: [{"name": str, "type": str}, ...]
    返回: {items: [{name, cost}], transport_cost, food_cost, total}
    """
    item_costs = []
    for act in activities:
        cost = estimate_activity_cost(act.get("type", "free_attraction"), style, num_people)
        if cost > 0:
            item_costs.append({"name": act["name"], "cost": round(cost, 2)})

    # 交通费
    if has_car:
        transport_cost = estimate_activity_cost("gas_day", style, num_people)
        transport_cost += estimate_activity_cost("parking", style, num_people)
    else:
        # Uber：按行程数估算，简化为2程
        transport_cost = estimate_activity_cost("uber_city", style, num_people) * 2

    # 餐饮（默认：早餐+午餐+咖啡）
    food_cost = (
        estimate_activity_cost("breakfast", style, num_people)
        + estimate_activity_cost("lunch", style, num_people)
        + estimate_activity_cost("coffee_snack", style, num_people)
    )

    attraction_cost = sum(i["cost"] for i in item_costs)
    total = attraction_cost + transport_cost + food_cost

    return {
        "items": item_costs,
        "transport_cost": round(transport_cost, 2),
        "food_cost": round(food_cost, 2),
        "attraction_cost": round(attraction_cost, 2),
        "total": round(total, 2),
        "per_person": round(total / max(num_people, 1), 2),
    }
