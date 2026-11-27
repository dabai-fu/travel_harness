"""
子图 2：差旅政策与出行信息双通道问答（PolicyAndInfoSubgraph）。

对应文档章节：三、模块 2（通道 A 结构化精确差标 + 通道 B 租户预过滤 RAG + weather-mcp）
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from apps.agent_harness.middlewares.tenant_context import (
    TenantAuthContextMiddleware,
    require_tenant_context,
)
from apps.agent_harness.runtime.llm_client import UnifiedLLMClient
from apps.agent_harness.runtime.mcp_client import MCPClientHub
from apps.agent_harness.state import BaseAgentState
from apps.common.db import get_repository

_CITY_TO_TIER = {
    "北京": "TIER_1",
    "上海": "TIER_1",
    "深圳": "TIER_1",
    "广州": "TIER_1",
    "杭州": "TIER_2",
    "成都": "TIER_2",
    "南京": "TIER_2",
}


def _detect_city_and_tier(query: str, default_city: str = "北京") -> tuple[str, str]:
    for city, tier in _CITY_TO_TIER.items():
        if city in query:
            return city, tier
    if "二线" in query:
        return "杭州", "TIER_2"
    if "三线" in query:
        return "苏州", "TIER_3"
    return default_city, _CITY_TO_TIER.get(default_city, "TIER_1")


async def channel_a_structured_policy(state: BaseAgentState) -> dict[str, Any]:
    """通道 A：自动绑定 TenantContext.rank_no 精确查 travel_policy_rule，绝不依赖 RAG 猜数字。"""
    ctx = require_tenant_context()
    query = state.get("effective_query") or state.get("user_query", "")
    city, tier = _detect_city_and_tier(query)
    repo = get_repository()
    rule = await repo.get_policy_rule(ctx.tenant_id, ctx.rank_no, tier)
    return {
        "trip_slots": {**(state.get("trip_slots") or {}), "policy_city": city, "policy_tier": tier, "policy_rule": rule},
        "step_count": 1,
    }


async def channel_b_rag_and_weather(state: BaseAgentState) -> dict[str, Any]:
    """通道 B：租户 Pre-filtering RAG 制度检索 + weather-mcp 目的地天气。"""
    ctx = require_tenant_context()
    query = state.get("effective_query") or state.get("user_query", "")
    slots = state.get("trip_slots") or {}
    city = str(slots.get("policy_city") or "北京")

    repo = get_repository()
    q_emb = await UnifiedLLMClient().compute_embedding(query)
    chunks = await repo.search_policy_chunks(ctx.tenant_id, query, top_k=2, query_embedding=q_emb)

    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    weather = await MCPClientHub.get_city_weather_forecast(token, city=city)

    rule = slots.get("policy_rule")
    lines = [f"### 差旅政策与出行指南（职级 {ctx.rank_level} · {city} / {slots.get('policy_tier')}）\n"]
    if rule:
        lines.append(
            f"- **结构化精确差标（通道 A）**：\n"
            f"  - 飞机舱位上限：`{rule['flight_cabin']}` | 高铁席别上限：`{rule['train_seat']}`\n"
            f"  - 酒店每晚限额：`{rule['hotel_limit_per_night']} 元` | 餐补：`{rule['meal_allowance_per_day']} 元/天` | 市内交通：`{rule['local_transport_per_day']} 元/天`\n"
            f"  - 提前预订要求：至少提前 `{rule['min_advance_booking_days']} 天` | 升级审批阈值：`{rule['approval_threshold']} 元`"
        )
    if chunks:
        lines.append("\n- **公司制度条款检索（通道 B · 租户隔离 RAG）**：")
        for c in chunks:
            lines.append(f"  - [{c['doc_scope']}] **{c['title_path']}**：{c['content']}")
    lines.append(
        f"\n- **{city} 近期天气与出行提醒**：{weather['condition']}，气温 `{weather['temp_c']}`，{weather['wind']}。{weather['advice']}"
    )

    return {
        "final_answer": "\n".join(lines),
        "active_subgraph": "PolicyAndInfoSubgraph",
        "step_count": 1,
        "total_tokens": 180,
    }


def build_policy_info_subgraph(checkpointer: Any = None):
    builder = StateGraph(BaseAgentState)
    builder.add_node("channel_a_policy", channel_a_structured_policy)
    builder.add_node("channel_b_rag_weather", channel_b_rag_and_weather)
    builder.add_edge(START, "channel_a_policy")
    builder.add_edge("channel_a_policy", "channel_b_rag_weather")
    builder.add_edge("channel_b_rag_weather", END)
    return builder.compile(checkpointer=checkpointer)
