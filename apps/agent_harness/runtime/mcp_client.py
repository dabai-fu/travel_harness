"""
统一 FastMCP 客户端路由中心（MCPClientHub）。

对应文档章节：
  - 四、4.1 标准化能力扩展层（FastMCP）
  - 七、7.2 4 个标准 MCP Server 工具清单
  - 十一、v1.1 P0 #7（跨进程调用传递 X-PyTravel-Ctx 短时内部令牌）

双模式设计：
  - `MCP_MODE=inprocess`：进程内直调 MCP 工具函数（零网络开销，用于本地单测与离线 Eval）；
  - `MCP_MODE=http`：通过 `fastmcp.Client` 以标准 Streamable HTTP 协议连接独立 MCP Server 容器
    （`tuniu-mcp` / `oa-mcp` / `erp-expense-mcp` / `weather-mcp`），实现真正的进程级解耦部署。
"""

from __future__ import annotations

import json
from typing import Any

from apps.common.config import get_settings


class MCPClientHub:
    """统一封装对 4 个 FastMCP Server 的调用，自动在进程内直调与 Streamable HTTP 间切换。"""

    @staticmethod
    async def _call_remote(server_url: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        headers: dict[str, str] = {}
        if arguments.get("internal_token"):
            headers["X-PyTravel-Ctx"] = str(arguments["internal_token"])

        transport = StreamableHttpTransport(url=server_url, headers=headers)
        async with Client(transport) as client:
            result = await client.call_tool(tool_name, arguments)
            if getattr(result, "data", None) is not None:
                return result.data
            if getattr(result, "structured_content", None) is not None:
                return result.structured_content
            content = getattr(result, "content", None) or []
            if content and hasattr(content[0], "text"):
                text_val = content[0].text
                try:
                    return json.loads(text_val)
                except Exception:
                    return text_val
            return {}

    @classmethod
    async def search_flights(
        cls,
        internal_token: str,
        origin_city: str,
        dest_city: str,
        depart_date: str,
        tuniu_credential: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.tuniu_mcp_url,
                "search_flights",
                {
                    "internal_token": internal_token,
                    "origin_city": origin_city,
                    "dest_city": dest_city,
                    "depart_date": depart_date,
                    "tuniu_credential": tuniu_credential,
                },
            )
        from apps.mcp_servers.tuniu_mcp import search_flights

        return await search_flights(internal_token, origin_city, dest_city, depart_date, tuniu_credential)

    @classmethod
    async def search_trains(
        cls,
        internal_token: str,
        origin_city: str,
        dest_city: str,
        depart_date: str,
        tuniu_credential: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.tuniu_mcp_url,
                "search_trains",
                {
                    "internal_token": internal_token,
                    "origin_city": origin_city,
                    "dest_city": dest_city,
                    "depart_date": depart_date,
                    "tuniu_credential": tuniu_credential,
                },
            )
        from apps.mcp_servers.tuniu_mcp import search_trains

        return await search_trains(internal_token, origin_city, dest_city, depart_date, tuniu_credential)

    @classmethod
    async def search_hotels(
        cls,
        internal_token: str,
        city: str,
        checkin_date: str,
        nights: int = 2,
        district: str = "国贸",
        tuniu_credential: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.tuniu_mcp_url,
                "search_hotels",
                {
                    "internal_token": internal_token,
                    "city": city,
                    "checkin_date": checkin_date,
                    "nights": nights,
                    "district": district,
                    "tuniu_credential": tuniu_credential,
                },
            )
        from apps.mcp_servers.tuniu_mcp import search_hotels

        return await search_hotels(internal_token, city, checkin_date, nights, district, tuniu_credential)

    @classmethod
    async def create_tuniu_booking(
        cls,
        internal_token: str,
        idempotent_key: str,
        segment_category: str,
        vendor_name: str,
        amount: float,
        tuniu_credential: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.tuniu_mcp_url,
                "create_tuniu_booking",
                {
                    "internal_token": internal_token,
                    "idempotent_key": idempotent_key,
                    "segment_category": segment_category,
                    "vendor_name": vendor_name,
                    "amount": amount,
                    "tuniu_credential": tuniu_credential,
                },
            )
        from apps.mcp_servers.tuniu_mcp import create_tuniu_booking

        return await create_tuniu_booking(
            internal_token, idempotent_key, segment_category, vendor_name, amount, tuniu_credential
        )

    @classmethod
    async def get_booking_status(cls, internal_token: str, idempotent_key: str) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.tuniu_mcp_url,
                "get_booking_status",
                {"internal_token": internal_token, "idempotent_key": idempotent_key},
            )
        from apps.mcp_servers.tuniu_mcp import get_booking_status

        return await get_booking_status(internal_token, idempotent_key)

    @classmethod
    async def list_employee_trips(cls, internal_token: str) -> list[dict[str, Any]]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.oa_mcp_url,
                "list_employee_trips",
                {"internal_token": internal_token},
            )
        from apps.mcp_servers.oa_mcp import list_employee_trips

        return await list_employee_trips(internal_token)

    @classmethod
    async def submit_travel_approval(
        cls,
        internal_token: str,
        origin_city: str,
        dest_city: str,
        dest_city_tier: str,
        start_date: str,
        end_date: str,
        purpose: str,
        estimated_amount: float,
        auto_approve: bool = True,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.oa_mcp_url,
                "submit_travel_approval",
                {
                    "internal_token": internal_token,
                    "origin_city": origin_city,
                    "dest_city": dest_city,
                    "dest_city_tier": dest_city_tier,
                    "start_date": start_date,
                    "end_date": end_date,
                    "purpose": purpose,
                    "estimated_amount": estimated_amount,
                    "auto_approve": auto_approve,
                },
            )
        from apps.mcp_servers.oa_mcp import submit_travel_approval

        return await submit_travel_approval(
            internal_token, origin_city, dest_city, dest_city_tier, start_date, end_date, purpose, estimated_amount, auto_approve
        )

    @classmethod
    async def send_oa_notification(
        cls,
        internal_token: str,
        dept_id: str,
        title: str,
        content: str,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.oa_mcp_url,
                "send_oa_notification",
                {"internal_token": internal_token, "dept_id": dept_id, "title": title, "content": content},
            )
        from apps.mcp_servers.oa_mcp import send_oa_notification

        return await send_oa_notification(internal_token, dept_id, title, content)

    @classmethod
    async def export_expense_csv_to_sandbox(
        cls,
        internal_token: str,
        conversation_id: str,
        fiscal_year: int = 2026,
        fiscal_quarter: int = 4,
        requested_dept_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.erp_mcp_url,
                "export_expense_csv_to_sandbox",
                {
                    "internal_token": internal_token,
                    "conversation_id": conversation_id,
                    "fiscal_year": fiscal_year,
                    "fiscal_quarter": fiscal_quarter,
                    "requested_dept_ids": requested_dept_ids,
                },
            )
        from apps.mcp_servers.erp_expense_mcp import export_expense_csv_to_sandbox

        return await export_expense_csv_to_sandbox(
            internal_token, conversation_id, fiscal_year, fiscal_quarter, requested_dept_ids
        )

    @classmethod
    async def submit_expense_draft(
        cls,
        internal_token: str,
        travel_order_id: int | None,
        total_amount: float,
        invoices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.erp_mcp_url,
                "submit_expense_draft",
                {
                    "internal_token": internal_token,
                    "travel_order_id": travel_order_id,
                    "total_amount": total_amount,
                    "invoices": invoices,
                },
            )
        from apps.mcp_servers.erp_expense_mcp import submit_expense_draft

        return await submit_expense_draft(internal_token, travel_order_id, total_amount, invoices)

    @classmethod
    async def get_city_weather_forecast(
        cls,
        internal_token: str,
        city: str,
        target_date: str = "",
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.mcp_mode == "http":
            return await cls._call_remote(
                settings.weather_mcp_url,
                "get_city_weather_forecast",
                {"internal_token": internal_token, "city": city, "target_date": target_date},
            )
        from apps.mcp_servers.weather_mcp import get_city_weather_forecast

        return await get_city_weather_forecast(internal_token, city, target_date)
