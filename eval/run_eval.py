"""
自动化离线 Eval 跑批脚本（对应文档 10.1 六大硬指标）。

运行方式：
  python eval/run_eval.py

输出 6 项量化指标对比报告：
  1. 三层意图路由收益（延迟降幅 & LLM 调用减少比例）
  2. 上下文工程 Token 降幅（途牛大 JSON 压缩 + 历史 Skill 折叠 + CSV 落盘卸载）
  3. 行程规划合规率与冲突检出率
  4. 预算归因数值精确率（与 Decimal 真值 100% 核对）
  5. 多租户越权拦截率 & WRITE HITL 阻断率
  6. Redis/Lua 三态熔断器稳定性
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import SystemMessage

from apps.agent_harness.graphs.budget_insight import build_budget_insight_subgraph
from apps.agent_harness.graphs.trip_plan_book import build_trip_plan_book_subgraph
from apps.agent_harness.middlewares.circuit_breaker import (
    CircuitBreakerMiddleware,
    CircuitBreakerOpenError,
    UpstreamToolError,
)
from apps.agent_harness.middlewares.context_engineering import ContextEngineeringMiddleware, estimate_tokens
from apps.agent_harness.middlewares.tenant_context import (
    InternalTokenError,
    bind_tenant_context,
)
from apps.agent_harness.router.l3_llm import ThreeTierIntentRouter
from apps.agent_harness.runtime.guardrails import HitlGuardrailError, ToolGuardrails
from apps.agent_harness.state import TenantContext
from apps.common.db import reset_repository_for_tests
from apps.common.redis_client import reset_redis_for_tests
from apps.mcp_servers.tuniu_mcp import _generate_mock_flights, search_flights
from scripts.seed_mock_data import compute_budget_ground_truth


async def _make_ctx(repo: Any, tenant_id: str, user_id: str) -> TenantContext:
    acc = await repo.get_user_account(tenant_id, user_id)
    prof = await repo.get_user_profile(tenant_id, user_id)
    assert acc and prof
    managed = await repo.compute_managed_dept_ids(tenant_id, acc["role"], prof["dept_id"])
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        dept_id=prof["dept_id"],
        managed_dept_ids=managed,
        role=acc["role"],
        rank_level=prof["rank_level"],
        rank_no=int(prof["rank_no"]),
        trace_id=f"eval_{user_id}",
    )


async def evaluate_all() -> dict[str, Any]:
    reset_redis_for_tests()
    repo = reset_repository_for_tests(auto_seed=True)
    dataset = json.loads((Path(__file__).parent / "golden_dataset.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # 1. 三层意图路由对比评测
    # ------------------------------------------------------------------
    router = ThreeTierIntentRouter()
    routing_cases = [c for c in dataset if c["category"] == "routing"]
    fast_latencies: list[float] = []
    llm_only_latencies: list[float] = []
    route_hits = 0
    llm_skipped = 0

    for c in routing_cases:
        t0 = time.perf_counter()
        res_fast = await router.route(
            c["query"],
            active_subgraph=c.get("sticky_subgraph"),
            has_pending_hitl=bool(c.get("has_pending_hitl")),
            history_summaries=["刚才查询了上海去北京的航班"] if c.get("coreference") else None,
        )
        fast_latencies.append((time.perf_counter() - t0) * 1000.0)
        if not res_fast.llm_called:
            llm_skipped += 1
        if res_fast.direct_route_target == c["expected_target"]:
            route_hits += 1

        t1 = time.perf_counter()
        await router.route(c["query"], force_llm_only=True)
        llm_only_latencies.append((time.perf_counter() - t1) * 1000.0)

    avg_fast_ms = sum(fast_latencies) / len(fast_latencies)
    avg_llm_ms = max(0.01, sum(llm_only_latencies) / len(llm_only_latencies))
    routing_report = {
        "accuracy_pct": round(route_hits / len(routing_cases) * 100, 2),
        "llm_call_reduction_pct": round(llm_skipped / len(routing_cases) * 100, 2),
        "avg_three_tier_ms": round(avg_fast_ms, 3),
        "avg_llm_only_ms": round(avg_llm_ms, 3),
    }

    # ------------------------------------------------------------------
    # 2. 上下文工程 Token 降幅评测
    # ------------------------------------------------------------------
    ctx_eng = ContextEngineeringMiddleware()
    raw_tuniu = {"flights": _generate_mock_flights("上海", "北京", "2026-12-15")}
    compressed_tuniu = ctx_eng.compress_tuniu_payload(raw_tuniu)
    raw_skills = [
        SystemMessage(content="长技能正文" * 200, additional_kwargs={"skill_name": "trip-planning"}),
        SystemMessage(content="长技能正文" * 200, additional_kwargs={"skill_name": "expense-drafting"}),
    ]
    folded_skills = ctx_eng.fold_historical_skills(raw_skills, active_skill_name="expense-drafting")

    before_tokens = estimate_tokens(raw_tuniu) + sum(estimate_tokens(m.content) for m in raw_skills)
    after_tokens = estimate_tokens(compressed_tuniu) + sum(estimate_tokens(m.content) for m in folded_skills)
    token_reduction_pct = round((before_tokens - after_tokens) / before_tokens * 100, 2)

    # ------------------------------------------------------------------
    # 3. 行程规划合规率与冲突检出率评测
    # ------------------------------------------------------------------
    trip_graph = build_trip_plan_book_subgraph()
    planning_cases = [c for c in dataset if c["category"] == "planning"]
    opt4_ok = 0
    compliant_ok = 0
    conflict_ok = 0
    conflict_total = 0

    for pc in planning_cases:
        ctx = await _make_ctx(repo, pc["tenant_id"], pc["user_id"])
        with bind_tenant_context(ctx):
            out = await trip_graph.ainvoke(
                {"conversation_id": f"eval_{pc['id']}", "user_query": pc["query"], "effective_query": pc["query"], "messages": []},
                config={"configurable": {"thread_id": f"eval_trip_{pc['id']}"}},
            )
        opts = out.get("travel_options") or []
        if len(opts) == 4:
            opt4_ok += 1
        if any(o["label"] == "最合规" and o["is_policy_compliant"] for o in opts):
            compliant_ok += 1
        if pc.get("expect_conflict"):
            conflict_total += 1
            if len(out.get("trip_conflicts") or []) > 0:
                conflict_ok += 1

    # ------------------------------------------------------------------
    # 4. 预算归因 Decimal 数值精确率（100% 核对）
    # ------------------------------------------------------------------
    budget_graph = build_budget_insight_subgraph()
    budget_cases = [c for c in dataset if c["category"] == "budget"]
    budget_exact_matches = 0
    for bc in budget_cases:
        ctx = await _make_ctx(repo, bc["tenant_id"], bc["user_id"])
        truth = compute_budget_ground_truth(repo, bc["tenant_id"], tuple(bc["depts"]), bc["year"], bc["quarter"])
        with bind_tenant_context(ctx):
            out = await budget_graph.ainvoke(
                {
                    "conversation_id": f"eval_{bc['id']}",
                    "user_query": "分析部门差旅预算与超支归因",
                    "effective_query": "分析部门差旅预算与超支归因",
                    "budget_fiscal_year": bc["year"],
                    "budget_fiscal_quarter": bc["quarter"],
                    "messages": [],
                }
            )
        vp = (out.get("budget_metrics") or {}).get("volume_price", {})
        tot = vp.get("total", {})
        if (
            tot.get("budget") == truth["total_budget_yuan"]
            and tot.get("spent") == truth["total_spent_yuan"]
            and tot.get("overspend") == truth["total_overspend_yuan"]
            and truth["total_spent_yuan"] in (out.get("final_answer") or "")
        ):
            budget_exact_matches += 1

    # ------------------------------------------------------------------
    # 5. 多租户与安全拦截率评测（越权查账 + RAG 隔离 + TOCTOU + 伪造令牌）
    # ------------------------------------------------------------------
    sec_cases = [c for c in dataset if c["category"] == "security"]
    sec_blocked = 0
    for sc in sec_cases:
        if sc["type"] == "cross_dept":
            ctx = await _make_ctx(repo, sc["tenant_id"], sc["user_id"])
            with bind_tenant_context(ctx):
                out = await budget_graph.ainvoke(
                    {"conversation_id": f"eval_{sc['id']}", "user_query": sc["query"], "effective_query": sc["query"], "messages": []}
                )
            if (out.get("error_info") or {}).get("code") == "RBAC_FORBIDDEN":
                sec_blocked += 1
        elif sc["type"] == "cross_tenant_rag":
            chunks = await repo.search_policy_chunks(sc["tenant_id"], sc["query"], top_k=5)
            if all(sc["expect_no_leak"] not in c["content"] for c in chunks):
                sec_blocked += 1
        elif sc["type"] == "hitl_tamper":
            ctx = await _make_ctx(repo, sc["tenant_id"], sc["user_id"])
            hitl = ToolGuardrails.build_hitl_payload("confirm", "test", "desc", "create_tuniu_booking", {"amount": 500})
            try:
                await ToolGuardrails.verify_and_audit_decision(
                    ctx, "eval_tamper", hitl.model_dump(), {"interrupt_id": hitl.interrupt_id, "action_hash": "tampered_hash", "decision": "CONFIRMED"}
                )
            except HitlGuardrailError:
                sec_blocked += 1
        elif sc["type"] == "mcp_forged_token":
            try:
                await search_flights("forged.token", "上海", "北京", "2026-12-15")
            except InternalTokenError:
                sec_blocked += 1

    # ------------------------------------------------------------------
    # 6. 熔断器稳定性验证（3 次失败转 OPEN；401 不污染其他租户）
    # ------------------------------------------------------------------
    cb = CircuitBreakerMiddleware()
    cb_pass = True

    async def _fail_500() -> None:
        raise UpstreamToolError("503", status_code=503)

    for _ in range(3):
        try:
            await cb.call("T001", "eval_cb_tool", _fail_500, is_write=True)
        except UpstreamToolError:
            pass
    try:
        await cb.call("T001", "eval_cb_tool", _fail_500, is_write=True)
        cb_pass = False
    except CircuitBreakerOpenError:
        pass

    async def _fail_401() -> None:
        raise UpstreamToolError("401", status_code=401)

    for _ in range(3):
        try:
            await cb.call("T001", "eval_tenant_cb", _fail_401, is_write=True)
        except UpstreamToolError:
            pass
    # T002 租户应仍为 CLOSED 正常调用
    t2_state = await cb.get_state("T002", "eval_tenant_cb")
    if t2_state["tenant"].get("state", "CLOSED") != "CLOSED":
        cb_pass = False

    report = {
        "1_routing": routing_report,
        "2_context_optimization": {
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "token_reduction_pct": token_reduction_pct,
        },
        "3_trip_planning": {
            "four_options_rate_pct": round(opt4_ok / len(planning_cases) * 100, 2),
            "compliant_hit_rate_pct": round(compliant_ok / len(planning_cases) * 100, 2),
            "conflict_detect_rate_pct": round(conflict_ok / max(1, conflict_total) * 100, 2),
        },
        "4_budget_accuracy": {
            "exact_decimal_match_pct": round(budget_exact_matches / len(budget_cases) * 100, 2),
            "hallucination_rate_pct": round((1 - budget_exact_matches / len(budget_cases)) * 100, 2),
        },
        "5_security_interception": {
            "interception_rate_pct": round(sec_blocked / len(sec_cases) * 100, 2),
            "blocked_count": sec_blocked,
            "total_cases": len(sec_cases),
        },
        "6_circuit_breaker_verified": cb_pass,
    }
    return report


if __name__ == "__main__":
    result = asyncio.run(evaluate_all())
    print("=== PyTravel-Harness 离线自动化评测报告 (Eval) ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    Path("eval/last_eval_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
