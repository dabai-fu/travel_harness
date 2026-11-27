"""
统一异步 Redis 适配器（支持真实 Redis 7 + Lua 与零依赖内存模拟）。

对应文档章节：八、Redis Key 规范表 (8.2) & 六、熔断/流式/集群中断中间件
设计亮点：
  1. 在真实 Redis 模式下，熔断器状态流转与计数通过原生 Lua 脚本执行，保证多副本并发零竞态；
  2. 在 REDIS_MODE=memory 模式下，使用 asyncio.Lock 复现完全一致的 Stream、Pub/Sub、TTL 与熔断语义；
  3. 对外暴露统一的异步方法，上层中间件（SSE Stream、CircuitBreaker、ClusterCancel、Quota）
     无需关心底层是单机内存还是集群 Redis。
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from apps.common.config import get_settings

# ----------------------------------------------------------------------
# Redis 7 真实环境使用的熔断器原子 Lua 脚本（对应 Middleware 3 / v1.1 P1 #15）
# ----------------------------------------------------------------------
# 1) 准入判断 Lua：原子完成 OPEN->HALF_OPEN 冷却期转换与单探活抢占（防惊群）
_CB_CHECK_LUA = """
local key = KEYS[1]
local probe_key = KEYS[2]
local now = tonumber(ARGV[1])
local probe_ttl = tonumber(ARGV[2])

local state = redis.call('HGET', key, 'state')
if not state or state == 'CLOSED' then
    return {'ALLOW', 'CLOSED'}
end

if state == 'OPEN' then
    local next_retry = tonumber(redis.call('HGET', key, 'next_retry_at') or '0')
    if now < next_retry then
        return {'DENY', 'OPEN', tostring(math.ceil(next_retry - now))}
    end
    -- 冷却期已过：尝试原子转 HALF_OPEN 并抢占唯一探活名额
    redis.call('HSET', key, 'state', 'HALF_OPEN')
    local acquired = redis.call('SET', probe_key, '1', 'NX', 'EX', probe_ttl)
    if acquired then
        return {'ALLOW_PROBE', 'HALF_OPEN'}
    else
        return {'DENY', 'HALF_OPEN', '1'}
    end
end

if state == 'HALF_OPEN' then
    local acquired = redis.call('SET', probe_key, '1', 'NX', 'EX', probe_ttl)
    if acquired then
        return {'ALLOW_PROBE', 'HALF_OPEN'}
    else
        return {'DENY', 'HALF_OPEN', '1'}
    end
end

return {'ALLOW', state}
"""

# 2) 失败记录 Lua：累加失败次数，达到阈值（3次）转 OPEN 并按 30/60/120s 指数退避
_CB_FAIL_LUA = """
local key = KEYS[1]
local probe_key = KEYS[2]
local now = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local base_cooldown = tonumber(ARGV[3])
local max_cooldown = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local state = redis.call('HGET', key, 'state') or 'CLOSED'
local fails = tonumber(redis.call('HGET', key, 'fail_count') or '0') + 1
local open_count = tonumber(redis.call('HGET', key, 'open_count') or '0')

if state == 'HALF_OPEN' or fails >= threshold then
    open_count = open_count + 1
    local cooldown = math.min(max_cooldown, base_cooldown * (2 ^ math.max(0, open_count - 1)))
    redis.call('HSET', key,
        'state', 'OPEN',
        'fail_count', tostring(fails),
        'open_count', tostring(open_count),
        'next_retry_at', tostring(now + cooldown)
    )
    redis.call('DEL', probe_key)
    redis.call('EXPIRE', key, ttl)
    return {'OPEN', tostring(fails), tostring(cooldown)}
else
    redis.call('HSET', key, 'state', 'CLOSED', 'fail_count', tostring(fails))
    redis.call('EXPIRE', key, ttl)
    return {'CLOSED', tostring(fails), '0'}
end
"""

# 3) 成功归零 Lua：探活或正常调用成功后，闭合熔断器并清零计数
_CB_SUCCESS_LUA = """
local key = KEYS[1]
local probe_key = KEYS[2]
local ttl = tonumber(ARGV[1])

redis.call('HSET', key, 'state', 'CLOSED', 'fail_count', '0', 'open_count', '0', 'next_retry_at', '0')
redis.call('DEL', probe_key)
redis.call('EXPIRE', key, ttl)
return 'CLOSED'
"""


@dataclass
class _Entry:
    value: Any
    expires_at: float | None = None

    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at


class InMemoryRedis:
    """
    零外部依赖协程安全内存 Redis 实现。
    完整模拟 String/Counter/Hash/Stream(XADD/XRANGE/XREAD)/PubSub(PSUBSCRIBE) 及熔断原子操作。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._kv: dict[str, _Entry] = {}
        self._hashes: dict[str, _Entry] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
        self._stream_seq: dict[str, int] = defaultdict(int)
        self._stream_events: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        # 订阅者队列列表：(pattern, asyncio.Queue)
        self._psubscribers: list[tuple[str, asyncio.Queue[dict[str, str]]]] = []

    def _cleanup_kv(self, key: str) -> None:
        entry = self._kv.get(key)
        if entry and entry.is_expired():
            del self._kv[key]

    def _cleanup_hash(self, key: str) -> None:
        entry = self._hashes.get(key)
        if entry and entry.is_expired():
            del self._hashes[key]

    async def get(self, key: str) -> str | None:
        async with self._lock:
            self._cleanup_kv(key)
            entry = self._kv.get(key)
            return None if entry is None else str(entry.value)

    async def set(
        self,
        key: str,
        value: str | int,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        async with self._lock:
            self._cleanup_kv(key)
            if nx and key in self._kv:
                return False
            expires_at = (time.time() + ex) if ex is not None else None
            self._kv[key] = _Entry(value=str(value), expires_at=expires_at)
            return True

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            count = 0
            for k in keys:
                if k in self._kv:
                    del self._kv[k]
                    count += 1
                if k in self._hashes:
                    del self._hashes[k]
                    count += 1
            return count

    async def expire(self, key: str, seconds: int) -> bool:
        async with self._lock:
            expires_at = time.time() + seconds
            if key in self._kv:
                self._kv[key].expires_at = expires_at
                return True
            if key in self._hashes:
                self._hashes[key].expires_at = expires_at
                return True
            return False

    async def incrby(self, key: str, amount: int, ex: int | None = None) -> int:
        async with self._lock:
            self._cleanup_kv(key)
            current = int(self._kv[key].value) if key in self._kv else 0
            new_val = current + amount
            expires_at = (
                self._kv[key].expires_at
                if key in self._kv
                else ((time.time() + ex) if ex is not None else None)
            )
            self._kv[key] = _Entry(value=str(new_val), expires_at=expires_at)
            return new_val

    async def hgetall(self, key: str) -> dict[str, str]:
        async with self._lock:
            self._cleanup_hash(key)
            entry = self._hashes.get(key)
            return dict(entry.value) if entry else {}

    async def hset(self, key: str, mapping: dict[str, Any], ex: int | None = None) -> int:
        async with self._lock:
            self._cleanup_hash(key)
            data = dict(self._hashes[key].value) if key in self._hashes else {}
            for k, v in mapping.items():
                data[str(k)] = str(v)
            expires_at = (
                (time.time() + ex)
                if ex is not None
                else (self._hashes[key].expires_at if key in self._hashes else None)
            )
            self._hashes[key] = _Entry(value=data, expires_at=expires_at)
            return len(mapping)

    # --- Redis Stream 操作（用于 SSE 断线续传 Last-Event-ID）---
    async def xadd(
        self,
        stream_key: str,
        fields: dict[str, str],
        maxlen: int = 2000,
    ) -> str:
        async with self._lock:
            self._stream_seq[stream_key] += 1
            msg_id = f"{int(time.time() * 1000)}-{self._stream_seq[stream_key]}"
            str_fields = {str(k): str(v) for k, v in fields.items()}
            self._streams[stream_key].append((msg_id, str_fields))
            if len(self._streams[stream_key]) > maxlen:
                self._streams[stream_key] = self._streams[stream_key][-maxlen:]
        cond = self._stream_events[stream_key]
        async with cond:
            cond.notify_all()
        return msg_id

    async def xread_after(
        self,
        stream_key: str,
        last_id: str = "0-0",
        block_ms: int = 1000,
        count: int = 100,
    ) -> list[tuple[str, dict[str, str]]]:
        """读取 stream_key 中严格晚于 last_id 的事件；若暂无则阻塞等待 block_ms。"""

        def _collect() -> list[tuple[str, dict[str, str]]]:
            items = self._streams.get(stream_key, [])
            if not last_id or last_id in ("0", "0-0"):
                return items[:count]
            result: list[tuple[str, dict[str, str]]] = []
            found = False
            for mid, payload in items:
                if found:
                    result.append((mid, payload))
                    if len(result) >= count:
                        break
                elif mid == last_id:
                    found = True
            # 若 last_id 早于已裁剪位置或格式比较，则按序兜底
            if not found:
                for mid, payload in items:
                    if _compare_stream_id(mid, last_id) > 0:
                        result.append((mid, payload))
                        if len(result) >= count:
                            break
            return result

        batch = _collect()
        if batch or block_ms <= 0:
            return batch

        cond = self._stream_events[stream_key]
        async with cond:
            try:
                await asyncio.wait_for(cond.wait(), timeout=block_ms / 1000.0)
            except TimeoutError:
                return []
        return _collect()

    # --- Pub/Sub（用于集群跨副本取消信号）---
    async def publish(self, channel: str, message: str) -> int:
        delivered = 0
        for pattern, q in list(self._psubscribers):
            if fnmatch.fnmatch(channel, pattern):
                await q.put({"channel": channel, "data": message})
                delivered += 1
        return delivered

    async def psubscribe_iter(self, pattern: str) -> AsyncIterator[dict[str, str]]:
        q: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        sub = (pattern, q)
        self._psubscribers.append(sub)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            if sub in self._psubscribers:
                self._psubscribers.remove(sub)

    # --- 熔断器原子操作（等价于 Redis Lua 脚本）---
    async def cb_check(self, key: str, probe_key: str, probe_ttl: int = 15) -> list[str]:
        now = time.time()
        async with self._lock:
            self._cleanup_hash(key)
            self._cleanup_kv(probe_key)
            data = self._hashes[key].value if key in self._hashes else {}
            state = data.get("state", "CLOSED")
            if state == "CLOSED":
                return ["ALLOW", "CLOSED"]
            if state == "OPEN":
                next_retry = float(data.get("next_retry_at", "0"))
                if now < next_retry:
                    return ["DENY", "OPEN", str(int(max(1, next_retry - now)))]
                # 冷却期结束：转 HALF_OPEN 并抢占单探活锁
                data["state"] = "HALF_OPEN"
                self._hashes[key] = _Entry(value=data, expires_at=now + 600)
                if probe_key not in self._kv:
                    self._kv[probe_key] = _Entry(value="1", expires_at=now + probe_ttl)
                    return ["ALLOW_PROBE", "HALF_OPEN"]
                return ["DENY", "HALF_OPEN", "1"]
            if state == "HALF_OPEN":
                if probe_key not in self._kv:
                    self._kv[probe_key] = _Entry(value="1", expires_at=now + probe_ttl)
                    return ["ALLOW_PROBE", "HALF_OPEN"]
                return ["DENY", "HALF_OPEN", "1"]
            return ["ALLOW", state]

    async def cb_record_failure(
        self,
        key: str,
        probe_key: str,
        threshold: int = 3,
        base_cooldown: int = 30,
        max_cooldown: int = 120,
        ttl: int = 600,
    ) -> list[str]:
        now = time.time()
        async with self._lock:
            self._cleanup_hash(key)
            data = dict(self._hashes[key].value) if key in self._hashes else {}
            state = data.get("state", "CLOSED")
            fails = int(data.get("fail_count", "0")) + 1
            open_count = int(data.get("open_count", "0"))

            if state == "HALF_OPEN" or fails >= threshold:
                open_count += 1
                cooldown = min(max_cooldown, base_cooldown * (2 ** max(0, open_count - 1)))
                data.update(
                    {
                        "state": "OPEN",
                        "fail_count": str(fails),
                        "open_count": str(open_count),
                        "next_retry_at": str(now + cooldown),
                    }
                )
                self._hashes[key] = _Entry(value=data, expires_at=now + ttl)
                self._kv.pop(probe_key, None)
                return ["OPEN", str(fails), str(cooldown)]

            data.update({"state": "CLOSED", "fail_count": str(fails)})
            self._hashes[key] = _Entry(value=data, expires_at=now + ttl)
            return ["CLOSED", str(fails), "0"]

    async def cb_record_success(self, key: str, probe_key: str, ttl: int = 600) -> str:
        now = time.time()
        async with self._lock:
            self._hashes[key] = _Entry(
                value={"state": "CLOSED", "fail_count": "0", "open_count": "0", "next_retry_at": "0"},
                expires_at=now + ttl,
            )
            self._kv.pop(probe_key, None)
            return "CLOSED"


def _compare_stream_id(a: str, b: str) -> int:
    """比较两条 Redis Stream ID (ms-seq) 的先后顺序。"""
    def _parse(s: str) -> tuple[int, int]:
        parts = s.split("-", maxsplit=1)
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

    try:
        pa, pb = _parse(a), _parse(b)
        return (pa > pb) - (pa < pb)
    except Exception:
        return (a > b) - (a < b)


class RealRedisAdapter:
    """基于 redis.asyncio.Redis 的真实 Redis 7 适配器（生产模式使用）。"""

    def __init__(self, redis_url: str, password: str | None = None) -> None:
        import redis.asyncio as aioredis

        kwargs: dict[str, Any] = {"decode_responses": True}
        if password and "@" not in redis_url:
            kwargs["password"] = password
        self._client = aioredis.from_url(redis_url, **kwargs)
        self._cb_check_script = self._client.register_script(_CB_CHECK_LUA)
        self._cb_fail_script = self._client.register_script(_CB_FAIL_LUA)
        self._cb_success_script = self._client.register_script(_CB_SUCCESS_LUA)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str | int, ex: int | None = None, nx: bool = False) -> bool:
        res = await self._client.set(key, str(value), ex=ex, nx=nx)
        return bool(res)

    async def delete(self, *keys: str) -> int:
        return int(await self._client.delete(*keys))

    async def expire(self, key: str, seconds: int) -> bool:
        return bool(await self._client.expire(key, seconds))

    async def incrby(self, key: str, amount: int, ex: int | None = None) -> int:
        val = int(await self._client.incrby(key, amount))
        if ex is not None:
            await self._client.expire(key, ex)
        return val

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(await self._client.hgetall(key))

    async def hset(self, key: str, mapping: dict[str, Any], ex: int | None = None) -> int:
        res = int(await self._client.hset(key, mapping={str(k): str(v) for k, v in mapping.items()}))
        if ex is not None:
            await self._client.expire(key, ex)
        return res

    async def xadd(self, stream_key: str, fields: dict[str, str], maxlen: int = 2000) -> str:
        return str(await self._client.xadd(stream_key, fields, maxlen=maxlen, approximate=True))

    async def xread_after(
        self,
        stream_key: str,
        last_id: str = "0-0",
        block_ms: int = 1000,
        count: int = 100,
    ) -> list[tuple[str, dict[str, str]]]:
        resp = await self._client.xread({stream_key: last_id or "0-0"}, count=count, block=block_ms)
        if not resp:
            return []
        _, entries = resp[0]
        return [(str(mid), dict(fields)) for mid, fields in entries]

    async def publish(self, channel: str, message: str) -> int:
        return int(await self._client.publish(channel, message))

    async def psubscribe_iter(self, pattern: str) -> AsyncIterator[dict[str, str]]:
        pubsub = self._client.pubsub()
        await pubsub.psubscribe(pattern)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") == "pmessage":
                    yield {"channel": str(msg["channel"]), "data": str(msg["data"])}
        finally:
            await pubsub.punsubscribe(pattern)
            await pubsub.close()

    async def cb_check(self, key: str, probe_key: str, probe_ttl: int = 15) -> list[str]:
        res = await self._cb_check_script(keys=[key, probe_key], args=[time.time(), probe_ttl])
        return [str(x) for x in res]

    async def cb_record_failure(
        self,
        key: str,
        probe_key: str,
        threshold: int = 3,
        base_cooldown: int = 30,
        max_cooldown: int = 120,
        ttl: int = 600,
    ) -> list[str]:
        res = await self._cb_fail_script(
            keys=[key, probe_key],
            args=[time.time(), threshold, base_cooldown, max_cooldown, ttl],
        )
        return [str(x) for x in res]

    async def cb_record_success(self, key: str, probe_key: str, ttl: int = 600) -> str:
        res = await self._cb_success_script(keys=[key, probe_key], args=[ttl])
        return str(res)


_redis_singleton: InMemoryRedis | RealRedisAdapter | None = None


def get_redis() -> InMemoryRedis | RealRedisAdapter:
    """获取全局 Redis 适配器单例。"""
    global _redis_singleton
    if _redis_singleton is None:
        settings = get_settings()
        if settings.redis_mode == "redis":
            _redis_singleton = RealRedisAdapter(settings.redis_url, password=settings.redis_password or None)
        else:
            _redis_singleton = InMemoryRedis()
    return _redis_singleton


def reset_redis_for_tests() -> None:
    """仅供测试重置内存 Redis 状态。"""
    global _redis_singleton
    _redis_singleton = InMemoryRedis()
