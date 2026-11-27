"""
模拟企业现有老系统（Mock OA 审批 + Mock 费控 ERP）独立 REST 服务。

对应文档章节：
  - 一、1.1（绝不推翻重造企业老 OA / ERP，通过 MCP 解耦对接）
  - 九、9.3 apps/mock_enterprise_systems/server.py

运行方式（可选独立进程启动）：
  uvicorn apps.mock_enterprise_systems.server:app --port 8010
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from apps.common.config import get_settings
from apps.common.crypto import sign_hmac_sha256
from apps.common.db import get_repository

app = FastAPI(title="Mock Enterprise OA & ERP API", version="1.1.0")


class ApprovalSubmitReq(BaseModel):
    tenant_id: str
    order_id: int
    approver_id: str | None = None
    auto_approve: bool = True


class ErpExpensePushReq(BaseModel):
    tenant_id: str
    user_id: str
    dept_id: str
    travel_order_id: int | None = None
    total_amount: float
    invoices: list[dict[str, Any]]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "mock-enterprise-oa-erp"}


@app.post("/oa/approvals")
async def submit_oa_approval(req: ApprovalSubmitReq, x_tenant_id: str | None = Header(default=None)) -> dict[str, Any]:
    if x_tenant_id and x_tenant_id != req.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant header mismatch")
    repo = get_repository()
    order = await repo.get_travel_order(req.tenant_id, req.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Travel order not found")

    oa_ext_id = f"OA-EXT-{uuid.uuid4().hex[:8].upper()}"
    new_status = "APPROVED" if req.auto_approve else "SUBMITTED"
    updated = await repo.update_travel_order_status(
        req.tenant_id,
        req.order_id,
        new_status=new_status,
        oa_external_id=oa_ext_id,
    )
    if req.auto_approve:
        await repo.add_approval_record(req.tenant_id, req.order_id, req.approver_id or "oa_auto_rule", "APPROVE", "自动审批通过")

    return {"oa_external_id": oa_ext_id, "status": updated["status"], "version": updated["version"]}


@app.post("/erp/expense-drafts")
async def create_erp_expense_draft(req: ErpExpensePushReq) -> dict[str, Any]:
    repo = get_repository()
    erp_id = f"ERP-EXP-{uuid.uuid4().hex[:8].upper()}"
    draft = await repo.create_expense_draft(
        {
            "tenant_id": req.tenant_id,
            "user_id": req.user_id,
            "dept_id": req.dept_id,
            "travel_order_id": req.travel_order_id,
            "total_amount": req.total_amount,
            "invoices_json": req.invoices,
            "erp_external_id": erp_id,
            "status": "PUSHED_TO_ERP",
        }
    )
    return {"erp_external_id": erp_id, "draft_no": draft["draft_no"], "status": draft["status"]}


def build_signed_oa_webhook_headers(body_bytes: bytes) -> dict[str, str]:
    """辅助生成带 HMAC-SHA256 签名与时间戳的 OA 审批回调请求头，供集成测试使用。"""
    settings = get_settings()
    ts = str(int(datetime.now(UTC).timestamp()))
    signed_msg = ts.encode("utf-8") + b"." + body_bytes
    sig = sign_hmac_sha256(signed_msg, settings.oa_webhook_secret)
    return {"X-OA-Timestamp": ts, "X-OA-Signature": sig}
