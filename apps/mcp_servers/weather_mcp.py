"""
城市天气预报 FastMCP Server（weather-mcp）。

对应文档章节：
  - 七、7.2 工具清单：get_city_weather_forecast（白名单过滤无关接口，降低 Tool Schema Token）
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from apps.agent_harness.middlewares.tenant_context import TenantAuthContextMiddleware

mcp = FastMCP("weather-mcp")

_CITY_WEATHER_PRESETS: dict[str, dict[str, Any]] = {
    "北京": {"condition": "晴转多云", "temp_c": "2°C ~ 11°C", "wind": "北风3-4级", "advice": "昼夜温差较大，建议携带防风外套或薄羽绒服，首都/大兴机场能见度良好。"},
    "上海": {"condition": "多云", "temp_c": "9°C ~ 16°C", "wind": "东南风3级", "advice": "气候温和偏湿，建议穿风衣或针织外套，虹桥枢纽运行正常。"},
    "深圳": {"condition": "阵雨转晴", "temp_c": "18°C ~ 25°C", "wind": "微风", "advice": "温暖湿润，建议携带折叠晴雨伞及薄衬衫。"},
    "杭州": {"condition": "阴有小雨", "temp_c": "8°C ~ 14°C", "wind": "东北风2-3级", "advice": "体感略凉，建议携带雨具与轻便外套。"},
    "成都": {"condition": "多云间阴", "temp_c": "9°C ~ 15°C", "wind": "微风", "advice": "早晚微凉，建议备好外套，双流/天府机场上午偶有薄雾。"},
}


@mcp.tool()
async def get_city_weather_forecast(internal_token: str, city: str, target_date: str = "") -> dict[str, Any]:
    """获取目的地城市近期天气预报与商务出行穿衣/航班延误提醒。"""
    TenantAuthContextMiddleware.verify_internal_token(internal_token)
    preset = _CITY_WEATHER_PRESETS.get(
        city,
        {"condition": "晴", "temp_c": "10°C ~ 18°C", "wind": "微风", "advice": "天气平稳，适合商务差旅出行。"},
    )
    return {"city": city, "date": target_date or "近期", **preset}


if __name__ == "__main__":
    import os

    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8024")),
    )
