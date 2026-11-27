"""
Markdown Skills 渐进式加载器（Progressive Disclosure）。

对应文档章节：
  - 七、7.3 Markdown Skills 渐进式加载设计
  - 六、中间件 4（历史 Skill 正文折叠优化）

设计收益：
  初始 System Prompt 仅注入每个 Skill 的 `name` + `1 句话触发条件`（极少 Token）；
  只有当三层路由命中具体意图时，才按需读取完整 `SKILL.md` 正文与关联脚本路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    trigger: str
    file_path: Path


class SkillLoader:
    """渐进式 Markdown Skill 加载器。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or SKILLS_ROOT
        self._catalog: dict[str, SkillMetadata] = {}
        self._scan_catalog()

    def _scan_catalog(self) -> None:
        if not self.root.exists():
            return
        for skill_md in self.root.glob("*/SKILL.md"):
            name = skill_md.parent.name
            trigger = ""
            lines = skill_md.read_text(encoding="utf-8").splitlines()
            for line in lines:
                if line.startswith("trigger:"):
                    trigger = line.split(":", maxsplit=1)[1].strip().strip('"').strip("'")
                    break
            if not trigger:
                trigger = f"用于处理 {name} 相关差旅协同任务。"
            self._catalog[name] = SkillMetadata(name=name, trigger=trigger, file_path=skill_md)

    def get_index_prompt(self) -> str:
        """返回供初始 System Prompt 使用的极简元数据索引（仅 name + 1 句话触发条件）。"""
        lines = ["可用专业技能清单（按需加载）："]
        for meta in sorted(self._catalog.values(), key=lambda m: m.name):
            lines.append(f"- `{meta.name}`: {meta.trigger}")
        return "\n".join(lines)

    def load_skill_body(self, skill_name: str) -> str:
        """按需加载目标 Skill 的完整 Markdown 正文。"""
        meta = self._catalog.get(skill_name)
        if not meta:
            raise KeyError(f"未找到名为 {skill_name} 的 Skill")
        return meta.file_path.read_text(encoding="utf-8")
