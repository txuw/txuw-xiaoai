from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class MemoryCommitRequest:
    user_id: str
    messages: list[dict[str, str]]
    idempotency_key: str


class MemoryCommitWorker:
    def __init__(
        self,
        *,
        memory: Any,
        queue_maxsize: int = 256,
        worker_count: int = 2,
    ) -> None:
        self._memory = memory
        self._queue: asyncio.Queue[MemoryCommitRequest | None] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._worker_count = worker_count
        self._workers: list[asyncio.Task[None]] = []
        self._seen_keys: set[str] = set()
        self._running = False

    async def start(self) -> None:
        if self._running:
            return

        self._running = True
        for index in range(self._worker_count):
            self._workers.append(
                asyncio.create_task(self._worker_loop(name=f"memory-commit-{index}"))
            )

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        for _ in self._workers:
            await self._queue.put(None)

        workers = list(self._workers)
        self._workers.clear()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def enqueue(
        self,
        user_id: str,
        messages: list[dict[str, str]],
        idempotency_key: str,
    ) -> bool:
        if idempotency_key in self._seen_keys:
            logger.info(
                "memory.commit.skipped",
                extra={
                    "userId": user_id,
                    "idempotencyKey": idempotency_key,
                    "status": "duplicate",
                    "summary": "memory commit skipped: duplicate request",
                },
            )
            return False

        try:
            self._queue.put_nowait(
                MemoryCommitRequest(
                    user_id=user_id,
                    messages=messages,
                    idempotency_key=idempotency_key,
                )
            )
        except asyncio.QueueFull:
            logger.warning(
                "memory.commit.dropped",
                extra={
                    "userId": user_id,
                    "idempotencyKey": idempotency_key,
                    "status": "dropped",
                    "summary": "memory commit queue full",
                },
            )
            return False

        self._seen_keys.add(idempotency_key)
        logger.info(
            "memory.commit.queued",
            extra={
                "userId": user_id,
                "idempotencyKey": idempotency_key,
                "messageCount": len(messages),
                "status": "queued",
                "summary": "memory commit queued",
            },
        )
        return True

    async def _worker_loop(self, *, name: str) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return

                response = await asyncio.to_thread(
                    self._memory.add,
                    item.messages,
                    user_id=item.user_id,
                )
                result_items = _extract_memory_results(response)
                if not result_items:
                    logger.info(
                        "memory.commit.noop",
                        extra={
                            "worker": name,
                            "userId": item.user_id,
                            "idempotencyKey": item.idempotency_key,
                            "messageCount": len(item.messages),
                            "resultCount": 0,
                            "status": "noop",
                            "summary": "memory commit completed with no extracted memories",
                        },
                    )
                else:
                    logger.info(
                        "memory.commit.completed",
                        extra={
                            "worker": name,
                            "userId": item.user_id,
                            "idempotencyKey": item.idempotency_key,
                            "messageCount": len(item.messages),
                            "resultCount": len(result_items),
                            "status": "completed",
                            "summary": "memory commit completed",
                        },
                    )
            except Exception:
                logger.exception(
                    "memory.commit.failed",
                    extra={
                        "worker": name,
                        "userId": getattr(item, "user_id", "-"),
                        "idempotencyKey": getattr(item, "idempotency_key", "-"),
                        "status": "failed",
                        "summary": "memory commit failed",
                    },
                )
            finally:
                self._queue.task_done()


def _extract_memory_results(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        results = response.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return []

    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]

    return []
