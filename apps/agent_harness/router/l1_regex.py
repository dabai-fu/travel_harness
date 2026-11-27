"""
L1 正则与关键词短路路由器（目标延迟 < 50ms）。

对应文档章节：
  - 五、5.1 三层意图识别短路设计（L1 RegexRuleRouter）
  - 十一、v1.1 P1 #17（防止“快但错”的四大短路前置条件）

关键约束（v1.1 P1 #17）：
  1. 会话存在活跃子图（如等待用户从方案卡片选择“就第二个吧”）时，粘性直跳回该子图；
  2. 仅当**恰好命中 1 个意图规则**时才允许短路；若同时命中 2 个以上意图（如“订机票顺便看预算”），
     直接判定为复合意图并交给 L3 / MasterSupervisorGraph 拆解；
  3. 若用户输入包含指代/省略词（“那个”“换成”“再”“上一个”“第二个”等），绝不跳过 QueryRewritingNode。
"""

from __future__ import annotations

import re
import time

from apps.agent_harness.state import IntentResult, IntentType

_INTENT_PATTERNS: list[tuple[IntentType, str, re.Pattern[str]]] = [
    (
        "BUDGET_INSIGHT",
        "BudgetInsightSubgraph",
        re.compile(r"(预算|超支|归因|执行率|临期溢价|降本|费控分析|花了多少)"),
    ),
    (
        "EXPENSE_DRAFT",
        "ExpenseDraftSubgraph",
        re.compile(r"(报销|发票|水单|行程单|贴票|报销草稿)"),
    ),
    (
        "POLICY_INFO",
        "PolicyAndInfoSubgraph",
        re.compile(r"(差旅政策|差旅制度|差标|标准|限额|舱位要求|席别|餐补|交通补贴|天气|穿什么)"),
    ),
    (
        "TRIP_PLAN_BOOK",
        "TripPlanAndBookSubgraph",
        re.compile(r"(出差|规划行程|订机票|订酒店|订高铁|查航班|查高铁|机票比价|帮我预订|去.+出差)"),
    ),
]

_COREFERENCE_RE = re.compile(r"(那个|这个|它|换成|改为|再查|上一个|刚才|第[一二三四]个|就选)")


def contains_coreference(query: str) -> bool:
    """检测输入是否包含多轮指代或省略词，命中时不得跳过 QueryRewrite。"""
    return bool(_COREFERENCE_RE.search(query))


class RegexRuleRouter:
    """L1 高频规则短路路由器。"""

    def route(
        self,
        query: str,
        active_subgraph: str | None = None,
        has_pending_hitl: bool = False,
    ) -> IntentResult | None:
        t0 = time.perf_counter()

        # 前置条件 1：存在挂起 HITL 或活跃子图且输入像后续选择/追问时，优先粘性路由
        if has_pending_hitl and active_subgraph:
            return IntentResult(
                intents=[self._subgraph_to_intent(active_subgraph)],
                confidence=0.99,
                source="STICKY",
                direct_route_target=active_subgraph,
                needs_rewrite=False,
                llm_called=False,
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            )

        needs_rw = contains_coreference(query)
        matched: list[tuple[IntentType, str]] = []
        for intent, subgraph, pattern in _INTENT_PATTERNS:
            if pattern.search(query):
                matched.append((intent, subgraph))

        # 前置条件 2 & 3：恰好命中 1 个意图且不含指代词，才触发单意图直跳短路
        if len(matched) == 1 and not needs_rw:
            intent, subgraph = matched[0]
            return IntentResult(
                intents=[intent],
                confidence=0.95,
                source="RULE",
                direct_route_target=subgraph,
                needs_rewrite=False,
                llm_called=False,
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            )

        # 同时命中多个意图：直接标记为复合意图交给 MasterSupervisorGraph
        if len(matched) >= 2:
            return IntentResult(
                intents=[m[0] for m in matched],
                confidence=0.90,
                source="RULE",
                direct_route_target="MasterSupervisorGraph",
                needs_rewrite=needs_rw,
                llm_called=False,
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            )

        return None

    @staticmethod
    def _subgraph_to_intent(subgraph: str) -> IntentType:
        mapping: dict[str, IntentType] = {
            "TripPlanAndBookSubgraph": "TRIP_PLAN_BOOK",
            "PolicyAndInfoSubgraph": "POLICY_INFO",
            "ExpenseDraftSubgraph": "EXPENSE_DRAFT",
            "BudgetInsightSubgraph": "BUDGET_INSIGHT",
        }
        return mapping.get(subgraph, "GENERAL_CHAT")
