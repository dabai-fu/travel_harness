"""
企业 OA 协同标准 FastMCP Server（oa-mcp-server）。

对应文档章节：
  - 七、7.2 工具清单：get_employee_profile / list_employee_trips / submit_travel_approval(WRITE) / send_oa_notification(WRITE)
  - 二、2.2 / 十一、v1.1 P0 #7 & P2 #29

核心安全约束：
  所有工具必须先验签 `internal_token` 重建 `TenantContext`，**绝不信任 LLM 传入的 user_id / tenant_id / dept_id**。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastmcp import FastMCP

from apps.agent_harness.middlewares.tenant_context import TenantAuthContextMiddleware
from apps.common.db import get_repository

mcp = FastMCP("oa-mcp-server")

# 通知发送留痕队列（供测试与审计验证）
_SENT_OA_NOTIFICATIONS: list[dict[str, Any]] = []


def get_sent_oa_notifications() -> list[dict[str, Any]]:
    return list(_SENT_OA_NOTIFICATIONS)


@mcp.tool()
async def get_employee_profile(internal_token: str) -> dict[str, Any]:
    """获取当前鉴权员工档案与职级（从令牌读取身份，忽略任何外部伪造参数）。"""
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    repo = get_repository()
    profile = await repo.get_user_profile(ctx.tenant_id, ctx.user_id)
    if not profile:
        raise ValueError(f"未找到员工档案: {ctx.user_id}")
    return {
        "user_id": profile["user_id"],
        "tenant_id": profile["tenant_id"],
        "dept_id": profile["dept_id"],
        "dept_name": profile["dept_name"],
        "real_name": profile["real_name"],
        "rank_level": profile["rank_level"],
        "rank_no": profile["rank_no"],
        "home_city": profile["home_city"],
    }


@mcp.tool()
async def list_employee_trips(internal_token: str) -> list[dict[str, Any]]:
    """查询本人未取消的差旅单列表，供行程冲突检测使用。"""
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    repo = get_repository()
    trips = await repo.list_user_trips(ctx.tenant_id, ctx.user_id)
    return [
        {
            "id": t["id"],
            "order_no": t["order_no"],
            "origin_city": t["origin_city"],
            "dest_city": t["dest_city"],
            "start_date": str(t["start_date"]),
            "end_date": str(t["end_date"]),
            "status": t["status"],
            "estimated_amount": float(t["estimated_amount"]),
        }
        for t in trips
        if t["status"] not in ("CANCELLED", "REJECTED")
    ]


@mcp.tool()
async def submit_travel_approval(
    internal_token: str,
    origin_city: str,
    dest_city: str,
    dest_city_tier: str,
    start_date: str,
    end_date: str,
    purpose: str,
    estimated_amount: float,
    auto_approve: bool = True,
) -> dict[str, Any]:
    """
    [WRITE 高危工具] 在 OA 系统创建出差申请单并发起审批。
    支持同步自动审批（auto_approve=True）与异步等待主管 Webhook 审批回调（auto_approve=False）。
    """
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    repo = get_repository()
    oa_ext_id = f"OA-{ctx.tenant_id}-{uuid.uuid4().hex[:8].upper()}"
    status = "APPROVED" if auto_approve else "SUBMITTED"
    order = await repo.create_travel_order(
        {
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "dept_id": ctx.dept_id,
            "origin_city": origin_city,
            "dest_city": dest_city,
            "dest_city_tier": dest_city_tier,
            "start_date": date.fromisoformat(start_date),
            "end_date": date.fromisoformat(end_date),
            "purpose": purpose,
            "estimated_amount": estimated_amount,
            "status": status,
            "oa_external_id": oa_ext_id,
        }
    )
    if auto_approve:
        await repo.add_approval_record(ctx.tenant_id, order["id"], "oa_auto_policy", "APPROVE", "差标合规自动审批通过")
    return {
        "travel_order_id": order["id"],
        "order_no": order["order_no"],
        "oa_external_id": oa_ext_id,
        "status": order["status"],
    }


@mcp.tool()
async def send_oa_notification(
    internal_token: str,
    dept_id: str,
    title: str,
    content: str,
) -> dict[str, Any]:
    """
    [WRITE 高危工具] 通过企业统一 OA 渠道发送部门预算预警通知（v1.1 P2 #29：解耦具体 IM 厂商）。
    严格校验只有具备目标部门管理权限的主管/财务才能发送。
    """
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    if ctx.role not in ("DEPT_MANAGER", "FINANCE_ADMIN", "TENANT_ADMIN"):
        raise PermissionError("仅部门主管或财务管理员有权发送预算预警通知")
    if dept_id not in ctx.managed_dept_ids:
        raise PermissionError(f"越权拦截：无权向未管辖部门 {dept_id} 发送通知")

    notice = {
        "notice_id": f"NTC-{uuid.uuid4().hex[:8].upper()}",
        "tenant_id": ctx.tenant_id,
        "sender_user_id": ctx.user_id,
        "dept_id": dept_id,
        "title": title,
        "content": content,
        "sent_at": datetime.now(UTC).isoformat(),
    }
    _SENT_OA_NOTIFICATIONS.append(notice)
    return {"status": "SENT", **notice}


if __name__ == "__main__":
    import os

    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8022")),
    )
