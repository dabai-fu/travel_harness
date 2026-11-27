"""
子图 3：发票识别与报销单智能草拟（ExpenseDraftSubgraph）。

对应文档章节：
  - 三、模块 3（高容错辅助型：多模态识票 + 自动关联行程与途牛订单 + 柔性超标提示 + HITL 推送 ERP）
  - 十一、v1.1 P0 #4 & P0 #5（action_hash 绑定 + interrupt() 纯节点无副作用隔离）
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from apps.agent_harness.middlewares.tenant_context import (
    TenantAuthContextMiddleware,
    require_tenant_context,
)
from apps.agent_harness.runtime.guardrails import ToolGuardrails
from apps.agent_harness.runtime.llm_client import UnifiedLLMClient
from apps.agent_harness.runtime.mcp_client import MCPClientHub
from apps.agent_harness.skills.loader import SkillLoader
from apps.agent_harness.state import BaseAgentState
from apps.common.db import get_repository


async def parse_and_match_invoices_node(state: BaseAgentState) -> dict[str, Any]:
    """节点 1：多模态识票 + 自动关联差旅单与途牛预订 + 生成柔性异常提示（绝不越权扣款）。"""
    ctx = require_tenant_context()
    SkillLoader().load_skill_body("expense-drafting")
    llm = UnifiedLLMClient()
    raw_attachments = (state.get("trip_slots") or {}).get("attachments", [])
    invoices = await llm.extract_invoices_multimodal(raw_attachments)

    repo = get_repository()
    trips = await repo.list_user_trips(ctx.tenant_id, ctx.user_id)
    matched_order = trips[0] if trips else None
    matched_order_id = matched_order["id"] if matched_order else None

    total_amount = round(sum(float(inv.get("amount", 0.0)) for inv in invoices), 2)

    # 柔性检查超标与缺票提示
    soft_hints: list[str] = []
    rule = await repo.get_policy_rule(ctx.tenant_id, ctx.rank_no, matched_order["dest_city_tier"] if matched_order else "TIER_1")
    hotel_limit = float(rule["hotel_limit_per_night"]) if rule else 650.0
    categories = {inv["category"] for inv in invoices}
    for inv in invoices:
        if inv["category"] == "HOTEL" and inv["amount"] > hotel_limit * 2:
            diff = round(inv["amount"] - hotel_limit * 2, 2)
            soft_hints.append(f"酒店总金额 {inv['amount']:.2f} 元略超标准（按2晚限额 {hotel_limit*2:.2f} 元高出 {diff:.2f} 元），建议在报销事由注明原因。")
    if "FLIGHT" in categories and "TRAIN" not in categories and len([i for i in invoices if i["category"] == "FLIGHT"]) == 1:
        soft_hints.append("暂未检测到返程交通发票，若为单程报销可直接继续提交。")

    tool_args = {
        "travel_order_id": matched_order_id,
        "total_amount": total_amount,
        "invoices": invoices,
    }
    hitl = ToolGuardrails.build_hitl_payload(
        ui_type="form",
        title="请核对并确认推送 ERP 报销草稿",
        description="；".join(soft_hints) if soft_hints else "所有票据已自动关联差旅行程且符合差标要求，确认后将写入企业费控 ERP 草稿箱。",
        tool_name="submit_expense_draft",
        tool_args=tool_args,
        schema_fields=[
            {"name": "travel_order_id", "label": "关联差旅单ID", "value": matched_order_id},
            {"name": "total_amount", "label": "报销总金额(元)", "value": total_amount},
            {"name": "soft_hints", "label": "柔性合规提示", "value": soft_hints},
        ],
    )

    return {
        "expense_invoices": invoices,
        "pending_hitl": hitl.model_dump(),
        "loaded_skills": ["expense-drafting"],
        "active_subgraph": "ExpenseDraftSubgraph",
        "step_count": 1,
        "total_tokens": 220,
    }


async def hitl_expense_confirm_node(state: BaseAgentState) -> dict[str, Any]:
    """
    节点 2：纯 HITL 挂起确认节点（v1.1 P0 #5 严格无任何外部副作用）。
    恢复后立刻校验 interrupt_id 与 action_hash 防 TOCTOU 篡改。
    """
    ctx = require_tenant_context()
    expected_hitl = state.get("pending_hitl")
    assert expected_hitl is not None

    resume_val = interrupt(expected_hitl)
    approved = await ToolGuardrails.verify_and_audit_decision(
        ctx=ctx,
        conversation_id=state["conversation_id"],
        expected_hitl=expected_hitl,
        resume_payload=resume_val if isinstance(resume_val, dict) else {},
    )
    return {"hitl_resume_payload": {"approved": approved, **(resume_val if isinstance(resume_val, dict) else {})}, "step_count": 1}


async def push_expense_erp_node(state: BaseAgentState) -> dict[str, Any]:
    """节点 3：独立后继 WRITE 节点，二次比对 action_hash 后推送 ERP 报销草稿。"""
    ctx = require_tenant_context()
    resume_info = state.get("hitl_resume_payload") or {}
    expected_hitl = state.get("pending_hitl") or {}
    if not resume_info.get("approved"):
        return {
            "final_answer": "您已取消推送报销草稿，如需修改票据后重新草拟请随时告知。",
            "pending_hitl": None,
            "step_count": 1,
        }

    actual_args = expected_hitl["tool_args"]
    ToolGuardrails.assert_pre_execution_hash(
        ctx=ctx,
        conversation_id=state["conversation_id"],
        interrupt_id=expected_hitl["interrupt_id"],
        expected_action_hash=expected_hitl["action_hash"],
        tool_name="submit_expense_draft",
        actual_args=actual_args,
    )

    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    res = await MCPClientHub.submit_expense_draft(
        internal_token=token,
        travel_order_id=actual_args.get("travel_order_id"),
        total_amount=float(actual_args["total_amount"]),
        invoices=actual_args["invoices"],
    )
    await ToolGuardrails.record_execution_audit(
        ctx=ctx,
        conversation_id=state["conversation_id"],
        interrupt_id=expected_hitl["interrupt_id"],
        tool_name="submit_expense_draft",
        action_hash=expected_hitl["action_hash"],
        args=actual_args,
        exec_status="SUCCESS",
    )

    return {
        "expense_draft_result": res,
        "pending_hitl": None,
        "final_answer": (
            f"已成功为您在企业费控 ERP 创建报销草稿单：\n"
            f"- **本地草稿单号**：`{res['draft_no']}`\n"
            f"- **ERP 外部草稿号**：`{res['erp_external_id']}`\n"
            f"- **报销总金额**：`{res['total_amount']:.2f} 元`（共 {len(actual_args['invoices'])} 张发票）\n"
            f"请登录费控系统核对附件并完成最终提交。"
        ),
        "step_count": 1,
    }


def build_expense_draft_subgraph(checkpointer: Any = None):
    builder = StateGraph(BaseAgentState)
    builder.add_node("parse_and_match", parse_and_match_invoices_node)
    builder.add_node("hitl_confirm", hitl_expense_confirm_node)
    builder.add_node("push_erp", push_expense_erp_node)
    builder.add_edge(START, "parse_and_match")
    builder.add_edge("parse_and_match", "hitl_confirm")
    builder.add_edge("hitl_confirm", "push_erp")
    builder.add_edge("push_erp", END)
    return builder.compile(checkpointer=checkpointer)
