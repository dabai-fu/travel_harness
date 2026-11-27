"""
对话流式、HITL 恢复与集群取消路由（/api/chat/*）。

对应文档章节：
  - 四、4.1 / 4.2
  - 九、9.2 SSE 流式协议 & 9.3 routes/chat.py
  - 十一、v1.1 P0 #4（/resume action_hash 校验）& P1 #14（Redis Stream + Last-Event-ID 续传）
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from apps.agent_harness.middlewares.cluster_cancel import ClusterCancelMiddleware
from apps.agent_harness.middlewares.stream_progress import StreamProgressMiddleware
from apps.agent_harness.runtime.guardrails import HitlGuardrailError
from apps.agent_harness.runtime.run_manager import AgentRunManager, get_run_manager
from apps.agent_harness.state import TenantContext
from apps.api_gateway.auth import get_current_tenant_context
from apps.common.db import CrossTenantAccessError, get_repository

router = APIRouter(prefix="/api/chat", tags=["Chat"])


class ChatStreamRequest(BaseModel):
    conversation_id: str | None = None
    query: str = Field(min_length=1, max_length=4000)
    extra_slots: dict[str, Any] = Field(default_factory=dict)


class ChatResumeRequest(BaseModel):
    conversation_id: str
    interrupt_id: str
    action_hash: str
    decision: str = "CONFIRMED"  # CONFIRMED / REJECTED
    selected_option_id: str | None = None


class ChatCancelRequest(BaseModel):
    conversation_id: str


class FeedbackRequest(BaseModel):
    feedback: int  # 1 or -1


@router.post("/stream")
async def chat_stream(
    req: ChatStreamRequest,
    ctx: TenantContext = Depends(get_current_tenant_context),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """
    发起一轮智能体对话并返回 SSE 事件流。
    后台图执行写入 Redis Stream，本端点按 Last-Event-ID 实时订阅推送，实现 Run 与连接彻底解耦。
    """
    conv_id = req.conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
    mgr = get_run_manager()
    try:
        thread_id = await mgr.start_chat_run(
            ctx=ctx,
            conversation_id=conv_id,
            user_query=req.query,
            extra_slots=req.extra_slots,
        )
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return StreamingResponse(
        StreamProgressMiddleware.iter_sse_events(
            tenant_id=ctx.tenant_id,
            thread_id=thread_id,
            last_event_id=last_event_id or "0-0",
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Conversation-Id": conv_id,
            "X-Thread-Id": thread_id,
        },
    )


@router.get("/events/{conversation_id}")
async def reconnect_sse_events(
    conversation_id: str,
    ctx: TenantContext = Depends(get_current_tenant_context),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """断线重连 / 页面刷新续读接口：从 Redis Stream 按 Last-Event-ID 续传未读事件。"""
    thread_id = AgentRunManager.make_thread_id(ctx.tenant_id, ctx.user_id, conversation_id)
    return StreamingResponse(
        StreamProgressMiddleware.iter_sse_events(
            tenant_id=ctx.tenant_id,
            thread_id=thread_id,
            last_event_id=last_event_id or "0-0",
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/resume")
async def chat_resume(
    req: ChatResumeRequest,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> dict[str, Any]:
    """
    恢复 HITL 挂起断点（校验租户归属 + interrupt_id + action_hash，防 TOCTOU 篡改）。
    """
    mgr = get_run_manager()
    try:
        return await mgr.resume_hitl_run(
            ctx=ctx,
            conversation_id=req.conversation_id,
            resume_payload=req.model_dump(),
        )
    except HitlGuardrailError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/cancel")
async def chat_cancel(
    req: ChatCancelRequest,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> dict[str, Any]:
    """跨副本优雅中断当前正在执行的 Run（先写 cancel_flag Key 再发 Pub/Sub 广播）。"""
    thread_id = AgentRunManager.make_thread_id(ctx.tenant_id, ctx.user_id, req.conversation_id)
    hit_local = await ClusterCancelMiddleware().request_cancel(ctx.tenant_id, thread_id)
    return {"status": "cancel_requested", "thread_id": thread_id, "cancelled_locally": hit_local}


@router.get("/conversations")
async def list_conversations(ctx: TenantContext = Depends(get_current_tenant_context)) -> list[dict[str, Any]]:
    return await get_repository().list_conversations(ctx.tenant_id, ctx.user_id)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> list[dict[str, Any]]:
    repo = get_repository()
    await repo.get_or_create_conversation(ctx.tenant_id, ctx.user_id, conversation_id)
    return await repo.list_messages(ctx.tenant_id, conversation_id)


@router.post("/messages/{msg_id}/feedback")
async def submit_feedback(
    msg_id: int,
    req: FeedbackRequest,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> dict[str, Any]:
    repo = get_repository()
    ok = await repo.update_message_feedback(ctx.tenant_id, msg_id, req.feedback)
    if ok:
        return {"status": "ok", "msg_id": msg_id, "feedback": 1 if req.feedback >= 0 else -1}
    raise HTTPException(status_code=404, detail="消息不存在或无权限")
