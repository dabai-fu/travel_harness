"""
统一 LLM / VLM 调用客户端（支持真实 OpenAI 兼容协议与零成本确定性 Mock 模式）。

对应文档章节：九、9.1 技术栈清单（DashScope / DeepSeek OpenAI 兼容协议）
设计说明：
  - LLM_MODE=openai：通过 `langchain_openai.ChatOpenAI` 调用通义千问（qwen-max/qwen-turbo/qwen-vl-max）或 DeepSeek；
  - LLM_MODE=mock：提供零延迟、100% 可复现的本地推理（含意图分类、槽位提取、发票多模态模拟、
    以及严格仅输出指标占位符的预算归因报告生成），确保本地调试与 CI/Eval 无需真实 API Key。
"""

from __future__ import annotations

import json
import re
from typing import Any

from apps.common.config import get_settings


class UnifiedLLMClient:
    """统一封装文本对话、结构化输出与多模态识票能力。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _is_real_openai(self) -> bool:
        return self.settings.llm_mode == "openai" and bool(self.settings.openai_api_key)

    async def classify_intent(self, query: str) -> dict[str, Any]:
        """L3 兜底轻量意图分类器（返回 intents 与 confidence）。"""
        if self._is_real_openai():
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model=self.settings.llm_fast_model,
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                temperature=0.0,
            )
            prompt = (
                "请将用户差旅请求分类为以下意图之一或多个（JSON 返回 {\"intents\": [...], \"confidence\": 0.9}）：\n"
                "- TRIP_PLAN_BOOK: 规划行程、比价、订机票/高铁/酒店\n"
                "- POLICY_INFO: 差旅政策、差标限额、天气出行指南\n"
                "- EXPENSE_DRAFT: 上传发票、报销草稿\n"
                "- BUDGET_INSIGHT: 部门预算执行、超支归因、降本测算\n"
                "- GENERAL_CHAT: 闲聊或无法归类\n"
                f"用户输入：{query}"
            )
            resp = await llm.ainvoke(prompt)
            try:
                text = str(resp.content).strip()
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    return json.loads(m.group(0))
            except Exception:
                pass

        # Mock 模式：基于语义关键词组合给出高准确度分类
        intents: list[str] = []
        if any(k in query for k in ("出差", "机票", "高铁", "火车", "酒店", "行程", "预订", "订票", "去北京", "去上海", "去深圳")):
            intents.append("TRIP_PLAN_BOOK")
        if any(k in query for k in ("政策", "差标", "标准", "限额", "报销制度", "天气", "穿衣", "补贴", "舱位")):
            intents.append("POLICY_INFO")
        if any(k in query for k in ("报销", "发票", "水单", "行程单", "报销单")):
            intents.append("EXPENSE_DRAFT")
        if any(k in query for k in ("预算", "超支", "归因", "执行率", "临期", "降本", "花哪", "费用分析")):
            intents.append("BUDGET_INSIGHT")
        if not intents:
            intents = ["GENERAL_CHAT"]
        return {"intents": intents, "confidence": 0.91 if intents[0] != "GENERAL_CHAT" else 0.72}

    async def rewrite_query(self, query: str, history_summaries: list[str]) -> str:
        """多轮指代消解：将“那个”“换成高铁”“第二个”等省略句补全为自包含查询。"""
        if not history_summaries:
            return query
        last_ctx = history_summaries[-1]
        if self._is_real_openai():
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model=self.settings.llm_fast_model,
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                temperature=0.0,
            )
            resp = await llm.ainvoke(
                f"上轮摘要：{last_ctx}\n当前追问：{query}\n请改写为一句语义完整、无指代省略的独立查询，只输出改写后的句子。"
            )
            return str(resp.content).strip() or query

        return f"{query}（结合上文：{last_ctx[:80]}）"

    async def generate_budget_placeholder_report(
        self,
        dept_ids: list[str],
        dept_names: dict[str, str],
        qualitative_context: dict[str, Any],
    ) -> str:
        """
        为 BudgetInsightSubgraph 生成“纯占位符”管理层归因报告（落实 v1.1 P1 #10）。

        核心铁律：
          大模型**禁止在报告中直接书写任何金额或百分比数字**，只能引用 `{{...}}` 占位符，
          随后由纯 Python 从精确的 `budget_metrics` 渲染真实 Decimal 数值。
          这样从源头杜绝 LLM 抄错数字、凑整数或产生数字幻觉。
        """
        if self._is_real_openai():
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model=self.settings.llm_main_model,
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                temperature=0.1,
            )
            dept_placeholders = "\n".join(
                f"- {dept_names.get(d, d)}({d}): 预算={{{{dept.{d}.budget}}}}, 实际={{{{dept.{d}.spent}}}}, "
                f"执行率={{{{dept.{d}.exec_rate}}}}, 超支/节余={{{{dept.{d}.overspend}}}}"
                for d in dept_ids
            )
            prompt = (
                "你是企业财务 BP 分析师。请根据以下部门列表撰写差旅预算执行与超支归因分析报告。\n"
                "【严格约束】：绝对不允许自己手写任何具体金额数字或百分比数字，所有指标必须原样使用下方占位符：\n"
                "全局占位符：{{total.budget}}, {{total.spent}}, {{total.exec_rate}}, {{total.overspend}}, "
                "{{vp.volume_effect}}, {{vp.price_effect}}, {{vp.total_delta}}, "
                "{{behavior.late_ratio}}, {{behavior.late_premium}}, {{behavior.hotel_exceed_ratio}}, "
                "{{saving.top_city}}, {{saving.estimated_saving}}\n"
                f"部门占位符：\n{dept_placeholders}\n"
                f"定性参考事实：{json.dumps(qualitative_context, ensure_ascii=False)}"
            )
            resp = await llm.ainvoke(prompt)
            return str(resp.content).strip()

        # Mock 模式：输出结构清晰、100% 使用占位符的财务分析报告模板
        dept_lines = []
        for d in dept_ids:
            dname = dept_names.get(d, d)
            dept_lines.append(
                f"- **{dname}**：季度预算 {{{{dept.{d}.budget}}}} 元，实际支出 {{{{dept.{d}.spent}}}} 元，"
                f"执行率 {{{{dept.{d}.exec_rate}}}}%，预算差额（实际-预算）为 {{{{dept.{d}.overspend}}}} 元。"
            )
        dept_block = "\n".join(dept_lines)
        return (
            "### 部门差旅预算执行与超支归因分析报告\n\n"
            "#### 一、总体执行概览\n"
            "权限范围内汇总季度预算为 {{total.budget}} 元，本期实际累计支出 {{total.spent}} 元，"
            "整体预算执行率 {{total.exec_rate}}%，总预算差额为 {{total.overspend}} 元。\n\n"
            "#### 二、各部门预算明细\n"
            f"{dept_block}\n\n"
            "#### 三、无残差两因素量价归因（对比基期）\n"
            "较基期相比，差旅总支出变动额为 {{vp.total_delta}} 元，其中：\n"
            "- **出差频次/人次变动（量增效应）**：贡献 {{vp.volume_effect}} 元；\n"
            "- **人均单次机酒均价变动（价升效应）**：贡献 {{vp.price_effect}} 元。\n\n"
            "#### 四、预订行为偏离与降本测算\n"
            "1. **临期订票溢价**：低于差标规定提前预订天数的订单占比为 {{behavior.late_ratio}}%，"
            "产生可量化临期机票溢价约 {{behavior.late_premium}} 元；酒店软超标订单占比为 {{behavior.hotel_exceed_ratio}}%。\n"
            "2. **高频城市协议降本抓手**：消费最集中的目的地城市为 {{saving.top_city}}，"
            "若在高频城市推行协议酒店锁价并收紧提前预订约束，预计单季度可节省差旅支出 {{saving.estimated_saving}} 元。"
        )

    async def compute_embedding(self, text_input: str) -> list[float]:
        """
        生成 1024 维文本向量：
          - `LLM_MODE=openai` 时调用 OpenAI 兼容 Embeddings API（如 DashScope text-embedding-v3）；
          - 本地 Mock 或无 Key 时回退至确定性本地向量。
        """
        from apps.common.db import compute_text_embedding

        if self._is_real_openai():
            try:
                from openai import AsyncOpenAI

                client = AsyncOpenAI(
                    api_key=self.settings.openai_api_key,
                    base_url=self.settings.openai_base_url,
                )
                resp = await client.embeddings.create(
                    model=self.settings.embedding_model,
                    input=text_input[:2000],
                    dimensions=1024,
                )
                return list(resp.data[0].embedding)
            except Exception:
                pass
        return compute_text_embedding(text_input, dim=1024)

    async def extract_invoices_multimodal(self, raw_attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        多模态视觉模型（Qwen-VL）发票识别封装：
          - 当 `LLM_MODE=openai` 且附件包含 `image_base64` 时，真实调用 `llm_vision_model` 解析图片中的发票清单；
          - 无图片或 Mock 模式时，解析结构化样例或文本凭证。
        """
        if self._is_real_openai() and raw_attachments:
            image_items = [a for a in raw_attachments if a.get("image_base64")]
            if image_items:
                try:
                    from langchain_core.messages import HumanMessage
                    from langchain_openai import ChatOpenAI

                    vlm = ChatOpenAI(
                        model=self.settings.llm_vision_model,
                        api_key=self.settings.openai_api_key,
                        base_url=self.settings.openai_base_url,
                        temperature=0.0,
                    )
                    content_blocks: list[dict[str, Any]] = [
                        {
                            "type": "text",
                            "text": (
                                "请识别图片中的差旅发票/行程单/水单，严格输出 JSON 数组，每项包含："
                                "invoice_no, invoice_date, merchant, category(FLIGHT/HOTEL/TRAIN/TAXI/MEAL), "
                                "amount(数字), tax_amount(数字), notes。"
                            ),
                        }
                    ]
                    for img in image_items:
                        mime = img.get("mime_type", "image/jpeg")
                        b64 = img["image_base64"]
                        url = b64 if str(b64).startswith("data:") else f"data:{mime};base64,{b64}"
                        content_blocks.append({"type": "image_url", "image_url": {"url": url}})
                    resp = await vlm.ainvoke([HumanMessage(content=content_blocks)])
                    m = re.search(r"\[.*\]", str(resp.content), re.S)
                    if m:
                        parsed_json = json.loads(m.group(0))
                        if isinstance(parsed_json, list) and parsed_json:
                            return parsed_json
                except Exception:
                    pass

        if not raw_attachments:
            return [
                {
                    "invoice_no": "INV-20261115-001",
                    "invoice_date": "2026-12-10",
                    "merchant": "中国国航电子客票行程单",
                    "category": "FLIGHT",
                    "amount": 1120.00,
                    "tax_amount": 92.48,
                    "notes": "上海虹桥->北京首都",
                },
                {
                    "invoice_no": "INV-20261117-002",
                    "invoice_date": "2026-12-12",
                    "merchant": "北京国贸大酒店",
                    "category": "HOTEL",
                    "amount": 1460.00,
                    "tax_amount": 82.64,
                    "notes": "入住2晚，每晚730元（高于P6一线城市限额650元/晚，差额80元/晚）",
                },
                {
                    "invoice_no": "INV-20261117-003",
                    "invoice_date": "2026-12-12",
                    "merchant": "滴滴出行电子发票",
                    "category": "TAXI",
                    "amount": 168.00,
                    "tax_amount": 4.89,
                    "notes": "首都机场往返国贸",
                },
            ]

        parsed: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_attachments, start=1):
            parsed.append(
                {
                    "invoice_no": str(item.get("invoice_no") or f"INV-AUTO-{idx:03d}"),
                    "invoice_date": str(item.get("invoice_date") or "2026-12-10"),
                    "merchant": str(item.get("merchant") or item.get("filename") or "识别电子发票"),
                    "category": str(item.get("category") or "HOTEL"),
                    "amount": float(item.get("amount", 620.0)),
                    "tax_amount": float(item.get("tax_amount", 35.09)),
                    "notes": str(item.get("notes", "")),
                }
            )
        return parsed
