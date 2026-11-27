"""
端到端子图与 FastAPI 网关集成测试（预算并行沙箱+勾稽、行程HITL+涨价二次确认、报销草稿、OA Webhook）。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from apps.agent_harness.graphs.budget_insight import build_budget_insight_subgraph
from apps.agent_harness.graphs.expense_draft import build_expense_draft_subgraph
from apps.agent_harness.graphs.trip_plan_book import build_trip_plan_book_subgraph
from apps.agent_harness.middlewares.tenant_context import bind_tenant_context
from apps.agent_harness.state import TenantContext
from apps.api_gateway.main import app
from apps.common.db import reset_repository_for_tests
from apps.common.redis_client import reset_redis_for_tests
from apps.mcp_servers.tuniu_mcp import configure_tuniu_mock
from apps.mock_enterprise_systems.server import build_signed_oa_webhook_headers


async def test_budget_insight_subgraph_send_parallel_and_verification() -> None:
    reset_redis_for_tests()
    reset_repository_for_tests(auto_seed=True)
    ctx = TenantContext("T001", "u_fin_t1", "D004", ("D001", "D002", "D003", "D004"), "FINANCE_ADMIN", "P8", 8, "tr_b")
    graph = build_budget_insight_subgraph()

    with bind_tenant_context(ctx):
        out = await graph.ainvoke(
            {
                "conversation_id": "conv_bud_1",
                "user_query": "分析2026年第四季度全公司差旅预算与超支归因",
                "effective_query": "分析2026年第四季度全公司差旅预算与超支归因",
                "budget_fiscal_year": 2026,
                "budget_fiscal_quarter": 4,
                "messages": [],
            }
        )

    assert len(out["chart_payloads"]) == 3
    assert "volume_price" in out["budget_metrics"]
    assert "behavior_premium" in out["budget_metrics"]
    assert "cost_saving" in out["budget_metrics"]
    assert "{{" not in out["final_answer"]
    assert "华东业务交付部" in out["final_answer"]


async def test_trip_plan_hitl_and_price_recheck_interrupt() -> None:
    reset_redis_for_tests()
    reset_repository_for_tests(auto_seed=True)
    configure_tuniu_mock(fail_remaining=0, price_bump_yuan=0.0, sold_out=False)

    ctx = TenantContext("T001", "u_emp_1", "D002", ("D002",), "EMPLOYEE", "P6", 6, "tr_trip")
    graph = build_trip_plan_book_subgraph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "trip_recheck_test"}}

    with bind_tenant_context(ctx):
        out1 = await graph.ainvoke(
            {"conversation_id": "conv_trip_1", "user_query": "下周二从上海去北京出差3天，住国贸附近", "effective_query": "下周二从上海去北京出差3天，住国贸附近", "messages": []},
            config=cfg,
        )
        assert len(out1["travel_options"]) == 4
        hitl1 = out1["pending_hitl"]

        # 模拟审批期间途牛机酒突然大幅涨价 (+120元/段) -> 下单前复核必须再次触发 interrupt()
        configure_tuniu_mock(price_bump_yuan=120.0)
        out2 = await graph.ainvoke(
            Command(resume={"interrupt_id": hitl1["interrupt_id"], "action_hash": hitl1["action_hash"], "decision": "CONFIRMED"}),
            config=cfg,
        )
        assert out2.get("price_recheck_needed") is True
        hitl2 = out2["pending_hitl"]
        assert hitl2["interrupt_id"] != hitl1["interrupt_id"]

        # 二次确认新报价后，完成 3 段途牛幂等下单
        out3 = await graph.ainvoke(
            Command(resume={"interrupt_id": hitl2["interrupt_id"], "action_hash": hitl2["action_hash"], "decision": "CONFIRMED"}),
            config=cfg,
        )
        assert "途牛预订全部完成" in out3["final_answer"]
    configure_tuniu_mock(price_bump_yuan=0.0)


async def test_expense_draft_subgraph_hitl_flow() -> None:
    reset_redis_for_tests()
    reset_repository_for_tests(auto_seed=True)
    ctx = TenantContext("T001", "u_emp_1", "D002", ("D002",), "EMPLOYEE", "P6", 6, "tr_exp")
    graph = build_expense_draft_subgraph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "exp_test_1"}}

    with bind_tenant_context(ctx):
        out1 = await graph.ainvoke(
            {"conversation_id": "conv_exp_1", "user_query": "帮我把发票整理成报销单草稿", "messages": []},
            config=cfg,
        )
        hitl = out1["pending_hitl"]
        out2 = await graph.ainvoke(
            Command(resume={"interrupt_id": hitl["interrupt_id"], "action_hash": hitl["action_hash"], "decision": "CONFIRMED"}),
            config=cfg,
        )
        assert out2["expense_draft_result"]["status"] == "PUSHED_TO_ERP"


def test_fastapi_auth_chat_and_signed_webhook() -> None:
    reset_redis_for_tests()
    repo = reset_repository_for_tests(auto_seed=True)

    with TestClient(app) as client:
        # 1. 登录获取 JWT
        resp = client.post("/api/auth/login", json={"tenant_id": "T001", "username": "alice", "password": "123456"})
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. 查个人上下文
        me = client.get("/api/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["rank_level"] == "P6"

        # 3. 无效签名 Webhook 必须返回 401
        bad_wh = client.post("/api/webhook/oa-approval", json={"event_id": "e1"})
        assert bad_wh.status_code == 401

        # 4. 发起一轮需要异步 OA 审批的行程规划并验证合法 HMAC Webhook 唤醒
        stream_resp = client.post(
            "/api/chat/stream",
            headers=headers,
            json={
                "conversation_id": "conv_wh_demo",
                "query": "下周二从上海去北京出差3天，住国贸附近",
                "extra_slots": {"auto_approve_oa": False},
            },
        )
        assert stream_resp.status_code == 200
        # 提取 hitl_form 事件载荷中的 interrupt_id 与 action_hash
        hitl_data = None
        for line in stream_resp.text.splitlines():
            if line.startswith("data: ") and "action_hash" in line:
                hitl_data = json.loads(line[6:])
                break
        assert hitl_data is not None

        res1 = client.post(
            "/api/chat/resume",
            headers=headers,
            json={
                "conversation_id": "conv_wh_demo",
                "interrupt_id": hitl_data["interrupt_id"],
                "action_hash": hitl_data["action_hash"],
                "decision": "CONFIRMED",
            },
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "interrupted"
        order_id = res1.json()["pending_hitl"]["travel_order_id"]

        wh_body = json.dumps(
            {
                "event_id": "evt_oa_001",
                "tenant_id": "T001",
                "conversation_id": "conv_wh_demo",
                "travel_order_id": order_id,
                "action": "APPROVED",
                "approver_id": "u_mgr_d2",
            }
        ).encode("utf-8")
        wh_headers = {"Content-Type": "application/json", **build_signed_oa_webhook_headers(wh_body)}
        wh_resp = client.post("/api/webhook/oa-approval", content=wh_body, headers=wh_headers)
        assert wh_resp.status_code == 200
        assert wh_resp.json()["order_status"] == "APPROVED"
        assert wh_resp.json()["graph_result"]["status"] == "completed"

        # 5. 重复投递同一 event_id 必须被幂等忽略
        wh_dup = client.post("/api/webhook/oa-approval", content=wh_body, headers=wh_headers)
        assert wh_dup.status_code == 200
        assert wh_dup.json()["status"] == "duplicate_ignored"
        assert len(repo.list_bookings_sync("T001", travel_order_id=order_id)) == 3
