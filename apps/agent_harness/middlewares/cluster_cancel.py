"""
中间件 5：集群级跨副本优雅中断（ClusterCancelMiddleware）。

对应文档章节：
  - 六、中间件 5（ClusterCancelMiddleware）
  - 八、8.2 Redis Key 规范（cancel Pub/Sub 频道 + cancel_flag 兜底 Key）
  - 十一、v1.1 P1 #16

核心工程设计：
  1. 单连接模式匹配订阅：每个服务副本启动时仅维持 1 条 `PSUBSCRIBE pytravel:*:cancel:*` 订阅连接，
     而不是每个任务开一条订阅，节省大量 Redis 连接；
  2. 双保险防丢失（弥补 Pub/Sub 即发即弃缺陷）：
     用户点“停止”时，先写 `SET pytravel:{tenant_id}:cancel_flag:{thread_id} 1 EX 600`，再 `PUBLISH`；
     即使消息到达时任务刚好尚未注册或网络闪断，节点在 super-step 边界和工具调用前检查 `cancel_flag` 也会立即优雅退出；
  3. 命中本副本注册表中的 `asyncio.Task` 时立即调用 `task.cancel()`。
"""

from __future__ import annotations

import asyncio
import contextlib

from apps.common.redis_client import get_redis

CANCEL_FLAG_TTL_SECONDS = 600


class RunCancelledError(asyncio.CancelledError):
    """用户主动取消或跨节点取消触发的优雅退出异常。"""


def make_cancel_channel(tenant_id: str, thread_id: str) -> str:
    return f"pytravel:{tenant_id}:cancel:{thread_id}"


def make_cancel_flag_key(tenant_id: str, thread_id: str) -> str:
    return f"pytravel:{tenant_id}:cancel_flag:{thread_id}"


class ClusterCancelMiddleware:
    """支持多副本横向扩展的任务注册与跨进程优雅取消中间件。"""

    def __init__(self) -> None:
        self._local_tasks: dict[str, asyncio.Task[object]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._redis = get_redis()

    def register_task(self, tenant_id: str, thread_id: str, task: asyncio.Task[object]) -> None:
        """将当前副本正在执行的 Run Task 注册到本地表。"""
        self._local_tasks[f"{tenant_id}:{thread_id}"] = task

    def unregister_task(self, tenant_id: str, thread_id: str) -> None:
        self._local_tasks.pop(f"{tenant_id}:{thread_id}", None)

    async def start_listener(self) -> None:
        """启动副本级单连接模式订阅监听协程。"""
        if self._listener_task is not None and not self._listener_task.done():
            return
        self._listener_task = asyncio.create_task(self._listen_loop(), name="cluster-cancel-psub")

    async def stop_listener(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

    async def _listen_loop(self) -> None:
        try:
            async for msg in self._redis.psubscribe_iter("pytravel:*:cancel:*"):
                channel = msg.get("channel", "")
                # channel 格式: pytravel:{tenant_id}:cancel:{thread_id}
                parts = channel.split(":", maxsplit=3)
                if len(parts) == 4:
                    tenant_id, thread_id = parts[1], parts[3]
                    task = self._local_tasks.get(f"{tenant_id}:{thread_id}")
                    if task and not task.done():
                        task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 订阅异常退出时保留 cancel_flag 兜底，不拖垮主进程
            return

    async def request_cancel(self, tenant_id: str, thread_id: str) -> bool:
        """
        触发取消指令（先写 600s 取消标记，再发布 Pub/Sub 广播）。
        返回是否在当前副本立刻命中并取消了本地 Task。
        """
        flag_key = make_cancel_flag_key(tenant_id, thread_id)
        channel = make_cancel_channel(tenant_id, thread_id)
        await self._redis.set(flag_key, "1", ex=CANCEL_FLAG_TTL_SECONDS)
        await self._redis.publish(channel, "CANCEL")

        task = self._local_tasks.get(f"{tenant_id}:{thread_id}")
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def clear_cancel_flag(self, tenant_id: str, thread_id: str) -> None:
        await self._redis.delete(make_cancel_flag_key(tenant_id, thread_id))

    async def check_cancelled(self, tenant_id: str, thread_id: str) -> None:
        """节点边界与工具调用前兜底检查：若存在取消标记则立即中断执行。"""
        flag = await self._redis.get(make_cancel_flag_key(tenant_id, thread_id))
        if flag == "1":
            raise RunCancelledError(f"会话 {thread_id} 已被用户取消")
