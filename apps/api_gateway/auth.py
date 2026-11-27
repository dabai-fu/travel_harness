"""
API 网关鉴权、Redis Session 与动态组织权限计算（Auth & TenantContext Injector）。

对应文档章节：
  - 二、2.2 四层隔离之身份认证与异步上下文透传层
  - 八、8.2 Redis Key 规范（pytravel:auth:session:{jti} & pytravel:{tenant_id}:perm:{user_id}）
  - 十一、v1.1 P1 #19 & P2 #25

关键设计：
  1. Redis Session Key 使用 JWT 的 `jti` 而非原始 Token，仅存储 `user_id / tenant_id / perm_version`，支持主动踢下线与 7 天滑动续期；
  2. `managed_dept_ids` 与 `role` 不死存在 7 天 Session 中，而是结合 `sys_dept` 物化路径与 10 分钟权限缓存实时计算，
     组织调整或调岗后删除权限缓存即可立即生效，彻底杜绝“调岗后还能越权查原部门预算”。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from apps.agent_harness.state import TenantContext
from apps.common.config import get_settings
from apps.common.crypto import verify_password
from apps.common.db import get_repository
from apps.common.redis_client import get_redis

router = APIRouter(prefix="/api/auth", tags=["Auth"])

SESSION_TTL_SECONDS = 7 * 24 * 3600
PERM_CACHE_TTL_SECONDS = 600


class LoginRequest(BaseModel):
    tenant_id: str = "T001"
    username: str
    password: str = "123456"


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    user_id: str
    role: str
    real_name: str
    dept_id: str
    dept_name: str
    rank_level: str


async def build_tenant_context_for_user(
    tenant_id: str,
    user_id: str,
    trace_id: str | None = None,
) -> TenantContext:
    """
    根据 tenant_id + user_id 构建不可变 TenantContext。
    同时复用于：1) JWT 网关鉴权；2) OA Webhook /resume 按会话归属重建上下文（v1.1 P0 #7）。
    """
    repo = get_repository()
    acc = await repo.get_user_account(tenant_id, user_id)
    prof = await repo.get_user_profile(tenant_id, user_id)
    if not acc or not prof:
        raise HTTPException(status_code=401, detail="用户不存在或已停用")

    redis = get_redis()
    perm_key = f"pytravel:{tenant_id}:perm:{user_id}"
    cached_perm = await redis.get(perm_key)
    perm_data: dict[str, Any] | None = None
    if cached_perm:
        parsed = json.loads(cached_perm)
        if parsed.get("perm_version") == acc["perm_version"]:
            perm_data = parsed

    if perm_data is None:
        managed = await repo.compute_managed_dept_ids(tenant_id, acc["role"], prof["dept_id"])
        perm_data = {
            "role": acc["role"],
            "managed_dept_ids": list(managed),
            "perm_version": acc["perm_version"],
        }
        await redis.set(perm_key, json.dumps(perm_data), ex=PERM_CACHE_TTL_SECONDS)

    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        dept_id=prof["dept_id"],
        managed_dept_ids=tuple(perm_data["managed_dept_ids"]),
        role=perm_data["role"],
        rank_level=prof["rank_level"],
        rank_no=int(prof["rank_no"]),
        trace_id=trace_id or f"tr_{uuid.uuid4().hex[:12]}",
    )


async def issue_jwt_and_session(tenant_id: str, user_id: str) -> str:
    """签发带 jti 的 JWT 并写入 Redis Session（类 Sa-Token 机制）。"""
    settings = get_settings()
    repo = get_repository()
    acc = await repo.get_user_account(tenant_id, user_id)
    if not acc:
        raise ValueError("账号不存在")

    jti = uuid.uuid4().hex
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode(
        {
            "sub": user_id,
            "tid": tenant_id,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    session_key = f"pytravel:auth:session:{jti}"
    await get_redis().set(
        session_key,
        json.dumps({"user_id": user_id, "tenant_id": tenant_id, "perm_version": acc["perm_version"]}),
        ex=SESSION_TTL_SECONDS,
    )
    return token


async def get_current_tenant_context(
    authorization: str | None = Header(default=None),
    x_trace_id: str | None = Header(default=None),
) -> TenantContext:
    """FastAPI 依赖项：校验 Bearer JWT + Redis Session 滑动续期，输出不可变 TenantContext。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 认证令牌")
    raw_token = authorization.split(" ", maxsplit=1)[1].strip()
    settings = get_settings()

    try:
        claims = jwt.decode(raw_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"JWT 无效或已过期: {exc}") from exc

    jti = claims.get("jti")
    tenant_id = claims.get("tid")
    user_id = claims.get("sub")
    if not jti or not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="JWT 载荷缺少必要声明")

    redis = get_redis()
    session_key = f"pytravel:auth:session:{jti}"
    sess_raw = await redis.get(session_key)
    if not sess_raw:
        raise HTTPException(status_code=401, detail="会话已失效或已被管理员踢下线")

    # 滑动续期 7 天
    await redis.expire(session_key, SESSION_TTL_SECONDS)
    return await build_tenant_context_for_user(tenant_id, user_id, trace_id=x_trace_id)


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest) -> LoginResponse:
    repo = get_repository()
    acc = await repo.get_user_by_username(req.tenant_id, req.username)
    if not acc or not verify_password(req.password, acc["password_hash"]):
        raise HTTPException(status_code=401, detail="租户、用户名或密码错误")

    prof = await repo.get_user_profile(req.tenant_id, acc["id"])
    assert prof is not None
    token = await issue_jwt_and_session(req.tenant_id, acc["id"])
    return LoginResponse(
        access_token=token,
        tenant_id=req.tenant_id,
        user_id=acc["id"],
        role=acc["role"],
        real_name=prof["real_name"],
        dept_id=prof["dept_id"],
        dept_name=prof["dept_name"],
        rank_level=prof["rank_level"],
    )


@router.get("/me")
async def get_me(ctx: TenantContext = Depends(get_current_tenant_context)) -> dict[str, Any]:
    prof = await get_repository().get_user_profile(ctx.tenant_id, ctx.user_id)
    return {
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "role": ctx.role,
        "dept_id": ctx.dept_id,
        "dept_name": prof["dept_name"] if prof else ctx.dept_id,
        "managed_dept_ids": list(ctx.managed_dept_ids),
        "rank_level": ctx.rank_level,
        "real_name": prof["real_name"] if prof else ctx.user_id,
    }
