"""
图执行与 SSE 连接彻底解耦的后台 Run 调度器（RunManager）。

对应文档章节：
  - 六、中间件 2（StreamProgressMiddleware：Run 在后台任务执行，写入 Redis Stream）
  - 九、9.3 runtime/run_manager.py
  - 十一、v1.1 P1 #14（断网/刷新不丢事件；OA Webhook 异步唤醒同样能推入 Stream）

生产级增强：
  将挂起断点的 `thread_meta`（active_subgraph / sub_thread_id / pending_hitl）
  持久化写入 Redis（Key: `pytravel:{tenant_id}:thread_meta:{thread_id}`，TTL 24h），
  配合 PostgreSQL `AsyncPostgresSaver`，保证服务重启或多副本负载均衡时 `/resume` 与 Webhook 100% 可恢复。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from apps.agent_harness.graphs.master import get_master_coordinator
from apps.agent_harness.middlewares.cluster_cancel import ClusterCancelMiddleware, RunCancelledError
from apps.agent_harness.middlewares.langfuse_observability import (
    LangfuseObservabilityMiddleware,
    TenantQuotaExceededError,
)
from apps.agent_harness.middlewares.stream_progress import StreamProgressMiddleware
from apps.agent_harness.middlewares.tenant_context import bind_tenant_context
from apps.agent_harness.runtime.services import (
    ConversationTitleService,
    QuestionRecommendationService,
)
from apps.agent_harness.state import TenantContext
from apps.common.db import get_repository
from apps.common.redis_client import get_redis

THREAD_META_TTL_SECONDS = 86400


class AgentRunManager:
    """
    管理会话状态（thread_id = `{tenant_id}:{user_id}:{conversation_id}`）、
    后台异步图执行、HITL 断点挂起与恢复、以及 SSE 标准事件流推送。
    """

    def __init__(self) -> None:
        self.coordinator = get_master_coordinator()
        self.cancel_mw = ClusterCancelMiddleware()
        self.obs_mw = LangfuseObservabilityMiddleware()

    @staticmethod
    def make_thread_id(tenant_id: str, user_id: str, conversation_id: str) -> str:
        """强制命名空间前缀，防止跨租户会话劫持（2.2 节）。"""
        return f"{tenant_id}:{user_id}:{conversation_id}"

    @staticmethod
    def _meta_key(tenant_id: str, thread_id: str) -> str:
        return f"pytravel:{tenant_id}:thread_meta:{thread_id}"

    async def save_thread_meta(self, tenant_id: str, thread_id: str, meta: dict[str, Any]) -> None:
        await get_redis().set(
            self._meta_key(tenant_id, thread_id),
            json.dumps(meta, ensure_ascii=False, default=str),
            ex=THREAD_META_TTL_SECONDS,
        )

    async def get_thread_meta(self, tenant_id: str, thread_id: str) -> dict[str, Any]:
        raw = await get_redis().get(self._meta_key(tenant_id, thread_id))
        return dict(json.loads(raw)) if raw else {}

    async def start_chat_run(
        self,
        ctx: TenantContext,
        conversation_id: str,
        user_query: str,
        extra_slots: dict[str, Any] | None = None,
    ) -> str:
        """
        启动一次异步后台对话 Run（立刻返回 thread_id，前端通过 SSE 订阅 Redis Stream）。
        """
        repo = get_repository()
        await repo.get_or_create_conversation(ctx.tenant_id, ctx.user_id, conversation_id)
        await repo.append_message(ctx.tenant_id, conversation_id, "user", user_query)

        thread_id = self.make_thread_id(ctx.tenant_id, ctx.user_id, conversation_id)
        await self.cancel_mw.clear_cancel_flag(ctx.tenant_id, thread_id)

        task = asyncio.create_task(
            self._execute_run(ctx, conversation_id, thread_id, user_query, extra_slots or {}),
            name=f"run-{conversation_id}",
        )
        self.cancel_mw.register_task(ctx.tenant_id, thread_id, task)
        return thread_id

    async def _execute_run(
        self,
        ctx: TenantContext,
        conversation_id: str,
        thread_id: str,
        user_query: str,
        extra_slots: dict[str, Any],
    ) -> None:
        t0 = time.perf_counter()
        stream = StreamProgressMiddleware(ctx.tenant_id, thread_id)
        repo = get_repository()

        with bind_tenant_context(ctx):
            try:
                await self.obs_mw.check_tenant_quota(ctx.tenant_id)
                await self.cancel_mw.check_cancelled(ctx.tenant_id, thread_id)

                await ConversationTitleService.generate_and_save(ctx.tenant_id, conversation_id, user_query)

                meta = await self.get_thread_meta(ctx.tenant_id, thread_id)
                msgs = await repo.list_messages(ctx.tenant_id, conversation_id)
                history_msgs = [m["content"] for m in msgs[-4:-1]]
                intent_res = await self.coordinator.route_query(
                    query=user_query,
                    active_subgraph=meta.get("active_subgraph"),
                    has_pending_hitl=bool(meta.get("pending_hitl")),
                    history_summaries=history_msgs,
                )
                targets = self.coordinator.resolve_target_subgraphs(intent_res)

                if not targets:
                    answer = (
                        "您好！我是 PyTravel 企业差旅智能协同助手。我可以帮您：\n"
                        "1. 规划国内外差旅行程并给出 **4 套代表方案（最省钱/最省时/最合规/最舒适）**；\n"
                        "2. 精准查询您职级对应的差旅标准与目的地天气；\n"
                        "3. 识别差旅发票并智能草拟 ERP 报销单；\n"
                        "4. 为部门主管/财务生成带图表的预算执行与超支归因报告。"
                    )
                    await stream.emit("message", {"delta": answer})
                    await stream.emit("suggestions", {"questions": QuestionRecommendationService.recommend(None, ctx.role)})
                    await stream.emit("done", {"thread_id": thread_id, "usage": {"tokens": 60, "cost_ms": int((time.perf_counter() - t0) * 1000)}})
                    await repo.append_message(ctx.tenant_id, conversation_id, "assistant", answer)
                    return

                plan_steps = [{"id": i + 1, "title": f"执行 {t}", "status": "pending"} for i, t in enumerate(targets)]
                await stream.emit("plan_update", {"tasks": plan_steps})

                combined_answers: list[str] = []
                total_tokens = 0
                last_out_state: dict[str, Any] = {}
                for idx, sub_name in enumerate(targets):
                    await self.cancel_mw.check_cancelled(ctx.tenant_id, thread_id)
                    plan_steps[idx]["status"] = "running"
                    await stream.emit("agent_switch", {"from": "router", "to": sub_name, "route_source": intent_res.source})
                    await stream.emit("plan_update", {"tasks": plan_steps})
                    await stream.emit("progress", {"step": f"正在运行 {sub_name}...", "status": "running"})

                    sub_thread_id = f"{thread_id}:{sub_name}"
                    config = {"configurable": {"thread_id": sub_thread_id}, "recursion_limit": 25}
                    subgraph = self.coordinator.subgraphs[sub_name]

                    init_state = {
                        "conversation_id": conversation_id,
                        "user_query": user_query,
                        "effective_query": intent_res.rewritten_query or user_query,
                        "messages": [HumanMessage(content=user_query)],
                        "intent_result": intent_res.model_dump(),
                        "trip_slots": dict(extra_slots),
                    }
                    out_state = await subgraph.ainvoke(init_state, config=config)
                    last_out_state = out_state
                    snap = await subgraph.aget_state(config)
                    total_tokens += int(out_state.get("total_tokens") or 0)

                    if out_state.get("travel_options"):
                        await stream.emit("travel_cards", {"options": out_state["travel_options"]})
                    if out_state.get("chart_payloads"):
                        await stream.emit("chart_data", {"charts": out_state["chart_payloads"]})
                    if out_state.get("error_info"):
                        await stream.emit("error", out_state["error_info"])

                    if snap.next:
                        pending_hitl: dict[str, Any] = {}
                        if snap.tasks:
                            for t in snap.tasks:
                                if getattr(t, "interrupts", None):
                                    pending_hitl = dict(t.interrupts[0].value)
                                    break
                        if not pending_hitl:
                            pending_hitl = dict(out_state.get("pending_hitl") or {})

                        await self.save_thread_meta(
                            ctx.tenant_id,
                            thread_id,
                            {
                                "tenant_id": ctx.tenant_id,
                                "user_id": ctx.user_id,
                                "conversation_id": conversation_id,
                                "active_subgraph": sub_name,
                                "sub_thread_id": sub_thread_id,
                                "pending_hitl": pending_hitl,
                            },
                        )
                        if pending_hitl:
                            await stream.emit("hitl_form", pending_hitl)
                        await stream.emit("progress", {"step": f"{sub_name} 等待人工确认", "status": "done"})
                        await stream.emit("interrupted", {"thread_id": thread_id, "interrupt_id": pending_hitl.get("interrupt_id", "")})
                        return

                    plan_steps[idx]["status"] = "done"
                    await stream.emit("progress", {"step": f"{sub_name} 执行完成", "status": "done"})
                    await stream.emit("plan_update", {"tasks": plan_steps})
                    if out_state.get("final_answer"):
                        combined_answers.append(out_state["final_answer"])

                final_md = "\n\n---\n\n".join(combined_answers)
                for chunk_start in range(0, len(final_md), 24):
                    await stream.emit_delta("message", final_md[chunk_start : chunk_start + 24])
                await stream.flush_all()

                suggestions = QuestionRecommendationService.recommend(targets[-1], ctx.role)
                await stream.emit("suggestions", {"questions": suggestions})
                cost_ms = int((time.perf_counter() - t0) * 1000)
                await stream.emit("done", {"thread_id": thread_id, "usage": {"tokens": total_tokens, "cost_ms": cost_ms}})
                await repo.append_message(
                    ctx.tenant_id,
                    conversation_id,
                    "assistant",
                    final_md,
                    extra_json={
                        "travel_options": last_out_state.get("travel_options"),
                        "chart_payloads": last_out_state.get("chart_payloads"),
                    },
                )
                await self.obs_mw.record_usage_and_trace(
                    ctx.tenant_id, ctx.user_id, ctx.trace_id, targets[-1], total_tokens, cost_ms, {"query": user_query, "answer": final_md}
                )
            except TenantQuotaExceededError as exc:
                await stream.emit("error", {"code": "QUOTA_EXCEEDED", "message": str(exc), "retryable": False})
            except (RunCancelledError, asyncio.CancelledError):
                await stream.emit("error", {"code": "RUN_CANCELLED", "message": "任务已应您的请求停止。", "retryable": True})
            except Exception as exc:
                await stream.emit("error", {"code": "INTERNAL_ERROR", "message": f"执行异常: {exc}", "retryable": True})
            finally:
                self.cancel_mw.unregister_task(ctx.tenant_id, thread_id)

    async def resume_hitl_run(
        self,
        ctx: TenantContext,
        conversation_id: str,
        resume_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """恢复挂起的 HITL 断点（从 Redis thread_meta 与 PG Checkpointer 恢复，支持重启不丢状态）。"""
        thread_id = self.make_thread_id(ctx.tenant_id, ctx.user_id, conversation_id)
        meta = await self.get_thread_meta(ctx.tenant_id, thread_id)
        if not meta or not meta.get("active_subgraph"):
            raise ValueError(f"会话 {conversation_id} 当前没有待恢复的挂起断点")

        if meta["tenant_id"] != ctx.tenant_id or meta["user_id"] != ctx.user_id:
            raise PermissionError("跨租户或跨用户恢复会话断点被拒绝")

        sub_name = meta["active_subgraph"]
        sub_thread_id = meta["sub_thread_id"]
        subgraph = self.coordinator.subgraphs[sub_name]
        config = {"configurable": {"thread_id": sub_thread_id}, "recursion_limit": 25}
        stream = StreamProgressMiddleware(ctx.tenant_id, thread_id)

        with bind_tenant_context(ctx):
            out_state = await subgraph.ainvoke(Command(resume=resume_payload), config=config)
            snap = await subgraph.aget_state(config)
            if snap.next:
                pending_hitl: dict[str, Any] = {}
                if snap.tasks:
                    for t in snap.tasks:
                        if getattr(t, "interrupts", None):
                            pending_hitl = dict(t.interrupts[0].value)
                            break
                if not pending_hitl:
                    pending_hitl = dict(out_state.get("pending_hitl") or {})
                meta["pending_hitl"] = pending_hitl
                await self.save_thread_meta(ctx.tenant_id, thread_id, meta)
                await stream.emit("hitl_form", pending_hitl)
                await stream.emit("interrupted", {"thread_id": thread_id, "interrupt_id": pending_hitl.get("interrupt_id", "")})
                return {"status": "interrupted", "pending_hitl": pending_hitl}

            meta["pending_hitl"] = None
            await self.save_thread_meta(ctx.tenant_id, thread_id, meta)
            final_md = out_state.get("final_answer") or "操作已确认并执行完成。"
            await stream.emit("message", {"delta": final_md})
            await stream.emit("done", {"thread_id": thread_id, "usage": {"tokens": out_state.get("total_tokens", 0), "cost_ms": 120}})
            await get_repository().append_message(ctx.tenant_id, conversation_id, "assistant", final_md)
            return {"status": "completed", "final_answer": final_md, "state": out_state}


_run_manager_singleton: AgentRunManager | None = None


def get_run_manager() -> AgentRunManager:
    global _run_manager_singleton
    if _run_manager_singleton is None:
        _run_manager_singleton = AgentRunManager()
    return _run_manager_singleton
