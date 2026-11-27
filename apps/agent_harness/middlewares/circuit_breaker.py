"""
中间件 3：Redis + Lua 两级三态熔断器（CircuitBreakerMiddleware）。

对应文档章节：
  - 六、中间件 3（CircuitBreakerMiddleware）
  - 八、8.2 Redis Key 规范（pytravel:cb:global:{tool} 与 pytravel:{tenant_id}:cb:{tool}）
  - 十一、v1.1 P1 #15

核心工程设计：
  1. 三态流转：CLOSED -> 连续失败 3 次转 OPEN -> 指数退避冷却（30s / 60s / 120s）转 HALF_OPEN；
  2. 防惊群探活：HALF_OPEN 状态通过 Redis `SET probe_key 1 NX EX 15` 仅放行 1 个探活请求，其余并发请求直接快速失败；
  3. 两级隔离维度：
     - 超时 / 5xx 上游整体故障 -> 计入全局熔断 `pytravel:cb:global:{tool}`（所有租户立刻降级保护）；
     - 401 / 403 / 配额不足等凭证类错误 -> 计入租户级熔断 `pytravel:{tenant_id}:cb:{tool}`（仅隔离问题租户，不波及正常客户）；
  4. 资金安全铁律：**WRITE 类工具超时绝对禁止自动重试**，必须转为查询状态确认或提示人工核对。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from apps.common.redis_client import get_redis

T = TypeVar("T")


class CircuitBreakerOpenError(RuntimeError):
    """当熔断器处于 OPEN 或 HALF_OPEN 探活被占用时抛出的快速失败异常。"""

    def __init__(self, tool_name: str, scope: str, retry_after_seconds: int) -> None:
        self.tool_name = tool_name
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"工具 {tool_name} 处于熔断保护状态（维度={scope}），请 {retry_after_seconds}s 后重试"
        )


class UpstreamToolError(RuntimeError):
    """上游工具调用失败异常，携带 HTTP 状态码/是否超时，供熔断器区分全局故障与租户凭证故障。"""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        is_timeout: bool = False,
    ) -> None:
        self.status_code = status_code
        self.is_timeout = is_timeout
        super().__init__(message)


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    base_cooldown_seconds: int = 30
    max_cooldown_seconds: int = 120
    ttl_seconds: int = 600
    probe_ttl_seconds: int = 15


class CircuitBreakerMiddleware:
    """两级三态原子熔断中间件。"""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self.config = config or CircuitBreakerConfig()
        self._redis = get_redis()

    @staticmethod
    def _global_keys(tool_name: str) -> tuple[str, str]:
        return (
            f"pytravel:cb:global:{tool_name}",
            f"pytravel:cb:global:{tool_name}:probe",
        )

    @staticmethod
    def _tenant_keys(tenant_id: str, tool_name: str) -> tuple[str, str]:
        return (
            f"pytravel:{tenant_id}:cb:{tool_name}",
            f"pytravel:{tenant_id}:cb:{tool_name}:probe",
        )

    async def get_state(self, tenant_id: str, tool_name: str) -> dict[str, Any]:
        """查询当前工具的全局与租户两级熔断状态（供 Eval 与监控观测）。"""
        gk, _ = self._global_keys(tool_name)
        tk, _ = self._tenant_keys(tenant_id, tool_name)
        g_data = await self._redis.hgetall(gk)
        t_data = await self._redis.hgetall(tk)
        return {
            "global": g_data or {"state": "CLOSED", "fail_count": "0"},
            "tenant": t_data or {"state": "CLOSED", "fail_count": "0"},
        }

    async def call(
        self,
        tenant_id: str,
        tool_name: str,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        is_write: bool = False,
        max_read_retries: int = 1,
        **kwargs: Any,
    ) -> T:
        """
        受两级熔断保护的异步工具调用包装器。

        执行流程：
          1. 先检全局熔断键，再检租户熔断键；任一拒绝则直接抛 CircuitBreakerOpenError；
          2. 调用真实异步工具 `fn`；
             - READ 工具遇到瞬时超时可做受限重试（不跨过熔断器）；
             - WRITE 工具遇到任何失败/超时**绝不自动重试**（防止重复下单扣款）；
          3. 成功则闭合对应熔断器；失败则按错误类型路由到全局或租户级失败计数。
        """
        gk, g_probe = self._global_keys(tool_name)
        tk, t_probe = self._tenant_keys(tenant_id, tool_name)

        # 1. 准入检查（全局 -> 租户）
        g_res = await self._redis.cb_check(gk, g_probe, self.config.probe_ttl_seconds)
        if g_res[0] == "DENY":
            retry_after = int(g_res[2]) if len(g_res) > 2 else self.config.base_cooldown_seconds
            raise CircuitBreakerOpenError(tool_name, "global", retry_after)

        t_res = await self._redis.cb_check(tk, t_probe, self.config.probe_ttl_seconds)
        if t_res[0] == "DENY":
            retry_after = int(t_res[2]) if len(t_res) > 2 else self.config.base_cooldown_seconds
            raise CircuitBreakerOpenError(tool_name, f"tenant:{tenant_id}", retry_after)

        # 2. 执行工具调用（WRITE 严格单次执行，绝不盲目重试）
        attempts = 1 if is_write else (1 + max(0, max_read_retries))
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                result = await fn(*args, **kwargs)
                # 仅在此前非全新状态或处于探活时归零，保证 HALF_OPEN 成功即自动闭合
                await self._redis.cb_record_success(gk, g_probe, self.config.ttl_seconds)
                await self._redis.cb_record_success(tk, t_probe, self.config.ttl_seconds)
                return result
            except Exception as exc:
                last_exc = exc
                is_auth_or_quota = isinstance(exc, UpstreamToolError) and exc.status_code in (401, 403, 429)
                if is_write or is_auth_or_quota or attempt == attempts - 1:
                    break

        assert last_exc is not None
        # 3. 按错误维度记账（401/403/429 -> 租户维度；超时/5xx/连接错误 -> 全局维度）
        is_tenant_err = isinstance(last_exc, UpstreamToolError) and last_exc.status_code in (401, 403, 429)
        target_k, target_probe = (tk, t_probe) if is_tenant_err else (gk, g_probe)
        await self._redis.cb_record_failure(
            target_k,
            target_probe,
            threshold=self.config.failure_threshold,
            base_cooldown=self.config.base_cooldown_seconds,
            max_cooldown=self.config.max_cooldown_seconds,
            ttl=self.config.ttl_seconds,
        )
        raise last_exc
