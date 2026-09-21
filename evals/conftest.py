"""
共享 fixture —— 不设 autouse，只在需要的测试文件里通过
pytestmark = pytest.mark.usefixtures(...) 引入，保证纯规则的
test_verifier_unit.py 不依赖 vLLM 服务器或 ANTHROPIC_API_KEY。
"""

import os

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv()

VLLM_HEALTH_URL = "http://localhost:8000/v1/models"


# 用 skip 而不是 exit：exit 会中止整个 session，连不依赖外部服务的
# test_verifier_unit / test_structural_checks 都跑不成。缺依赖应该只影响
# 需要它的那些用例，其余照跑。


@pytest.fixture(scope="session")
def require_vllm_server():
    try:
        httpx.get(VLLM_HEALTH_URL, timeout=3.0).raise_for_status()
    except Exception as e:
        pytest.skip(
            "vLLM 服务未在 :8000 端口响应，跳过生成式评测。启动方式：\n"
            "  vllm serve Qwen/Qwen2.5-3B-Instruct-AWQ --port 8000 "
            "--enable-auto-tool-choice --tool-call-parser hermes\n"
            f"({type(e).__name__})",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def require_anthropic_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "ANTHROPIC_API_KEY 未设置，跳过 LLM-judge 评测（在 .env 里配置后可启用）。",
            allow_module_level=True,
        )
