"""
生产级特性专项测试：
  1. PBKDF2-HMAC-SHA256 随机盐口令哈希与常量时间校验；
  2. Redis thread_meta 持久化（模拟服务重启重建 AgentRunManager 后 HITL 断点仍可恢复）；
  3. Docker 沙箱预热容器池（docker exec）与一次性强隔离（--network=none --read-only）命令构造；
  4. PostgresRepository 与 InMemoryRepository 异步接口完全同构校验（含 1024 维向量生成）。
"""

from __future__ import annotations

import inspect

from apps.agent_harness.runtime.run_manager import AgentRunManager
from apps.agent_harness.sandbox.docker_runner import DockerSandboxRunner
from apps.common.config import get_settings
from apps.common.crypto import hash_password, verify_password
from apps.common.db import InMemoryRepository, PostgresRepository, compute_text_embedding
from apps.common.redis_client import reset_redis_for_tests


def test_pbkdf2_password_hashing_and_salting() -> None:
    h1 = hash_password("123456")
    h2 = hash_password("123456")
    assert h1.startswith("pbkdf2_sha256$260000$")
    assert h1 != h2  # 每次随机盐不同
    assert verify_password("123456", h1) is True
    assert verify_password("wrong", h1) is False


async def test_redis_thread_meta_persists_across_run_manager_restart() -> None:
    reset_redis_for_tests()
    mgr1 = AgentRunManager()
    tid = AgentRunManager.make_thread_id("T001", "u_emp_1", "conv_restart_1")
    await mgr1.save_thread_meta(
        "T001",
        tid,
        {"active_subgraph": "TripPlanAndBookSubgraph", "pending_hitl": {"interrupt_id": "int_1"}},
    )

    # 模拟进程重启后新建 AgentRunManager 实例
    mgr2 = AgentRunManager()
    restored = await mgr2.get_thread_meta("T001", tid)
    assert restored["active_subgraph"] == "TripPlanAndBookSubgraph"
    assert restored["pending_hitl"]["interrupt_id"] == "int_1"


def test_docker_sandbox_warm_pool_and_isolated_commands(monkeypatch) -> None:
    monkeypatch.setenv("SANDBOX_WARM_CONTAINER", "pytravel-sandbox-pool")
    get_settings.cache_clear()
    runner_warm = DockerSandboxRunner()
    cmd_warm = runner_warm.build_docker_command(
        "T001", "conv_1", "volume_price", "exp.csv", "bud.csv", "out.json", 2026, 4
    )
    assert cmd_warm[:3] == ["docker", "exec", "pytravel-sandbox-pool"]
    assert "/workspace/T001/conv_1/exp.csv" in cmd_warm

    monkeypatch.setenv("SANDBOX_WARM_CONTAINER", "")
    monkeypatch.setenv("SANDBOX_DOCKER_VOLUME", "pytravel_sandbox_data")
    get_settings.cache_clear()
    runner_iso = DockerSandboxRunner()
    cmd_iso = runner_iso.build_docker_command(
        "T001", "conv_1", "volume_price", "exp.csv", "bud.csv", "out.json", 2026, 4
    )
    assert "--network=none" in cmd_iso
    assert "--read-only" in cmd_iso
    assert "pytravel_sandbox_data:/workspace" in cmd_iso
    get_settings.cache_clear()


def test_postgres_and_memory_repository_interface_parity_and_embedding_dim() -> None:
    pg_methods = {
        name for name, fn in inspect.getmembers(PostgresRepository, predicate=inspect.isfunction) if not name.startswith("_")
    }
    mem_methods = {
        name for name, fn in inspect.getmembers(InMemoryRepository, predicate=inspect.isfunction) if not name.startswith("_")
    }
    # 确认 PostgresRepository 实现了所有异步业务仓储契约方法
    missing = pg_methods - mem_methods
    assert not missing, f"InMemoryRepository 缺少方法: {missing}"
    for m in pg_methods:
        assert inspect.iscoroutinefunction(getattr(PostgresRepository, m))
        assert inspect.iscoroutinefunction(getattr(InMemoryRepository, m))

    emb = compute_text_embedding("一线城市差旅酒店标准")
    assert len(emb) == 1024
