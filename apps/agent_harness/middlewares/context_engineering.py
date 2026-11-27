"""
中间件 4：上下文工程优化与 Token 预算止损（ContextEngineeringMiddleware）。

对应文档章节：
  - 六、中间件 4（ContextEngineeringMiddleware）
  - 三、模块 4（大结果落盘卸载，只向 LLM 返回 artifact_id + schema + 前 5 行）
  - 十一、v1.1 P1 #23

四大核心降本提效能力：
  1. **大结果落盘卸载（Context Offloading）**：工具返回 > 4KB 时自动写入会话沙箱目录，
     只向 LLM 上下文返回 `{artifact_id, schema, row_count, sample_5_rows}`，防止大表撑爆 Token；
  2. **途牛/MCP 结果去重压缩**：裁剪航司内部营销字段、冗余图片 URL 等噪声，平均压缩 40%~60% Token；
  3. **历史 `SKILL.md` 正文折叠**：仅保留当前轮激活的完整 Skill 指令，历史轮次 Skill 正文折叠为 1 行摘要；
  4. **单次请求 Token 预算硬熔断**：累计消耗逼近或超过 `request_token_budget` 时果断止损。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from apps.common.config import get_settings


class TokenBudgetExceededError(RuntimeError):
    """单次请求消耗 Token 超过硬预算上限，防止死循环烧钱。"""


def estimate_tokens(text_or_obj: Any) -> int:
    """
    轻量级中英文混合 Token 估算器（无需联网下载 tiktoken 词表）。
    按经验规则：中文约 1.5 char/token，英文/JSON 约 3.5 char/token，综合取约 2.2 char/token。
    """
    raw = text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj, ensure_ascii=False, default=str)
    if not raw:
        return 0
    return max(1, int(len(raw) / 2.2))


class ContextEngineeringMiddleware:
    """上下文瘦身、卸载与预算控制中间件。"""

    OFFLOAD_THRESHOLD_BYTES = 4096

    def __init__(self, token_budget: int | None = None) -> None:
        settings = get_settings()
        self.token_budget = token_budget or settings.request_token_budget
        self._consumed_tokens = 0
        # 维护本会话 artifact_id -> 沙箱内安全绝对路径的映射，防止 LLM 构造路径穿越
        self._artifact_registry: dict[str, Path] = {}

    @property
    def consumed_tokens(self) -> int:
        return self._consumed_tokens

    def add_and_check_budget(self, delta_tokens: int) -> int:
        """累加本次推理/工具 Token 消耗，超过硬预算时立刻抛 TokenBudgetExceededError。"""
        self._consumed_tokens += max(0, delta_tokens)
        if self._consumed_tokens > self.token_budget:
            raise TokenBudgetExceededError(
                f"单次请求 Token 消耗 ({self._consumed_tokens}) 已超过预算上限 ({self.token_budget})，触发安全止损"
            )
        return self._consumed_tokens

    def compress_tuniu_payload(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        """
        对途牛返回的大体积航班/酒店/车次 JSON 进行字段裁剪与去重，降低 40%~60% Tool Token。
        保留行程规划所需的 core 字段，剥离 marketing_tags、raw_html、banner_urls 等冗余字段。
        """
        drop_keys = {
            "marketing_tags",
            "promo_banner_urls",
            "raw_html_desc",
            "vendor_internal_meta",
            "cancellation_long_text",
            "seat_map_matrix",
            "images",
            "amenities_full_text",
        }

        def _prune(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: _prune(v) for k, v in obj.items() if k not in drop_keys}
            if isinstance(obj, list):
                # 同时对完全相同的字符串列表项去重
                seen: set[str] = set()
                out: list[Any] = []
                for item in obj:
                    pruned = _prune(item)
                    sig = json.dumps(pruned, sort_keys=True, ensure_ascii=False, default=str)
                    if sig not in seen:
                        seen.add(sig)
                        out.append(pruned)
                return out
            return obj

        return _prune(raw_payload)

    def offload_if_large(
        self,
        payload: Any,
        workspace_dir: Path,
        prefix: str = "tool_result",
    ) -> dict[str, Any]:
        """
        若工具结果序列化后超过 4KB，则落盘至会话沙箱目录，仅向 LLM 返回精简句柄：
        `{offloaded: True, artifact_id, schema, row_count, sample_5_rows}`。
        """
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        byte_len = len(serialized.encode("utf-8"))
        if byte_len <= self.OFFLOAD_THRESHOLD_BYTES:
            return {"offloaded": False, "data": payload, "bytes": byte_len}

        workspace_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = f"art_{prefix}_{uuid.uuid4().hex[:10]}"
        target_path = (workspace_dir / f"{artifact_id}.json").resolve()
        # 严格校验 realpath 前缀，防路径穿越（v1.1 2.2 节）
        if not str(target_path).startswith(str(workspace_dir.resolve())):
            raise ValueError("非法沙箱落盘路径")

        target_path.write_text(serialized, encoding="utf-8")
        self._artifact_registry[artifact_id] = target_path

        # 提取 Schema 与前 5 行样本给 LLM，足够其理解数据结构而不占满上下文
        if isinstance(payload, list):
            row_count = len(payload)
            sample = payload[:5]
            schema = list(payload[0].keys()) if payload and isinstance(payload[0], dict) else []
        elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            rows = payload["rows"]
            row_count = len(rows)
            sample = rows[:5]
            schema = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else list(payload.keys())
        else:
            row_count = 1
            sample = [serialized[:300] + "..."]
            schema = list(payload.keys()) if isinstance(payload, dict) else [type(payload).__name__]

        return {
            "offloaded": True,
            "artifact_id": artifact_id,
            "schema": schema,
            "row_count": row_count,
            "sample_5_rows": sample,
            "original_bytes": byte_len,
        }

    def fold_historical_skills(
        self,
        messages: list[BaseMessage],
        active_skill_name: str | None,
    ) -> list[BaseMessage]:
        """
        历史 `SKILL.md` 正文折叠优化：
          - 消息 metadata 中标记 `skill_name` 的系统指令消息，若不是当前激活的 Skill，
            则将其数百行完整规范正文折叠为 1 行摘要，显著压缩多轮对话 Token。
        """
        folded: list[BaseMessage] = []
        for msg in messages:
            skill_name = msg.additional_kwargs.get("skill_name") if hasattr(msg, "additional_kwargs") else None
            if skill_name and skill_name != active_skill_name:
                summary = f"[已折叠历史技能规范: {skill_name}（本轮非激活状态，如需细节可按需重新加载）]"
                folded.append(SystemMessage(content=summary, additional_kwargs={"skill_name": skill_name, "folded": True}))
            else:
                folded.append(msg)
        return folded
