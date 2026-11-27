"""
途牛商旅数据归一化与冗余裁剪器（TravelDataNormalizer）。

对应文档章节：
  - 三、模块 1（通过 TravelDataNormalizer 归一化为统一卡片模型）
  - 六、中间件 4（裁剪冗余 JSON 字段，降 40%~60% Tool Token）
  - 九、9.3 apps/mcp_servers/normalizer.py
"""

from __future__ import annotations

from typing import Any

from apps.agent_harness.state import HotelOption, TravelLeg


class TravelDataNormalizer:
    """将途牛或第三方 OTA 原始异构报文清洗、裁剪并归一化为强类型交通/酒店模型。"""

    @staticmethod
    def normalize_flights(raw_list: list[dict[str, Any]]) -> list[TravelLeg]:
        legs: list[TravelLeg] = []
        for item in raw_list:
            depart_time = str(item.get("depart_time", "08:00"))
            hour = int(depart_time.split(":")[0]) if ":" in depart_time else 8
            is_red_eye = hour >= 23 or hour < 6
            legs.append(
                TravelLeg(
                    leg_id=str(item["flight_no"]),
                    category="FLIGHT",
                    carrier_or_train_no=f"{item.get('airline', '未知航司')} {item['flight_no']}",
                    depart_time=depart_time,
                    arrive_time=str(item.get("arrive_time", "10:15")),
                    duration_minutes=int(item.get("duration_minutes", 135)),
                    cabin_or_seat=str(item.get("cabin", "ECONOMY")),
                    price=float(item["price"]),
                    is_direct=bool(item.get("is_direct", True)),
                    is_red_eye=is_red_eye,
                )
            )
        return legs

    @staticmethod
    def normalize_trains(raw_list: list[dict[str, Any]]) -> list[TravelLeg]:
        legs: list[TravelLeg] = []
        for item in raw_list:
            legs.append(
                TravelLeg(
                    leg_id=str(item["train_no"]),
                    category="TRAIN",
                    carrier_or_train_no=f"高铁 {item['train_no']}",
                    depart_time=str(item.get("depart_time", "09:00")),
                    arrive_time=str(item.get("arrive_time", "13:30")),
                    duration_minutes=int(item.get("duration_minutes", 270)),
                    cabin_or_seat=str(item.get("seat_type", "SECOND_CLASS")),
                    price=float(item["price"]),
                    is_direct=True,
                    is_red_eye=False,
                )
            )
        return legs

    @staticmethod
    def normalize_hotels(raw_list: list[dict[str, Any]], nights: int) -> list[HotelOption]:
        options: list[HotelOption] = []
        for item in raw_list:
            nightly = float(item["nightly_price"])
            options.append(
                HotelOption(
                    hotel_id=str(item["hotel_id"]),
                    hotel_name=str(item["hotel_name"]),
                    brand=str(item.get("brand", "通用连锁")),
                    city=str(item["city"]),
                    district=str(item.get("district", "核心商圈")),
                    nightly_price=nightly,
                    total_price=round(nightly * nights, 2),
                    nights=nights,
                    is_corporate_agreed=bool(item.get("is_corporate_agreed", False)),
                    rating=float(item.get("rating", 4.6)),
                )
            )
        return options
