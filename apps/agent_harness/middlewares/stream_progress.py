"""
中间件 2：SSE 进度流、Redis Stream 解耦与增量合批（StreamProgressMiddleware）。

对应文档章节：
  - 六、中间件 2（StreamProgressMiddleware）
  - 九、9.2 SSE 流式协议标准事件表（11 种事件，含 error 与 Last-Event-ID 续传）
  - 十一、v1.1 P1 #14 & P2 #28

核心设计说明：
  1. 图执行（Run）在后台 asyncio.Task 中运行，节点通过本中间件向 Redis Stream
     `pytravel:{tenant_id}:run:{thread_id}:events` 写入自定义进度/卡片/图表/文本事件；
  2. SSE 接口只负责从该 Redis Stream 按 `Last-Event-ID` 消费推送，即使前端断网、刷新页面、
     或由 OA Webhook 异步唤醒（无实时 HTTP 连接），事件也不会丢失且支持完整回放；
  3. 对 `message` 和 `thinking` 增量采用 32 字符或 50ms 合批策略，显著降低高频小包开销。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from apps.api_gateway.security.masker import SensitiveDataMasker
from apps.common.redis_client import get_redis

STREAM_TTL_SECONDS = 3600
STREAM_MAXLEN = 2000


def make_stream_key(tenant_id: str, thread_id: str) -> str:
    """构造严格符合 8.2 规范的 SSE 事件流 Redis Stream Key。"""
    return f"pytravel:{tenant_id}:run:{thread_id}:events"


class StreamProgressMiddleware:
    """
    负责将 Agent 执行过程中的结构化事件推送到 Redis Stream，并支持增量合批与 SSE 格式化输出。
    """

    def __init__(self, tenant_id: str, thread_id: str) -> None:
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        self.stream_key = make_stream_key(tenant_id, thread_id)
        self._redis = get_redis()

        # 增量合批缓冲区：按事件类型（message / thinking）分别缓冲
        self._buffers: dict[str, str] = {"message": "", "thinking": ""}
        self._last_flush_ts: dict[str, float] = {"message": time.monotonic(), "thinking": time.monotonic()}

    async def emit(self, event_type: str, payload: dict[str, Any]) -> str:
        """
        发送单条结构化 SSE 事件到 Redis Stream，并在发送前自动做敏感信息脱敏。
        发送非流式增量事件（如 progress/travel_cards/done）前会先冲刷文本缓冲，保证时序严格正确。
        """
        if event_type not in ("message", "thinking"):
            await self.flush_all()

        safe_payload = SensitiveDataMasker.mask_value(payload)
        msg_id = await self._redis.xadd(
            self.stream_key,
            {
                "event": event_type,
                "data": json.dumps(safe_payload, ensure_ascii=False, default=str),
            },
            maxlen=STREAM_MAXLEN,
        )
        await self._redis.expire(self.stream_key, STREAM_TTL_SECONDS)
        return msg_id

    async def emit_delta(self, event_type: str, delta: str) -> str | None:
        """
        对 `message` / `thinking` 文本增量进行 32 字符或 50ms 合批推送（v1.1 9.2 节）。
        """
        if event_type not in self._buffers:
            return await self.emit(event_type, {"delta": delta})

        self._buffers[event_type] += delta
        elapsed_ms = (time.monotonic() - self._last_flush_ts[event_type]) * 1000.0
        if len(self._buffers[event_type]) >= 32 or elapsed_ms >= 50.0:
            return await self.flush(event_type)
        return None

    async def flush(self, event_type: str) -> str | None:
        buf = self._buffers.get(event_type, "")
        if not buf:
            return None
        self._buffers[event_type] = ""
        self._last_flush_ts[event_type] = time.monotonic()
        return await self.emit(event_type, {"delta": buf})

    async def flush_all(self) -> None:
        for key in ("thinking", "message"):
            await self.flush(key)

    @staticmethod
    async def iter_sse_events(
        tenant_id: str,
        thread_id: str,
        last_event_id: str = "0-0",
        idle_timeout_seconds: float = 30.0,
    ) -> AsyncIterator[str]:
        """
        从 Redis Stream 持续读取事件并格式化为标准 SSE 帧（带 id / event / data）。
        支持浏览器 EventSource 断线重连携带 `Last-Event-ID` 无缝续推。
        """
        redis = get_redis()
        stream_key = make_stream_key(tenant_id, thread_id)
        cursor = last_event_id or "0-0"
        idle_start = time.monotonic()

        while True:
            entries = await redis.xread_after(stream_key, last_id=cursor, block_ms=500, count=100)
            if entries:
                idle_start = time.monotonic()
                for msg_id, fields in entries:
                    cursor = msg_id
                    ev = fields.get("event", "message")
                    data = fields.get("data", "{}")
                    yield f"id: {msg_id}\nevent: {ev}\ndata: {data}\n\n"
                    if ev in ("done", "interrupted", "error"):
                        return
            else:
                if time.monotonic() - idle_start >= idle_timeout_seconds:
                    # 防止长连接无限悬挂
                    return
