"""
主控编排图（MasterSupervisorGraph）与生产级 Checkpointer 管理。

对应文档章节：
  - 四、4.1 五层系统架构图（ROUTER -> 单意图直跳子图 / 复合意图 -> MasterSupervisorGraph）
  - 八、8.1 注释（LangGraph AsyncPostgresSaver 在 PostgreSQL 自动维护 checkpoints / checkpoint_writes / checkpoint_blobs）
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from apps.agent_harness.graphs.budget_insight import build_budget_insight_subgraph
from apps.agent_harness.graphs.expense_draft import build_expense_draft_subgraph
from apps.agent_harness.graphs.policy_info import build_policy_info_subgraph
from apps.agent_harness.graphs.trip_plan_book import build_trip_plan_book_subgraph
from apps.agent_harness.router.l3_llm import INTENT_TO_SUBGRAPH, ThreeTierIntentRouter
from apps.agent_harness.state import IntentResult
from apps.common.config import get_settings


class MasterSupervisorCoordinator:
    """
    统一持有 Checkpointer、三层路由器与 4 个专家子图的编译实例。
    生产模式（APP_STORAGE_MODE=postgres）下自动初始化 `AsyncPostgresSaver`，
    实现 HITL 断点与 OA Webhook 审批等待跨进程、跨重启持久化恢复。
    """

    def __init__(self) -> None:
        self.checkpointer: Any = MemorySaver()
        self._pg_pool: Any = None
        self._initialized: bool = False
        self.router = ThreeTierIntentRouter()
        self.subgraphs = self._compile_subgraphs(self.checkpointer)

    @staticmethod
    def _compile_subgraphs(checkpointer: Any) -> dict[str, Any]:
        return {
            "TripPlanAndBookSubgraph": build_trip_plan_book_subgraph(checkpointer=checkpointer),
            "PolicyAndInfoSubgraph": build_policy_info_subgraph(checkpointer=checkpointer),
            "ExpenseDraftSubgraph": build_expense_draft_subgraph(checkpointer=checkpointer),
            "BudgetInsightSubgraph": build_budget_insight_subgraph(checkpointer=checkpointer),
        }

    async def initialize(self) -> None:
        """
        在 FastAPI Lifespan 启动时初始化生产级 Checkpointer：
          - `APP_STORAGE_MODE=postgres`：使用 psycopg AsyncConnectionPool + AsyncPostgresSaver，
            并自动执行 `await checkpointer.setup()` 初始化断点持久化表；
          - `APP_STORAGE_MODE=memory`：保留内存 MemorySaver。
        """
        if self._initialized:
            return
        settings = get_settings()
        if settings.app_storage_mode == "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            conninfo = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
            self._pg_pool = AsyncConnectionPool(
                conninfo=conninfo,
                max_size=10,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await self._pg_pool.open()
            pg_saver = AsyncPostgresSaver(self._pg_pool)
            await pg_saver.setup()
            self.checkpointer = pg_saver
            self.subgraphs = self._compile_subgraphs(self.checkpointer)
        self._initialized = True

    async def shutdown(self) -> None:
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None
        self._initialized = False

    async def route_query(
        self,
        query: str,
        active_subgraph: str | None = None,
        has_pending_hitl: bool = False,
        history_summaries: list[str] | None = None,
    ) -> IntentResult:
        return await self.router.route(
            query=query,
            active_subgraph=active_subgraph,
            has_pending_hitl=has_pending_hitl,
            history_summaries=history_summaries,
        )

    def resolve_target_subgraphs(self, intent_res: IntentResult) -> list[str]:
        """根据 IntentResult 决定是单意图直跳还是复合多子图串联。"""
        if intent_res.direct_route_target in self.subgraphs:
            return [intent_res.direct_route_target]
        targets = [INTENT_TO_SUBGRAPH[i] for i in intent_res.intents if i in INTENT_TO_SUBGRAPH]
        return targets


_coordinator_singleton: MasterSupervisorCoordinator | None = None


def get_master_coordinator() -> MasterSupervisorCoordinator:
    global _coordinator_singleton
    if _coordinator_singleton is None:
        _coordinator_singleton = MasterSupervisorCoordinator()
    return _coordinator_singleton
