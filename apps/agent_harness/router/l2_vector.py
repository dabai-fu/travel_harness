"""
L2 向量原型语义路由器（目标延迟 < 100ms）。

对应文档章节：
  - 五、5.1 VectorIntentRouter
  - 十一、v1.1 P1 #17（绝对阈值 >= 0.86 且 Top1 - Top2 >= 0.05 置信间隔，防止边界混淆）
"""

from __future__ import annotations

import time

from apps.agent_harness.router.l1_regex import contains_coreference
from apps.agent_harness.state import IntentResult, IntentType
from apps.common.db import compute_text_embedding, cosine_similarity

_INTENT_PROTOTYPES: list[tuple[IntentType, str, list[str]]] = [
    (
        "TRIP_PLAN_BOOK",
        "TripPlanAndBookSubgraph",
        [
            "安排下周从上海飞北京出差三天的航班和酒店方案",
            "帮我比价一下明天去深圳的高铁和住宿怎么订最省钱",
            "下周一去杭州开会两天需要帮忙安排交通和住宿",
        ],
    ),
    (
        "POLICY_INFO",
        "PolicyAndInfoSubgraph",
        [
            "请问P6职级在一线城市住宿每晚最高能花多少钱以及补贴多少",
            "公司规定飞机能坐商务舱还是经济舱，提前几天订票",
            "北京这两天气温怎么样需要带厚外套吗",
        ],
    ),
    (
        "EXPENSE_DRAFT",
        "ExpenseDraftSubgraph",
        [
            "我出差回来了把这几张电子发票和酒店水单整理进费控系统",
            "上传打车票和机票行程单帮我核对一下有没有超标",
        ],
    ),
    (
        "BUDGET_INSIGHT",
        "BudgetInsightSubgraph",
        [
            "帮我看一下本季度部门差旅开销为什么比去年多了那么多",
            "分析各团队第三四季度花费情况以及高频城市降本空间",
            "统计一下临期订票带来的额外成本占比",
        ],
    ),
]


class VectorIntentRouter:
    """基于意图原型向量的 L2 快速语义路由器。"""

    THRESHOLD = 0.86
    MIN_MARGIN = 0.05

    def __init__(self) -> None:
        self._proto_vecs: list[tuple[IntentType, str, list[list[float]]]] = [
            (intent, subgraph, [compute_text_embedding(p) for p in samples])
            for intent, subgraph, samples in _INTENT_PROTOTYPES
        ]

    def route(self, query: str) -> IntentResult | None:
        t0 = time.perf_counter()
        if contains_coreference(query):
            return None

        q_vec = compute_text_embedding(query)
        intent_scores: list[tuple[float, IntentType, str]] = []
        for intent, subgraph, vecs in self._proto_vecs:
            best_sim = max(cosine_similarity(q_vec, pv) for pv in vecs)
            intent_scores.append((best_sim, intent, subgraph))

        intent_scores.sort(key=lambda x: x[0], reverse=True)
        top1_score, top1_intent, top1_sub = intent_scores[0]
        top2_score = intent_scores[1][0] if len(intent_scores) > 1 else 0.0

        # v1.1 双条件校验：既要高置信 (>=0.86)，又要显著拉开与第二名的间隔 (>=0.05)
        if top1_score >= self.THRESHOLD and (top1_score - top2_score) >= self.MIN_MARGIN:
            return IntentResult(
                intents=[top1_intent],
                confidence=round(top1_score, 4),
                source="VECTOR",
                direct_route_target=top1_sub,
                needs_rewrite=False,
                llm_called=False,
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            )
        return None
