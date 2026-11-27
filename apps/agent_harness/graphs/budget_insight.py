"""
子图 4：部门预算执行与超支归因分析（BudgetInsightSubgraph — 沙箱并行王炸模块）。

对应文档章节：
  - 三、模块 4（安全查数卸载 -> LangGraph Send 并行 3 路沙箱 Pandas 精算 -> 占位符渲染 + VerificationNode -> Recharts 图表 -> 预警 HITL）
  - 五、5.2 / 5.3
  - 十一、v1.1 P0 #6（并行 Reducer）、P1 #10（占位符+勾稽校验）、P1 #11（int64 分+Decimal）、P1 #12（无残差量价公式）
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from apps.agent_harness.middlewares.tenant_context import (
    TenantAuthContextMiddleware,
    require_tenant_context,
)
from apps.agent_harness.runtime.guardrails import ToolGuardrails
from apps.agent_harness.runtime.llm_client import UnifiedLLMClient
from apps.agent_harness.runtime.mcp_client import MCPClientHub
from apps.agent_harness.sandbox.docker_runner import get_sandbox_runner
from apps.agent_harness.skills.loader import SkillLoader
from apps.agent_harness.state import BaseAgentState

_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z0-9_.-]+)\}\}")
# 检查大模型写的草稿模板中是否违禁偷写裸数字金额/百分比（如 123.45元、85.2%）
_BARE_METRIC_RE = re.compile(r"(?<!\d)(?:\d+(?:,\d{3})*(?:\.\d+)?\s*(?:元|万元|%))")


def _extract_requested_depts(query: str, tenant_id: str) -> list[str] | None:
    """从自然语言中识别用户是否指定了特定部门（用于测试跨平行部门越权拦截）。"""
    mapping = {
        "研发与交付中心": "D001",
        "华东业务交付部": "D002",
        "基础架构部": "D003",
        "财务与行政部": "D004",
        "智能装备事业部": "D201",
        "华南供应链部": "D202",
    }
    found: list[str] = []
    for name, did in mapping.items():
        if name in query or did in query:
            found.append(did)
    return found or None


async def fetch_and_offload_node(state: BaseAgentState) -> dict[str, Any]:
    """
    节点 1：鉴权并调 erp-expense-mcp 将脱敏消费明细与预算表落盘到沙箱 CSV。
    若出现越权访问平行部门或普通员工查全公司预算，立即在此拦截并拒绝扇出。
    """
    ctx = require_tenant_context()
    SkillLoader().load_skill_body("budget-fpa-attribution")
    query = state.get("effective_query") or state.get("user_query", "")
    req_depts = _extract_requested_depts(query, ctx.tenant_id)
    fy = int(state.get("budget_fiscal_year") or 2026)
    fq = int(state.get("budget_fiscal_quarter") or 4)

    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    try:
        export_res = await MCPClientHub.export_expense_csv_to_sandbox(
            internal_token=token,
            conversation_id=state["conversation_id"],
            fiscal_year=fy,
            fiscal_quarter=fq,
            requested_dept_ids=req_depts,
        )
    except PermissionError as exc:
        return {
            "error_info": {"code": "RBAC_FORBIDDEN", "message": str(exc), "retryable": False},
            "final_answer": f"权限拦截：{exc}",
            "active_subgraph": "BudgetInsightSubgraph",
            "step_count": 1,
        }

    return {
        "budget_fiscal_year": fy,
        "budget_fiscal_quarter": fq,
        "budget_allowed_depts": export_res["allowed_dept_ids"],
        "budget_csv_artifact_id": export_res["budget_artifact_id"],
        "budget_metrics": {"_meta": {"expense_artifact_id": export_res["expense_artifact_id"], "dept_names": export_res["dept_names"]}},
        "artifact_files": [
            {"artifact_id": export_res["expense_artifact_id"], "type": "csv", "summary": f"消费明细 {export_res['row_count']} 行"},
            {"artifact_id": export_res["budget_artifact_id"], "type": "csv", "summary": "部门预算表"},
        ],
        "loaded_skills": ["budget-fpa-attribution"],
        "active_subgraph": "BudgetInsightSubgraph",
        "step_count": 1,
    }


def fan_out_sandbox_workers(state: BaseAgentState) -> list[Send] | str:
    """使用 LangGraph Send API 并行扇出 3 路沙箱分析 Worker；若权限拦截则直接结束。"""
    if state.get("error_info"):
        return END
    base_payload = {
        "conversation_id": state["conversation_id"],
        "budget_fiscal_year": state["budget_fiscal_year"],
        "budget_fiscal_quarter": state["budget_fiscal_quarter"],
        "budget_csv_artifact_id": state["budget_csv_artifact_id"],
        "budget_metrics": state.get("budget_metrics", {}),
    }
    return [
        Send("volume_price_worker", base_payload),
        Send("behavior_premium_worker", base_payload),
        Send("cost_saving_worker", base_payload),
    ]


async def _run_sandbox_mode(state: BaseAgentState, mode: str) -> dict[str, Any]:
    ctx = require_tenant_context()
    meta = (state.get("budget_metrics") or {}).get("_meta", {})
    exp_art = meta["expense_artifact_id"]
    bud_art = state["budget_csv_artifact_id"]
    assert bud_art is not None

    runner = get_sandbox_runner()
    res = await runner.run_budget_script(
        tenant_id=ctx.tenant_id,
        conversation_id=state["conversation_id"],
        mode=mode,
        expense_artifact_id=exp_art,
        budget_artifact_id=bud_art,
        fiscal_year=state["budget_fiscal_year"],
        fiscal_quarter=state["budget_fiscal_quarter"],
    )
    update: dict[str, Any] = {"budget_metrics": {mode: res}, "step_count": 1}
    if "charts" in res:
        update["chart_payloads"] = res["charts"]
    return update


async def volume_price_worker(state: BaseAgentState) -> dict[str, Any]:
    """并行 Worker A：执行率与无残差两因素量价拆分。"""
    return await _run_sandbox_mode(state, "volume_price")


async def behavior_premium_worker(state: BaseAgentState) -> dict[str, Any]:
    """并行 Worker B：基于差标 min_advance_booking_days 计算临期溢价与酒店超标。"""
    return await _run_sandbox_mode(state, "behavior_premium")


async def cost_saving_worker(state: BaseAgentState) -> dict[str, Any]:
    """并行 Worker C：高频城市/酒店集中度与协议降本测算。"""
    return await _run_sandbox_mode(state, "cost_saving")


def _build_placeholder_lookup(metrics: dict[str, Any]) -> dict[str, str]:
    """从 3 路沙箱 Decimal 精确 JSON 指标构建扁平占位符字典。"""
    vp_res = metrics.get("volume_price", {})
    beh_res = metrics.get("behavior_premium", {}).get("behavior", {})
    sav_res = metrics.get("cost_saving", {}).get("saving", {})

    lookup: dict[str, str] = {}
    tot = vp_res.get("total", {})
    for k in ("budget", "spent", "overspend", "exec_rate"):
        if k in tot:
            lookup[f"total.{k}"] = str(tot[k])

    for did, dinfo in vp_res.get("depts", {}).items():
        for k in ("budget", "spent", "overspend", "exec_rate"):
            lookup[f"dept.{did}.{k}"] = str(dinfo[k])

    vp = vp_res.get("vp", {})
    for k in ("volume_effect", "price_effect", "total_delta"):
        if k in vp:
            lookup[f"vp.{k}"] = str(vp[k])

    for k in ("late_ratio", "late_premium", "hotel_exceed_ratio"):
        if k in beh_res:
            lookup[f"behavior.{k}"] = str(beh_res[k])

    for k in ("top_city", "estimated_saving"):
        if k in sav_res:
            lookup[f"saving.{k}"] = str(sav_res[k])

    return lookup


async def aggregate_and_write_node(state: BaseAgentState) -> dict[str, Any]:
    """
    汇聚 3 路沙箱指标 -> 让 LLM 生成纯占位符归因报告 -> 由 Python 确定性渲染真实数值（v1.1 P1 #10）。
    """
    metrics = state.get("budget_metrics") or {}
    meta = metrics.get("_meta", {})
    dept_ids = list(state.get("budget_allowed_depts") or [])
    dept_names = dict(meta.get("dept_names") or {})

    llm = UnifiedLLMClient()
    draft = await llm.generate_budget_placeholder_report(
        dept_ids=dept_ids,
        dept_names=dept_names,
        qualitative_context={
            "feedback_from_verifier": state.get("verification_feedback"),
            "top_city": metrics.get("cost_saving", {}).get("saving", {}).get("top_city"),
            "alert_depts": [d["dept_name"] for d in metrics.get("volume_price", {}).get("alert_depts", [])],
        },
    )

    lookup = _build_placeholder_lookup(metrics)
    rendered = _PLACEHOLDER_RE.sub(lambda m: lookup.get(m.group(1), m.group(0)), draft)

    return {
        "draft_report_with_placeholders": draft,
        "rendered_report": rendered,
        "step_count": 1,
        "total_tokens": 350,
    }


async def verification_node(state: BaseAgentState) -> dict[str, Any]:
    """
    确定性纯 Python 勾稽校验节点（v1.1 P1 #10，零容差防幻觉）。

    四项硬校验：
      1. 所有占位符均被成功渲染（无残留 `{{...}}`）；
      2. LLM 原始草稿中不得出现占位符之外的裸数字金额/百分比；
      3. 部门名称全部属于当前权限范围；
      4. 财务不变量恒成立（量增 + 价升 == 总变动额；各部门超支之和 == 汇总超支）。
    不通过则携带精准 Diff 打回重写（最多 2 次），仍失败则降级为纯确定性数据表+模板。
    """
    draft = state.get("draft_report_with_placeholders") or ""
    rendered = state.get("rendered_report") or ""
    metrics = state.get("budget_metrics") or {}
    vp_res = metrics.get("volume_price", {})
    retries = int(state.get("verification_retry_count") or 0)

    errors: list[str] = []
    # 1. 未解析占位符检查
    unresolved = _PLACEHOLDER_RE.findall(rendered)
    if unresolved:
        errors.append(f"存在未解析占位符: {unresolved}")

    # 2. 原始草稿裸数字检查（剥离占位符和章节号后，不得出现带元/万元/%的裸数值）
    draft_no_ph = _PLACEHOLDER_RE.sub("", draft)
    bare_nums = _BARE_METRIC_RE.findall(draft_no_ph)
    if bare_nums:
        errors.append(f"模型违禁直接书写了裸数字指标: {bare_nums}")

    # 3. 部门权限范围不越界检查（禁止报告中出现未授权部门名）
    known_depts = {"D001": "研发与交付中心", "D002": "华东业务交付部", "D003": "基础架构部", "D004": "财务与行政部", "D201": "智能装备事业部", "D202": "华南供应链部"}
    allowed_set = set(state.get("budget_allowed_depts") or [])
    for did, dname in known_depts.items():
        if did not in allowed_set and dname in rendered:
            errors.append(f"报告提到了权限外部门: {dname}")

    # 4. 财务不变量零容差校验
    vp = vp_res.get("vp", {})
    if vp:
        vol_e = Decimal(str(vp["volume_effect"]))
        pri_e = Decimal(str(vp["price_effect"]))
        tot_d = Decimal(str(vp["total_delta"]))
        if vol_e + pri_e != tot_d:
            errors.append(f"量价分解不变量破坏: {vol_e} + {pri_e} != {tot_d}")

    depts = vp_res.get("depts", {})
    tot = vp_res.get("total", {})
    if depts and tot:
        sum_over = sum(Decimal(str(d["overspend"])) for d in depts.values())
        tot_over = Decimal(str(tot["overspend"]))
        if sum_over != tot_over:
            errors.append(f"部门超支之和不等于汇总超支: {sum_over} != {tot_over}")

    if not errors:
        # 校验通过！检查是否有超阈值预警部门需要人工确认发送 OA 通知
        alert_depts = vp_res.get("alert_depts", [])
        pending_hitl = None
        if alert_depts and (state.get("trip_slots") or {}).get("enable_alert_hitl", False):
            top_alert = alert_depts[0]
            hitl = ToolGuardrails.build_hitl_payload(
                ui_type="confirm",
                title="预算超支预警通知确认",
                description=f"检测到 {top_alert['dept_name']} 执行率达 {top_alert['exec_rate']}%，是否发送 OA 预算预警通知？",
                tool_name="send_oa_notification",
                tool_args={
                    "dept_id": top_alert["dept_id"],
                    "title": f"{top_alert['dept_name']} Q{state['budget_fiscal_quarter']} 差旅预算超支预警",
                    "content": f"当前预算执行率 {top_alert['exec_rate']}%，差额 {top_alert['overspend']} 元，请关注临期订票管控。",
                },
            )
            pending_hitl = hitl.model_dump()
        return {
            "verification_feedback": None,
            "pending_hitl": pending_hitl,
            "final_answer": rendered,
            "step_count": 1,
        }

    # 校验失败：最多重试 2 次
    if retries < 2:
        return {
            "verification_feedback": "；".join(errors),
            "verification_retry_count": retries + 1,
            "step_count": 1,
        }

    # 重试 2 次仍失败：强制降级为纯确定性结构化模板（100% 保真）
    fallback_lines = ["### 部门差旅预算执行汇总（确定性校验降级输出）"]
    for did, dinfo in depts.items():
        fallback_lines.append(
            f"- {dinfo['dept_name']}({did}): 预算 {dinfo['budget']} 元 | 支出 {dinfo['spent']} 元 | 执行率 {dinfo['exec_rate']}% | 差额 {dinfo['overspend']} 元"
        )
    return {
        "verification_feedback": None,
        "final_answer": "\n".join(fallback_lines),
        "step_count": 1,
    }


def route_after_verification(state: BaseAgentState) -> str:
    if state.get("verification_feedback"):
        return "aggregate_and_write"
    if state.get("pending_hitl"):
        return "hitl_alert_confirm"
    return END


async def hitl_alert_confirm_node(state: BaseAgentState) -> dict[str, Any]:
    """纯 HITL 预警通知确认节点（无副作用）。"""
    ctx = require_tenant_context()
    expected_hitl = state["pending_hitl"]
    assert expected_hitl is not None
    resume_val = interrupt(expected_hitl)
    approved = await ToolGuardrails.verify_and_audit_decision(
        ctx=ctx,
        conversation_id=state["conversation_id"],
        expected_hitl=expected_hitl,
        resume_payload=resume_val if isinstance(resume_val, dict) else {},
    )
    return {"hitl_resume_payload": {"approved": approved}, "step_count": 1}


async def send_alert_notification_node(state: BaseAgentState) -> dict[str, Any]:
    """独立后继节点：发送 OA 预算预警通知。"""
    ctx = require_tenant_context()
    expected_hitl = state.get("pending_hitl") or {}
    if not (state.get("hitl_resume_payload") or {}).get("approved"):
        return {"pending_hitl": None, "step_count": 1}

    args = expected_hitl["tool_args"]
    ToolGuardrails.assert_pre_execution_hash(
        ctx, state["conversation_id"], expected_hitl["interrupt_id"], expected_hitl["action_hash"], "send_oa_notification", args
    )
    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    res = await MCPClientHub.send_oa_notification(token, dept_id=args["dept_id"], title=args["title"], content=args["content"])
    await ToolGuardrails.record_execution_audit(
        ctx, state["conversation_id"], expected_hitl["interrupt_id"], "send_oa_notification", expected_hitl["action_hash"], args, "SUCCESS"
    )
    return {
        "pending_hitl": None,
        "final_answer": (state.get("final_answer") or "") + f"\n\n> 已向部门发送 OA 预算预警通知（单号 `{res['notice_id']}`）。",
        "step_count": 1,
    }


def build_budget_insight_subgraph(checkpointer: Any = None):
    builder = StateGraph(BaseAgentState)
    builder.add_node("fetch_and_offload", fetch_and_offload_node)
    builder.add_node("volume_price_worker", volume_price_worker)
    builder.add_node("behavior_premium_worker", behavior_premium_worker)
    builder.add_node("cost_saving_worker", cost_saving_worker)
    builder.add_node("aggregate_and_write", aggregate_and_write_node)
    builder.add_node("verification_node", verification_node)
    builder.add_node("hitl_alert_confirm", hitl_alert_confirm_node)
    builder.add_node("send_alert_notification", send_alert_notification_node)

    builder.add_edge(START, "fetch_and_offload")
    builder.add_conditional_edges(
        "fetch_and_offload",
        fan_out_sandbox_workers,
        ["volume_price_worker", "behavior_premium_worker", "cost_saving_worker", END],
    )
    builder.add_edge("volume_price_worker", "aggregate_and_write")
    builder.add_edge("behavior_premium_worker", "aggregate_and_write")
    builder.add_edge("cost_saving_worker", "aggregate_and_write")
    builder.add_edge("aggregate_and_write", "verification_node")
    builder.add_conditional_edges(
        "verification_node",
        route_after_verification,
        {"aggregate_and_write": "aggregate_and_write", "hitl_alert_confirm": "hitl_alert_confirm", END: END},
    )
    builder.add_edge("hitl_alert_confirm", "send_alert_notification")
    builder.add_edge("send_alert_notification", END)
    return builder.compile(checkpointer=checkpointer)
