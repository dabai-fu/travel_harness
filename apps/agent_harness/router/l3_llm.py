"""
L3 LLM 兜底路由器 + 条件 QueryRewrite + 三层融合入口（ThreeTierIntentRouter）。

对应文档章节：五、5.1 三层意图识别短路设计
"""

from __future__ import annotations

import time

from apps.agent_harness.router.l1_regex import RegexRuleRouter, contains_coreference
from apps.agent_harness.router.l2_vector import VectorIntentRouter
from apps.agent_harness.runtime.llm_client import UnifiedLLMClient
from apps.agent_harness.state import IntentResult, IntentType

INTENT_TO_SUBGRAPH: dict[str, str] = {
    "TRIP_PLAN_BOOK": "TripPlanAndBookSubgraph",
    "POLICY_INFO": "PolicyAndInfoSubgraph",
    "EXPENSE_DRAFT": "ExpenseDraftSubgraph",
    "BUDGET_INSIGHT": "BudgetInsightSubgraph",
}


class LLMIntentRouter:
    """L3 轻量 LLM 结构化分类与按需指代改写器。"""

    def __init__(self, llm_client: UnifiedLLMClient | None = None) -> None:
        self._llm = llm_client or UnifiedLLMClient()

    async def route(
        self,
        query: str,
        history_summaries: list[str] | None = None,
    ) -> IntentResult:
        t0 = time.perf_counter()
        needs_rw = contains_coreference(query)
        effective_query = query
        if needs_rw and history_summaries:
            effective_query = await self._llm.rewrite_query(query, history_summaries)

        raw = await self._llm.classify_intent(effective_query)
        intents: list[IntentType] = raw.get("intents") or ["GENERAL_CHAT"]
        confidence = float(raw.get("confidence", 0.75))

        if len(intents) == 1 and intents[0] in INTENT_TO_SUBGRAPH:
            target = INTENT_TO_SUBGRAPH[intents[0]]
        elif len(intents) >= 2:
            target = "MasterSupervisorGraph"
        else:
            target = "MasterSupervisorGraph"

        return IntentResult(
            intents=intents,
            confidence=confidence,
            source="LLM",
            direct_route_target=target,
            rewritten_query=effective_query if effective_query != query else None,
            needs_rewrite=needs_rw,
            llm_called=True,
            latency_ms=round((time.perf_counter() - t0) * 1000, 3),
        )


class ThreeTierIntentRouter:
    """三层串行短路总路由器：L1 正则 -> L2 向量 -> L3 LLM。"""

    def __init__(self) -> None:
        self.l1 = RegexRuleRouter()
        self.l2 = VectorIntentRouter()
        self.l3 = LLMIntentRouter()

    async def route(
        self,
        query: str,
        active_subgraph: str | None = None,
        has_pending_hitl: bool = False,
        history_summaries: list[str] | None = None,
        force_llm_only: bool = False,
    ) -> IntentResult:
        if not force_llm_only:
            res_l1 = self.l1.route(query, active_subgraph=active_subgraph, has_pending_hitl=has_pending_hitl)
            if res_l1 is not None:
                return res_l1

            res_l2 = self.l2.route(query)
            if res_l2 is not None:
                return res_l2

        return await self.l3.route(query, history_summaries=history_summaries)
