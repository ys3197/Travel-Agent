"""
生成式表面的 LLM-judge 评测：景点选择质量 + 行程批注忠实度/有用性 + 预算护栏回归。

需要本地 vLLM 服务器（被测系统）和 ANTHROPIC_API_KEY（judge），
见 evals/conftest.py 的 require_vllm_server / require_anthropic_key。
"""

import pytest

# deepeval 未安装时整体跳过，而不是让 `pytest evals/` 在收集阶段就炸掉——
# 同目录下的结构性检查和 Verifier 回归不依赖它，应该照常能跑。
pytest.importorskip(
    "deepeval",
    reason="LLM-judge 评测需要 pip install -r requirements-dev.txt",
)

from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from evals.golden_cases import GOLDEN_CASES
from evals.helpers import collect_notes_text, collect_rag_snippets, itinerary_text, make_profile, run_pipeline_sync
from evals.metrics import attraction_fit_metric, notes_faithfulness_metric, notes_helpfulness_metric
from evals.structural_checks import format_failures, run_structural_checks
from pipeline import Intent

pytestmark = pytest.mark.usefixtures("require_vllm_server", "require_anthropic_key")


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.name)
def test_golden_case(case):
    profile = make_profile(**case.profile_kwargs)
    result = run_pipeline_sync(profile, case.user_input, case.existing_plan)

    assert result.intent == case.expected_intent, (
        f"[{case.name}] 期望 intent={case.expected_intent}，实际得到 {result.intent}"
    )

    # ATTRACTION_QUERY / BUDGET_CHECK 是纯规则/格式化路径，没有生成式内容可评。
    if case.expected_intent not in (Intent.FULL_DAY_PLAN, Intent.MODIFY_PLAN):
        return

    if not isinstance(result.itinerary, dict) or "error" in result.itinerary:
        pytest.fail(f"[{case.name}] Planner 未返回可用行程: {result.itinerary}")

    itinerary_input = profile.to_prompt_prefix() + "\n\n用户需求：" + case.user_input
    output_text = itinerary_text(result)

    # (0) 结构性检查先跑：零成本、确定性、失败信息直指具体哪一条。
    #     结构就不对的话，没必要再花 judge 的 token 去评它好不好。
    checks = run_structural_checks(result.itinerary, profile)
    assert all(c.passed for c in checks), (
        f"[{case.name}] 结构性检查未通过：\n{format_failures(checks)}\n\n{output_text}"
    )

    # (a) 景点选择是否符合画像约束
    assert_test(
        LLMTestCase(input=itinerary_input, actual_output=output_text),
        [attraction_fit_metric],
    )

    # (b) 行程批注忠实度 + 有用性（只有拿到 notes 和对应 RAG 片段时才评）
    notes_text = collect_notes_text(result)
    snippets = collect_rag_snippets(result)
    if notes_text and snippets:
        assert_test(
            LLMTestCase(
                input=itinerary_input,
                actual_output=notes_text,
                retrieval_context=snippets,
            ),
            [notes_faithfulness_metric, notes_helpfulness_metric],
        )

    # (c) 预算护栏：要么自纠正到预算内，要么在 warnings 里明确报出 [预算问题]
    if case.expect_budget_overrun_guard:
        total_cost = result.itinerary.get("total_cost_usd", 0)
        guarded = total_cost <= profile.budget_usd or any(
            "[预算问题]" in w for w in result.warnings
        )
        assert guarded, (
            f"[{case.name}] 预算超限未被拦截: total=${total_cost}, "
            f"budget=${profile.budget_usd}, warnings={result.warnings}"
        )
