"""
Guardrails 读写分级安全护栏与“确认即绑定（action_hash）”防 TOCTOU 模块。

对应文档章节：
  - 六、Guardrails 读写分级安全策略
  - 十一、v1.1 P0 #4（action_hash 确认即绑定）& P0 #5（interrupt() 节点无副作用隔离）

核心安全规则：
  1. READ 级工具：校验 TenantContext 权限后自动放行；
  2. WRITE 级高危工具（submit_travel_approval / create_tuniu_booking / submit_expense_draft / send_oa_notification）：
     100% 阻断并生成带 `interrupt_id` + `action_hash` 的 HITL 确认载荷；
  3. `/resume` 与最终执行前进行**双重 action_hash 校验**：
     若用户确认后参数被任何一方改写（哪怕改动 1 分钱或酒店 ID），立即拒绝执行并记入 `agent_action_audit`。
"""

from __future__ import annotations

import uuid
from typing import Any

from apps.agent_harness.state import HitlPayload, TenantContext
from apps.api_gateway.security.masker import SensitiveDataMasker
from apps.common.crypto import make_action_hash
from apps.common.db import get_repository

READ_TOOLS = {
    "search_flights",
    "search_trains",
    "search_hotels",
    "get_booking_status",
    "get_employee_profile",
    "list_employee_trips",
    "get_dept_budgets",
    "export_expense_csv_to_sandbox",
    "get_city_weather_forecast",
}

WRITE_TOOLS = {
    "submit_travel_approval",
    "create_tuniu_booking",
    "submit_expense_draft",
    "send_oa_notification",
}


class HitlGuardrailError(PermissionError):
    """HITL 校验失败异常（interrupt_id 不匹配、action_hash 被篡改或用户拒绝）。"""


class ToolGuardrails:
    """工具读写分级判定与防 TOCTOU 护栏。"""

    @staticmethod
    def is_write_tool(tool_name: str) -> bool:
        return tool_name in WRITE_TOOLS

    @staticmethod
    def build_hitl_payload(
        ui_type: str,
        title: str,
        description: str,
        tool_name: str,
        tool_args: dict[str, Any],
        options: list[dict[str, Any]] | None = None,
        schema_fields: list[dict[str, Any]] | None = None,
    ) -> HitlPayload:
        """
        构造 HITL 前端确认卡片载荷，并计算不可篡改的 `action_hash`。
        注意：必须保证调用该函数时不产生任何外部副作用（满足 v1.1 P0 #5）。
        """
        if tool_name not in WRITE_TOOLS:
            raise ValueError(f"工具 {tool_name} 不在 WRITE 工具白名单中")
        interrupt_id = f"int_{uuid.uuid4().hex[:16]}"
        action_hash = make_action_hash(tool_name, tool_args)
        return HitlPayload(
            interrupt_id=interrupt_id,
            ui_type=ui_type,  # type: ignore[arg-type]
            title=title,
            description=description,
            tool_name=tool_name,
            tool_args=SensitiveDataMasker.mask_value(tool_args),
            action_hash=action_hash,
            options=options or [],
            schema_fields=schema_fields or [],
        )

    @staticmethod
    async def verify_and_audit_decision(
        ctx: TenantContext,
        conversation_id: str,
        expected_hitl: dict[str, Any],
        resume_payload: dict[str, Any],
    ) -> bool:
        """
        在 `/resume` 恢复及 WRITE 节点执行前校验确认合法性，并写入 `agent_action_audit`。

        校验项：
          1. `interrupt_id` 必须匹配；
          2. `action_hash` 必须匹配（防 TOCTOU）；
          3. 若用户拒绝（decision == "REJECTED"），写审计日志并返回 False。
        """
        repo = get_repository()
        exp_int_id = expected_hitl["interrupt_id"]
        exp_hash = expected_hitl["action_hash"]
        tool_name = expected_hitl["tool_name"]
        tool_args = expected_hitl["tool_args"]

        got_int_id = resume_payload.get("interrupt_id")
        got_hash = resume_payload.get("action_hash")
        decision = str(resume_payload.get("decision", "CONFIRMED")).upper()

        if got_int_id != exp_int_id:
            raise HitlGuardrailError(f"interrupt_id 不匹配: 期望 {exp_int_id}, 收到 {got_int_id}")

        # 重新计算服务端原始 tool_args 的哈希，比对请求提交的 action_hash
        recomputed_hash = make_action_hash(tool_name, tool_args)
        if got_hash != exp_hash or recomputed_hash != exp_hash:
            await repo.add_action_audit(
                {
                    "tenant_id": ctx.tenant_id,
                    "user_id": ctx.user_id,
                    "conversation_id": conversation_id,
                    "interrupt_id": exp_int_id,
                    "tool_name": tool_name,
                    "action_hash": str(got_hash or ""),
                    "args_masked_json": SensitiveDataMasker.mask_value(tool_args),
                    "decision": "REJECTED",
                    "exec_status": "BLOCKED_TAMPER",
                    "trace_id": ctx.trace_id,
                }
            )
            raise HitlGuardrailError("action_hash 校验失败：检测到高危工具参数在确认后被篡改（TOCTOU 拦截）")

        if decision != "CONFIRMED":
            await repo.add_action_audit(
                {
                    "tenant_id": ctx.tenant_id,
                    "user_id": ctx.user_id,
                    "conversation_id": conversation_id,
                    "interrupt_id": exp_int_id,
                    "tool_name": tool_name,
                    "action_hash": exp_hash,
                    "args_masked_json": SensitiveDataMasker.mask_value(tool_args),
                    "decision": "REJECTED",
                    "exec_status": "CANCELLED_BY_USER",
                    "trace_id": ctx.trace_id,
                }
            )
            return False

        return True

    @staticmethod
    def assert_pre_execution_hash(
        ctx: TenantContext,
        conversation_id: str,
        interrupt_id: str,
        expected_action_hash: str,
        tool_name: str,
        actual_args: dict[str, Any],
    ) -> None:
        """
        独立后继 WRITE 节点执行前二次校验（v1.1 P0 #4 双重校验）：
        确保实际传给途牛/OA/ERP 的参数与用户当初确认时的参数 100% 一致。
        """
        actual_hash = make_action_hash(tool_name, actual_args)
        if actual_hash != expected_action_hash:
            raise HitlGuardrailError(
                f"执行前参数哈希不一致（{tool_name}），已阻断未授权的参数变更"
            )

    @staticmethod
    async def record_execution_audit(
        ctx: TenantContext,
        conversation_id: str,
        interrupt_id: str,
        tool_name: str,
        action_hash: str,
        args: dict[str, Any],
        exec_status: str,
    ) -> None:
        repo = get_repository()
        await repo.add_action_audit(
            {
                "tenant_id": ctx.tenant_id,
                "user_id": ctx.user_id,
                "conversation_id": conversation_id,
                "interrupt_id": interrupt_id,
                "tool_name": tool_name,
                "action_hash": action_hash,
                "args_masked_json": SensitiveDataMasker.mask_value(args),
                "decision": "CONFIRMED",
                "exec_status": exec_status,
                "trace_id": ctx.trace_id,
            }
        )
