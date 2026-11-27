"""
OA 异步审批回调 Webhook 接口（/api/webhook/oa-approval）。

对应文档章节：
  - 二、2.2 断点恢复边界（Webhook 无用户 JWT，按会话归属重建 TenantContext）
  - 七、7.2 Webhook 安全规范
  - 八、8.2 Redis Key 规范（pytravel:{tenant_id}:webhook:{event_id} 去重）
  - 十一、v1.1 P0 #8（HMAC-SHA256 验签 + ±5 分钟时间窗 + event_id 去重）& P1 #21（乐观锁）
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from apps.agent_harness.runtime.run_manager import get_run_manager
from apps.api_gateway.auth import build_tenant_context_for_user
from apps.common.config import get_settings
from apps.common.crypto import verify_hmac_sha256
from apps.common.db import OptimisticLockError, get_repository
from apps.common.redis_client import get_redis

router = APIRouter(prefix="/api/webhook", tags=["Webhook"])


class OaApprovalWebhookPayload(BaseModel):
    event_id: str
    tenant_id: str
    conversation_id: str
    travel_order_id: int
    action: str  # APPROVED / REJECTED
    approver_id: str = "oa_manager"
    comment: str = ""
    expected_version: int | None = None


@router.post("/oa-approval")
async def oa_approval_webhook(
    request: Request,
    x_oa_timestamp: str | None = Header(default=None),
    x_oa_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    接收模拟企业 OA 系统的异步审批回调。
    四道安全防线：
      1. 时间戳窗口校验（±300 秒内，防重放）；
      2. HMAC-SHA256 签名校验（防伪造审批通过直接触发途牛扣款）；
      3. Redis `SET NX EX 86400` event_id 去重（OA 重复投递只处理一次）；
      4. 乐观锁更新差旅单状态，并按会话归属重建 TenantContext 唤醒 LangGraph 断点。
    """
    if not x_oa_timestamp or not x_oa_signature:
        raise HTTPException(status_code=401, detail="缺少 OA Webhook 签名或时间戳请求头")

    try:
        ts = int(x_oa_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法时间戳格式") from exc

    settings = get_settings()
    if abs(int(time.time()) - ts) > settings.oa_webhook_max_skew_seconds:
        raise HTTPException(status_code=401, detail="Webhook 时间戳超出 ±5 分钟允许窗口")

    raw_body = await request.body()
    signed_msg = x_oa_timestamp.encode("utf-8") + b"." + raw_body
    if not verify_hmac_sha256(signed_msg, x_oa_signature, settings.oa_webhook_secret):
        raise HTTPException(status_code=401, detail="OA Webhook HMAC-SHA256 签名验证失败")

    payload = OaApprovalWebhookPayload(**json.loads(raw_body.decode("utf-8")))

    # Redis event_id 去重（24h）
    dedup_key = f"pytravel:{payload.tenant_id}:webhook:{payload.event_id}"
    is_first = await get_redis().set(dedup_key, "1", ex=86400, nx=True)
    if not is_first:
        return {"status": "duplicate_ignored", "event_id": payload.event_id}

    repo = get_repository()
    # 按租户隔离作用域检索会话归属，兼容 PostgreSQL FORCE RLS（v1.1 P0 #7）
    conv = await repo.get_conversation_by_tenant(payload.tenant_id, payload.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="关联对话不存在或租户不匹配")

    try:
        updated_order = await repo.update_travel_order_status(
            payload.tenant_id,
            payload.travel_order_id,
            new_status=payload.action.upper(),
            expected_version=payload.expected_version,
        )
    except OptimisticLockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await repo.add_approval_record(
        payload.tenant_id,
        payload.travel_order_id,
        payload.approver_id,
        "APPROVE" if payload.action.upper() == "APPROVED" else "REJECT",
        payload.comment,
    )

    # 重建会话所有者的 TenantContext 并唤醒 wait_oa_webhook 断点
    ctx = await build_tenant_context_for_user(conv["tenant_id"], conv["user_id"])
    res = await get_run_manager().resume_hitl_run(
        ctx=ctx,
        conversation_id=payload.conversation_id,
        resume_payload={"action": payload.action.upper(), "event_id": payload.event_id},
    )
    return {
        "status": "processed",
        "event_id": payload.event_id,
        "order_status": updated_order["status"],
        "graph_result": res,
    }
