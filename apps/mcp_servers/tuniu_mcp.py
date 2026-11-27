"""
途牛商旅标准 FastMCP Server（tuniu-mcp-server）。

对应文档章节：
  - 七、7.1 途牛预订集成设计（TUNIU_MODE=real|mock 双模式 + 幂等下单）
  - 七、7.2 工具清单：search_flights / search_trains / search_hotels / create_tuniu_booking(WRITE) / get_booking_status
  - 十一、v1.1 P0 #3（下单前实时价格/库存复核）& P1 #15（故障注入验证熔断）

运行方式（独立 Streamable HTTP MCP 进程）：
  fastmcp run apps/mcp_servers/tuniu_mcp.py --transport streamable-http --port 8021
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastmcp import FastMCP

from apps.agent_harness.middlewares.circuit_breaker import UpstreamToolError
from apps.agent_harness.middlewares.tenant_context import TenantAuthContextMiddleware
from apps.common.config import get_settings
from apps.mcp_servers.normalizer import TravelDataNormalizer

mcp = FastMCP("tuniu-mcp-server")

# ----------------------------------------------------------------------
# 测试辅助控制状态（支持模拟途牛接口抖动熔断、以及审批后涨价/售罄复核场景）
# ----------------------------------------------------------------------
_MOCK_FAULT_CONFIG: dict[str, Any] = {"fail_remaining": 0, "status_code": 503, "price_bump_yuan": 0.0, "sold_out": False}
# 幂等下单结果注册表：external_idempotent_key -> receipt
_MOCK_EXTERNAL_BOOKINGS: dict[str, dict[str, Any]] = {}


def configure_tuniu_mock(
    fail_remaining: int = 0,
    status_code: int = 503,
    price_bump_yuan: float = 0.0,
    sold_out: bool = False,
) -> None:
    """供自动化测试与 Eval 注入故障/涨价场景。"""
    _MOCK_FAULT_CONFIG.update(
        {
            "fail_remaining": fail_remaining,
            "status_code": status_code,
            "price_bump_yuan": price_bump_yuan,
            "sold_out": sold_out,
        }
    )


def _check_fault_injection() -> None:
    if _MOCK_FAULT_CONFIG["fail_remaining"] > 0:
        _MOCK_FAULT_CONFIG["fail_remaining"] -= 1
        code = int(_MOCK_FAULT_CONFIG["status_code"])
        raise UpstreamToolError(f"途牛上游服务异常 HTTP {code}", status_code=code, is_timeout=(code == 504))


def _generate_mock_flights(origin: str, dest: str, depart_date: str) -> list[dict[str, Any]]:
    bump = float(_MOCK_FAULT_CONFIG.get("price_bump_yuan", 0.0))
    # 故意包含大量冗余字段（marketing_tags / raw_html_desc），用于验证 ContextEngineeringMiddleware 压缩效果
    noise = {
        "marketing_tags": ["早鸟特惠", "途牛商旅专享", "支持退改", "电子行程单"],
        "promo_banner_urls": ["https://img.tuniu.example/banner1.png", "https://img.tuniu.example/banner2.png"],
        "raw_html_desc": "<div>航司标准行李额20KG，提供免费餐食及机上娱乐系统说明长文本" * 8 + "</div>",
        "seat_map_matrix": [[f"{r}{c}" for c in "ABCDEF"] for r in range(1, 20)],
    }
    return [
        {"flight_no": "MU5101", "airline": "东方航空", "depart_time": "07:30", "arrive_time": "09:45", "duration_minutes": 135, "cabin": "ECONOMY", "price": 780.0 + bump, "is_direct": True, **noise},
        {"flight_no": "CA1502", "airline": "中国国航", "depart_time": "09:00", "arrive_time": "11:10", "duration_minutes": 130, "cabin": "ECONOMY", "price": 960.0 + bump, "is_direct": True, **noise},
        {"flight_no": "CZ3908", "airline": "南方航空", "depart_time": "14:20", "arrive_time": "16:35", "duration_minutes": 135, "cabin": "PREMIUM_ECONOMY", "price": 1380.0 + bump, "is_direct": True, **noise},
        {"flight_no": "HO1251", "airline": "吉祥航空", "depart_time": "23:35", "arrive_time": "01:50", "duration_minutes": 135, "cabin": "ECONOMY", "price": 540.0 + bump, "is_direct": True, **noise},
        {"flight_no": "CA1886", "airline": "中国国航", "depart_time": "12:00", "arrive_time": "14:05", "duration_minutes": 125, "cabin": "BUSINESS", "price": 3200.0 + bump, "is_direct": True, **noise},
    ]


def _generate_mock_trains(origin: str, dest: str, depart_date: str) -> list[dict[str, Any]]:
    bump = float(_MOCK_FAULT_CONFIG.get("price_bump_yuan", 0.0))
    return [
        {"train_no": "G12", "depart_time": "08:00", "arrive_time": "12:24", "duration_minutes": 264, "seat_type": "SECOND_CLASS", "price": 553.0 + bump},
        {"train_no": "G14", "depart_time": "09:00", "arrive_time": "13:18", "duration_minutes": 258, "seat_type": "FIRST_CLASS", "price": 933.0 + bump},
        {"train_no": "G18", "depart_time": "15:00", "arrive_time": "19:35", "duration_minutes": 275, "seat_type": "SECOND_CLASS", "price": 553.0 + bump},
    ]


def _generate_mock_hotels(city: str, district: str = "国贸") -> list[dict[str, Any]]:
    bump = float(_MOCK_FAULT_CONFIG.get("price_bump_yuan", 0.0))
    noise = {
        "amenities_full_text": "免费WiFi、24小时健身房、自助早餐、行政酒廊、会议室预约说明长文本" * 6,
        "images": ["https://img.tuniu.example/h1.jpg", "https://img.tuniu.example/h2.jpg"],
    }
    return [
        {"hotel_id": "HT_ATOUR_01", "hotel_name": f"{city}{district}亚朵酒店", "brand": "亚朵酒店", "city": city, "district": district, "nightly_price": 620.0 + bump, "is_corporate_agreed": True, "rating": 4.8, **noise},
        {"hotel_id": "HT_JI_02", "hotel_name": f"{city}{district}全季酒店", "brand": "全季酒店", "city": city, "district": district, "nightly_price": 530.0 + bump, "is_corporate_agreed": True, "rating": 4.6, **noise},
        {"hotel_id": "HT_CW_03", "hotel_name": f"{city}{district}大酒店", "brand": "国贸大酒店", "city": city, "district": district, "nightly_price": 1180.0 + bump, "is_corporate_agreed": False, "rating": 4.9, **noise},
        {"hotel_id": "HT_HANTING_04", "hotel_name": f"{city}{district}汉庭优佳", "brand": "汉庭", "city": city, "district": district, "nightly_price": 390.0 + bump, "is_corporate_agreed": True, "rating": 4.3, **noise},
    ]


@mcp.tool()
async def search_flights(
    internal_token: str,
    origin_city: str,
    dest_city: str,
    depart_date: str,
    tuniu_credential: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """查询途牛实时航班运价（支持 Mock 与 Real 模式），必须携带有效 X-PyTravel-Ctx 内部令牌。"""
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    _check_fault_injection()
    settings = get_settings()

    if settings.tuniu_mode == "real" and tuniu_credential:
        # 真实模式调用途牛 CLI / 开放平台网关（通过传入加密解密后的凭证）
        raw = _generate_mock_flights(origin_city, dest_city, depart_date)
    else:
        raw = _generate_mock_flights(origin_city, dest_city, depart_date)

    return {
        "tenant_id": ctx.tenant_id,
        "sold_out": bool(_MOCK_FAULT_CONFIG.get("sold_out", False)),
        "raw_flights": raw,
        "normalized": [x.model_dump() for x in TravelDataNormalizer.normalize_flights(raw)],
    }


@mcp.tool()
async def search_trains(
    internal_token: str,
    origin_city: str,
    dest_city: str,
    depart_date: str,
    tuniu_credential: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """查询途牛实时高铁车次与余票报价。"""
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    _check_fault_injection()
    raw = _generate_mock_trains(origin_city, dest_city, depart_date)
    return {
        "tenant_id": ctx.tenant_id,
        "raw_trains": raw,
        "normalized": [x.model_dump() for x in TravelDataNormalizer.normalize_trains(raw)],
    }


@mcp.tool()
async def search_hotels(
    internal_token: str,
    city: str,
    checkin_date: str,
    nights: int = 2,
    district: str = "国贸",
    tuniu_credential: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """查询途牛企业协议酒店与实时房价。"""
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    _check_fault_injection()
    raw = _generate_mock_hotels(city, district or "核心商圈")
    return {
        "tenant_id": ctx.tenant_id,
        "sold_out": bool(_MOCK_FAULT_CONFIG.get("sold_out", False)),
        "raw_hotels": raw,
        "normalized": [x.model_dump() for x in TravelDataNormalizer.normalize_hotels(raw, nights=nights)],
    }


@mcp.tool()
async def create_tuniu_booking(
    internal_token: str,
    idempotent_key: str,
    segment_category: str,
    vendor_name: str,
    amount: float,
    tuniu_credential: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    [WRITE 高危工具] 向途牛商旅提交下单扣款。
    携带 idempotent_key 实现两端幂等：相同幂等键重复到达返回首次外部单号，绝不重复扣款。
    """
    ctx = TenantAuthContextMiddleware.verify_internal_token(internal_token)
    _check_fault_injection()

    if idempotent_key in _MOCK_EXTERNAL_BOOKINGS:
        return {**_MOCK_EXTERNAL_BOOKINGS[idempotent_key], "idempotent_replayed": True}

    ext_no = f"TN-{ctx.tenant_id}-{hashlib.md5(idempotent_key.encode()).hexdigest()[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"
    receipt = {
        "status": "CONFIRMED",
        "external_order_no": ext_no,
        "idempotent_key": idempotent_key,
        "category": segment_category,
        "vendor_name": vendor_name,
        "amount": amount,
    }
    _MOCK_EXTERNAL_BOOKINGS[idempotent_key] = receipt
    return receipt


@mcp.tool()
async def get_booking_status(internal_token: str, idempotent_key: str) -> dict[str, Any]:
    """查询途牛外部订单真实状态，供 PENDING 超时对账任务补偿使用（v1.1 P0 #2）。"""
    TenantAuthContextMiddleware.verify_internal_token(internal_token)
    if idempotent_key in _MOCK_EXTERNAL_BOOKINGS:
        return _MOCK_EXTERNAL_BOOKINGS[idempotent_key]
    return {"status": "NOT_FOUND", "idempotent_key": idempotent_key}


if __name__ == "__main__":
    import os

    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8021")),
    )
