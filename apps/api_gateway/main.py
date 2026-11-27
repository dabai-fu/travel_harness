"""
FastAPI API 网关主入口（apps/api_gateway/main.py）。

对应文档章节：
  - 四、4.1 五层架构之 API 网关与接入层
  - 九、9.3 apps/api_gateway/main.py

启动命令：
  uvicorn apps.api_gateway.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.agent_harness.graphs.master import get_master_coordinator
from apps.agent_harness.middlewares.cluster_cancel import ClusterCancelMiddleware
from apps.agent_harness.middlewares.tenant_context import TenantAuthContextMiddleware
from apps.agent_harness.runtime.mcp_client import MCPClientHub
from apps.agent_harness.runtime.scheduled_jobs import BackgroundJobScheduler
from apps.agent_harness.state import TenantContext
from apps.api_gateway.auth import router as auth_router
from apps.api_gateway.routes.chat import router as chat_router
from apps.api_gateway.routes.webhook import router as webhook_router
from apps.common.config import get_settings
from apps.common.db import dispose_pg_engine, get_repository


async def _reconcile_query_status(tenant_id: str, idempotent_key: str) -> dict:
    """对账补偿回调：使用系统级内部令牌调用 tuniu-mcp 查询真实订单状态。"""
    sys_ctx = TenantContext(
        tenant_id=tenant_id,
        user_id="system_reconciler",
        dept_id="SYSTEM",
        managed_dept_ids=(),
        role="TENANT_ADMIN",
        rank_level="P10",
        rank_no=10,
        trace_id="reconcile",
    )
    token = TenantAuthContextMiddleware.issue_internal_token(sys_ctx)
    return await MCPClientHub.get_booking_status(token, idempotent_key)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 1. 初始化仓储（Memory 模式自动灌数；Postgres 模式初始化连接池）
    get_repository(auto_seed=(settings.app_storage_mode == "memory"))
    # 2. 初始化 LangGraph Checkpointer（Postgres 模式连接 AsyncPostgresSaver 建表）
    coordinator = get_master_coordinator()
    await coordinator.initialize()
    # 3. 启动集群级跨副本取消监听 & PENDING 订单对账定时任务
    cancel_mw = ClusterCancelMiddleware()
    scheduler = BackgroundJobScheduler(_reconcile_query_status)
    await cancel_mw.start_listener()
    await scheduler.start(interval_seconds=60)
    try:
        yield
    finally:
        await scheduler.stop()
        await cancel_mw.stop_listener()
        await coordinator.shutdown()
        await dispose_pg_engine()


app = FastAPI(
    title="PyTravel-Harness API Gateway",
    description="企业级多智能体差旅协同与预算归因分析系统",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": "pytravel-api-gateway",
        "version": "1.1.0",
        "storage_mode": settings.app_storage_mode,
        "redis_mode": settings.redis_mode,
        "sandbox_mode": settings.sandbox_mode,
        "mcp_mode": settings.mcp_mode,
    }
