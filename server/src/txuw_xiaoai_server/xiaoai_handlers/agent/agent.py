from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agents import Agent, Model, ModelProvider, OpenAIChatCompletionsModel, RunConfig, Runner
from openai import AsyncOpenAI


@dataclass(slots=True)
class AgentStreamConfig:
    """Agent SDK 文本流式输出所需的最小配置。"""

    api_key: str
    model: str
    base_url: str = ""
    timeout_seconds: float = 60.0
    system_prompt: str = ""
    agent_name: str = "XiaoAi Assistant"


class _OpenAiCompatibleModelProvider(ModelProvider):
    """把现有 OpenAI 兼容接口接到 Agent SDK 的模型查找入口。"""

    def __init__(self, client: AsyncOpenAI, default_model: str) -> None:
        self._client = client
        self._default_model = default_model

    def get_model(self, model_name: str | None) -> Model:
        return OpenAIChatCompletionsModel(
            model=model_name or self._default_model,
            openai_client=self._client,
        )


class AgentStreamService:
    """把 Agent SDK 的流式文本输出接回现有 TTS 链路。"""

    def __init__(self, config: AgentStreamConfig) -> None:
        self._config = config
        self._client = self._build_client(config)
        self._model_provider = _OpenAiCompatibleModelProvider(self._client, config.model)
        self._run_config = RunConfig(model_provider=self._model_provider)
        # 当前链路仍然由 Query/ASR 文本事件触发，所以这里保持无状态 Agent，
        # 避免为单轮问答额外维护会话历史，先把输出接入点做得清晰可读。
        self._agent = Agent(
            name=config.agent_name,
            instructions=config.system_prompt,
        )

    def stream_text(self, prompt: str) -> AsyncIterator[str]:
        return self._stream_text(prompt)

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if not callable(close):
            return

        result = close()
        if inspect.isawaitable(result):
            await result

    async def _stream_text(self, prompt: str) -> AsyncIterator[str]:
        result = Runner.run_streamed(
            self._agent,
            prompt,
            run_config=self._run_config,
        )

        try:
            async for event in result.stream_events():
                delta = _extract_output_text_delta(event)
                if not delta or not delta.strip():
                    continue
                yield delta
        except asyncio.CancelledError:
            result.cancel()
            raise

        if result.run_loop_exception is not None:
            raise result.run_loop_exception

    @staticmethod
    def _build_client(config: AgentStreamConfig) -> AsyncOpenAI:
        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_seconds,
            "max_retries": 1,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return AsyncOpenAI(**kwargs)


def _extract_output_text_delta(event: Any) -> str:
    # Agent SDK 会产出多种语义事件，但当前旧 TTS 链路只需要最终可播报的文本增量，
    # 因此这里严格只消费 `response.output_text.delta`，避免把工具、推理或其他事件误送进 TTS。
    if getattr(event, "type", None) != "raw_response_event":
        return ""

    data = getattr(event, "data", None)
    if getattr(data, "type", None) != "response.output_text.delta":
        return ""

    delta = getattr(data, "delta", None)
    return delta if isinstance(delta, str) else ""
