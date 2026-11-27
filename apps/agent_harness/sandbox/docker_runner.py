"""
租户隔离 Python 沙箱执行器（DockerRunner）。

对应文档章节：
  - 二、2.2 四层隔离之沙箱与工作区物理隔离层
  - 九、9.3 sandbox/docker_runner.py
  - 十一、v1.1 P1 #23（使用 conversation_id 建目录、LLM 只见 artifact_id、受信任脚本走预热池）

生产级三模式执行引擎：
  1. `SANDBOX_MODE=docker` + 配置了 `SANDBOX_WARM_CONTAINER`（如 `pytravel-sandbox-pool`）：
     使用 `docker exec` 直接在常驻禁网预热容器池内执行内置受信任脚本 `/opt/budget_calc.py`，
     消除 1~3s 冷启动开销，并通过共享命名卷 `/workspace` 实现宿主机与容器零路径错位；
  2. `SANDBOX_MODE=docker`（无预热容器，或一次性隔离任务）：
     使用 `docker run --rm --network=none --cpus=1 --memory=512m --pids-limit=64 --read-only --tmpfs /tmp:size=64m`
     配合命名卷 `SANDBOX_DOCKER_VOLUME:/workspace` 拉起强隔离一次性容器；
  3. `SANDBOX_MODE=local`：
     无 Docker 环境时在隔离工作目录拉起独立 Python 子进程执行同一份脚本，受 60s 超时约束。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from apps.common.config import get_settings

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BUDGET_CALC_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "budget-fpa-attribution"
    / "scripts"
    / "budget_calc.py"
)


class SandboxSecurityError(PermissionError):
    """沙箱路径穿越、越权租户目录访问或非法脚本调用异常。"""


class SandboxExecutionError(RuntimeError):
    """沙箱脚本执行超时或非零退出异常。"""


class DockerSandboxRunner:
    """租户物理隔离沙箱执行器，维护 artifact_id -> 安全文件路径映射。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._artifacts: dict[tuple[str, str, str], Path] = {}

    @staticmethod
    def _sanitize_segment(seg: str, label: str) -> str:
        if not _SAFE_ID_RE.match(seg):
            raise SandboxSecurityError(f"非法 {label} 标识符（禁止冒号或特殊路径字符）: {seg!r}")
        return seg

    def get_workspace_dir(self, tenant_id: str, conversation_id: str) -> Path:
        """
        返回并创建租户会话独占目录：`{root}/{tenant_id}/{conversation_id}`。
        v1.1 P1 #23：不再用含冒号的 thread_id 作为目录名，避免跨平台文件系统报错。
        """
        safe_tid = self._sanitize_segment(tenant_id, "tenant_id")
        safe_cid = self._sanitize_segment(conversation_id, "conversation_id")
        root = self.settings.sandbox_root_path
        ws = (root / safe_tid / safe_cid).resolve()
        if not str(ws).startswith(str(root)):
            raise SandboxSecurityError("沙箱工作区路径越界")
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def register_artifact(
        self,
        tenant_id: str,
        conversation_id: str,
        filename: str,
        content: str,
    ) -> str:
        """向当前会话沙箱写入文件并颁发 `artifact_id` 句柄。"""
        ws = self.get_workspace_dir(tenant_id, conversation_id)
        safe_name = Path(filename).name
        target = (ws / safe_name).resolve()
        if not str(target).startswith(str(ws)):
            raise SandboxSecurityError("检测到文件路径穿越企图")
        target.write_text(content, encoding="utf-8")
        artifact_id = f"art_{uuid.uuid4().hex[:10]}"
        self._artifacts[(tenant_id, conversation_id, artifact_id)] = target
        return artifact_id

    def resolve_artifact_path(self, tenant_id: str, conversation_id: str, artifact_id: str) -> Path:
        """将 artifact_id 解析为当前租户沙箱内的真实 Path，禁止跨租户或跨会话引用。"""
        path = self._artifacts.get((tenant_id, conversation_id, artifact_id))
        if path is None:
            raise SandboxSecurityError(f"未知或跨租户的 artifact_id: {artifact_id}")
        ws = self.get_workspace_dir(tenant_id, conversation_id)
        resolved = path.resolve()
        if not str(resolved).startswith(str(ws)):
            raise SandboxSecurityError("artifact 路径越界")
        return resolved

    def build_docker_command(
        self,
        tenant_id: str,
        conversation_id: str,
        mode: str,
        expense_csv_name: str,
        budget_csv_name: str,
        out_json_name: str,
        fiscal_year: int,
        fiscal_quarter: int,
    ) -> list[str]:
        """构造 Docker 沙箱执行命令（优先预热容器池 docker exec，其次一次性强隔离容器）。"""
        container_ws = f"/workspace/{tenant_id}/{conversation_id}"
        script_args = [
            "python",
            "/opt/budget_calc.py",
            "--mode",
            mode,
            "--expense-csv",
            f"{container_ws}/{expense_csv_name}",
            "--budget-csv",
            f"{container_ws}/{budget_csv_name}",
            "--year",
            str(fiscal_year),
            "--quarter",
            str(fiscal_quarter),
            "--output",
            f"{container_ws}/{out_json_name}",
        ]

        if self.settings.sandbox_warm_container:
            # v1.1 P1 #23：受信任脚本走常驻禁网预热容器池，零冷启动延迟
            return ["docker", "exec", self.settings.sandbox_warm_container, *script_args]

        ws = self.get_workspace_dir(tenant_id, conversation_id)
        volume_spec = (
            f"{self.settings.sandbox_docker_volume}:/workspace"
            if self.settings.sandbox_docker_volume
            else f"{ws}:{container_ws}"
        )
        return [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--cpus=1",
            "--memory=512m",
            "--pids-limit=64",
            "--read-only",
            "--tmpfs",
            "/tmp:size=64m",
            "-v",
            volume_spec,
            "-v",
            f"{BUDGET_CALC_SCRIPT}:/opt/budget_calc.py:ro",
            self.settings.sandbox_docker_image,
            *script_args,
        ]

    async def run_budget_script(
        self,
        tenant_id: str,
        conversation_id: str,
        mode: str,
        expense_artifact_id: str,
        budget_artifact_id: str,
        fiscal_year: int,
        fiscal_quarter: int,
    ) -> dict[str, Any]:
        """
        在沙箱内执行内置受信任脚本 `budget_calc.py` 并返回解析后的精确 JSON 指标。
        """
        ws = self.get_workspace_dir(tenant_id, conversation_id)
        expense_csv = self.resolve_artifact_path(tenant_id, conversation_id, expense_artifact_id)
        budget_csv = self.resolve_artifact_path(tenant_id, conversation_id, budget_artifact_id)
        out_json = (ws / f"out_{mode}_{uuid.uuid4().hex[:8]}.json").resolve()

        if self.settings.sandbox_mode == "docker":
            cmd = self.build_docker_command(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                mode=mode,
                expense_csv_name=expense_csv.name,
                budget_csv_name=budget_csv.name,
                out_json_name=out_json.name,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
            )
        else:
            cmd = [
                sys.executable,
                str(BUDGET_CALC_SCRIPT),
                "--mode",
                mode,
                "--expense-csv",
                str(expense_csv),
                "--budget-csv",
                str(budget_csv),
                "--year",
                str(fiscal_year),
                "--quarter",
                str(fiscal_quarter),
                "--output",
                str(out_json),
            ]

        def _exec() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                cwd=str(ws),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.settings.sandbox_timeout_seconds,
                check=False,
            )

        proc = await asyncio.to_thread(_exec)
        if proc.returncode != 0:
            raise SandboxExecutionError(
                f"沙箱脚本执行失败 (exit={proc.returncode}): {proc.stderr or proc.stdout}"
            )
        return json.loads(out_json.read_text(encoding="utf-8"))


_runner_singleton: DockerSandboxRunner | None = None


def get_sandbox_runner() -> DockerSandboxRunner:
    global _runner_singleton
    if _runner_singleton is None:
        _runner_singleton = DockerSandboxRunner()
    return _runner_singleton
