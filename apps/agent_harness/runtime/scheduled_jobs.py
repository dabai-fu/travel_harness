"""
后台定时运维任务（PENDING 订单对账补偿 + 沙箱临时文件清理）。

对应文档章节：
  - 六、中间件 6（定时对账任务扫描超时 PENDING 并调 get_booking_status 补偿）
  - 九、9.3 runtime/scheduled_jobs.py
  - 十一、v1.1 P0 #2 & P1 #23
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from typing import Any

from apps.common.config import get_settings
from apps.common.db import get_repository


async def reconcile_pending_bookings(
    query_status_fn: Callable[[str, str], Awaitable[dict[str, Any]]],
    older_than_seconds: int = 120,
) -> dict[str, int]:
    """
    扫描超过阈值的 PENDING 状态 booking_record，调用途牛 `get_booking_status` 对账补偿。
    解决“途牛侧已扣款成功但本进程写入前崩溃”的双写不一致问题。
    """
    repo = get_repository()
    stale_rows = await repo.list_stale_pending_bookings(older_than_seconds=older_than_seconds)
    stats = {"scanned": len(stale_rows), "confirmed": 0, "failed": 0, "still_pending": 0}

    for row in stale_rows:
        try:
            res = await query_status_fn(row["tenant_id"], row["idempotent_key"])
            remote_status = str(res.get("status", "UNKNOWN")).upper()
            if remote_status == "CONFIRMED":
                await repo.update_booking_status(
                    row["tenant_id"],
                    row["id"],
                    "CONFIRMED",
                    external_order_no=res.get("external_order_no"),
                    raw_detail_patch={"reconciled": True},
                )
                stats["confirmed"] += 1
            elif remote_status in ("FAILED", "NOT_FOUND"):
                await repo.update_booking_status(
                    row["tenant_id"],
                    row["id"],
                    "FAILED",
                    raw_detail_patch={"reconciled": True, "reason": remote_status},
                )
                stats["failed"] += 1
            else:
                stats["still_pending"] += 1
        except Exception:
            stats["still_pending"] += 1
    return stats


def cleanup_expired_sandbox_workspaces(max_age_seconds: int = 3600 * 12) -> int:
    """定时清理超过 max_age_seconds 未修改的会话沙箱临时 CSV/图表目录。"""
    settings = get_settings()
    root = settings.sandbox_root_path
    now = time.time()
    removed = 0
    if not root.exists():
        return 0
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir():
            continue
        for conv_dir in tenant_dir.iterdir():
            if not conv_dir.is_dir():
                continue
            mtime = conv_dir.stat().st_mtime
            if now - mtime > max_age_seconds:
                shutil.rmtree(conv_dir, ignore_errors=True)
                removed += 1
    return removed


class BackgroundJobScheduler:
    """轻量化后台定时调度器，挂载在 FastAPI Lifespan 中运行。"""

    def __init__(self, query_status_fn: Callable[[str, str], Awaitable[dict[str, Any]]]) -> None:
        self._query_status_fn = query_status_fn
        self._task: asyncio.Task[None] | None = None

    async def start(self, interval_seconds: int = 60) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(interval_seconds), name="pytravel-reconciler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self, interval_seconds: int) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            with contextlib.suppress(Exception):
                await reconcile_pending_bookings(self._query_status_fn, older_than_seconds=120)
                cleanup_expired_sandbox_workspaces()
