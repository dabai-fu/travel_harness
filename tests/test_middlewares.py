"""
7 大 Harness 中间件核心机制测试（熔断、流式续传、集群取消、PENDING 预写与对账、配额与压缩）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from apps.agent_harness.middlewares.circuit_breaker import (
    CircuitBreakerMiddleware,
    CircuitBreakerOpenError,
    UpstreamToolError,
)
from apps.agent_harness.middlewares.cluster_cancel import ClusterCancelMiddleware
from apps.agent_harness.middlewares.context_engineering import ContextEngineeringMiddleware
from apps.agent_harness.middlewares.credential_persist import CredentialAndPersistenceMiddleware
from apps.agent_harness.middlewares.langfuse_observability import (
    LangfuseObservabilityMiddleware,
    TenantQuotaExceededError,
)
from apps.agent_harness.middlewares.stream_progress import StreamProgressMiddleware
from apps.agent_harness.runtime.scheduled_jobs import reconcile_pending_bookings
from apps.agent_harness.sandbox.docker_runner import get_sandbox_runner
from apps.agent_harness.state import TenantContext
from apps.common.db import reset_repository_for_tests
from apps.common.redis_client import reset_redis_for_tests


async def test_circuit_breaker_two_levels_and_recovery() -> None:
    reset_redis_for_tests()
    cb = CircuitBreakerMiddleware()

    async def fail_503() -> None:
        raise UpstreamToolError("upstream down", status_code=503)

    for _ in range(3):
        with pytest.raises(UpstreamToolError):
            await cb.call("T001", "tuniu_search", fail_503, is_write=True)

    # 第 4 次调用（即使换了租户 T002）因全局熔断 OPEN 被快速拒绝
    with pytest.raises(CircuitBreakerOpenError):
        await cb.call("T002", "tuniu_search", fail_503, is_write=True)


async def test_stream_progress_batching_and_last_event_id_replay() -> None:
    reset_redis_for_tests()
    sp = StreamProgressMiddleware("T001", "T001:u_emp_1:conv_sse")
    id1 = await sp.emit("progress", {"step": "step1", "status": "done"})
    await sp.emit("message", {"delta": "hello"})
    await sp.emit("done", {"thread_id": "T001:u_emp_1:conv_sse"})

    # 从 id1 之后续传，不应重复收到 step1，能收到 message 与 done
    frames: list[str] = []
    async for frame in StreamProgressMiddleware.iter_sse_events("T001", "T001:u_emp_1:conv_sse", last_event_id=id1):
        frames.append(frame)
    joined = "".join(frames)
    assert "step1" not in joined
    assert "event: message" in joined
    assert "event: done" in joined


async def test_context_offload_and_quota_enforcement() -> None:
    reset_redis_for_tests()
    reset_repository_for_tests(auto_seed=True)
    ctx_eng = ContextEngineeringMiddleware(token_budget=500)
    ws = get_sandbox_runner().get_workspace_dir("T001", "conv_offload")
    big_rows = [{"id": i, "city": "北京", "desc": "x" * 100} for i in range(60)]
    res = ctx_eng.offload_if_large(big_rows, ws, prefix="big")
    assert res["offloaded"] is True
    assert res["row_count"] == 60
    assert len(res["sample_5_rows"]) == 5

    obs = LangfuseObservabilityMiddleware()
    await obs.record_usage_and_trace("T002", "u_emp_t2", "tr1", "test", 5_000_001, 10.0, {"phone": "13812345678"})
    assert "138****5678" in obs.local_traces[-1]["payload"]["phone"]
    with pytest.raises(TenantQuotaExceededError):
        await obs.check_tenant_quota("T002")


async def test_pending_booking_reconciliation_and_cluster_cancel() -> None:
    reset_redis_for_tests()
    repo = reset_repository_for_tests(auto_seed=True)
    ctx = TenantContext("T001", "u_emp_1", "D002", ("D002",), "EMPLOYEE", "P6", 6, "tr_rec")
    cred_mw = CredentialAndPersistenceMiddleware()

    # 模拟下单超时：保留 PENDING 状态
    async def timeout_book(cred, ikey):
        raise TimeoutError("tuniu gateway timeout")

    with pytest.raises(TimeoutError):
        await cred_mw.execute_idempotent_booking(
            ctx,
            travel_order_id=999,
            option_hash="opt999",
            segment_no=1,
            booking_meta={"category": "FLIGHT", "city": "北京", "vendor_name": "国航 CA1502", "amount": 960, "depart_date": date(2026, 12, 15)},
            book_fn=timeout_book,
        )

    # 手动将 updated_at 回拨 5 分钟模拟超时积压，并执行对账补偿
    for row in repo.booking_record.values():
        if row["travel_order_id"] == 999:
            row["updated_at"] = datetime.now(UTC) - timedelta(minutes=5)

    stats = await reconcile_pending_bookings(
        lambda tid, ikey: asyncio.sleep(0, result={"status": "CONFIRMED", "external_order_no": "TN-RECONCILED-001"}),
        older_than_seconds=120,
    )
    assert stats["confirmed"] == 1

    # 集群取消标记测试
    cmw = ClusterCancelMiddleware()
    await cmw.request_cancel("T001", "T001:u_emp_1:conv_cancel")
    assert await cmw._redis.get("pytravel:T001:cancel_flag:T001:u_emp_1:conv_cancel") == "1"
