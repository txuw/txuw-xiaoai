from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..ports import StreamingLlmClient


@dataclass(slots=True)
class OpenAiCompatibleLlmConfig:
    """OpenAI 兼容 LLM 配置。"""

    api_key: str
    model: str
    base_url: str = ""
    timeout_seconds: float = 60.0
    system_prompt: str = ""


class OpenAiCompatibleStreamingClient(StreamingLlmClient):
    """使用 OpenAI SDK 对接 LiteLLM 兼容接口。"""

    def __init__(self, config: OpenAiCompatibleLlmConfig) -> None:
        self._config = config
        self._client = self._build_client(config)

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
        messages = [
            {"role": "system", "content": self._config.system_prompt},
            {"role": "user", "content": prompt},
        ]
        stream = await self._client.chat.completions.create(
            model=self._config.model,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            delta_text = _extract_delta_text(chunk)
            if not delta_text or not delta_text.strip():
                continue
            yield delta_text

    @staticmethod
    def _build_client(config: OpenAiCompatibleLlmConfig) -> Any:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "The openai package is required. Run `uv sync` to install server dependencies."
            ) from exc

        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_seconds,
            "max_retries": 1,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return AsyncOpenAI(**kwargs)


def _extract_delta_text(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""

    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return ""

    content = getattr(delta, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_coerce_content_part_text(item) for item in content)
    return ""


def _coerce_content_part_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        text = item.get("text")
        return text if isinstance(text, str) else ""

    text = getattr(item, "text", None)
    return text if isinstance(text, str) else ""
