from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from types import ModuleType

import pytest

from txuw_xiaoai_server.xiaoai_handlers.memory import (
    MemoryCommitWorker,
    MemoryProvider,
    MemoryProviderConfig,
)


class _FakeConfig:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _FakeMemoryItem:
    def __init__(self, *, item_id: str, score: float, payload: dict[str, object]) -> None:
        self.id = item_id
        self.score = score
        self.payload = payload


class _FakeEmbeddingModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def embed(self, text: str, memory_action: str) -> list[float]:
        self.calls.append({"text": text, "memory_action": memory_action})
        time.sleep(0.001)
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.search_response: list[_FakeMemoryItem] = []

    def search(
        self,
        *,
        query: str,
        vectors: list[float],
        limit: int,
        filters: dict[str, object],
    ) -> list[_FakeMemoryItem]:
        self.calls.append(
            {
                "query": query,
                "vectors": vectors,
                "limit": limit,
                "filters": filters,
            }
        )
        time.sleep(0.001)
        return list(self.search_response)


class _FakeMemory:
    created_config = None

    def __init__(self, config) -> None:
        type(self).created_config = config
        self.add_calls: list[dict[str, object]] = []
        self.add_response: dict[str, object] | list[dict[str, object]] | None = None
        self.embedding_model = _FakeEmbeddingModel()
        self.vector_store = _FakeVectorStore()

    def add(self, messages: list[dict[str, str]], *, user_id: str) -> dict[str, object] | list[dict[str, object]] | None:
        self.add_calls.append({"messages": messages, "user_id": user_id})
        return self.add_response


def _install_fake_mem0(monkeypatch: pytest.MonkeyPatch) -> None:
    mem0_module = ModuleType("mem0")
    mem0_module.Memory = _FakeMemory

    configs_module = ModuleType("mem0.configs")
    base_module = ModuleType("mem0.configs.base")
    base_module.EmbedderConfig = _FakeConfig
    base_module.LlmConfig = _FakeConfig
    base_module.MemoryConfig = _FakeConfig
    base_module.VectorStoreConfig = _FakeConfig

    monkeypatch.setitem(sys.modules, "mem0", mem0_module)
    monkeypatch.setitem(sys.modules, "mem0.configs", configs_module)
    monkeypatch.setitem(sys.modules, "mem0.configs.base", base_module)


def test_memory_provider_disabled_does_not_initialize(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_mem0(monkeypatch)
    provider = MemoryProvider(MemoryProviderConfig(enabled=False))

    asyncio.run(provider.startup())

    assert provider.enabled is False
    assert provider.memory is None
    assert provider.commit_worker is None


def test_memory_provider_requires_llm_key_and_milvus_url() -> None:
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        asyncio.run(
            MemoryProvider(
                MemoryProviderConfig(enabled=True, milvus_url="http://milvus"),
            ).startup()
        )

    with pytest.raises(ValueError, match="MEMORY_MILVUS_URL"):
        asyncio.run(
            MemoryProvider(
                MemoryProviderConfig(enabled=True),
                llm_api_key="test-key",
            ).startup()
        )


def test_memory_provider_startup_uses_default_models_and_reused_litellm_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_mem0(monkeypatch)
    provider = MemoryProvider(
        MemoryProviderConfig(
            enabled=True,
            milvus_url="http://milvus:19530",
        ),
        llm_api_key="test-key",
        llm_base_url="http://litellm/v1",
    )

    asyncio.run(provider.startup())

    assert provider.enabled is True
    assert provider.commit_worker is not None
    assert os.environ["OPENAI_API_KEY"] == "test-key"
    assert os.environ["OPENAI_BASE_URL"] == "http://litellm/v1"
    assert os.environ["MEM0_TELEMETRY"] == "False"

    created_config = _FakeMemory.created_config
    assert created_config.llm.config["model"] == "gpt-5-nano-2025-08-07"
    assert created_config.llm.config["api_key"] == "test-key"
    assert created_config.llm.config["openai_base_url"] == "http://litellm/v1"
    assert created_config.embedder.config["model"] == "text-embedding-3-small"
    assert created_config.embedder.config["api_key"] == "test-key"
    assert created_config.embedder.config["openai_base_url"] == "http://litellm/v1"
    assert created_config.vector_store.config["collection_name"] == "txuw_xiaoai_mem0"
    assert created_config.vector_store.config["url"] == "http://milvus:19530"

    asyncio.run(provider.shutdown())
    assert provider.memory is None
    assert provider.commit_worker is None

def test_memory_provider_search_filters_by_score_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_mem0(monkeypatch)
    provider = MemoryProvider(
        MemoryProviderConfig(
            enabled=True,
            milvus_url="http://milvus:19530",
            recall_max_results=2,
            recall_min_score=0.5,
        ),
        llm_api_key="test-key",
    )
    asyncio.run(provider.startup())
    memory = provider.memory
    assert memory is not None
    memory.vector_store.search_response = [
        _FakeMemoryItem(item_id="1", score=0.2, payload={"data": "low"}),
        _FakeMemoryItem(item_id="2", score=0.9, payload={"data": "first"}),
        _FakeMemoryItem(item_id="3", score=0.8, payload={"data": "second"}),
        _FakeMemoryItem(item_id="4", score=0.7, payload={"data": "third"}),
    ]

    results = asyncio.run(provider.search("weather", user_id="custom-user"))

    assert results == [
        {
            "id": "2",
            "memory": "first",
            "hash": None,
            "created_at": None,
            "updated_at": None,
            "score": 0.9,
        },
        {
            "id": "3",
            "memory": "second",
            "hash": None,
            "created_at": None,
            "updated_at": None,
            "score": 0.8,
        },
    ]
    assert memory.embedding_model.calls == [
        {"text": "weather", "memory_action": "search"}
    ]
    assert memory.vector_store.calls == [
        {
            "query": "weather",
            "vectors": [0.1, 0.2, 0.3],
            "limit": 100,
            "filters": {"user_id": "custom-user"},
        }
    ]


def test_memory_provider_search_with_metrics_returns_stage_timings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_mem0(monkeypatch)
    provider = MemoryProvider(
        MemoryProviderConfig(
            enabled=True,
            milvus_url="http://milvus:19530",
        ),
        llm_api_key="test-key",
    )
    asyncio.run(provider.startup())
    memory = provider.memory
    assert memory is not None
    memory.vector_store.search_response = [
        _FakeMemoryItem(item_id="1", score=0.9, payload={"data": "first"}),
    ]

    result = asyncio.run(provider.search_with_metrics("weather", user_id="custom-user"))

    assert result.results == [
        {
            "id": "1",
            "memory": "first",
            "hash": None,
            "created_at": None,
            "updated_at": None,
            "score": 0.9,
        }
    ]
    assert result.metrics.embedding_http_ms is not None
    assert result.metrics.embedding_http_ms >= 0
    assert result.metrics.milvus_search_ms is not None
    assert result.metrics.milvus_search_ms >= 0
    assert result.metrics.memory_recall_total_ms >= result.metrics.embedding_http_ms
    assert result.metrics.memory_recall_total_ms >= result.metrics.milvus_search_ms


def test_memory_commit_worker_logs_completed_when_memories_are_written(
    caplog: pytest.LogCaptureFixture,
) -> None:
    memory = _FakeMemory(config=None)
    memory.add_response = {
        "results": [
            {"id": "mem-1", "memory": "主人喜欢咖啡", "event": "ADD"},
        ]
    }
    worker = MemoryCommitWorker(memory=memory, worker_count=1)

    async def scenario() -> None:
        await worker.start()
        await worker.enqueue(
            "txuw",
            [{"role": "user", "content": "我喜欢咖啡"}],
            "conn:dialog:query",
        )
        await asyncio.sleep(0.05)
        await worker.stop()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())

    completed_records = [
        record for record in caplog.records if record.msg == "memory.commit.completed"
    ]
    assert len(completed_records) == 1
    assert getattr(completed_records[0], "resultCount") == 1


def test_memory_commit_worker_logs_noop_when_mem0_extracts_no_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    memory = _FakeMemory(config=None)
    memory.add_response = {"results": []}
    worker = MemoryCommitWorker(memory=memory, worker_count=1)

    async def scenario() -> None:
        await worker.start()
        await worker.enqueue(
            "txuw",
            [{"role": "user", "content": "今天天气怎么样"}],
            "conn:dialog:asr",
        )
        await asyncio.sleep(0.05)
        await worker.stop()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())

    noop_records = [
        record for record in caplog.records if record.msg == "memory.commit.noop"
    ]
    assert len(noop_records) == 1
    assert getattr(noop_records[0], "resultCount") == 0
