"""
DeepEval metric 定义，全部绑定到 evals.judge_model.JUDGE（Claude），
不使用被测系统自己的 Qwen/vLLM 客户端评分自己。
"""

from deepeval.metrics import FaithfulnessMetric, GEval
from deepeval.test_case import SingleTurnParams

from evals.judge_model import JUDGE

# (a) 景点选择质量 —— 只评**需要判断力**的部分。
#
# 原先这条还包含"景点数量 3-4""总费用在预算内""无车时在半径内""无障碍需求"，
# 但这四项都是一行断言就能判定的事实，已移入 evals/structural_checks.py：
#   · 确定性判定比 judge 准，且零成本
#   · 六项揉成一个分，掉下来不知道是哪项坏了
# 留在这里的是判断题：选的景点"合不合这个人的口味"，这个没法写成断言。
#
# 不用 AnswerRelevancyMetric —— 那是为检索式问答设计的，不适合评"是否贴合画像"。
attraction_fit_metric = GEval(
    name="AttractionFitAndConstraints",
    criteria=(
        "评估行程是否为用户画像量身定制（只评选择是否贴切，不要核对数量、金额、距离——"
        "这些已由程序断言校验过）：\n"
        "1) 景点是否与用户声明的兴趣相符（自然风光/酒庄/美食/历史/艺术/户外）；\n"
        "2) 穷游风格是否体现在选择倾向上——优先免费或低价的去处，而非只是总额没超；\n"
        "3) 体力水平与景点强度是否匹配（体力一般的人不该被排满高强度徒步）；\n"
        "4) 若为家庭出行且有小孩，是否避免了危险或不适合儿童的活动；\n"
        "5) 整体路线是否合理——同一区域的景点相邻安排，而非来回折返。\n"
        "仅依据 input 中列出的用户画像约束和 actual_output 中的行程内容打分，"
        "不要因格式或语言风格扣分。"
    ),
    evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
    model=JUDGE,
    threshold=0.7,
)

# (b) 行程批注忠实度：notes 是否只引用 RAG 片段里的事实，不凭空编造。
# 阈值 0.5（非默认值）——语料是 Wikipedia + VisitRochester 列表，偏薄，
# 60字短note里合理的补充细节未必能逐字溯源到片段。
notes_faithfulness_metric = FaithfulnessMetric(threshold=0.5, model=JUDGE, include_reason=True)

# (b) 行程批注有用性：是否具体、不空洞、符合字数要求。
notes_helpfulness_metric = GEval(
    name="NotesHelpfulness",
    criteria=(
        "评估每一站的推荐理由（notes）和当日总结（summary）是否具体、有用、不空洞"
        "（字数限制已由程序断言校验，不必核对）：\n"
        "1) notes是否提到该景点的具体特色或注意事项，而非“值得一去”一类空泛套话；\n"
        "2) summary是否用一句话准确概括当天行程亮点；\n"
        "3) 语言是否自然流畅的中文，没有明显重复或AI套话。"
    ),
    evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
    model=JUDGE,
    threshold=0.7,
)
