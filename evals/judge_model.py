"""
Eval judge 模型 —— 独立于被测系统的 vLLM/Qwen 客户端，避免同模型自评偏差。
用 anthropic SDK 直接包一个 DeepEvalBaseLLM 子类，不依赖 deepeval 是否内置 Anthropic wrapper。
"""

import anthropic
from deepeval.models import DeepEvalBaseLLM

JUDGE_MODEL_ID = "claude-sonnet-5"


class ClaudeJudge(DeepEvalBaseLLM):
    def __init__(self, model_name: str = JUDGE_MODEL_ID):
        self.model_name = model_name
        self._client = anthropic.Anthropic()
        self._async_client = anthropic.AsyncAnthropic()

    def load_model(self):
        return self._client

    def generate(self, prompt: str) -> str:
        resp = self._client.messages.create(
            model=self.model_name,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    async def a_generate(self, prompt: str) -> str:
        resp = await self._async_client.messages.create(
            model=self.model_name,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    def get_model_name(self) -> str:
        return self.model_name


JUDGE = ClaudeJudge()
