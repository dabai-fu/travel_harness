"""
中间件 7：Langfuse 全链路可观测性与租户月度 Token 配额管控（LangfuseObservabilityMiddleware）。

对应文档章节：
  - 六、中间件 7（LangfuseObservabilityMiddleware）
  - 八、8.2 Redis Key 规范（pytravel:{tenant_id}:quota:{yyyyMM}）
  - 十一、v1.1 P1 #22（上报前复用脱敏器 + 配额真正可执行）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.api_gateway.security.masker import SensitiveDataMasker
from apps.common.config import get_settings
from apps.common.db import get_repository
from apps.common.redis_client import get_redis

QUOTA_TTL_SECONDS = 40 * 24 * 3600  # 40 天自然过期


class TenantQuotaExceededError(PermissionError):
    """租户本月 Token 用量已超过 sys_tenant.monthly_token_quota 配额限制。"""


class LangfuseObservabilityMiddleware:
    """
    负责全链路 Trace 采集（上报前强制 PII 脱敏）与租户月度 Token 配额硬拦截。
    支持 `LANGFUSE_ENABLED=true` 时通过官方 Langfuse SDK 实时上报到自部署或云端 Langfuse。
    """

    def __init__(self) -> None:
        self._redis = get_redis()
        self._settings = get_settings()
        self.local_traces: list[dict[str, Any]] = []
        self._lf_client: Any = None

    def _get_langfuse_client(self) -> Any:
        if not self._settings.langfuse_enabled:
            return None
        if self._lf_client is None:
            from langfuse import Langfuse

            self._lf_client = Langfuse(
                public_key=self._settings.langfuse_public_key,
                secret_key=self._settings.langfuse_secret_key,
                host=self._settings.langfuse_host,
                mask=SensitiveDataMasker.langfuse_mask_callback,
            )
        return self._lf_client

    @staticmethod
    def _quota_key(tenant_id: str) -> str:
        ym = datetime.now(UTC).strftime("%Y%m")
        return f"pytravel:{tenant_id}:quota:{ym}"

    async def check_tenant_quota(self, tenant_id: str) -> tuple[int, int]:
        """校验租户月度 Token 配额是否超限（v1.1 P1 #22）。"""
        repo = get_repository()
        tenant = await repo.get_tenant(tenant_id)
        limit = int(tenant["monthly_token_quota"]) if tenant else 10_000_000
        used_str = await self._redis.get(self._quota_key(tenant_id))
        used = int(used_str) if used_str else 0
        if used >= limit:
            raise TenantQuotaExceededError(
                f"租户 {tenant_id} 本月 Token 用量 ({used}) 已达上限 ({limit})，请联系管理员扩容"
            )
        return used, limit

    async def record_usage_and_trace(
        self,
        tenant_id: str,
        user_id: str,
        trace_id: str,
        span_name: str,
        tokens_used: int,
        latency_ms: float,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        1. 将本次消耗原子累加到 Redis 月度配额计数器；
        2. 复用 SensitiveDataMasker.langfuse_mask_callback 对 Trace 载荷全量脱敏后再上报。
        """
        if tokens_used > 0:
            await self._redis.incrby(self._quota_key(tenant_id), tokens_used, ex=QUOTA_TTL_SECONDS)

        masked_payload = SensitiveDataMasker.langfuse_mask_callback(payload)
        trace_record = {
            "trace_id": trace_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "span_name": span_name,
            "tokens_used": tokens_used,
            "latency_ms": round(latency_ms, 2),
            "payload": masked_payload,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.local_traces.append(trace_record)
        if len(self.local_traces) > 500:
            self.local_traces = self.local_traces[-500:]

        # 真实 Langfuse SDK 上报（非阻塞，异常不干扰主流程）
        lf = self._get_langfuse_client()
        if lf is not None:
            try:
                obs = lf.start_observation(
                    name=span_name,
                    as_type="span",
                    input={"query": masked_payload.get("query")},
                    output={"answer": masked_payload.get("answer")},
                    metadata={
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "trace_id": trace_id,
                        "tokens_used": tokens_used,
                        "latency_ms": round(latency_ms, 2),
                    },
                )
                obs.end()
                lf.flush()
            except Exception:
                pass
        return trace_record
