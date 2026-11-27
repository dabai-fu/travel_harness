"""
安全与多租户纵深隔离测试（对应文档第 2、6、7、8 章 & v1.1 P0/P1 安全项）。
"""

from __future__ import annotations

import pytest

from apps.agent_harness.middlewares.tenant_context import (
    InternalTokenError,
    TenantAuthContextMiddleware,
)
from apps.agent_harness.runtime.guardrails import HitlGuardrailError, ToolGuardrails
from apps.agent_harness.sandbox.docker_runner import SandboxSecurityError, get_sandbox_runner
from apps.agent_harness.state import TenantContext
from apps.api_gateway.security.masker import SensitiveDataMasker
from apps.common.crypto import (
    decrypt_aes_gcm,
    encrypt_aes_gcm,
    make_booking_idempotent_key,
)
from apps.common.db import reset_repository_for_tests
from apps.mcp_servers.erp_expense_mcp import export_expense_csv_to_sandbox
from apps.mcp_servers.tuniu_mcp import search_flights


def _ctx(tenant_id: str, user_id: str, role: str, dept_id: str, managed: tuple[str, ...]) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        dept_id=dept_id,
        managed_dept_ids=managed,
        role=role,  # type: ignore[arg-type]
        rank_level="P8",
        rank_no=8,
        trace_id="test_trace",
    )


def test_aes_gcm_key_version_and_masker() -> None:
    secret = '{"api_key":"tn_live_abc123456789","phone":"13812345678","id_card":"310101199506152218"}'
    cipher, ver = encrypt_aes_gcm(secret)
    assert cipher.startswith(f"v{ver}:")
    assert decrypt_aes_gcm(cipher) == secret

    masked = SensitiveDataMasker.mask_text("联系手机13812345678，身份证310101199506152218，密钥sk-abcd12345678")
    assert "138****5678" in masked
    assert "310101********2218" in masked
    assert "sk-abcd****" in masked


def test_booking_idempotent_key_separator_and_segment() -> None:
    # 防止无分隔符拼接碰撞 ("ab"+"c" vs "a"+"bc")
    k1 = make_booking_idempotent_key("ab", "c", "opt1", 1)
    k2 = make_booking_idempotent_key("a", "bc", "opt1", 1)
    assert k1 != k2
    # 同一方案不同段 (去程1/酒店2/返程3) 必须产生不同幂等键
    seg1 = make_booking_idempotent_key("T001", 100, "opt1", 1)
    seg2 = make_booking_idempotent_key("T001", 100, "opt1", 2)
    assert seg1 != seg2


async def test_mcp_internal_token_and_cross_dept_rbac() -> None:
    reset_repository_for_tests(auto_seed=True)
    # 1. 伪造或缺失内部令牌必须被 MCP Server 拒绝
    with pytest.raises(InternalTokenError):
        await search_flights("invalid.token", "上海", "北京", "2026-12-15")

    # 2. D002 主管试图越权导出平行部门 D003 预算消费 CSV -> 必须 100% 拦截
    mgr_d2 = _ctx("T001", "u_mgr_d2", "DEPT_MANAGER", "D002", ("D002",))
    tok = TenantAuthContextMiddleware.issue_internal_token(mgr_d2)
    with pytest.raises(PermissionError, match="越权拦截"):
        await export_expense_csv_to_sandbox(tok, "conv_sec_1", requested_dept_ids=["D003"])


async def test_rag_tenant_isolation_and_sandbox_path_traversal() -> None:
    repo = reset_repository_for_tests(auto_seed=True)
    # T001 员工绝对搜不出 T002 保密文档 YUANHANG-260
    t1_hits = await repo.search_policy_chunks("T001", "YUANHANG-260 保密津贴", top_k=5)
    assert all("YUANHANG-260" not in h["content"] for h in t1_hits)

    # 沙箱防路径穿越与跨租户 artifact_id 访问
    runner = get_sandbox_runner()
    art_id = runner.register_artifact("T001", "conv_sb_1", "data.csv", "a,b\n1,2\n")
    with pytest.raises(SandboxSecurityError):
        runner.resolve_artifact_path("T002", "conv_sb_1", art_id)
    with pytest.raises(SandboxSecurityError):
        runner.get_workspace_dir("T001", "bad:conv_id")


async def test_hitl_action_hash_anti_toctou() -> None:
    reset_repository_for_tests(auto_seed=True)
    ctx = _ctx("T001", "u_emp_1", "EMPLOYEE", "D002", ("D002",))
    hitl = ToolGuardrails.build_hitl_payload(
        "confirm", "下单确认", "确认下单", "create_tuniu_booking", {"hotel_id": "HT_01", "amount": 620.0}
    )
    with pytest.raises(HitlGuardrailError, match="TOCTOU"):
        await ToolGuardrails.verify_and_audit_decision(
            ctx,
            "conv_toctou",
            hitl.model_dump(),
            {"interrupt_id": hitl.interrupt_id, "action_hash": "0" * 64, "decision": "CONFIRMED"},
        )
