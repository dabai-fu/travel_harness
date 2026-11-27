"""
模块 5：长期出行偏好记忆与异步轻量体验服务。

对应文档章节：
  - 三、模块 5（UserPreferenceService / ConversationTitleService / QuestionRecommendationService）
  - 八、8.2 Redis Key 规范（pytravel:{tenant_id}:pref:{user_id} 1h 缓存）
"""

from __future__ import annotations

import json
import re

from apps.agent_harness.state import TenantContext
from apps.common.db import get_repository
from apps.common.redis_client import get_redis

PREF_CACHE_TTL_SECONDS = 3600


class UserPreferenceService:
    """员工跨会话长期偏好记忆服务（带 Redis 1h 请求级缓存防重复查库）。"""

    @staticmethod
    def _cache_key(tenant_id: str, user_id: str) -> str:
        return f"pytravel:{tenant_id}:pref:{user_id}"

    async def get_preferences(self, ctx: TenantContext) -> dict[str, str]:
        redis = get_redis()
        ckey = self._cache_key(ctx.tenant_id, ctx.user_id)
        cached = await redis.get(ckey)
        if cached:
            return dict(json.loads(cached))

        repo = get_repository()
        rows = await repo.list_user_preferences(ctx.tenant_id, ctx.user_id)
        prefs = {r["pref_key"]: r["pref_value"] for r in rows}
        await redis.set(ckey, json.dumps(prefs, ensure_ascii=False), ex=PREF_CACHE_TTL_SECONDS)
        return prefs

    async def extract_and_update_from_query(self, ctx: TenantContext, query: str) -> dict[str, str]:
        """从用户自然语言交互中提炼偏好（如航司、酒店品牌、红眼航班拒绝），写入库并刷新缓存。"""
        repo = get_repository()
        updated = False
        airlines = [a for a in ("国航", "东航", "南航", "海航") if a in query]
        if airlines:
            await repo.upsert_user_preference(ctx.tenant_id, ctx.user_id, "AIRLINE", ",".join(airlines), source="INFERRED", confidence=0.78)
            updated = True
        hotels = [h for h in ("亚朵", "全季", "万怡", "汉庭", "桔子") if h in query]
        if hotels:
            await repo.upsert_user_preference(ctx.tenant_id, ctx.user_id, "HOTEL_BRAND", ",".join(hotels), source="INFERRED", confidence=0.80)
            updated = True
        if any(k in query for k in ("不要红眼", "拒绝红眼", "不坐夜班机")):
            await repo.upsert_user_preference(ctx.tenant_id, ctx.user_id, "RED_EYE", "拒绝", source="INFERRED", confidence=0.90)
            updated = True
        if "靠窗" in query:
            await repo.upsert_user_preference(ctx.tenant_id, ctx.user_id, "TRAIN_SEAT", "靠窗", source="INFERRED", confidence=0.85)
            updated = True

        if updated:
            await get_redis().delete(self._cache_key(ctx.tenant_id, ctx.user_id))
        return await self.get_preferences(ctx)


class ConversationTitleService:
    """异步生成精简会话标题（≤18字），避免首轮对话一直显示“新对话”。"""

    @staticmethod
    async def generate_and_save(tenant_id: str, conversation_id: str, first_query: str) -> str:
        cleaned = re.sub(r"\s+", " ", first_query).strip()
        title = cleaned[:18] + ("..." if len(cleaned) > 18 else "")
        if not title:
            title = "差旅协同会话"
        await get_repository().update_conversation_title(tenant_id, conversation_id, title)
        return title


class QuestionRecommendationService:
    """每轮结束后的智能追问胶囊推荐服务（对应 SSE `suggestions` 事件）。"""

    @staticmethod
    def recommend(active_subgraph: str | None, role: str) -> list[str]:
        if active_subgraph == "TripPlanAndBookSubgraph":
            return [
                "查看北京近期天气与出行建议",
                "顺便查一下我这个职级的酒店和餐补标准",
                "帮我把上次出差的发票整理成报销草稿",
            ]
        if active_subgraph == "BudgetInsightSubgraph":
            return [
                "导出华东业务交付部临期订票明细",
                "测算将提前预订门槛从3天收紧到5天的降本空间",
                "发起部门预算超支预警 OA 通知",
            ]
        if active_subgraph == "ExpenseDraftSubgraph":
            return [
                "查看我的历史报销草稿状态",
                "一线城市住宿超标80元需要提交什么说明？",
            ]
        if role in ("DEPT_MANAGER", "FINANCE_ADMIN"):
            return [
                "帮我分析2026年Q4部门差旅预算执行与超支原因",
                "下周二从上海去北京出差3天，按公司差标帮我规划行程",
                "查询我当前职级在一线城市的住宿与机票差标",
            ]
        return [
            "下周二从上海去北京出差3天，住国贸附近，帮我规划并预订",
            "查询我当前职级去北京出差的差标限额和天气",
            "帮我把这几张出差发票整理成报销单草稿",
        ]
