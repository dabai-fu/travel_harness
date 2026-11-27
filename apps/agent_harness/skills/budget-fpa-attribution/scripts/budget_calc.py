"""
预算执行与超支归因确定性精算脚本（运行于隔离沙箱内）。

对应文档章节：
  - 三、模块 4（BudgetInsightSubgraph 三路并行精算）
  - 七、7.3 skills/budget-fpa-attribution/scripts/budget_calc.py
  - 十一、v1.1 P1 #11（int64 分向量化计算 + 输出层 Decimal）、P1 #12（无残差两因素量价拆分）、P2 #26（动态提前天数阈值）

使用方式（独立 CLI 进程调用，兼容 Docker 容器池与本地隔离沙箱）：
  python budget_calc.py --mode volume_price|behavior_premium|cost_saving \
      --expense-csv expense_detail.csv --budget-csv dept_budget.csv \
      --rules-json rules.json --year 2026 --quarter 4 --output out.json
"""

from __future__ import annotations

import argparse
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pandas as pd


def _str_yuan_to_cents(series: pd.Series) -> pd.Series:
    """
    将 CSV 字符串金额无损转换为 int64 分（cent），彻底规避浮点误差与 Decimal object 列性能退化。
    """
    cents = [
        int((Decimal(str(val).strip() or "0") * 100).to_integral_value(rounding=ROUND_HALF_UP))
        for val in series.tolist()
    ]
    return pd.Series(cents, index=series.index, dtype="int64")


def _cents_to_yuan_str(cents: int) -> str:
    """仅在最终输出层转为 Decimal 并四舍五入保留 2 位小数。"""
    val = (Decimal(int(cents)) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(val)


def _pct_str(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.00"
    val = (Decimal(int(numerator)) * Decimal("100") / Decimal(int(denominator))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return str(val)


def load_dataframes(expense_csv: Path, budget_csv: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """加载消费明细与部门预算表，所有金额列以字符串读取后转为 int64 分。"""
    exp_df = pd.read_csv(expense_csv, dtype={"amount": str, "policy_limit": str, "dept_id": str})
    bud_df = pd.read_csv(budget_csv, dtype={"budget_amount": str, "warn_threshold": str, "dept_id": str})

    if not exp_df.empty:
        exp_df["amount_cents"] = _str_yuan_to_cents(exp_df["amount"])
        exp_df["policy_limit_cents"] = _str_yuan_to_cents(exp_df["policy_limit"].fillna("0"))
        exp_df["fiscal_year"] = exp_df["fiscal_year"].astype(int)
        exp_df["fiscal_quarter"] = exp_df["fiscal_quarter"].astype(int)
        exp_df["advance_days"] = exp_df["advance_days"].astype(int)
        exp_df["min_advance_days_rule"] = exp_df["min_advance_days_rule"].astype(int)
        exp_df["is_policy_exceeded"] = exp_df["is_policy_exceeded"].astype(int)
        exp_df["travel_order_id"] = exp_df["travel_order_id"].astype(int)
    else:
        exp_df["amount_cents"] = pd.Series(dtype="int64")

    if not bud_df.empty:
        bud_df["budget_cents"] = _str_yuan_to_cents(bud_df["budget_amount"])
        bud_df["fiscal_year"] = bud_df["fiscal_year"].astype(int)
        bud_df["fiscal_quarter"] = bud_df["fiscal_quarter"].astype(int)
    else:
        bud_df["budget_cents"] = pd.Series(dtype="int64")

    return exp_df, bud_df


def run_volume_price_analysis(
    exp_df: pd.DataFrame,
    bud_df: pd.DataFrame,
    fiscal_year: int,
    fiscal_quarter: int,
) -> dict[str, Any]:
    """
    路径 A：部门预算执行率 + 无残差两因素量价拆分（v1.1 P1 #12）。
    公式：ΔC = (Q1 - Q0) * P0 + (P1 - P0) * Q1
    为保证整数分恒等成立，使用精确 Decimal 算术并把最终勾稽差归集验证为 0.00。
    """
    prev_quarter = 4 if fiscal_quarter == 1 else fiscal_quarter - 1
    prev_year = fiscal_year - 1 if fiscal_quarter == 1 else fiscal_year

    cur_bud = bud_df[(bud_df["fiscal_year"] == fiscal_year) & (bud_df["fiscal_quarter"] == fiscal_quarter)]
    cur_exp = exp_df[(exp_df["fiscal_year"] == fiscal_year) & (exp_df["fiscal_quarter"] == fiscal_quarter)]
    base_exp = exp_df[(exp_df["fiscal_year"] == prev_year) & (exp_df["fiscal_quarter"] == prev_quarter)]

    spent_by_dept = cur_exp.groupby("dept_id")["amount_cents"].sum().to_dict() if not cur_exp.empty else {}

    dept_metrics: dict[str, dict[str, str]] = {}
    bar_chart_data: list[dict[str, Any]] = []
    total_budget_cents = 0
    total_spent_cents = 0
    alert_depts: list[dict[str, Any]] = []

    for _, row in cur_bud.sort_values("dept_id").iterrows():
        did = str(row["dept_id"])
        dname = str(row["dept_name"])
        b_cents = int(row["budget_cents"])
        s_cents = int(spent_by_dept.get(did, 0))
        o_cents = s_cents - b_cents
        warn_th = Decimal(str(row["warn_threshold"]))

        total_budget_cents += b_cents
        total_spent_cents += s_cents
        exec_rate_str = _pct_str(s_cents, b_cents)

        dept_metrics[did] = {
            "dept_id": did,
            "dept_name": dname,
            "budget": _cents_to_yuan_str(b_cents),
            "spent": _cents_to_yuan_str(s_cents),
            "overspend": _cents_to_yuan_str(o_cents),
            "exec_rate": exec_rate_str,
        }
        bar_chart_data.append(
            {
                "name": dname,
                "dept_id": did,
                "预算金额": float(_cents_to_yuan_str(b_cents)),
                "实际支出": float(_cents_to_yuan_str(s_cents)),
            }
        )
        if b_cents > 0 and (Decimal(s_cents) / Decimal(b_cents)) >= warn_th:
            alert_depts.append(
                {
                    "dept_id": did,
                    "dept_name": dname,
                    "exec_rate": exec_rate_str,
                    "overspend": _cents_to_yuan_str(o_cents),
                }
            )

    # 无残差两因素量价拆分（在总费用层面，Q 为去重出差单人次，C 为总支出元）
    q0 = int(base_exp["travel_order_id"].nunique()) if not base_exp.empty else 0
    c0_cents = int(base_exp["amount_cents"].sum()) if not base_exp.empty else 0
    q1 = int(cur_exp["travel_order_id"].nunique()) if not cur_exp.empty else 0
    c1_cents = int(cur_exp["amount_cents"].sum()) if not cur_exp.empty else 0

    c0 = Decimal(c0_cents) / Decimal("100")
    c1 = Decimal(c1_cents) / Decimal("100")
    total_delta = (c1 - c0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if q0 > 0 and q1 > 0:
        p0 = c0 / Decimal(q0)
        # 量增效应: (Q1 - Q0) * P0
        vol_eff = (Decimal(q1 - q0) * p0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # 价升效应: 由 total_delta - vol_eff 精确闭环（数学上恒等于 (P1 - P0) * Q1，消除了 0.01 舍入残差）
        price_eff = (total_delta - vol_eff).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        vol_eff = total_delta
        price_eff = Decimal("0.00")

    # 月度支出趋势折线图
    line_data: list[dict[str, Any]] = []
    if not cur_exp.empty:
        cur_exp_copy = cur_exp.copy()
        cur_exp_copy["month"] = cur_exp_copy["depart_date"].astype(str).str.slice(0, 7)
        monthly = cur_exp_copy.groupby("month")["amount_cents"].sum().sort_index()
        for m, cents in monthly.items():
            line_data.append({"name": str(m), "月度支出": float(_cents_to_yuan_str(int(cents)))})

    pie_data = [
        {"name": "量增效应(出差频次)", "value": abs(float(vol_eff))},
        {"name": "价升效应(人均单价)", "value": abs(float(price_eff))},
    ]

    charts = [
        {
            "chart_id": "chart_dept_budget_vs_spent",
            "type": "bar",
            "title": f"{fiscal_year} Q{fiscal_quarter} 各部门差旅预算与实际支出对比 (元)",
            "x_key": "name",
            "series": [
                {"dataKey": "预算金额", "name": "季度预算", "fill": "#1677ff"},
                {"dataKey": "实际支出", "name": "实际支出", "fill": "#ff4d4f"},
            ],
            "data": bar_chart_data,
        },
        {
            "chart_id": "chart_monthly_trend",
            "type": "line",
            "title": f"{fiscal_year} Q{fiscal_quarter} 差旅消费月度走势 (元)",
            "x_key": "name",
            "series": [{"dataKey": "月度支出", "name": "实际支出", "stroke": "#13c2c2"}],
            "data": line_data,
        },
        {
            "chart_id": "chart_volume_price_pie",
            "type": "pie",
            "title": "环比支出变动归因贡献占比 (量增 vs 价升)",
            "x_key": "name",
            "series": [{"dataKey": "value", "name": "影响金额(绝对值)"}],
            "data": pie_data,
        },
    ]

    return {
        "worker": "volume_price_worker",
        "total": {
            "budget": _cents_to_yuan_str(total_budget_cents),
            "spent": _cents_to_yuan_str(total_spent_cents),
            "overspend": _cents_to_yuan_str(total_spent_cents - total_budget_cents),
            "exec_rate": _pct_str(total_spent_cents, total_budget_cents),
        },
        "depts": dept_metrics,
        "vp": {
            "q0": q0,
            "q1": q1,
            "volume_effect": str(vol_eff),
            "price_effect": str(price_eff),
            "total_delta": str(total_delta),
        },
        "alert_depts": alert_depts,
        "charts": charts,
    }


def run_behavior_premium_analysis(
    exp_df: pd.DataFrame,
    fiscal_year: int,
    fiscal_quarter: int,
) -> dict[str, Any]:
    """
    路径 B：预订行为与政策偏离归因（v1.1 P2 #26）。
    关键：临期判别条件直接取自每条记录绑定的差标规则 `advance_days < min_advance_days_rule`，
         不再硬编码 3 天！
    """
    cur_exp = exp_df[(exp_df["fiscal_year"] == fiscal_year) & (exp_df["fiscal_quarter"] == fiscal_quarter)]
    if cur_exp.empty:
        return {
            "worker": "behavior_premium_worker",
            "behavior": {"late_ratio": "0.00", "late_premium": "0.00", "hotel_exceed_ratio": "0.00"},
        }

    late_mask = cur_exp["advance_days"] < cur_exp["min_advance_days_rule"]
    total_cnt = len(cur_exp)
    late_cnt = int(late_mask.sum())

    # 临期机票溢价估算：临期机票均价高出非临期机票均价的净差额 × 临期机票张数
    flights = cur_exp[cur_exp["category"] == "FLIGHT"]
    late_flights = flights[flights["advance_days"] < flights["min_advance_days_rule"]]
    early_flights = flights[flights["advance_days"] >= flights["min_advance_days_rule"]]

    if not late_flights.empty and not early_flights.empty:
        late_avg = Decimal(int(late_flights["amount_cents"].sum())) / Decimal(len(late_flights))
        early_avg = Decimal(int(early_flights["amount_cents"].sum())) / Decimal(len(early_flights))
        diff_cents = max(Decimal("0"), late_avg - early_avg)
        late_premium_yuan = (diff_cents * Decimal(len(late_flights)) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        late_premium_yuan = Decimal("0.00")

    hotels = cur_exp[cur_exp["category"] == "HOTEL"]
    hotel_exceed_cnt = int((hotels["is_policy_exceeded"] == 1).sum()) if not hotels.empty else 0

    return {
        "worker": "behavior_premium_worker",
        "behavior": {
            "late_count": late_cnt,
            "total_segments": total_cnt,
            "late_ratio": _pct_str(late_cnt, total_cnt),
            "late_premium": str(late_premium_yuan),
            "hotel_exceed_ratio": _pct_str(hotel_exceed_cnt, max(1, len(hotels))),
        },
    }


def run_cost_saving_analysis(
    exp_df: pd.DataFrame,
    fiscal_year: int,
    fiscal_quarter: int,
) -> dict[str, Any]:
    """
    路径 C：高频城市/酒店集中度与协议降本测算。
    模型：Top 5 高频城市酒店消费按 12% 协议锁价折扣 + 临期订单提前 5 天改签减少 15% 票价溢价。
    """
    cur_exp = exp_df[(exp_df["fiscal_year"] == fiscal_year) & (exp_df["fiscal_quarter"] == fiscal_quarter)]
    if cur_exp.empty:
        return {
            "worker": "cost_saving_worker",
            "saving": {"top_city": "暂无", "top5_cities": [], "estimated_saving": "0.00"},
        }

    city_spend = cur_exp.groupby("city")["amount_cents"].sum().sort_values(ascending=False)
    top5_cities = [
        {"city": str(city), "spent": _cents_to_yuan_str(int(cents))}
        for city, cents in city_spend.head(5).items()
    ]
    top_city = top5_cities[0]["city"] if top5_cities else "暂无"

    top5_names = {x["city"] for x in top5_cities}
    top5_hotel_cents = int(
        cur_exp[(cur_exp["category"] == "HOTEL") & (cur_exp["city"].isin(top5_names))]["amount_cents"].sum()
    )
    late_flight_cents = int(
        cur_exp[(cur_exp["category"] == "FLIGHT") & (cur_exp["advance_days"] < cur_exp["min_advance_days_rule"])][
            "amount_cents"
        ].sum()
    )

    hotel_saving = (Decimal(top5_hotel_cents) * Decimal("0.12") / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    flight_saving = (Decimal(late_flight_cents) * Decimal("0.15") / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    total_saving = (hotel_saving + flight_saving).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "worker": "cost_saving_worker",
        "saving": {
            "top_city": top_city,
            "top5_cities": top5_cities,
            "hotel_agreement_saving": str(hotel_saving),
            "advance_policy_saving": str(flight_saving),
            "estimated_saving": str(total_saving),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PyTravel 沙箱预算归因确定性计算脚本")
    parser.add_argument("--mode", required=True, choices=["volume_price", "behavior_premium", "cost_saving"])
    parser.add_argument("--expense-csv", required=True)
    parser.add_argument("--budget-csv", required=True)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--quarter", type=int, default=4)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    exp_df, bud_df = load_dataframes(Path(args.expense_csv), Path(args.budget_csv))
    if args.mode == "volume_price":
        result = run_volume_price_analysis(exp_df, bud_df, args.year, args.quarter)
    elif args.mode == "behavior_premium":
        result = run_behavior_premium_analysis(exp_df, args.year, args.quarter)
    else:
        result = run_cost_saving_analysis(exp_df, args.year, args.quarter)

    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
