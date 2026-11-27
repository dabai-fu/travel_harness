"""
一键仿真造数脚本（支持 Memory 内存模式与真实 PostgreSQL 16 + pgvector + RLS 模式）。

对应文档章节：
  - 九、9.3 scripts/seed_mock_data.py
  - 十、10.1 离线评测（Eval）核心指标与真值来源

使用方式：
  - 内存验证：python scripts/seed_mock_data.py --mode memory
  - 生产 PG 灌数：python scripts/seed_mock_data.py --mode postgres
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.common.config import get_settings
from apps.common.crypto import encrypt_aes_gcm, hash_password, make_booking_idempotent_key

if TYPE_CHECKING:
    from apps.common.db import InMemoryRepository


def seed_into_repository(repo: InMemoryRepository) -> dict[str, Any]:
    """向 InMemoryRepository 灌入确定性仿真企业数据，并返回关键统计摘要。"""
    from apps.common.db import compute_text_embedding

    repo.reset()
    rng = random.Random(20261127)
    now = datetime(2026, 11, 20, 9, 0, 0, tzinfo=UTC)
    demo_pw_hash = hash_password("123456")

    # 1. 租户主表
    repo.sys_tenant["T001"] = {
        "id": "T001",
        "tenant_name": "星云科技有限公司",
        "status": 1,
        "monthly_token_quota": 10_000_000,
        "config_json": {"default_warn_threshold": 0.85, "enabled_skills": ["trip-planning", "expense-drafting", "budget-fpa-attribution"]},
        "created_at": now,
        "updated_at": now,
    }
    repo.sys_tenant["T002"] = {
        "id": "T002",
        "tenant_name": "远航智能制造集团",
        "status": 1,
        "monthly_token_quota": 5_000_000,
        "config_json": {"default_warn_threshold": 0.80, "enabled_skills": ["trip-planning", "budget-fpa-attribution"]},
        "created_at": now,
        "updated_at": now,
    }

    # 2. 部门组织树
    depts = [
        {"id": "D001", "tenant_id": "T001", "parent_id": None, "dept_name": "研发与交付中心", "dept_path": "/D001/", "manager_user_id": "u_mgr_d1"},
        {"id": "D002", "tenant_id": "T001", "parent_id": "D001", "dept_name": "华东业务交付部", "dept_path": "/D001/D002/", "manager_user_id": "u_mgr_d2"},
        {"id": "D003", "tenant_id": "T001", "parent_id": "D001", "dept_name": "基础架构部", "dept_path": "/D001/D003/", "manager_user_id": "u_mgr_d3"},
        {"id": "D004", "tenant_id": "T001", "parent_id": None, "dept_name": "财务与行政部", "dept_path": "/D004/", "manager_user_id": "u_fin_t1"},
        {"id": "D201", "tenant_id": "T002", "parent_id": None, "dept_name": "智能装备事业部", "dept_path": "/D201/", "manager_user_id": "u_mgr_t2"},
        {"id": "D202", "tenant_id": "T002", "parent_id": "D201", "dept_name": "华南供应链部", "dept_path": "/D201/D202/", "manager_user_id": None},
    ]
    for d in depts:
        repo.sys_dept[d["id"]] = {**d, "created_at": now, "updated_at": now}

    # 3. 账号与员工差旅档案（使用 PBKDF2-HMAC-SHA256 哈希）
    users = [
        ("u_emp_1", "T001", "alice", "EMPLOYEE", "D002", "华东业务交付部", "林小雨", "linxiaoyu", "P6", 6, "上海", "13812345678", "310101199506152218"),
        ("u_emp_2", "T001", "bob", "EMPLOYEE", "D003", "基础架构部", "张明", "zhangming", "P7", 7, "北京", "13987654321", "11010119920308112X"),
        ("u_mgr_d2", "T001", "mgr_east", "DEPT_MANAGER", "D002", "华东业务交付部", "陈志远", "chenzhiyuan", "P8", 8, "上海", "13611112222", "310104198811123311"),
        ("u_mgr_d3", "T001", "mgr_arch", "DEPT_MANAGER", "D003", "基础架构部", "周凯", "zhoukai", "P8", 8, "杭州", "13633334444", "330102198709214412"),
        ("u_mgr_d1", "T001", "vp_rd", "DEPT_MANAGER", "D001", "研发与交付中心", "王建国", "wangjianguo", "P9", 9, "上海", "13500001111", "310105198201015513"),
        ("u_fin_t1", "T001", "finance_bp", "FINANCE_ADMIN", "D004", "财务与行政部", "苏静", "sujing", "P8", 8, "上海", "13722223333", "310106198905056624"),
        ("u_admin_t1", "T001", "tenant_admin", "TENANT_ADMIN", "D004", "财务与行政部", "系统管理员", "admin", "P8", 8, "上海", "13799990000", "310106198501019999"),
        ("u_emp_t2", "T002", "carol", "EMPLOYEE", "D201", "智能装备事业部", "李雪", "lixue", "P6", 6, "深圳", "13588887777", "440304199608187725"),
        ("u_mgr_t2", "T002", "mgr_t2", "DEPT_MANAGER", "D201", "智能装备事业部", "赵强", "zhaoqiang", "P8", 8, "深圳", "13566665555", "440304198604128816"),
    ]
    for uid, tid, uname, role, did, dname, rname, pinyin, rlvl, rno, home, phone, idc in users:
        repo.user_account[uid] = {
            "id": uid,
            "tenant_id": tid,
            "username": uname,
            "password_hash": demo_pw_hash,
            "role": role,
            "status": 1,
            "perm_version": 1,
            "created_at": now,
            "updated_at": now,
        }
        p_cipher, _ = encrypt_aes_gcm(phone)
        i_cipher, _ = encrypt_aes_gcm(idc)
        repo.user_profile[uid] = {
            "user_id": uid,
            "tenant_id": tid,
            "dept_id": did,
            "dept_name": dname,
            "real_name": rname,
            "pinyin_name": pinyin,
            "rank_level": rlvl,
            "rank_no": rno,
            "home_city": home,
            "phone_cipher": p_cipher,
            "id_card_cipher": i_cipher,
            "email": f"{uname}@{tid.lower()}.example.com",
            "created_at": now,
            "updated_at": now,
        }

    # 4. 加密外部途牛凭证
    for tid, corp_code in [("T001", "TUNIU-CORP-XINGYUN"), ("T002", "TUNIU-CORP-YUANHANG")]:
        cred_json = json.dumps({"corp_id": corp_code, "api_key": f"tn_live_{tid.lower()}_99887766", "api_secret": f"sec_{tid.lower()}_abcd"})
        cipher, kver = encrypt_aes_gcm(cred_json)
        repo.tenant_credential.append(
            {
                "id": repo._next_id("tenant_credential"),
                "tenant_id": tid,
                "owner_user_id": None,
                "provider": "TUNIU",
                "credential_cipher": cipher,
                "key_version": kver,
                "is_active": 1,
                "created_at": now,
                "updated_at": now,
            }
        )

    # 5. 差旅政策规则表（12 条/租户，互不重叠）
    policy_matrix = [
        (1, 5, "TIER_1", "ECONOMY", "SECOND_CLASS", 550, 120, 80, 3, 2500),
        (1, 5, "TIER_2", "ECONOMY", "SECOND_CLASS", 420, 100, 60, 3, 2000),
        (1, 5, "TIER_3", "ECONOMY", "SECOND_CLASS", 320, 90, 50, 3, 1500),
        (6, 6, "TIER_1", "ECONOMY", "SECOND_CLASS", 650, 150, 100, 3, 3500),
        (6, 6, "TIER_2", "ECONOMY", "SECOND_CLASS", 500, 120, 80, 3, 2800),
        (6, 6, "TIER_3", "ECONOMY", "SECOND_CLASS", 380, 100, 60, 3, 2200),
        (7, 7, "TIER_1", "PREMIUM_ECONOMY", "FIRST_CLASS", 850, 180, 120, 2, 5000),
        (7, 7, "TIER_2", "PREMIUM_ECONOMY", "FIRST_CLASS", 650, 150, 100, 2, 4000),
        (7, 7, "TIER_3", "ECONOMY", "FIRST_CLASS", 480, 120, 80, 2, 3000),
        (8, 12, "TIER_1", "BUSINESS", "BUSINESS", 1200, 240, 150, 1, 10000),
        (8, 12, "TIER_2", "BUSINESS", "FIRST_CLASS", 900, 200, 120, 1, 8000),
        (8, 12, "TIER_3", "PREMIUM_ECONOMY", "FIRST_CLASS", 680, 160, 100, 1, 6000),
    ]
    for tid in ("T001", "T002"):
        offset = 0 if tid == "T001" else 50
        for rmin, rmax, ctier, fcab, tseat, hlim, meal, ltrans, adv, thresh in policy_matrix:
            repo.travel_policy_rule.append(
                {
                    "id": repo._next_id("travel_policy_rule"),
                    "tenant_id": tid,
                    "rank_min": rmin,
                    "rank_max": rmax,
                    "city_tier": ctier,
                    "flight_cabin": fcab,
                    "train_seat": tseat,
                    "hotel_limit_per_night": Decimal(str(hlim + offset)),
                    "meal_allowance_per_day": Decimal(str(meal)),
                    "local_transport_per_day": Decimal(str(ltrans)),
                    "min_advance_booking_days": adv,
                    "approval_threshold": Decimal(str(thresh)),
                    "created_at": now,
                    "updated_at": now,
                }
            )

    # 6. 政策 RAG 向量切片（1024 维）
    kb_docs = [
        (
            "PUBLIC",
            "PUBLIC",
            "TRAVEL_GUIDELINE",
            "doc_pub_1",
            "通用差旅出行指南 > 报销与发票规范",
            "员工出差结束后需在15个工作日内提交报销申请。机票需保留电子行程单，酒店需提供增值税专用或普通发票及水单；若遇航班延误或临时改签导致超标，需在报销单中填写说明。",
        ),
        (
            "T001",
            "TENANT",
            "TRAVEL_POLICY",
            "doc_t1_1",
            "星云科技差旅管理制度 v2026 > 提前预订与协议酒店",
            "星云科技规定：P6及以下员工前往一线城市需至少提前3天预订机票与酒店，优先选择全季、亚朵等途牛协议连锁酒店。单次出差预计费用超过3500元需升级至部门总监审批。",
        ),
        (
            "T001",
            "TENANT",
            "TRAVEL_POLICY",
            "doc_t1_2",
            "星云科技差旅管理制度 v2026 > 交通工具选择",
            "星云科技鼓励5小时以内直达高铁优先替代飞机；P6员工高铁默认二等座，飞机默认经济舱；红眼航班（23:00-06:00起飞）仅在紧急交付场景下审批通过。",
        ),
        (
            "T002",
            "TENANT",
            "TRAVEL_POLICY",
            "doc_t2_1",
            "远航制造保密差旅补充条款",
            "远航制造保密规则：厂区驻场技术支持人员享受每天260元特殊驻场津贴（仅限T002内部员工查阅，暗号：YUANHANG-260）。",
        ),
    ]
    for tid, scope, dtype, doc_id, tpath, content in kb_docs:
        repo.kb_policy_chunk.append(
            {
                "id": repo._next_id("kb_policy_chunk"),
                "tenant_id": tid,
                "doc_type": dtype,
                "doc_scope": scope,
                "doc_id": doc_id,
                "doc_version": 1,
                "title_path": tpath,
                "content": content,
                "embedding": compute_text_embedding(f"{tpath} {content}", dim=1024),
                "created_at": now,
            }
        )

    # 7. 部门季度差旅预算表
    budgets = [
        ("T001", "D001", "研发与交付中心", 2026, 3, "60000.00", "0.85"),
        ("T001", "D002", "华东业务交付部", 2026, 3, "95000.00", "0.85"),
        ("T001", "D003", "基础架构部", 2026, 3, "75000.00", "0.85"),
        ("T001", "D004", "财务与行政部", 2026, 3, "35000.00", "0.85"),
        ("T001", "D001", "研发与交付中心", 2026, 4, "65000.00", "0.85"),
        ("T001", "D002", "华东业务交付部", 2026, 4, "120000.00", "0.85"),
        ("T001", "D003", "基础架构部", 2026, 4, "80000.00", "0.85"),
        ("T001", "D004", "财务与行政部", 2026, 4, "35000.00", "0.85"),
        ("T002", "D201", "智能装备事业部", 2026, 4, "90000.00", "0.80"),
        ("T002", "D202", "华南供应链部", 2026, 4, "50000.00", "0.80"),
    ]
    for tid, did, dname, fy, fq, bamt, warn in budgets:
        repo.dept_budget.append(
            {
                "id": repo._next_id("dept_budget"),
                "tenant_id": tid,
                "dept_id": did,
                "dept_name": dname,
                "fiscal_year": fy,
                "fiscal_quarter": fq,
                "budget_amount": Decimal(bamt),
                "warn_threshold": Decimal(warn),
                "created_at": now,
                "updated_at": now,
            }
        )

    # 8. 员工长期偏好
    repo.upsert_user_preference_sync("T001", "u_emp_1", "AIRLINE", "中国国航,东方航空", source="EXPLICIT", confidence=0.95)
    repo.upsert_user_preference_sync("T001", "u_emp_1", "HOTEL_BRAND", "亚朵酒店,全季酒店", source="INFERRED", confidence=0.82)
    repo.upsert_user_preference_sync("T001", "u_emp_1", "RED_EYE", "拒绝", source="EXPLICIT", confidence=0.98)

    # 9. 1000+ 条差旅单与途牛预订明细
    city_catalog = [
        ("北京", "TIER_1", ["北京国贸大酒店", "北京国贸亚朵酒店", "北京朝阳全季酒店"]),
        ("上海", "TIER_1", ["上海陆家嘴亚朵酒店", "上海静安全季酒店", "上海虹桥万怡酒店"]),
        ("深圳", "TIER_1", ["深圳南山亚朵酒店", "深圳科技园全季酒店", "深圳福田香格里拉"]),
        ("杭州", "TIER_2", ["杭州西湖亚朵酒店", "杭州滨江全季酒店"]),
        ("成都", "TIER_2", ["成都高新亚朵酒店", "成都春熙路全季酒店"]),
        ("南京", "TIER_2", ["南京新街口全季酒店", "南京河西亚朵酒店"]),
    ]

    dept_profiles = [
        ("T001", "D002", "u_emp_1", 6, "上海", 48, 86, 0.58, 1.32),
        ("T001", "D003", "u_emp_2", 7, "北京", 38, 42, 0.22, 1.03),
        ("T001", "D001", "u_mgr_d1", 9, "上海", 26, 28, 0.18, 1.02),
        ("T001", "D004", "u_fin_t1", 8, "上海", 16, 15, 0.12, 0.96),
        ("T002", "D201", "u_emp_t2", 6, "深圳", 30, 35, 0.25, 1.05),
    ]

    order_count = 0
    booking_count = 0

    for tid, did, uid, rank_no, home, q3_cnt, q4_cnt, late_q4, mult_q4 in dept_profiles:
        for quarter, trip_cnt, q_start, q_end, late_ratio, p_mult in [
            (3, q3_cnt, date(2026, 7, 1), date(2026, 9, 25), 0.15, 1.00),
            (4, q4_cnt, date(2026, 10, 1), date(2026, 11, 25), late_q4, mult_q4),
        ]:
            span_days = (q_end - q_start).days
            for idx in range(trip_cnt):
                if did == "D002":
                    dest, ctier, hotels = city_catalog[rng.choices([0, 1, 2, 3, 4], weights=[40, 25, 20, 10, 5])[0]]
                else:
                    dest, ctier, hotels = rng.choice(city_catalog)
                if dest == home:
                    dest, ctier, hotels = city_catalog[0] if home != "北京" else city_catalog[1]

                depart_d = q_start + timedelta(days=rng.randint(0, max(1, span_days)))
                nights = rng.randint(2, 3)
                end_d = depart_d + timedelta(days=nights)

                rule = repo.get_policy_rule_sync(tid, rank_no, ctier)
                hotel_limit = float(rule["hotel_limit_per_night"]) if rule else 650.0
                min_adv = int(rule["min_advance_booking_days"]) if rule else 3

                is_late = rng.random() < late_ratio
                adv_days = rng.randint(0, max(0, min_adv - 1)) if is_late else rng.randint(min_adv + 1, 12)
                booked_dt = datetime(depart_d.year, depart_d.month, depart_d.day, 10, 0, tzinfo=UTC) - timedelta(days=adv_days)

                late_premium_factor = rng.uniform(1.25, 1.42) if is_late else 1.0
                flight_out = round(rng.uniform(760, 1180) * p_mult * late_premium_factor, 2)
                flight_back = round(rng.uniform(740, 1150) * p_mult * late_premium_factor, 2)
                hotel_nightly = round(rng.uniform(hotel_limit * 0.78, hotel_limit * (1.22 if is_late else 1.02)) * p_mult, 2)
                hotel_total = round(hotel_nightly * nights, 2)

                order = repo.create_travel_order_sync(
                    {
                        "tenant_id": tid,
                        "order_no": f"TRV-2026Q{quarter}-{tid}-{did}-{idx + 1:04d}",
                        "user_id": uid,
                        "dept_id": did,
                        "origin_city": home,
                        "dest_city": dest,
                        "dest_city_tier": ctier,
                        "start_date": depart_d,
                        "end_date": end_d,
                        "purpose": f"{dest}客户项目交付与技术对接",
                        "estimated_amount": round(flight_out + flight_back + hotel_total, 2),
                        "status": "COMPLETED",
                        "oa_external_id": f"OA-{tid}-{quarter}-{idx + 1:04d}",
                    }
                )
                order_count += 1

                hotel_name = hotels[0] if idx % 2 == 0 else rng.choice(hotels)
                flight_limit = Decimal("1200.00")
                hotel_total_limit = Decimal(str(hotel_limit * nights))

                segments = [
                    (1, "FLIGHT", dest, rng.choice(["中国国航 CA1501", "东方航空 MU5103", "南方航空 CZ3102"]), Decimal(str(flight_out)), flight_limit),
                    (2, "HOTEL", dest, hotel_name, Decimal(str(hotel_total)), hotel_total_limit),
                    (3, "FLIGHT", home, rng.choice(["中国国航 CA1502", "东方航空 MU5104", "南方航空 CZ3108"]), Decimal(str(flight_back)), flight_limit),
                ]
                opt_hash = hashlib.md5(f"{order['id']}".encode()).hexdigest()[:12]
                for seg_no, cat, city, vendor, amt, plim in segments:
                    ikey = make_booking_idempotent_key(tid, order["id"], opt_hash, seg_no)
                    rec = repo.create_booking_pending_sync(
                        {
                            "tenant_id": tid,
                            "idempotent_key": ikey,
                            "travel_order_id": order["id"],
                            "segment_no": seg_no,
                            "user_id": uid,
                            "dept_id": did,
                            "category": cat,
                            "city": city,
                            "vendor_name": vendor,
                            "amount": amt,
                            "policy_limit": plim,
                            "is_policy_exceeded": 1 if amt > plim else 0,
                            "booked_at": booked_dt,
                            "depart_date": depart_d,
                            "raw_detail_json": {
                                "fiscal_year": 2026,
                                "fiscal_quarter": quarter,
                                "nights": nights if cat == "HOTEL" else 0,
                                "is_late_booking": is_late,
                                "min_advance_days_rule": min_adv,
                                "advance_days": adv_days,
                            },
                        }
                    )
                    repo.update_booking_status_sync(
                        tid,
                        rec["id"],
                        "CONFIRMED",
                        external_order_no=f"TN-{tid}-{rec['id']:06d}",
                    )
                    booking_count += 1

    upcoming_d = date(2026, 12, 10)
    demo_order = repo.create_travel_order_sync(
        {
            "tenant_id": "T001",
            "order_no": "TRV-2026-DEMO-001",
            "user_id": "u_emp_1",
            "dept_id": "D002",
            "origin_city": "上海",
            "dest_city": "北京",
            "dest_city_tier": "TIER_1",
            "start_date": upcoming_d,
            "end_date": upcoming_d + timedelta(days=2),
            "purpose": "北京重点客户年度架构复盘",
            "estimated_amount": "2880.00",
            "status": "APPROVED",
            "oa_external_id": "OA-DEMO-2026-001",
        }
    )
    order_count += 1

    return {
        "tenants": len(repo.sys_tenant),
        "depts": len(repo.sys_dept),
        "users": len(repo.user_account),
        "travel_orders": order_count,
        "booking_records": booking_count,
        "demo_order_id": demo_order["id"],
    }


async def seed_postgres_from_memory(repo: InMemoryRepository) -> dict[str, Any]:
    """
    将确定性仿真数据幂等写入真实 PostgreSQL 16 数据库（生产初始化使用）。
    注：写入每个租户表时遵循 RLS，显式设置 `SET LOCAL app.tenant_id`。
    """
    from apps.common.db import _vector_literal

    settings = get_settings()
    engine = create_async_engine(settings.database_admin_url or settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as s:
            async with s.begin():
                # 1. sys_tenant（全局表，无 RLS）
                for t in repo.sys_tenant.values():
                    await s.execute(
                        text(
                            "INSERT INTO sys_tenant (id, tenant_name, status, monthly_token_quota, config_json) "
                            "VALUES (:id, :name, :st, :q, CAST(:cfg AS jsonb)) "
                            "ON CONFLICT (id) DO UPDATE SET tenant_name = EXCLUDED.tenant_name, monthly_token_quota = EXCLUDED.monthly_token_quota"
                        ),
                        {"id": t["id"], "name": t["tenant_name"], "st": t["status"], "q": t["monthly_token_quota"], "cfg": json.dumps(t["config_json"], ensure_ascii=False)},
                    )

                # 判断是否已经做过全量业务数据初始化（幂等保护）
                existing_orders = (await s.execute(text("SELECT COUNT(*) FROM travel_order"))).scalar_one()
                if existing_orders > 0:
                    return {"status": "already_seeded", "existing_travel_orders": existing_orders}

                # 2. 按租户灌入组织、用户、凭证、差标、预算、偏好与订单明细
                for d in repo.sys_dept.values():
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": d["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO sys_dept (id, tenant_id, parent_id, dept_name, dept_path, manager_user_id) "
                            "VALUES (:id, :tid, :pid, :name, :path, :mgr) ON CONFLICT (id) DO NOTHING"
                        ),
                        {"id": d["id"], "tid": d["tenant_id"], "pid": d["parent_id"], "name": d["dept_name"], "path": d["dept_path"], "mgr": d["manager_user_id"]},
                    )

                for u in repo.user_account.values():
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": u["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO user_account (id, tenant_id, username, password_hash, role, status, perm_version) "
                            "VALUES (:id, :tid, :un, :pw, :role, :st, :pv) ON CONFLICT (id) DO NOTHING"
                        ),
                        {"id": u["id"], "tid": u["tenant_id"], "un": u["username"], "pw": u["password_hash"], "role": u["role"], "st": u["status"], "pv": u["perm_version"]},
                    )

                for p in repo.user_profile.values():
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": p["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO user_profile (user_id, tenant_id, dept_id, dept_name, real_name, pinyin_name, rank_level, rank_no, home_city, phone_cipher, id_card_cipher, email) "
                            "VALUES (:uid, :tid, :did, :dn, :rn, :py, :rl, :rno, :hc, :pc, :ic, :em) ON CONFLICT (user_id) DO NOTHING"
                        ),
                        {
                            "uid": p["user_id"], "tid": p["tenant_id"], "did": p["dept_id"], "dn": p["dept_name"],
                            "rn": p["real_name"], "py": p["pinyin_name"], "rl": p["rank_level"], "rno": p["rank_no"],
                            "hc": p["home_city"], "pc": p["phone_cipher"], "ic": p["id_card_cipher"], "em": p["email"],
                        },
                    )

                for c in repo.tenant_credential:
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": c["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO tenant_credential (tenant_id, owner_user_id, provider, credential_cipher, key_version, is_active) "
                            "VALUES (:tid, :uid, :prov, :cipher, :kver, :act)"
                        ),
                        {"tid": c["tenant_id"], "uid": c["owner_user_id"], "prov": c["provider"], "cipher": c["credential_cipher"], "kver": c["key_version"], "act": c["is_active"]},
                    )

                for r in repo.travel_policy_rule:
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": r["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO travel_policy_rule (tenant_id, rank_min, rank_max, city_tier, flight_cabin, train_seat, hotel_limit_per_night, meal_allowance_per_day, local_transport_per_day, min_advance_booking_days, approval_threshold) "
                            "VALUES (:tid, :rmin, :rmax, :ct, :fc, :ts, :hl, :ma, :lt, :adv, :th)"
                        ),
                        {
                            "tid": r["tenant_id"], "rmin": r["rank_min"], "rmax": r["rank_max"], "ct": r["city_tier"],
                            "fc": r["flight_cabin"], "ts": r["train_seat"], "hl": r["hotel_limit_per_night"],
                            "ma": r["meal_allowance_per_day"], "lt": r["local_transport_per_day"],
                            "adv": r["min_advance_booking_days"], "th": r["approval_threshold"],
                        },
                    )

                for k in repo.kb_policy_chunk:
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": k["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO kb_policy_chunk (tenant_id, doc_type, doc_scope, doc_id, doc_version, title_path, content, embedding) "
                            "VALUES (:tid, :dt, :ds, :did, :dv, :tp, :ct, CAST(:emb AS vector))"
                        ),
                        {
                            "tid": k["tenant_id"], "dt": k["doc_type"], "ds": k["doc_scope"], "did": k["doc_id"],
                            "dv": k["doc_version"], "tp": k["title_path"], "ct": k["content"],
                            "emb": _vector_literal(k["embedding"]),
                        },
                    )

                for b in repo.dept_budget:
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": b["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO dept_budget (tenant_id, dept_id, dept_name, fiscal_year, fiscal_quarter, budget_amount, warn_threshold) "
                            "VALUES (:tid, :did, :dn, :fy, :fq, :ba, :wt)"
                        ),
                        {
                            "tid": b["tenant_id"], "did": b["dept_id"], "dn": b["dept_name"],
                            "fy": b["fiscal_year"], "fq": b["fiscal_quarter"],
                            "ba": b["budget_amount"], "wt": b["warn_threshold"],
                        },
                    )

                for pr in repo.user_preference:
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": pr["tenant_id"]})
                    await s.execute(
                        text(
                            "INSERT INTO user_preference (tenant_id, user_id, pref_key, pref_value, source, confidence, embedding) "
                            "VALUES (:tid, :uid, :pk, :pv, :src, :conf, CAST(:emb AS vector))"
                        ),
                        {
                            "tid": pr["tenant_id"], "uid": pr["user_id"], "pk": pr["pref_key"], "pv": pr["pref_value"],
                            "src": pr["source"], "conf": pr["confidence"], "emb": _vector_literal(pr["embedding"]),
                        },
                    )

                # 3. 插入差旅单并映射新自增 ID 给 booking_record
                oid_map: dict[int, int] = {}
                for o in repo.travel_order.values():
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": o["tenant_id"]})
                    new_id = (
                        await s.execute(
                            text(
                                "INSERT INTO travel_order (tenant_id, order_no, user_id, dept_id, origin_city, dest_city, dest_city_tier, start_date, end_date, purpose, estimated_amount, status, oa_external_id, version) "
                                "VALUES (:tid, :ono, :uid, :did, :orig, :dest, :tier, :sd, :ed, :purp, :amt, :st, :oa, :ver) RETURNING id"
                            ),
                            {
                                "tid": o["tenant_id"], "ono": o["order_no"], "uid": o["user_id"], "did": o["dept_id"],
                                "orig": o["origin_city"], "dest": o["dest_city"], "tier": o["dest_city_tier"],
                                "sd": o["start_date"], "ed": o["end_date"], "purp": o["purpose"],
                                "amt": o["estimated_amount"], "st": o["status"], "oa": o["oa_external_id"], "ver": o["version"],
                            },
                        )
                    ).scalar_one()
                    oid_map[o["id"]] = int(new_id)

                for bk in repo.booking_record.values():
                    await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": bk["tenant_id"]})
                    mapped_oid = oid_map[bk["travel_order_id"]]
                    await s.execute(
                        text(
                            "INSERT INTO booking_record (tenant_id, idempotent_key, travel_order_id, segment_no, user_id, dept_id, provider, external_order_no, category, city, vendor_name, amount, policy_limit, is_policy_exceeded, booked_at, depart_date, booking_status, raw_detail_json) "
                            "VALUES (:tid, :ikey, :oid, :seg, :uid, :did, :prov, :ext, :cat, :city, :vend, :amt, :plim, :ex, :bat, :ddate, :st, CAST(:raw AS jsonb))"
                        ),
                        {
                            "tid": bk["tenant_id"], "ikey": bk["idempotent_key"], "oid": mapped_oid, "seg": bk["segment_no"],
                            "uid": bk["user_id"], "did": bk["dept_id"], "prov": bk["provider"], "ext": bk["external_order_no"],
                            "cat": bk["category"], "city": bk["city"], "vend": bk["vendor_name"], "amt": bk["amount"],
                            "plim": bk["policy_limit"], "ex": bk["is_policy_exceeded"], "bat": bk["booked_at"],
                            "ddate": bk["depart_date"], "st": bk["booking_status"],
                            "raw": json.dumps(bk["raw_detail_json"], ensure_ascii=False, default=str),
                        },
                    )
        return {"status": "seeded_postgres", "travel_orders": len(repo.travel_order), "booking_records": len(repo.booking_record)}
    finally:
        await engine.dispose()


def compute_budget_ground_truth(
    repo: InMemoryRepository,
    tenant_id: str,
    dept_ids: tuple[str, ...],
    fiscal_year: int = 2026,
    fiscal_quarter: int = 4,
) -> dict[str, Any]:
    """使用纯 Python Decimal + int64 分精确计算预算执行真值（Eval 100% 核对基准）。"""
    budgets = repo.list_dept_budgets_sync(tenant_id, dept_ids, fiscal_year, fiscal_quarter)
    q_start = date(fiscal_year, (fiscal_quarter - 1) * 3 + 1, 1)
    next_q_month = fiscal_quarter * 3 + 1
    q_end = (date(fiscal_year + 1, 1, 1) if next_q_month > 12 else date(fiscal_year, next_q_month, 1)) - timedelta(days=1)

    bookings = repo.list_bookings_sync(
        tenant_id=tenant_id,
        dept_ids=dept_ids,
        start_date=q_start,
        end_date=q_end,
    )
    dept_spent_cents: dict[str, int] = {b["dept_id"]: 0 for b in budgets}
    for bk in bookings:
        if bk["booking_status"] != "CONFIRMED":
            continue
        cents = int((Decimal(str(bk["amount"])) * 100).to_integral_value(rounding=ROUND_HALF_UP))
        dept_spent_cents[bk["dept_id"]] = dept_spent_cents.get(bk["dept_id"], 0) + cents

    total_budget_cents = 0
    total_spent_cents = 0
    per_dept: dict[str, dict[str, str]] = {}
    for b in budgets:
        did = b["dept_id"]
        b_cents = int((Decimal(str(b["budget_amount"])) * 100).to_integral_value(rounding=ROUND_HALF_UP))
        s_cents = dept_spent_cents.get(did, 0)
        total_budget_cents += b_cents
        total_spent_cents += s_cents
        exec_rate = (Decimal(s_cents) / Decimal(b_cents) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if b_cents else Decimal("0.00")
        overspend = (Decimal(s_cents - b_cents) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        per_dept[did] = {
            "dept_name": b["dept_name"],
            "budget_yuan": str((Decimal(b_cents) / Decimal("100")).quantize(Decimal("0.01"))),
            "spent_yuan": str((Decimal(s_cents) / Decimal("100")).quantize(Decimal("0.01"))),
            "overspend_yuan": str(overspend),
            "exec_rate_pct": str(exec_rate),
        }

    return {
        "total_budget_yuan": str((Decimal(total_budget_cents) / Decimal("100")).quantize(Decimal("0.01"))),
        "total_spent_yuan": str((Decimal(total_spent_cents) / Decimal("100")).quantize(Decimal("0.01"))),
        "total_overspend_yuan": str((Decimal(total_spent_cents - total_budget_cents) / Decimal("100")).quantize(Decimal("0.01"))),
        "depts": per_dept,
    }


if __name__ == "__main__":
    from apps.common.db import InMemoryRepository

    parser = argparse.ArgumentParser(description="PyTravel 仿真造数工具")
    parser.add_argument("--mode", choices=["memory", "postgres"], default="memory")
    args = parser.parse_args()

    mem_repo = InMemoryRepository()
    summary = seed_into_repository(mem_repo)
    truth = compute_budget_ground_truth(mem_repo, "T001", ("D001", "D002", "D003", "D004"), 2026, 4)

    if args.mode == "postgres":
        pg_res = asyncio.run(seed_postgres_from_memory(mem_repo))
        print("=== PostgreSQL 生产造数结果 ===")
        print(json.dumps(pg_res, ensure_ascii=False, indent=2))
    else:
        print("=== Memory 仿真造数完成 ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("=== T001 2026 Q4 预算执行精确真值 (Decimal) ===")
        print(json.dumps(truth, ensure_ascii=False, indent=2))
        Path("scripts/seed_truth_summary.json").write_text(
            json.dumps({"summary": summary, "truth_t001_q4": truth}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
