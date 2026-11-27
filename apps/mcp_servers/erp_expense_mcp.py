"""
费控 ERP 标准 FastMCP Server（erp-expense-mcp-server）。

对应文档章节：
  - 二、2.1 RBAC 严禁跨平行部门查账
  - 三、模块 4（安全查数与上下文卸载：CSV 落盘，仅返回 artifact_id + schema + 前 5 行）
  - 七、7.2 工具清单：get_dept_budgets / export_expense_csv_to_sandbox / submit_expense_draft(WRITE)
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date, timedelta
from typing import Any

from fastmcp import FastMCP

from apps.agent_harness.middlewares.tenant_context import TenantAuthContextMiddleware
from apps.agent_harness.sandbox.docker_runner import get_sandbox_runner
from apps.common.db import get_repository

mcp = FastMCP("erp-expense-mcp-server")


def _resolve_and_verify_depts(
    role: str,
    managed_dept_ids: tuple[str, ...],
    requested_dept_ids: list[str] | None,
) -> tuple[str, ...]:
    """
    核心 RBAC 部门越权拦截逻辑：
      - EMPLOYEE 禁止查阅部门级汇总预算与全员明细；
      - DEPT_MANAGER 严禁跨平行部门查账（若请求超出 managed_dept_ids，立即抛 PermissionError）。
    """
    if role == "EMPLOYEE":
        raise PermissionError("越权拦截：普通员工无权查阅部门预算及团队消费明细")

    if not requested_dept_ids:
        return managed_dept_ids

    unauthorized = [d for d in requested_dept_ids if d not in managed_dept_ids]
    if unauthorized:
        raise PermissionError(
            f"越权拦截：您无权访问平行/未管辖部门 {unauthorized} 的预算数据（当前权限范围：{list(managed_dept_ids)}）"
        )
    return tuple(requested_dept_ids)


@mcp.tool()
async def get_dept_budgets(
    internal_token: str,
    fiscal_year: int = 2026,
    fiscal_quarter: int = 4,
    requested_dept_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """查询权限范围内各部门季度预算额度。"""
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    allowed = _resolve_and_verify_depts(ctx.role, ctx.managed_dept_ids, requested_dept_ids)
    repo = get_repository()
    rows = await repo.list_dept_budgets(ctx.tenant_id, allowed, fiscal_year, fiscal_quarter)
    return [
        {
            "dept_id": r["dept_id"],
            "dept_name": r["dept_name"],
            "fiscal_year": r["fiscal_year"],
            "fiscal_quarter": r["fiscal_quarter"],
            "budget_amount": str(r["budget_amount"]),
            "warn_threshold": str(r["warn_threshold"]),
        }
        for r in rows
    ]


@mcp.tool()
async def export_expense_csv_to_sandbox(
    internal_token: str,
    conversation_id: str,
    fiscal_year: int = 2026,
    fiscal_quarter: int = 4,
    requested_dept_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    安全导出脱敏差旅消费明细 CSV 与预算表 CSV 直接落盘至租户隔离沙箱。
    遵守 Context Offloading 原则：绝不将成千上万行明细塞入 LLM 上下文，只返回 artifact_id、Schema 与前 5 行预览。
    """
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    allowed = _resolve_and_verify_depts(ctx.role, ctx.managed_dept_ids, requested_dept_ids)
    repo = get_repository()

    # 拉取当期及上一基期（Q-1）数据以支撑无残差两因素量价拆分
    prev_q = 4 if fiscal_quarter == 1 else fiscal_quarter - 1
    prev_y = fiscal_year - 1 if fiscal_quarter == 1 else fiscal_year
    q_start = date(prev_y, (prev_q - 1) * 3 + 1, 1)
    next_q_month = fiscal_quarter * 3 + 1
    q_end = (date(fiscal_year + 1, 1, 1) if next_q_month > 12 else date(fiscal_year, next_q_month, 1)) - timedelta(days=1)

    bookings = await repo.list_bookings(
        tenant_id=ctx.tenant_id,
        dept_ids=allowed,
        start_date=q_start,
        end_date=q_end,
    )
    budgets = await repo.list_dept_budgets(ctx.tenant_id, allowed, fiscal_year, fiscal_quarter)

    # 1. 构造脱敏消费明细 CSV（金额以精确字符串导出，不写真实姓名/身份证）
    exp_buf = io.StringIO()
    exp_fields = [
        "booking_id",
        "travel_order_id",
        "dept_id",
        "category",
        "city",
        "vendor_name",
        "amount",
        "policy_limit",
        "is_policy_exceeded",
        "depart_date",
        "fiscal_year",
        "fiscal_quarter",
        "advance_days",
        "min_advance_days_rule",
    ]
    writer = csv.DictWriter(exp_buf, fieldnames=exp_fields)
    writer.writeheader()
    sample_rows: list[dict[str, Any]] = []
    confirmed_count = 0
    for b in bookings:
        if b["booking_status"] != "CONFIRMED":
            continue
        raw = b.get("raw_detail_json", {})
        dep_d: date = b["depart_date"]
        fq = raw.get("fiscal_quarter") or ((dep_d.month - 1) // 3 + 1)
        fy = raw.get("fiscal_year") or dep_d.year
        adv = raw.get("advance_days")
        if adv is None:
            adv = max(0, (dep_d - b["booked_at"].date()).days)
        row_dict = {
            "booking_id": b["id"],
            "travel_order_id": b["travel_order_id"],
            "dept_id": b["dept_id"],
            "category": b["category"],
            "city": b["city"],
            "vendor_name": b["vendor_name"],
            "amount": str(b["amount"]),
            "policy_limit": str(b["policy_limit"] or "0.00"),
            "is_policy_exceeded": int(b["is_policy_exceeded"]),
            "depart_date": str(dep_d),
            "fiscal_year": fy,
            "fiscal_quarter": fq,
            "advance_days": int(adv),
            "min_advance_days_rule": int(raw.get("min_advance_days_rule", 3)),
        }
        writer.writerow(row_dict)
        confirmed_count += 1
        if len(sample_rows) < 5:
            sample_rows.append(row_dict)

    # 2. 构造部门预算 CSV
    bud_buf = io.StringIO()
    bud_fields = ["dept_id", "dept_name", "fiscal_year", "fiscal_quarter", "budget_amount", "warn_threshold"]
    bwriter = csv.DictWriter(bud_buf, fieldnames=bud_fields)
    bwriter.writeheader()
    dept_names: dict[str, str] = {}
    for bd in budgets:
        dept_names[bd["dept_id"]] = bd["dept_name"]
        bwriter.writerow(
            {
                "dept_id": bd["dept_id"],
                "dept_name": bd["dept_name"],
                "fiscal_year": bd["fiscal_year"],
                "fiscal_quarter": bd["fiscal_quarter"],
                "budget_amount": str(bd["budget_amount"]),
                "warn_threshold": str(bd["warn_threshold"]),
            }
        )

    runner = get_sandbox_runner()
    expense_art_id = runner.register_artifact(ctx.tenant_id, conversation_id, "expense_detail.csv", exp_buf.getvalue())
    budget_art_id = runner.register_artifact(ctx.tenant_id, conversation_id, "dept_budget.csv", bud_buf.getvalue())

    return {
        "expense_artifact_id": expense_art_id,
        "budget_artifact_id": budget_art_id,
        "schema": exp_fields,
        "row_count": confirmed_count,
        "sample_5_rows": sample_rows,
        "allowed_dept_ids": list(allowed),
        "dept_names": dept_names,
        "rules_json": json.dumps([{"dept_id": d} for d in allowed], ensure_ascii=False),
    }


@mcp.tool()
async def submit_expense_draft(
    internal_token: str,
    travel_order_id: int | None,
    total_amount: float,
    invoices: list[dict[str, Any]],
) -> dict[str, Any]:
    """[WRITE 高危工具] 将员工确认后的报销单明细推送至企业 ERP 生成报销草稿。"""
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    repo = get_repository()
    erp_id = f"ERP-{ctx.tenant_id}-{uuid.uuid4().hex[:8].upper()}"
    draft = await repo.create_expense_draft(
        {
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "dept_id": ctx.dept_id,
            "travel_order_id": travel_order_id,
            "total_amount": total_amount,
            "invoices_json": invoices,
            "erp_external_id": erp_id,
            "status": "PUSHED_TO_ERP",
        }
    )
    return {
        "draft_id": draft["id"],
        "draft_no": draft["draft_no"],
        "erp_external_id": erp_id,
        "total_amount": float(draft["total_amount"]),
        "status": draft["status"],
    }


if __name__ == "__main__":
    import os

    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8023")),
    )
