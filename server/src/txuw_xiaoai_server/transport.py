from __future__ import annotations

import asyncio
import json
import logging
from uuid import uuid4

from fastapi import WebSocket

from .xiaoai_handlers.ports import AudioChunkSink, PlaybackControlPort


logger = logging.getLogger(__name__)


class ClientSessionTransport(AudioChunkSink, PlaybackControlPort):
    """面向单个 websocket 连接的远端调用与音频输出传输层。"""

    def __init__(
        self,
        websocket: WebSocket,
        connection_id: str,
        *,
        play_config: dict[str, object],
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self._websocket = websocket
        self._connection_id = connection_id
        self._play_config = play_config
        self._request_timeout_seconds = request_timeout_seconds
        self._send_lock = asyncio.Lock()
        self._pending_requests: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._started = False

    async def ensure_started(self) -> None:
        if self._started:
            return
        await self._call_remote("start_play", self._play_config)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await self._call_remote("stop_play", None)
        finally:
            self._started = False

    async def run_shell(self, script: str) -> str:
        response = await self._call_remote("run_shell", script)
        return json.dumps(response.get("data"), ensure_ascii=False)

    async def write(self, data: bytes) -> None:
        frame = {
            "id": str(uuid4()),
            "tag": "play",
            "bytes": list(data),
            "data": None,
        }
        async with self._send_lock:
            await self._websocket.send_bytes(json.dumps(frame).encode("utf-8"))

    def accept_response(self, message) -> bool:
        # request/response 配对属于 transport 内部职责，业务层不需要感知请求 ID。
        body = getattr(message, "body", None)
        if body is None:
            return False

        future = self._pending_requests.pop(body.id, None)
        if future is None:
            return False

        if not future.done():
            future.set_result(
                {
                    "id": body.id,
                    "code": body.code,
                    "msg": body.msg,
                    "data": body.data,
                }
            )
        return True

    async def fail_pending(self, reason: str) -> None:
        for request_id, future in list(self._pending_requests.items()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
            self._pending_requests.pop(request_id, None)

    async def _call_remote(
        self,
        command: str,
        payload: object | None,
    ) -> dict[str, object]:
        # 主动调用旧设备能力时，需要在这里维护请求 ID 与 future 的映射，
        # 才能把异步返回的 response 正确配对回发起方。
        request_id = str(uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self._pending_requests[request_id] = future

        request = {
            "Request": {
                "id": request_id,
                "command": command,
                "payload": payload,
            }
        }

        try:
            async with self._send_lock:
                await self._websocket.send_text(json.dumps(request, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=self._request_timeout_seconds)
        except Exception:
            self._pending_requests.pop(request_id, None)
            logger.exception(
                "transport.remote_call.failed",
                extra={
                    "connectionId": self._connection_id,
                    "command": command,
                    "summary": "remote call failed",
                },
            )
            raise
