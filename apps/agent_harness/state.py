"""
核心数据结构与 LangGraph State 定义。

对应文档章节：
  - 二、2.2 TenantContext 不可变多租户上下文
  - 五、5.1 IntentResult 与 5.2 BaseAgentState
  - 十一、v1.1 优化记录 #6（LangGraph Send 并行分支必须声明 Reducer）
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

RoleType = Literal["EMPLOYEE", "DEPT_MANAGER", "FINANCE_ADMIN", "TENANT_ADMIN"]
IntentType = Literal[
    "TRIP_PLAN_BOOK",
    "POLICY_INFO",
    "EXPENSE_DRAFT",
    "BUDGET_INSIGHT",
    "COMPOSITE",
    "GENERAL_CHAT",
]


@dataclass(frozen=True)
class TenantContext:
    """
    不可变且可哈希的多租户安全上下文（对应 2.2 节）。

    v1.1 关键设计：
      - `managed_dept_ids` 使用 `tuple[str, ...]` 而非 `list[str]`，保证 `frozen=True` 真正不可变且可哈希；
      - `rank_no` 为数值职级（如 P6 -> 6），防止字符串比较 `'P10' < 'P5'` 的排序陷阱；
      - 跨进程调用 MCP 时不可直接依赖 contextvars，必须由中间件签发短时 `X-PyTravel-Ctx` 令牌传递。
    """

    tenant_id: str
    user_id: str
    dept_id: str
    managed_dept_ids: tuple[str, ...]
    role: RoleType
    rank_level: str
    rank_no: int
    trace_id: str


class IntentResult(BaseModel):
    """三层意图路由统一输出模型，使下游图编排与路由算法零耦合。"""

    intents: list[IntentType]
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["STICKY", "RULE", "VECTOR", "LLM"]
    direct_route_target: str | None = None
    rewritten_query: str | None = None
    needs_rewrite: bool = False
    llm_called: bool = False
    latency_ms: float = 0.0


class TravelLeg(BaseModel):
    """归一化后的单程交通选项（航班或高铁，对应 7.2 TravelDataNormalizer）。"""

    leg_id: str
    category: Literal["FLIGHT", "TRAIN"]
    carrier_or_train_no: str
    depart_time: str
    arrive_time: str
    duration_minutes: int
    cabin_or_seat: str
    price: float
    is_direct: bool = True
    is_red_eye: bool = False


class HotelOption(BaseModel):
    """归一化后的酒店选项。"""

    hotel_id: str
    hotel_name: str
    brand: str
    city: str
    district: str
    nightly_price: float
    total_price: float
    nights: int
    is_corporate_agreed: bool = False
    rating: float = 4.5


class TripOption(BaseModel):
    """RoundtripPlanner 生成的 4 套代表性往返组合方案之一。"""

    option_id: str
    option_hash: str
    label: Literal["最省钱", "最省时", "最合规", "最舒适"]
    outbound: TravelLeg
    hotel: HotelOption
    inbound: TravelLeg
    total_price: float
    total_duration_minutes: int
    policy_penalty: float
    is_policy_compliant: bool
    pref_score: float
    cost_score: float
    compliance_notes: list[str] = Field(default_factory=list)


class HitlPayload(BaseModel):
    """
    Human-in-the-Loop 前端交互确认卡片载荷（对应 6 章 Guardrails / 9.2 SSE 协议）。

    v1.1 P0 #4 防 TOCTOU 关键字段：
      `action_hash = SHA256(tool_name | canonical_json(tool_args))`
      一旦推给前端确认，后续恢复执行前会双重比对 action_hash，杜绝模型暗改参数。
    """

    interrupt_id: str
    ui_type: Literal["select", "confirm", "form"]
    title: str
    description: str
    tool_name: str
    tool_args: dict[str, Any]
    action_hash: str
    options: list[dict[str, Any]] = Field(default_factory=list)
    schema_fields: list[dict[str, Any]] = Field(default_factory=list)


class ChartPayload(BaseModel):
    """Recharts 3 标准图表 JSON 载荷。"""

    chart_id: str
    type: Literal["bar", "line", "pie"]
    title: str
    x_key: str = "name"
    series: list[dict[str, Any]]
    data: list[dict[str, Any]]


class InvoiceItem(BaseModel):
    """多模态发票识别提取的结构化单据条目。"""

    invoice_no: str
    invoice_date: str
    merchant: str
    category: Literal["FLIGHT", "HOTEL", "TRAIN", "TAXI", "MEAL"]
    amount: float
    tax_amount: float = 0.0
    notes: str = ""


def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """LangGraph 字典合并 Reducer：供并行 Send Worker 安全写入各自命名空间指标。"""
    merged = dict(left or {})
    if right:
        merged.update(right)
    return merged


class BaseAgentState(TypedDict, total=False):
    """
    LangGraph 主图与子图共享核心状态（严格对齐文档 5.2 节与 v1.1 P0 #6）。

    关键避坑说明：
      BudgetInsightSubgraph 通过 `Send` API 在同一个 super-step 并行启动 3 个 Worker。
      在 LangGraph 中，若多个并行节点写入没有 Reducer 的普通字段，会直接抛出
      `InvalidUpdateError: At key '...': Can receive only one value per step`。
      因此 `artifact_files`、`chart_payloads`、`step_count`、`total_tokens` 使用 `operator.add`，
      `budget_metrics` 使用 `merge_dicts`。
    """

    # 会话与用户输入
    conversation_id: str
    user_query: str
    effective_query: str
    messages: Annotated[list[BaseMessage], add_messages]

    # 路由与任务规划状态
    intent_result: dict[str, Any] | None
    plan_steps: list[dict[str, Any]]
    active_subgraph: str | None
    loaded_skills: list[str]

    # 沙箱与上下文卸载产物（Context Offloading：只存 artifact_id 与摘要，不存大文本）
    workspace_dir: str
    artifact_files: Annotated[list[dict[str, Any]], operator.add]

    # 各业务子图结构化状态
    trip_slots: dict[str, Any]
    trip_conflicts: list[dict[str, Any]]
    travel_options: list[dict[str, Any]] | None
    selected_option_id: str | None
    travel_order_id: int | None
    price_recheck_needed: bool

    expense_invoices: list[dict[str, Any]]
    expense_draft_result: dict[str, Any] | None

    # 预算归因并行子图专属状态
    budget_fiscal_year: int
    budget_fiscal_quarter: int
    budget_allowed_depts: list[str]
    budget_csv_artifact_id: str | None
    budget_rules_json: str
    budget_metrics: Annotated[dict[str, Any], merge_dicts]
    chart_payloads: Annotated[list[dict[str, Any]], operator.add]

    # 确定性校验与降级控制
    draft_report_with_placeholders: str | None
    rendered_report: str | None
    verification_feedback: str | None
    verification_retry_count: int

    # HITL 断点与最终响应
    pending_hitl: dict[str, Any] | None
    hitl_resume_payload: dict[str, Any] | None
    final_answer: str | None
    error_info: dict[str, Any] | None

    # 步数与 Token 成本追踪（并行分支安全累加）
    step_count: Annotated[int, operator.add]
    total_tokens: Annotated[int, operator.add]
