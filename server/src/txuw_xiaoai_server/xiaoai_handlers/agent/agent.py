from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agents import Agent, Model, ModelProvider, OpenAIChatCompletionsModel, RunConfig, Runner
from openai import AsyncOpenAI

from .runtime import AgentRunContext, DEFAULT_SKILL_PROFILE, SkillProfile
from .tool import AgentToolsetFactory, RegionLookupService


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

    def __init__(
        self,
        config: AgentStreamConfig,
        *,
        region_service: RegionLookupService,
        toolset_factory: AgentToolsetFactory | None = None,
        default_skill_profile: SkillProfile = DEFAULT_SKILL_PROFILE,
    ) -> None:
        self._config = config
        self._client = self._build_client(config)
        self._model_provider = _OpenAiCompatibleModelProvider(self._client, config.model)
        self._region_service = region_service
        self._toolset_factory = toolset_factory or AgentToolsetFactory(
            amap_web_service_key="",
            region_tool_enabled=False,
            region_tool_timeout_seconds=1.0,
        )
        self._default_skill_profile = default_skill_profile
        self._run_config = RunConfig(
            model_provider=self._model_provider,
            tracing_disabled=True,
        )

    @property
    def region_service(self) -> RegionLookupService:
        return self._region_service

    def stream_text(
        self,
        prompt: str,
        run_context: AgentRunContext,
    ) -> AsyncIterator[str]:
        return self._stream_text(prompt, run_context)

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

        await self._region_service.close()

    async def _stream_text(
        self,
        prompt: str,
        run_context: AgentRunContext,
    ) -> AsyncIterator[str]:
        result = Runner.run_streamed(
            self._build_agent(run_context),
            prompt,
            context=run_context,
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

    def _build_agent(self, run_context: AgentRunContext) -> Agent[AgentRunContext]:
        toolset = self._toolset_factory.build_toolset(
            run_context,
            skill_profile=self._default_skill_profile,
        )

        # 当前 tools 数量仍然非常少，所以按轮动态构建 Agent 比预先维护全局 registry 更易读。
        # 这样后续只需要替换 toolset factory，就能平滑接入 MCP 和 Skill，而不用改主链路。
        return Agent(
            name=self._config.agent_name,
            instructions=_merge_instructions(self._config.system_prompt, toolset.extra_instructions),
            tools=toolset.tools,
            mcp_servers=toolset.mcp_servers,
            mcp_config={"convert_schemas_to_strict": True},
        )

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


def _merge_instructions(system_prompt: str, extra_instructions: str) -> str:
    if not extra_instructions.strip():
        return system_prompt
    if not system_prompt.strip():
        return extra_instructions.strip()
    return f"{system_prompt.strip()}\n\n{extra_instructions.strip()}"
