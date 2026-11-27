"""
三层意图路由与 RoundtripPlanner 多目标优化算法测试。
"""

from __future__ import annotations

from apps.agent_harness.graphs.roundtrip_planner import RoundtripPlanner
from apps.agent_harness.router.l3_llm import ThreeTierIntentRouter
from apps.agent_harness.state import HotelOption, TravelLeg


async def test_three_tier_router_shortcut_and_preconditions() -> None:
    router = ThreeTierIntentRouter()

    # 1. 高置信单意图 L1 短路 (<50ms，不调 LLM)
    r1 = await router.route("分析华东业务交付部第四季度差旅预算超支归因")
    assert r1.source == "RULE"
    assert r1.direct_route_target == "BudgetInsightSubgraph"
    assert r1.llm_called is False

    # 2. 复合意图不得错误短路为单子图
    r2 = await router.route("帮我订机票去北京出差，顺便查一下北京差旅标准")
    assert r2.direct_route_target == "MasterSupervisorGraph"
    assert len(r2.intents) >= 2

    # 3. 挂起断点粘性路由优先
    r3 = await router.route("就第二个吧", active_subgraph="TripPlanAndBookSubgraph", has_pending_hitl=True)
    assert r3.source == "STICKY"
    assert r3.direct_route_target == "TripPlanAndBookSubgraph"

    # 4. 含指代词不跳过改写
    r4 = await router.route("那个换成高铁吧", history_summaries=["刚才查询上海去北京的航班"])
    assert r4.needs_rewrite is True
    assert r4.rewritten_query is not None


def test_roundtrip_planner_outputs_four_distinct_options_and_strict_compliant() -> None:
    outbound = [
        TravelLeg(leg_id="F1", category="FLIGHT", carrier_or_train_no="吉祥 HO1251", depart_time="23:40", arrive_time="01:55", duration_minutes=135, cabin_or_seat="ECONOMY", price=490.0, is_red_eye=True),
        TravelLeg(leg_id="F2", category="FLIGHT", carrier_or_train_no="国航 CA1502", depart_time="09:00", arrive_time="11:10", duration_minutes=130, cabin_or_seat="ECONOMY", price=960.0),
        TravelLeg(leg_id="F3", category="FLIGHT", carrier_or_train_no="国航 CA1886", depart_time="12:00", arrive_time="14:00", duration_minutes=120, cabin_or_seat="BUSINESS", price=3200.0),
        TravelLeg(leg_id="T1", category="TRAIN", carrier_or_train_no="高铁 G12", depart_time="08:00", arrive_time="12:24", duration_minutes=264, cabin_or_seat="SECOND_CLASS", price=553.0),
    ]
    inbound = [
        TravelLeg(leg_id="F4", category="FLIGHT", carrier_or_train_no="东航 MU5102", depart_time="18:00", arrive_time="20:10", duration_minutes=130, cabin_or_seat="ECONOMY", price=920.0),
        TravelLeg(leg_id="T2", category="TRAIN", carrier_or_train_no="高铁 G15", depart_time="16:00", arrive_time="20:30", duration_minutes=270, cabin_or_seat="SECOND_CLASS", price=553.0),
    ]
    hotels = [
        HotelOption(hotel_id="H1", hotel_name="北京国贸亚朵酒店", brand="亚朵酒店", city="北京", district="国贸", nightly_price=620.0, total_price=1240.0, nights=2, is_corporate_agreed=True, rating=4.8),
        HotelOption(hotel_id="H2", hotel_name="北京国贸全季酒店", brand="全季酒店", city="北京", district="国贸", nightly_price=530.0, total_price=1060.0, nights=2, is_corporate_agreed=True, rating=4.6),
        HotelOption(hotel_id="H3", hotel_name="北京国贸大酒店", brand="国贸大酒店", city="北京", district="国贸", nightly_price=1200.0, total_price=2400.0, nights=2, is_corporate_agreed=False, rating=4.9),
    ]
    rule = {"flight_cabin": "ECONOMY", "train_seat": "SECOND_CLASS", "hotel_limit_per_night": 650.0}
    prefs = {"AIRLINE": "国航", "HOTEL_BRAND": "亚朵", "RED_EYE": "拒绝"}

    options, has_compliant = RoundtripPlanner().plan(outbound, hotels, inbound, rule, prefs)
    assert has_compliant is True
    assert len(options) == 4
    labels = {o.label for o in options}
    assert labels == {"最省钱", "最省时", "最合规", "最舒适"}
    compliant_opt = next(o for o in options if o.label == "最合规")
    assert compliant_opt.is_policy_compliant is True
    assert compliant_opt.policy_penalty == 0.0
