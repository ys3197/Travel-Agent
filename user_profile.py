"""
用户画像构建 — 旅行前问卷

在规划行程前，通过对话收集用户的基础偏好，
构建结构化的 UserProfile 作为后续所有 Agent 的输入。
"""

from dataclasses import dataclass, field
from enum import Enum


class Transport(str, Enum):
    OWN_CAR = "own_car"           # 自驾
    RENTAL_CAR = "rental_car"     # 租车
    UBER_LYFT = "uber_lyft"       # Uber/Lyft
    PUBLIC = "public_transit"     # 公共交通 + 步行


class TravelStyle(str, Enum):
    BUDGET = "budget"             # 穷游，能省则省
    MIDRANGE = "midrange"         # 适中，该花花
    LUXURY = "luxury"             # 精致，不差钱


class GroupType(str, Enum):
    SOLO = "solo"
    COUPLE = "couple"
    FAMILY = "family"             # 含小孩
    FRIENDS = "friends"


class FitnessLevel(str, Enum):
    EASY = "easy"                 # 平路散步为主
    MODERATE = "moderate"         # 可以走一些坡路
    ACTIVE = "active"             # 爬山徒步没问题


class Interest(str, Enum):
    NATURE = "nature"             # 自然风光、州立公园
    WINERY = "winery"             # 酒庄品酒（手指湖特色）
    FOOD = "food"                 # 餐厅、本地美食
    HISTORY = "history"           # 历史文化、博物馆
    ARTS = "arts"                 # 艺术、展览
    OUTDOOR = "outdoor"           # 户外运动、徒步


@dataclass
class UserProfile:
    transport: Transport
    style: TravelStyle
    group: GroupType
    group_size: int
    fitness: FitnessLevel
    interests: list[Interest]
    budget_usd: float             # 全天总预算（美元）
    start_time: str               # "09:00"
    end_time: str                 # "19:00"
    has_kids: bool = False
    kids_age_min: int | None = None
    accessible: bool = False      # 轮椅无障碍需求
    pet_friendly: bool = False
    departure: str = ""           # 出发地点，如 "University of Rochester"
    notes: str = ""               # 用户补充说明

    @property
    def available_hours(self) -> float:
        sh, sm = map(int, self.start_time.split(":"))
        eh, em = map(int, self.end_time.split(":"))
        return (eh * 60 + em - sh * 60 - sm) / 60

    @property
    def can_drive(self) -> bool:
        return self.transport in (Transport.OWN_CAR, Transport.RENTAL_CAR)

    @property
    def transport_radius_km(self) -> float:
        """根据交通方式估算合理的活动半径。"""
        return {
            Transport.OWN_CAR: 150,
            Transport.RENTAL_CAR: 150,
            Transport.UBER_LYFT: 40,    # Uber 远途费用高，限制范围
            Transport.PUBLIC: 10,        # RTS 公交覆盖范围有限
        }[self.transport]

    def to_prompt_prefix(self) -> str:
        """生成 CAG prefix，注入所有 Agent 的 system message。"""
        transport_desc = {
            Transport.OWN_CAR: "自驾（有自己的车）",
            Transport.RENTAL_CAR: "租车自驾",
            Transport.UBER_LYFT: "打车出行（Uber/Lyft）",
            Transport.PUBLIC: "公共交通+步行",
        }[self.transport]

        style_desc = {
            TravelStyle.BUDGET: "穷游风格，优先免费或低价选项",
            TravelStyle.MIDRANGE: "适中消费，性价比优先",
            TravelStyle.LUXURY: "精致出行，体验优先不计成本",
        }[self.style]

        group_desc = {
            GroupType.SOLO: "独自出行",
            GroupType.COUPLE: "两人出行",
            GroupType.FAMILY: f"家庭出行（含{self.kids_age_min}岁以上小孩）" if self.has_kids else "家庭出行",
            GroupType.FRIENDS: f"{self.group_size}人朋友同行",
        }[self.group]

        interest_labels = {
            Interest.NATURE: "自然风光",
            Interest.WINERY: "酒庄品酒",
            Interest.FOOD: "本地美食",
            Interest.HISTORY: "历史文化",
            Interest.ARTS: "艺术展览",
            Interest.OUTDOOR: "户外徒步",
        }
        interests_str = "、".join(interest_labels[i] for i in self.interests)

        constraints = []
        if self.accessible:
            constraints.append("需要轮椅无障碍设施")
        if self.pet_friendly:
            constraints.append("携带宠物")
        if self.has_kids and self.kids_age_min:
            constraints.append(f"有{self.kids_age_min}岁以上小孩，避免危险活动")
        if self.transport == Transport.UBER_LYFT:
            constraints.append(f"打车出行，活动范围控制在Rochester市区40km以内")
        if self.transport == Transport.PUBLIC:
            constraints.append("仅公共交通，只推荐RTS公交可达的市区景点")
        constraints_str = "；".join(constraints) if constraints else "无特殊限制"

        departure_line = f"- 出发地点：{self.departure}\n" if self.departure else ""

        return (
            f"你是一位专业的Rochester（纽约上州）本地旅游规划师。\n"
            f"当前用户信息：\n"
            f"- 出行方式：{transport_desc}，活动半径约{self.transport_radius_km}km\n"
            f"- 消费风格：{style_desc}\n"
            f"- 出行人员：{group_desc}\n"
            f"- 体力水平：{self.fitness.value}\n"
            f"- 兴趣偏好：{interests_str}\n"
            f"- 全天预算：${self.budget_usd:.0f} USD\n"
            f"- 行程时间：{self.start_time} — {self.end_time}（共{self.available_hours:.1f}小时）\n"
            f"{departure_line}"
            f"- 特殊需求：{constraints_str}\n"
            f"- 补充说明：{self.notes or '无'}\n\n"
            f"规划行程时必须严格遵守以上约束，不得推荐超出活动半径或不符合预算的选项。"
        )


# ── 问卷交互 ──────────────────────────────────────────────

QUESTIONS = [
    {
        "key": "transport",
        "question": "你们打算怎么出行？",
        "options": {
            "1": ("own_car",    "自驾（有自己的车）"),
            "2": ("rental_car", "租车自驾"),
            "3": ("uber_lyft",  "Uber/Lyft 打车"),
            "4": ("public",     "公共交通 + 步行"),
        },
        "tip": "💡 Rochester周边的手指湖、莱奇沃斯等景点没有公共交通，建议有车出行",
    },
    {
        "key": "style",
        "question": "消费风格是？",
        "options": {
            "1": ("budget",   "穷游 — 能省则省，优先免费景点"),
            "2": ("midrange", "适中 — 该花花，性价比优先"),
            "3": ("luxury",   "精致 — 体验第一，不太在意花费"),
        },
    },
    {
        "key": "group",
        "question": "和谁一起出行？",
        "options": {
            "1": ("solo",    "独自"),
            "2": ("couple",  "两人（情侣/夫妻）"),
            "3": ("family",  "家庭（含小孩）"),
            "4": ("friends", "朋友/同学"),
        },
    },
    {
        "key": "fitness",
        "question": "体力/运动能力怎么样？",
        "options": {
            "1": ("easy",     "轻松型 — 平路散步为主，不爬山"),
            "2": ("moderate", "普通型 — 走坡路没问题，偶尔短距离徒步"),
            "3": ("active",   "活跃型 — 爬山徒步完全OK"),
        },
    },
    {
        "key": "interests",
        "question": "对哪些感兴趣？（可多选，用逗号分隔，如 1,3,5）",
        "options": {
            "1": ("nature",  "自然风光（州立公园、瀑布、湖泊）"),
            "2": ("winery",  "酒庄品酒（手指湖葡萄酒产区）"),
            "3": ("food",    "本地美食（餐厅、农场市集）"),
            "4": ("history", "历史文化（博物馆、历史遗迹）"),
            "5": ("arts",    "艺术展览（美术馆、画廊）"),
            "6": ("outdoor", "户外运动（徒步、骑行）"),
        },
        "multi": True,
    },
]


def ask_question(q: dict) -> str | list[str]:
    print(f"\n{q['question']}")
    if "tip" in q:
        print(q["tip"])
    for k, (_, label) in q["options"].items():
        print(f"  {k}. {label}")

    while True:
        raw = input("请输入选项: ").strip()
        if q.get("multi"):
            keys = [x.strip() for x in raw.split(",")]
            if all(k in q["options"] for k in keys):
                return [q["options"][k][0] for k in keys]
        else:
            if raw in q["options"]:
                return q["options"][raw][0]
        print("输入无效，请重试")


def collect_profile() -> UserProfile:
    print("=" * 50)
    print("Rochester 一日游规划 — 快速问卷（共6题）")
    print("=" * 50)

    answers = {}
    for q in QUESTIONS:
        answers[q["key"]] = ask_question(q)

    # 追加问题：有无小孩
    has_kids = answers["group"] == "family"
    kids_age = None
    if has_kids:
        age_str = input("\n小孩大概几岁？(直接回车跳过): ").strip()
        kids_age = int(age_str) if age_str.isdigit() else None

    # 预算
    print("\n全天人均预算大约多少美元？")
    while True:
        budget_str = input("请输入金额（如 80）: ").strip()
        if budget_str.isdigit() and int(budget_str) > 0:
            budget = float(budget_str)
            break
        print("请输入有效金额")

    # 时间
    start = input("\n计划几点出发？（默认 09:00，直接回车跳过）: ").strip() or "09:00"
    end = input("计划几点结束？（默认 19:00，直接回车跳过）: ").strip() or "19:00"

    # 特殊需求
    special = input("\n有特殊需求吗？（轮椅/宠物/其他，直接回车跳过）: ").strip()
    accessible = "轮椅" in special
    pet = "宠物" in special

    notes = input("\n还有什么想告诉我们的？（直接回车跳过）: ").strip()

    transport_map = {
        "own_car": Transport.OWN_CAR,
        "rental_car": Transport.RENTAL_CAR,
        "uber_lyft": Transport.UBER_LYFT,
        "public": Transport.PUBLIC,
    }
    style_map = {
        "budget": TravelStyle.BUDGET,
        "midrange": TravelStyle.MIDRANGE,
        "luxury": TravelStyle.LUXURY,
    }
    group_map = {
        "solo": GroupType.SOLO,
        "couple": GroupType.COUPLE,
        "family": GroupType.FAMILY,
        "friends": GroupType.FRIENDS,
    }
    fitness_map = {
        "easy": FitnessLevel.EASY,
        "moderate": FitnessLevel.MODERATE,
        "active": FitnessLevel.ACTIVE,
    }
    interest_map = {
        "nature": Interest.NATURE,
        "winery": Interest.WINERY,
        "food": Interest.FOOD,
        "history": Interest.HISTORY,
        "arts": Interest.ARTS,
        "outdoor": Interest.OUTDOOR,
    }

    profile = UserProfile(
        transport=transport_map[answers["transport"]],
        style=style_map[answers["style"]],
        group=group_map[answers["group"]],
        group_size=1 if answers["group"] == "solo" else 2,
        fitness=fitness_map[answers["fitness"]],
        interests=[interest_map[i] for i in answers["interests"]],
        budget_usd=budget,
        start_time=start,
        end_time=end,
        has_kids=has_kids,
        kids_age_min=kids_age,
        accessible=accessible,
        pet_friendly=pet,
        notes=notes,
    )

    print("\n✓ 问卷完成，开始规划你的行程...\n")
    return profile


if __name__ == "__main__":
    profile = collect_profile()
    print(profile.to_prompt_prefix())
