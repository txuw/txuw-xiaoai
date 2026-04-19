from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from .commit import MemoryCommitWorker


@dataclass(slots=True, frozen=True)
class MemoryProviderConfig:
    enabled: bool = False
    user_id: str = "txuw"
    llm_model: str = "gpt-5-nano-2025-08-07"
    embedding_model: str = "text-embedding-3-small"
    milvus_url: str = ""
    milvus_token: str = ""
    milvus_db_name: str = "default"
    milvus_collection_name: str = "txuw_xiaoai_mem0"
    recall_max_results: int = 5
    recall_min_score: float = 0.3
    commit_queue_maxsize: int = 256
    commit_worker_count: int = 2
    timeout_seconds: float = 60.0


class MemoryProvider:
    def __init__(
        self,
        config: MemoryProviderConfig,
        *,
        llm_api_key: str = "",
        llm_base_url: str = "",
    ) -> None:
        self._config = config
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url
        self._memory: Any | None = None
        self._commit_worker: MemoryCommitWorker | None = None

    @property
    def enabled(self) -> bool:
        return self._memory is not None

    @property
    def user_id(self) -> str:
        return self._config.user_id

    @property
    def memory(self) -> Any | None:
        return self._memory

    @property
    def commit_worker(self) -> MemoryCommitWorker | None:
        return self._commit_worker

    async def startup(self) -> None:
        if not self._config.enabled or self._memory is not None:
            return

        if not self._llm_api_key:
            raise ValueError("Memory enabled but LLM_API_KEY is empty.")
        if not self._config.milvus_url:
            raise ValueError("Memory enabled but MEMORY_MILVUS_URL is empty.")

        self._memory = self._create_memory()
        self._commit_worker = MemoryCommitWorker(
            memory=self._memory,
            queue_maxsize=self._config.commit_queue_maxsize,
            worker_count=self._config.commit_worker_count,
        )
        await self._commit_worker.start()

    async def shutdown(self) -> None:
        if self._commit_worker is not None:
            await self._commit_worker.stop()
        self._commit_worker = None
        self._memory = None

    async def search(
        self,
        query: str,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.enabled or not query.strip():
            return []

        assert self._memory is not None
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self._memory.search,
                query,
                user_id=user_id or self._config.user_id,
            ),
            timeout=self._config.timeout_seconds,
        )
        if not isinstance(response, dict):
            return []

        results = response.get("results", [])
        if not isinstance(results, list):
            return []

        filtered: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            memory_text = item.get("memory")
            if not isinstance(memory_text, str) or not memory_text.strip():
                continue
            try:
                score = float(item.get("score", 0))
            except (TypeError, ValueError):
                score = 0.0
            if score < self._config.recall_min_score:
                continue
            filtered.append(item)
            if len(filtered) >= self._config.recall_max_results:
                break
        return filtered

    def _create_memory(self) -> Any:
        from mem0 import Memory
        from mem0.configs.base import (
            EmbedderConfig,
            LlmConfig,
            MemoryConfig,
            VectorStoreConfig,
        )

        os.environ.setdefault("MEM0_TELEMETRY", "False")
        os.environ["OPENAI_API_KEY"] = self._llm_api_key
        if self._llm_base_url:
            os.environ["OPENAI_BASE_URL"] = self._llm_base_url

        memory_config = MemoryConfig(
            llm=LlmConfig(
                provider="openai",
                config={
                    "api_key": self._llm_api_key,
                    "openai_base_url": self._llm_base_url,
                    "model": self._config.llm_model,
                    "temperature": 0.1,
                    "max_tokens": 1500,
                },
            ),
            embedder=EmbedderConfig(
                provider="openai",
                config={
                    "api_key": self._llm_api_key,
                    "openai_base_url": self._llm_base_url,
                    "model": self._config.embedding_model,
                },
            ),
            vector_store=VectorStoreConfig(
                provider="milvus",
                config={
                    "collection_name": self._config.milvus_collection_name,
                    "embedding_model_dims": 1536,
                    "url": self._config.milvus_url,
                    "token": self._config.milvus_token,
                    "metric_type": "L2",
                    "db_name": self._config.milvus_db_name,
                },
            ),
        )
        return Memory(memory_config)
