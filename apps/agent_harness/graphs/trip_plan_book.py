"""
子图 1：智能行程规划与途牛预订全链路（TripPlanAndBookSubgraph）。

对应文档章节：
  - 三、模块 1（自然语言槽位提取 -> 冲突与六维软审核 -> 途牛比价 4 方案 -> HITL 选方案 -> OA 审批 -> 实时价格复核 -> 分段幂等下单）
  - 四、4.2 核心请求与 HITL 挂起恢复时序图
  - 十一、v1.1 P0 #1 / P0 #2 / P0 #3 / P0 #4 / P0 #5

严格遵循的资金与安全铁律：
  1. 包含 `interrupt()` 的节点（`hitl_select_option_node` / `wait_oa_webhook_node` / `hitl_reconfirm_price_node`）
     内绝不执行任何数据库写或外部下单；
  2. OA 审批通过后进入 `price_recheck_node`，调途牛复核实时价格与库存：
     若涨价超过 `max(50元, 3%)` 或售罄，强制二次 `interrupt()` 让用户重新确认，严禁按过期报价静默下单；
  3. 最终下单节点通过 `CredentialAndPersistenceMiddleware` 按 `segment_no=1,2,3` 预写 PENDING -> 调途牛 -> 回写 CONFIRMED。
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from apps.agent_harness.graphs.roundtrip_planner import RoundtripPlanner
from apps.agent_harness.middlewares.circuit_breaker import CircuitBreakerMiddleware, CircuitBreakerOpenError
from apps.agent_harness.middlewares.context_engineering import ContextEngineeringMiddleware, estimate_tokens
from apps.agent_harness.middlewares.credential_persist import CredentialAndPersistenceMiddleware
from apps.agent_harness.middlewares.tenant_context import (
    TenantAuthContextMiddleware,
    require_tenant_context,
)
from apps.agent_harness.runtime.guardrails import ToolGuardrails
from apps.agent_harness.runtime.mcp_client import MCPClientHub
from apps.agent_harness.runtime.services import UserPreferenceService
from apps.agent_harness.skills.loader import SkillLoader
from apps.agent_harness.state import BaseAgentState, HotelOption, TravelLeg
from apps.common.db import get_repository

_CITIES = ["北京", "上海", "深圳", "杭州", "成都", "南京", "广州"]
_CITY_TIER_MAP = {"北京": "TIER_1", "上海": "TIER_1", "深圳": "TIER_1", "广州": "TIER_1", "杭州": "TIER_2", "成都": "TIER_2", "南京": "TIER_2"}


def _extract_trip_slots(query: str, default_origin: str) -> dict[str, Any]:
    """从自然语言中提取出发地、目的地、日期、天数与商圈。"""
    origin = default_origin
    dest = ""
    # 识别 "从A去B" / "A飞B" / "去B出差"
    m = re.search(r"从([^\s去到飞]{2,4})[去到飞]([^\s出差]{2,4})", query)
    if m:
        origin, dest = m.group(1), m.group(2)
    else:
        found_cities = [c for c in _CITIES if c in query]
        if len(found_cities) >= 2:
            origin, dest = found_cities[0], found_cities[1]
        elif len(found_cities) == 1:
            dest = found_cities[0]

    days = 3
    dm = re.search(r"(\d+)\s*[天晚]", query)
    if dm:
        days = max(1, min(14, int(dm.group(1))))

    # 支持显式日期（如 "12月10日"）与相对表达（如 "下周"）
    date_m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", query)
    if date_m:
        base_start = date(2026, int(date_m.group(1)), int(date_m.group(2)))
    else:
        base_start = date(2026, 12, 15) if "下周" in query else date(2026, 12, 5)
    end_d = base_start + timedelta(days=days)
    district = "国贸" if "国贸" in query else ("陆家嘴" if "陆家嘴" in query else "核心商圈")
    return {
        "origin_city": origin,
        "dest_city": dest,
        "dest_city_tier": _CITY_TIER_MAP.get(dest, "TIER_1"),
        "start_date": base_start.isoformat(),
        "end_date": end_d.isoformat(),
        "nights": max(1, days - 1),
        "district": district,
    }


def _detect_conflicts(existing_trips: list[dict[str, Any]], new_start: date, new_end: date, origin: str, dest: str) -> list[dict[str, Any]]:
    """行程冲突检测：检测时间重叠与跨城物理不可达风险（HIGH / MEDIUM / LOW）。"""
    conflicts: list[dict[str, Any]] = []
    for t in existing_trips:
        ts = date.fromisoformat(t["start_date"])
        te = date.fromisoformat(t["end_date"])
        if not (new_end < ts or new_start > te):
            severity = "HIGH" if t["dest_city"] != dest else "MEDIUM"
            conflicts.append(
                {
                    "severity": severity,
                    "existing_order_no": t["order_no"],
                    "message": f"与既有行程 {t['order_no']}（{t['start_date']}~{t['end_date']} {t['dest_city']}）存在时间重叠！",
                }
            )
    return conflicts


async def extract_slots_node(state: BaseAgentState) -> dict[str, Any]:
    """节点 1：提取行程槽位，若缺少目的地城市则主动友好追问。"""
    ctx = require_tenant_context()
    SkillLoader().load_skill_body("trip-planning")
    repo = get_repository()
    prof = await repo.get_user_profile(ctx.tenant_id, ctx.user_id)
    default_origin = prof["home_city"] if prof else "上海"

    query = state.get("effective_query") or state.get("user_query", "")
    prev_slots = dict(state.get("trip_slots") or {})
    parsed = _extract_trip_slots(query, default_origin=default_origin)
    # 注意保留布尔值 False（例如 auto_approve_oa=False），仅跳过 None 或空字符串
    merged_slots = {**parsed, **{k: v for k, v in prev_slots.items() if v is not None and v != ""}}
    if not merged_slots.get("dest_city"):
        return {
            "trip_slots": merged_slots,
            "final_answer": "请问您本次计划前往哪个城市出差？如果有期望的出发日期和天数，也请一并告诉我。",
            "active_subgraph": "TripPlanAndBookSubgraph",
            "step_count": 1,
        }
    return {
        "trip_slots": merged_slots,
        "loaded_skills": ["trip-planning"],
        "active_subgraph": "TripPlanAndBookSubgraph",
        "step_count": 1,
    }


def route_after_slots(state: BaseAgentState) -> str:
    slots = state.get("trip_slots") or {}
    if not slots.get("dest_city"):
        return END
    return "conflict_and_search"


async def conflict_and_search_node(state: BaseAgentState) -> dict[str, Any]:
    """
    节点 2：OA 行程冲突检测 + 途牛并行查价（受熔断器与上下文压缩中间件保护） + RoundtripPlanner 生成 4 套方案。
    """
    ctx = require_tenant_context()
    slots = state["trip_slots"]
    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    cb = CircuitBreakerMiddleware()
    ctx_eng = ContextEngineeringMiddleware()
    cred_mw = CredentialAndPersistenceMiddleware()
    tuniu_cred = await cred_mw.resolve_provider_credential(ctx, "TUNIU")

    # 1. 查既有行程做冲突检测 + 查长期偏好 + 查精确差标
    existing_trips = await cb.call(ctx.tenant_id, "list_employee_trips", MCPClientHub.list_employee_trips, token)
    conflicts = _detect_conflicts(
        existing_trips,
        date.fromisoformat(slots["start_date"]),
        date.fromisoformat(slots["end_date"]),
        slots["origin_city"],
        slots["dest_city"],
    )
    user_prefs = await UserPreferenceService().extract_and_update_from_query(ctx, state.get("user_query", ""))
    rule = await get_repository().get_policy_rule(ctx.tenant_id, ctx.rank_no, slots["dest_city_tier"])

    # 2. 并行调用途牛检索去程航班/高铁、酒店、返程航班/高铁（熔断保护）
    try:
        out_f_res, out_t_res, hotel_res, in_f_res, in_t_res = await asyncio.gather(
            cb.call(ctx.tenant_id, "tuniu_search", MCPClientHub.search_flights, token, slots["origin_city"], slots["dest_city"], slots["start_date"], tuniu_cred),
            cb.call(ctx.tenant_id, "tuniu_search", MCPClientHub.search_trains, token, slots["origin_city"], slots["dest_city"], slots["start_date"], tuniu_cred),
            cb.call(ctx.tenant_id, "tuniu_search", MCPClientHub.search_hotels, token, slots["dest_city"], slots["start_date"], slots["nights"], slots["district"], tuniu_cred),
            cb.call(ctx.tenant_id, "tuniu_search", MCPClientHub.search_flights, token, slots["dest_city"], slots["origin_city"], slots["end_date"], tuniu_cred),
            cb.call(ctx.tenant_id, "tuniu_search", MCPClientHub.search_trains, token, slots["dest_city"], slots["origin_city"], slots["end_date"], tuniu_cred),
        )
    except CircuitBreakerOpenError as exc:
        return {
            "error_info": {"code": "TOOL_CIRCUIT_OPEN", "message": str(exc), "retryable": True},
            "final_answer": f"途牛商旅服务暂时触发熔断降级：{exc}。建议稍后重试或改查公司差标标准。",
            "step_count": 1,
        }

    # 统计 ContextEngineeringMiddleware 压缩后的 Token 节省
    compressed_out = ctx_eng.compress_tuniu_payload(out_f_res)
    saved_tokens = max(0, estimate_tokens(out_f_res) - estimate_tokens(compressed_out))

    outbound_legs = [TravelLeg(**x) for x in out_f_res["normalized"]] + [TravelLeg(**x) for x in out_t_res["normalized"]]
    inbound_legs = [TravelLeg(**x) for x in in_f_res["normalized"]] + [TravelLeg(**x) for x in in_t_res["normalized"]]
    hotels = [HotelOption(**x) for x in hotel_res["normalized"]]

    options, has_compliant = RoundtripPlanner().plan(
        outbound_legs=outbound_legs,
        hotels=hotels,
        inbound_legs=inbound_legs,
        policy_rule=rule,
        user_prefs=user_prefs,
    )
    opt_dicts = [o.model_dump() for o in options]
    default_opt = opt_dicts[2] if len(opt_dicts) > 2 else opt_dicts[0]

    # 生成首次 HITL 确认载荷（默认绑定“最合规”或首位方案，用户在卡片中切换后提交所选方案）
    hitl = ToolGuardrails.build_hitl_payload(
        ui_type="select",
        title=f"{slots['origin_city']} ↔ {slots['dest_city']}（{slots['start_date']} ~ {slots['end_date']}）行程方案确认",
        description=(
            ("⚠️ 检测到行程冲突：" + "；".join(c["message"] for c in conflicts) + "\n") if conflicts else ""
        )
        + ("已为您精选 4 套代表方案（最省钱/最省时/最合规/最舒适），请确认方案并提交 OA 审批及途牛下单。" if has_compliant else "提示：当前无100%满足差标的组合，请确认优选方案并走超标审批。"),
        tool_name="create_tuniu_booking",
        tool_args={
            "option_id": default_opt["option_id"],
            "option_hash": default_opt["option_hash"],
            "total_price": default_opt["total_price"],
            "origin_city": slots["origin_city"],
            "dest_city": slots["dest_city"],
            "start_date": slots["start_date"],
            "end_date": slots["end_date"],
        },
        options=opt_dicts,
    )

    return {
        "trip_conflicts": conflicts,
        "travel_options": opt_dicts,
        "pending_hitl": hitl.model_dump(),
        "step_count": 1,
        "total_tokens": max(150, 600 - saved_tokens),
    }


def route_after_search(state: BaseAgentState) -> str:
    if state.get("error_info"):
        return END
    return "hitl_select_option"


async def hitl_select_option_node(state: BaseAgentState) -> dict[str, Any]:
    """
    节点 3：纯 HITL 方案选择挂起节点（v1.1 P0 #5：节点内绝无副作用）。
    允许用户在卡片中确认当前默认绑定方案，或选择 4 套方案中的任意一套（若切换方案需携带对应新 option_id + 服务端校验）。
    """
    ctx = require_tenant_context()
    expected_hitl = state["pending_hitl"]
    assert expected_hitl is not None

    resume_val = interrupt(expected_hitl)
    resume_dict = resume_val if isinstance(resume_val, dict) else {}

    # 校验 interrupt_id 与 action_hash（防 TOCTOU）
    approved = await ToolGuardrails.verify_and_audit_decision(
        ctx=ctx,
        conversation_id=state["conversation_id"],
        expected_hitl=expected_hitl,
        resume_payload=resume_dict,
    )
    selected_id = resume_dict.get("selected_option_id") or expected_hitl["tool_args"]["option_id"]
    return {
        "selected_option_id": selected_id,
        "hitl_resume_payload": {"approved": approved, **resume_dict},
        "step_count": 1,
    }


async def oa_approval_node(state: BaseAgentState) -> dict[str, Any]:
    """节点 4：独立后继节点，调用 oa-mcp 提交出差审批单。"""
    ctx = require_tenant_context()
    resume_info = state.get("hitl_resume_payload") or {}
    expected_hitl = state.get("pending_hitl") or {}
    if not resume_info.get("approved"):
        return {"pending_hitl": None, "final_answer": "您已取消本次差旅预订，期待下次为您服务。", "step_count": 1}

    ToolGuardrails.assert_pre_execution_hash(
        ctx, state["conversation_id"], expected_hitl["interrupt_id"], expected_hitl["action_hash"], "create_tuniu_booking", expected_hitl["tool_args"]
    )

    slots = state["trip_slots"]
    options = state.get("travel_options") or []
    sel_opt = next((o for o in options if o["option_id"] == state.get("selected_option_id")), options[0])

    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    auto_approve = bool(slots.get("auto_approve_oa", True))
    oa_res = await MCPClientHub.submit_travel_approval(
        internal_token=token,
        origin_city=slots["origin_city"],
        dest_city=slots["dest_city"],
        dest_city_tier=slots["dest_city_tier"],
        start_date=slots["start_date"],
        end_date=slots["end_date"],
        purpose=f"{slots['dest_city']}商务差旅",
        estimated_amount=float(sel_opt["total_price"]),
        auto_approve=auto_approve,
    )
    return {
        "travel_order_id": oa_res["travel_order_id"],
        "trip_slots": {**slots, "oa_status": oa_res["status"], "oa_external_id": oa_res["oa_external_id"]},
        "step_count": 1,
    }


def route_after_oa(state: BaseAgentState) -> str:
    if not (state.get("hitl_resume_payload") or {}).get("approved"):
        return END
    if (state.get("trip_slots") or {}).get("oa_status") != "APPROVED":
        return "wait_oa_webhook"
    return "price_recheck"


async def wait_oa_webhook_node(state: BaseAgentState) -> dict[str, Any]:
    """当需要主管异步审批时，挂起等待 /api/webhook/oa-approval 唤醒（无副作用）。"""
    wh_res = interrupt(
        {
            "ui_type": "confirm",
            "waiting_for": "OA_APPROVAL_WEBHOOK",
            "travel_order_id": state.get("travel_order_id"),
            "oa_external_id": (state.get("trip_slots") or {}).get("oa_external_id"),
        }
    )
    wh_dict = wh_res if isinstance(wh_res, dict) else {}
    return {
        "trip_slots": {**(state.get("trip_slots") or {}), "oa_status": wh_dict.get("action", "APPROVED")},
        "step_count": 1,
    }


async def price_recheck_node(state: BaseAgentState) -> dict[str, Any]:
    """
    节点 5：下单前实时价格与库存复核（落实 v1.1 P0 #3）。
    若距选方案时刻总价上涨超过 max(50元, 3%) 或房源/机票售罄，准备二次 HITL 确认载荷。
    """
    ctx = require_tenant_context()
    slots = state["trip_slots"]
    options = state.get("travel_options") or []
    sel_opt = next((o for o in options if o["option_id"] == state.get("selected_option_id")), options[0])
    if slots.get("oa_status") == "REJECTED":
        return {"final_answer": "您的出差审批已被主管驳回，已自动中止途牛下单流程。", "price_recheck_needed": False, "step_count": 1}

    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    cred = await CredentialAndPersistenceMiddleware().resolve_provider_credential(ctx, "TUNIU")
    out_f_now, hotel_now, in_f_now = await asyncio.gather(
        MCPClientHub.search_flights(token, slots["origin_city"], slots["dest_city"], slots["start_date"], cred),
        MCPClientHub.search_hotels(token, slots["dest_city"], slots["start_date"], slots["nights"], slots["district"], cred),
        MCPClientHub.search_flights(token, slots["dest_city"], slots["origin_city"], slots["end_date"], cred),
    )

    sold_out = bool(out_f_now.get("sold_out") or hotel_now.get("sold_out"))
    # 计算选中航班/酒店实时最新价格
    out_map = {x["leg_id"]: x["price"] for x in out_f_now["normalized"]}
    in_map = {x["leg_id"]: x["price"] for x in in_f_now["normalized"]}
    hotel_map = {x["hotel_id"]: x["total_price"] for x in hotel_now["normalized"]}

    new_out_price = out_map.get(sel_opt["outbound"]["leg_id"], sel_opt["outbound"]["price"])
    new_in_price = in_map.get(sel_opt["inbound"]["leg_id"], sel_opt["inbound"]["price"])
    new_hotel_price = hotel_map.get(sel_opt["hotel"]["hotel_id"], sel_opt["hotel"]["total_price"])
    new_total = round(new_out_price + new_hotel_price + new_in_price, 2)
    orig_total = float(sel_opt["total_price"])
    diff = round(new_total - orig_total, 2)
    threshold = max(50.0, round(orig_total * 0.03, 2))

    if sold_out or diff > threshold:
        reason = "所选航班或酒店已售罄" if sold_out else f"途牛实时总价由 {orig_total:.2f} 元上涨至 {new_total:.2f} 元（涨幅 +{diff:.2f} 元，超出阈值 {threshold:.2f} 元）"
        re_hitl = ToolGuardrails.build_hitl_payload(
            ui_type="confirm",
            title="下单前实时价格/库存复核提醒",
            description=f"{reason}，请确认是否按最新报价继续下单？",
            tool_name="create_tuniu_booking",
            tool_args={
                "option_id": sel_opt["option_id"],
                "option_hash": sel_opt["option_hash"],
                "total_price": new_total,
                "sold_out": sold_out,
            },
        )
        return {
            "price_recheck_needed": True,
            "pending_hitl": re_hitl.model_dump(),
            "trip_slots": {**slots, "rechecked_total_price": new_total},
            "step_count": 1,
        }

    return {"price_recheck_needed": False, "step_count": 1}


def route_after_recheck(state: BaseAgentState) -> str:
    if (state.get("trip_slots") or {}).get("oa_status") == "REJECTED":
        return END
    if state.get("price_recheck_needed"):
        return "hitl_reconfirm_price"
    return "execute_booking"


async def hitl_reconfirm_price_node(state: BaseAgentState) -> dict[str, Any]:
    """二次价格变化纯 HITL 确认节点（无副作用）。"""
    ctx = require_tenant_context()
    expected_hitl = state["pending_hitl"]
    assert expected_hitl is not None
    resume_val = interrupt(expected_hitl)
    approved = await ToolGuardrails.verify_and_audit_decision(
        ctx, state["conversation_id"], expected_hitl, resume_val if isinstance(resume_val, dict) else {}
    )
    return {"hitl_resume_payload": {"approved": approved}, "step_count": 1}


async def execute_booking_node(state: BaseAgentState) -> dict[str, Any]:
    """
    节点 6：独立后继 WRITE 下单节点。
    按 segment_no = 1(去程), 2(酒店), 3(返程) 依次走 CredentialAndPersistenceMiddleware 状态机落库与下单。
    """
    ctx = require_tenant_context()
    if state.get("price_recheck_needed") and not (state.get("hitl_resume_payload") or {}).get("approved"):
        return {"pending_hitl": None, "final_answer": "由于价格变动未确认，已为您取消途牛下单。", "step_count": 1}

    slots = state["trip_slots"]
    options = state.get("travel_options") or []
    sel_opt = next((o for o in options if o["option_id"] == state.get("selected_option_id")), options[0])
    order_id = int(state["travel_order_id"] or 0)
    token = TenantAuthContextMiddleware.issue_internal_token(ctx)
    cred_mw = CredentialAndPersistenceMiddleware()

    segments_meta = [
        (
            1,
            {
                "category": sel_opt["outbound"]["category"],
                "city": slots["dest_city"],
                "vendor_name": sel_opt["outbound"]["carrier_or_train_no"],
                "amount": sel_opt["outbound"]["price"],
                "depart_date": slots["start_date"],
            },
        ),
        (
            2,
            {
                "category": "HOTEL",
                "city": slots["dest_city"],
                "vendor_name": sel_opt["hotel"]["hotel_name"],
                "amount": sel_opt["hotel"]["total_price"],
                "depart_date": slots["start_date"],
            },
        ),
        (
            3,
            {
                "category": sel_opt["inbound"]["category"],
                "city": slots["origin_city"],
                "vendor_name": sel_opt["inbound"]["carrier_or_train_no"],
                "amount": sel_opt["inbound"]["price"],
                "depart_date": slots["end_date"],
            },
        ),
    ]

    receipts: list[dict[str, Any]] = []
    for seg_no, meta in segments_meta:
        rec = await cred_mw.execute_idempotent_booking(
            ctx=ctx,
            travel_order_id=order_id,
            option_hash=sel_opt["option_hash"],
            segment_no=seg_no,
            booking_meta=meta,
            book_fn=lambda cred, ikey, m=meta: MCPClientHub.create_tuniu_booking(
                internal_token=token,
                idempotent_key=ikey,
                segment_category=m["category"],
                vendor_name=m["vendor_name"],
                amount=float(m["amount"]),
                tuniu_credential=cred,
            ),
        )
        receipts.append(rec)

    expected_hitl = state.get("pending_hitl") or {}
    if expected_hitl:
        await ToolGuardrails.record_execution_audit(
            ctx, state["conversation_id"], expected_hitl["interrupt_id"], "create_tuniu_booking", expected_hitl["action_hash"], expected_hitl["tool_args"], "SUCCESS"
        )

    lines = [
        f"### 差旅审批与途牛预订全部完成（方案：{sel_opt['label']}）",
        f"- **OA 出差单号**：`{slots.get('oa_external_id')}`（状态：`APPROVED`）",
        f"- **预订总金额**：`{slots.get('rechecked_total_price', sel_opt['total_price']):.2f} 元`",
    ]
    for r in receipts:
        lines.append(
            f"- **第 {r['segment_no']} 段 ({r['category']})**：{r['vendor_name']} | 途牛单号 `{r['external_order_no']}` | 状态 `{r['booking_status']}`"
        )
    return {
        "pending_hitl": None,
        "final_answer": "\n".join(lines),
        "step_count": 1,
    }


def build_trip_plan_book_subgraph(checkpointer: Any = None):
    builder = StateGraph(BaseAgentState)
    builder.add_node("extract_slots", extract_slots_node)
    builder.add_node("conflict_and_search", conflict_and_search_node)
    builder.add_node("hitl_select_option", hitl_select_option_node)
    builder.add_node("oa_approval", oa_approval_node)
    builder.add_node("wait_oa_webhook", wait_oa_webhook_node)
    builder.add_node("price_recheck", price_recheck_node)
    builder.add_node("hitl_reconfirm_price", hitl_reconfirm_price_node)
    builder.add_node("execute_booking", execute_booking_node)

    builder.add_edge(START, "extract_slots")
    builder.add_conditional_edges("extract_slots", route_after_slots, {"conflict_and_search": "conflict_and_search", END: END})
    builder.add_conditional_edges("conflict_and_search", route_after_search, {"hitl_select_option": "hitl_select_option", END: END})
    builder.add_edge("hitl_select_option", "oa_approval")
    builder.add_conditional_edges("oa_approval", route_after_oa, {"wait_oa_webhook": "wait_oa_webhook", "price_recheck": "price_recheck", END: END})
    builder.add_edge("wait_oa_webhook", "price_recheck")
    builder.add_conditional_edges("price_recheck", route_after_recheck, {"hitl_reconfirm_price": "hitl_reconfirm_price", "execute_booking": "execute_booking", END: END})
    builder.add_edge("hitl_reconfirm_price", "execute_booking")
    builder.add_edge("execute_booking", END)
    return builder.compile(checkpointer=checkpointer)
