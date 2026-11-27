"""
往返行程多目标组合优化算法（RoundtripPlanner）。

对应文档章节：
  - 五、5.3 核心算法 1（RoundtripPlanner）
  - 十一、v1.1 P1 #13（方向统一为最小化 Cost、P5/P95 截尾 + ε 防除零、100%合规用硬约束、分段 Pareto 预裁剪）

核心算法步骤：
  1. **分段硬过滤与 Top-K 预裁剪（≤10^3）**：先对去程、酒店、返程各自按可行性过滤并取 Pareto/低价+低时 Top-K（K=8），
     把笛卡尔组合规模从数十万压缩到 <= 512 组，保证毫秒级规划响应；
  2. **P5/P95 截尾 + ε 防除零归一化**：避免个别天价头等舱拉偏整体区间，也避免所有价格相同时除以零；
  3. **统一越小越好的代价函数**：
     Cost = w_price * Norm(Price) + w_time * Norm(Duration) + w_policy * PolicyPenalty - w_pref * PrefScore
  4. **输出 4 套不重复代表方案**：
     - 最省钱（价格权重 0.75）
     - 最省时（耗时权重 0.75）
     - 最合规（**硬约束 PolicyPenalty == 0 子集**取最小 Cost，若子集为空明确提示无完全合规方案）
     - 最舒适（偏好与酒店评分权重优先）
"""

from __future__ import annotations

import hashlib
from itertools import product
from typing import Any

from apps.agent_harness.state import HotelOption, TravelLeg, TripOption

_EPSILON = 1e-6


def _p5_p95_norm(values: list[float]) -> list[float]:
    """P5/P95 截尾后执行 Min-Max 归一化（加 ε 防除零）。"""
    if not values:
        return []
    sorted_v = sorted(values)
    n = len(sorted_v)
    p5 = sorted_v[max(0, int(0.05 * (n - 1)))]
    p95 = sorted_v[min(n - 1, int(0.95 * (n - 1)))]
    span = (p95 - p5) + _EPSILON
    return [min(1.0, max(0.0, (v - p5) / span)) for v in values]


def _cabin_rank(cabin_or_seat: str) -> int:
    order = {
        "SECOND_CLASS": 1,
        "ECONOMY": 1,
        "FIRST_CLASS": 2,
        "PREMIUM_ECONOMY": 2,
        "BUSINESS": 3,
    }
    return order.get(cabin_or_seat, 1)


class RoundtripPlanner:
    """途牛往返交通 + 酒店多目标组合优化器。"""

    @staticmethod
    def _pre_prune_legs(legs: list[TravelLeg], reject_red_eye: bool, top_k: int = 8) -> list[TravelLeg]:
        """分段预裁剪：先过滤红眼（若用户拒绝），再按价格与耗时综合保留 Top-K。"""
        filtered = [x for x in legs if not (reject_red_eye and x.is_red_eye)]
        if not filtered:
            filtered = list(legs)
        filtered.sort(key=lambda x: (x.price * 0.6 + x.duration_minutes * 1.2))
        return filtered[:top_k]

    @staticmethod
    def _pre_prune_hotels(hotels: list[HotelOption], top_k: int = 8) -> list[HotelOption]:
        ranked = sorted(hotels, key=lambda h: (h.nightly_price - (60.0 if h.is_corporate_agreed else 0.0) - h.rating * 30.0))
        return ranked[:top_k]

    def plan(
        self,
        outbound_legs: list[TravelLeg],
        hotels: list[HotelOption],
        inbound_legs: list[TravelLeg],
        policy_rule: dict[str, Any] | None,
        user_prefs: dict[str, str] | None = None,
    ) -> tuple[list[TripOption], bool]:
        """
        执行多目标组合优化，返回 `(4套代表方案列表, 是否存在100%合规方案)`。
        """
        prefs = user_prefs or {}
        reject_red_eye = prefs.get("RED_EYE") == "拒绝"
        pref_airlines = [x.strip() for x in prefs.get("AIRLINE", "").split(",") if x.strip()]
        pref_hotels = [x.strip() for x in prefs.get("HOTEL_BRAND", "").split(",") if x.strip()]

        out_k = self._pre_prune_legs(outbound_legs, reject_red_eye=reject_red_eye)
        hotel_k = self._pre_prune_hotels(hotels)
        in_k = self._pre_prune_legs(inbound_legs, reject_red_eye=reject_red_eye)

        max_flight_rank = _cabin_rank(str((policy_rule or {}).get("flight_cabin", "ECONOMY")))
        max_train_rank = _cabin_rank(str((policy_rule or {}).get("train_seat", "SECOND_CLASS")))
        hotel_limit = float((policy_rule or {}).get("hotel_limit_per_night", 650.0))

        candidates: list[dict[str, Any]] = []
        for out_l, h, in_l in product(out_k, hotel_k, in_k):
            notes: list[str] = []
            penalty = 0.0

            # 检查去程、返程舱位/席别合规性
            for seg_label, leg in (("去程", out_l), ("返程", in_l)):
                allowed_rank = max_flight_rank if leg.category == "FLIGHT" else max_train_rank
                if _cabin_rank(leg.cabin_or_seat) > allowed_rank:
                    penalty += 0.6
                    notes.append(f"{seg_label}({leg.cabin_or_seat})超出差标等级")

            # 检查酒店每晚限额
            if h.nightly_price > hotel_limit:
                over_ratio = (h.nightly_price - hotel_limit) / max(1.0, hotel_limit)
                penalty += 0.5 + over_ratio
                notes.append(f"酒店每晚{h.nightly_price:.0f}元超出差标限额{hotel_limit:.0f}元")

            # 红眼提示
            if out_l.is_red_eye or in_l.is_red_eye:
                if reject_red_eye:
                    penalty += 0.4
                notes.append("含夜间红眼航班")

            # 计算个人长期偏好匹配分 [0, 1]
            pref_score = 0.0
            if any(a in out_l.carrier_or_train_no or a in in_l.carrier_or_train_no for a in pref_airlines):
                pref_score += 0.45
            if any(b in h.hotel_name or b in h.brand for b in pref_hotels):
                pref_score += 0.35
            if h.is_corporate_agreed:
                pref_score += 0.15
            pref_score += max(0.0, (h.rating - 4.0) * 0.1)
            pref_score = min(1.0, pref_score)

            total_price = round(out_l.price + h.total_price + in_l.price, 2)
            total_dur = out_l.duration_minutes + in_l.duration_minutes

            candidates.append(
                {
                    "outbound": out_l,
                    "hotel": h,
                    "inbound": in_l,
                    "total_price": total_price,
                    "total_duration": total_dur,
                    "policy_penalty": round(penalty, 4),
                    "pref_score": round(pref_score, 4),
                    "notes": notes,
                }
            )

        if not candidates:
            return [], False

        norm_prices = _p5_p95_norm([c["total_price"] for c in candidates])
        norm_durs = _p5_p95_norm([float(c["total_duration"]) for c in candidates])
        for idx, c in enumerate(candidates):
            c["norm_price"] = norm_prices[idx]
            c["norm_dur"] = norm_durs[idx]

        def _cost(c: dict[str, Any], wp: float, wt: float, wpol: float, wpref: float) -> float:
            return wp * c["norm_price"] + wt * c["norm_dur"] + wpol * c["policy_penalty"] - wpref * c["pref_score"]

        def _sig(c: dict[str, Any]) -> str:
            return f"{c['outbound'].leg_id}|{c['hotel'].hotel_id}|{c['inbound'].leg_id}"

        used_sigs: set[str] = set()
        selected: list[TripOption] = []

        def _pick(label: str, pool: list[dict[str, Any]], weights: tuple[float, float, float, float]) -> None:
            ranked = sorted(pool, key=lambda x: _cost(x, *weights))
            chosen = None
            for cand in ranked:
                if _sig(cand) not in used_sigs:
                    chosen = cand
                    break
            if chosen is None and ranked:
                chosen = ranked[0]
            if chosen is None:
                return
            used_sigs.add(_sig(chosen))
            sig_str = _sig(chosen)
            opt_hash = hashlib.sha256(sig_str.encode("utf-8")).hexdigest()[:16]
            cost_val = round(_cost(chosen, *weights), 4)
            selected.append(
                TripOption(
                    option_id=f"OPT-{len(selected) + 1}",
                    option_hash=opt_hash,
                    label=label,  # type: ignore[arg-type]
                    outbound=chosen["outbound"],
                    hotel=chosen["hotel"],
                    inbound=chosen["inbound"],
                    total_price=chosen["total_price"],
                    total_duration_minutes=chosen["total_duration"],
                    policy_penalty=chosen["policy_penalty"],
                    is_policy_compliant=(chosen["policy_penalty"] == 0.0),
                    pref_score=chosen["pref_score"],
                    cost_score=cost_val,
                    compliance_notes=chosen["notes"] or ["100% 符合公司差旅标准"],
                )
            )

        # 1. 最合规方案：严格在 PolicyPenalty == 0 的硬约束子集中选（v1.1 P1 #13）
        compliant_pool = [c for c in candidates if c["policy_penalty"] == 0.0]
        has_compliant = len(compliant_pool) > 0
        if has_compliant:
            _pick("最合规", compliant_pool, (0.45, 0.25, 0.0, 0.30))
        else:
            _pick("最合规", candidates, (0.20, 0.15, 0.85, 0.10))

        # 2. 最省钱方案
        _pick("最省钱", candidates, (0.75, 0.15, 0.10, 0.05))
        # 3. 最省时方案
        _pick("最省时", candidates, (0.15, 0.75, 0.10, 0.05))
        # 4. 最舒适方案（偏好与评分权重最高）
        _pick("最舒适", candidates, (0.20, 0.20, 0.20, 0.65))

        # 按固定展示顺序重排：最省钱 / 最省时 / 最合规 / 最舒适
        order_map = {"最省钱": 0, "最省时": 1, "最合规": 2, "最舒适": 3}
        selected.sort(key=lambda o: order_map.get(o.label, 99))
        for idx, opt in enumerate(selected, start=1):
            opt.option_id = f"OPT-{idx}"
        return selected, has_compliant
