"""
中间件 1：多租户上下文透传与内部服务令牌签发/验签（TenantAuthContextMiddleware）。

对应文档章节：
  - 二、2.2 四层隔离之身份认证与异步上下文透传层
  - 六、中间件 1（TenantAuthContextMiddleware）
  - 十一、v1.1 P0 #7（明确 contextvars 管不到的 3 个边界：跨进程 MCP / 断点恢复 / 线程池）

核心设计要点：
  1. 利用 Python 原生 `contextvars.ContextVar` 在 FastAPI 协程与 LangGraph `Send` 并行分支间无损透传；
  2. 提供 `run_in_thread_with_ctx` 显式复制 ContextVar（或使用 `asyncio.to_thread`），解决线程池断链；
  3. 调用独立进程 FastMCP Server 时，签发 60 秒短时 HMAC-SHA256 内部令牌 `X-PyTravel-Ctx`，
     MCP 端验签重建 `TenantContext` 并自行鉴权，绝不信任 LLM 传入的 `tenant_id/dept_id`。
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from apps.agent_harness.state import TenantContext
from apps.api_gateway.security.masker import SensitiveDataMasker
from apps.common.config import get_settings
from apps.common.crypto import sign_hmac_sha256, verify_hmac_sha256

# 协程级不可变上下文变量（对标 Java TransmittableThreadLocal）
_TENANT_CTX_VAR: contextvars.ContextVar[TenantContext | None] = contextvars.ContextVar(
    "pytravel_tenant_ctx",
    default=None,
)


class InternalTokenError(PermissionError):
    """MCP 内部服务令牌缺失、过期或签名无效异常。"""


def get_current_tenant_context() -> TenantContext | None:
    """获取当前协程绑定的 TenantContext（未绑定返回 None）。"""
    return _TENANT_CTX_VAR.get()


def require_tenant_context() -> TenantContext:
    """强制获取当前 TenantContext，未注入时立即抛异常，防止裸奔查询。"""
    ctx = _TENANT_CTX_VAR.get()
    if ctx is None:
        raise PermissionError("当前执行上下文未绑定 TenantContext，禁止访问多租户资源")
    return ctx


@contextmanager
def bind_tenant_context(ctx: TenantContext) -> Iterator[TenantContext]:
    """上下文管理器：安全绑定并在退出时 reset，避免协程复用导致租户串台。"""
    token = _TENANT_CTX_VAR.set(ctx)
    try:
        yield ctx
    finally:
        _TENANT_CTX_VAR.reset(token)


async def run_in_thread_with_ctx[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    在线程池执行同步函数时显式传递 ContextVar（对应 v1.1 2.2 节边界 3）。
    注：Python 3.12 的 `asyncio.to_thread` 内部已调用 `contextvars.copy_context().run`，
    这里封装为统一辅助函数，显式保证所有同步沙箱/文件 IO 都带着 TenantContext。
    """
    ctx_snapshot = contextvars.copy_context()
    return await asyncio.to_thread(ctx_snapshot.run, fn, *args, **kwargs)


class TenantAuthContextMiddleware:
    """
    中间件 1：负责上下文绑定、内部 MCP 服务令牌签发/验签与敏感数据全链路脱敏。
    """

    HEADER_NAME = "X-PyTravel-Ctx"

    @staticmethod
    def issue_internal_token(ctx: TenantContext, ttl_seconds: int | None = None) -> str:
        """
        签发有效期 60s 的内部服务令牌，透传给独立进程 FastMCP Server（v1.1 P0 #7）。
        令牌格式：<base64url(payload_json)>.<hmac_sha256_hex>
        """
        settings = get_settings()
        ttl = ttl_seconds or settings.internal_token_ttl_seconds
        payload = {
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "dept_id": ctx.dept_id,
            "managed_dept_ids": list(ctx.managed_dept_ids),
            "role": ctx.role,
            "rank_level": ctx.rank_level,
            "rank_no": ctx.rank_no,
            "trace_id": ctx.trace_id,
            "exp": int(time.time()) + ttl,
        }
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        sig = sign_hmac_sha256(payload_b64, settings.internal_token_secret)
        return f"{payload_b64}.{sig}"

    @staticmethod
    def verify_internal_token(token: str | None) -> TenantContext:
        """
        MCP Server 端验签并重建不可变 TenantContext。
        若伪造、篡改、缺失或超时（>60s），立即抛出 InternalTokenError 拒绝执行。
        """
        if not token or "." not in token:
            raise InternalTokenError("缺少或格式非法的 X-PyTravel-Ctx 内部服务令牌")
        payload_b64, sig = token.rsplit(".", maxsplit=1)
        settings = get_settings()
        if not verify_hmac_sha256(payload_b64, sig, settings.internal_token_secret):
            raise InternalTokenError("X-PyTravel-Ctx 内部令牌签名校验失败")

        try:
            raw = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
            data = json.loads(raw)
        except Exception as exc:
            raise InternalTokenError(f"X-PyTravel-Ctx 载荷解析失败: {exc}") from exc

        if int(data.get("exp", 0)) < int(time.time()):
            raise InternalTokenError("X-PyTravel-Ctx 内部令牌已过期")

        return TenantContext(
            tenant_id=str(data["tenant_id"]),
            user_id=str(data["user_id"]),
            dept_id=str(data["dept_id"]),
            managed_dept_ids=tuple(str(x) for x in data.get("managed_dept_ids", [])),
            role=data["role"],
            rank_level=str(data["rank_level"]),
            rank_no=int(data["rank_no"]),
            trace_id=str(data["trace_id"]),
        )

    @staticmethod
    def mask_output(data: Any) -> Any:
        """对日志、响应或工具出参执行全链路 PII/密钥脱敏。"""
        return SensitiveDataMasker.mask_value(data)
