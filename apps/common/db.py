"""
数据库与仓储抽象层（PostgreSQL 16 + pgvector + RLS 与零依赖内存仿真库双模式）。

对应文档章节：
  - 二、2.2 四层隔离之存储与检索隔离层（DB / RAG / RLS）
  - 八、8.1 PostgreSQL 核心表 DDL（16 张领域表）
  - 十一、v1.1 优化记录 #9, #18, #19, #20, #21

核心设计说明：
  1. **生产模式（`APP_STORAGE_MODE=postgres`）**：
     `PostgresRepository` 使用 SQLAlchemy 2.0 + `asyncpg` 连接非 superuser 账号 `pytravel_app`；
     通过 `tenant_db_session(tenant_id)` 在每个事务开头执行 `SET LOCAL app.tenant_id = :tid`
     激活 PostgreSQL 行级安全（RLS），与 SQL `WHERE tenant_id = ...` 构成双保险。
  2. **仿真模式（`APP_STORAGE_MODE=memory`）**：
     `InMemoryRepository` 与 `PostgresRepository` 实现完全一致的异步接口，在内存中严格模拟
     16 张表结构、RLS 隔离、部门物化路径权限树、乐观锁版本校验及幂等键唯一约束。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from apps.common.config import get_settings


class RepositoryError(Exception):
    """仓储层通用异常。"""


class DuplicateIdempotentKeyError(RepositoryError):
    """途牛预订幂等键冲突（等价于 PostgreSQL UNIQUE 约束违规 23505）。"""


class OptimisticLockError(RepositoryError):
    """乐观锁版本冲突（等价于 UPDATE ... WHERE version = :old_ver 影响 0 行）。"""


class CrossTenantAccessError(RepositoryError):
    """跨租户或跨平行部门越权访问异常。"""


# ==============================================================================
# 1. PostgreSQL 16 异步引擎与 RLS 会话上下文管理器
# ==============================================================================
_pg_engine: AsyncEngine | None = None
_pg_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_pg_session_factory() -> async_sessionmaker[AsyncSession]:
    """懒加载创建 SQLAlchemy AsyncEngine 与 Session 工厂（使用非 superuser 业务连接串）。"""
    global _pg_engine, _pg_session_factory
    if _pg_session_factory is None:
        settings = get_settings()
        _pg_engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
            echo=False,
        )
        _pg_session_factory = async_sessionmaker(_pg_engine, expire_on_commit=False)
    return _pg_session_factory


async def dispose_pg_engine() -> None:
    """服务关闭时优雅释放连接池。"""
    global _pg_engine, _pg_session_factory
    if _pg_engine is not None:
        await _pg_engine.dispose()
        _pg_engine = None
        _pg_session_factory = None


@asynccontextmanager
async def pg_session() -> AsyncIterator[AsyncSession]:
    """普通数据库事务上下文（仅用于访问无 RLS 的全局系统表 sys_tenant）。"""
    factory = get_pg_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session


@asynccontextmanager
async def tenant_db_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """
    绑定租户行级安全（RLS）的 PostgreSQL 异步事务上下文（v1.1 P1 #20）。

    关键原理：
      在事务开头执行 `SET LOCAL app.tenant_id = :tid`，即使手写 SQL 漏了 WHERE 条件，
      PostgreSQL 的 `FORCE ROW LEVEL SECURITY` 也会阻止读取或修改其他租户数据。
    """
    factory = get_pg_session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": tenant_id},
            )
            yield session


# ==============================================================================
# 2. 文本向量化器（1024 维，严格对齐 DDL kb_policy_chunk.embedding VECTOR(1024)）
# ==============================================================================
def compute_text_embedding(text_input: str, dim: int = 1024) -> list[float]:
    """
    基于字符 N-gram 哈希的确定性 1024 维 L2 归一化向量生成器。
    用于离线/Mock 场景及无独立 Embedding API 时的向量索引写入；生产亦可接入 DashScope text-embedding-v3。
    """
    vec = [0.0] * dim
    cleaned = text_input.strip().lower()
    if not cleaned:
        return vec
    tokens = list(cleaned) + [cleaned[i : i + 2] for i in range(max(0, len(cleaned) - 1))]
    for tok in tokens:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        weight = 1.5 if len(tok) == 2 else 0.8
        vec[idx] += sign * weight
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _vector_literal(vec: list[float]) -> str:
    """将浮点列表序列化为 pgvector 标准文本格式 '[0.123456,...]'。"""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


# ==============================================================================
# 3. 生产级 PostgreSQL 16 + pgvector + RLS 异步仓储
# ==============================================================================
class PostgresRepository:
    """
    面向真实 PostgreSQL 16 + pgvector 的异步仓储实现。
    所有多租户表访问全部运行在 `tenant_db_session(tenant_id)` 下，受数据库层 RLS 强制保护。
    """

    async def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        async with pg_session() as s:
            row = (
                await s.execute(
                    text("SELECT * FROM sys_tenant WHERE id = :tid"),
                    {"tid": tenant_id},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def get_user_by_username(self, tenant_id: str, username: str) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT * FROM user_account WHERE tenant_id = :tid AND username = :uname AND status = 1"
                    ),
                    {"tid": tenant_id, "uname": username},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def get_user_account(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text("SELECT * FROM user_account WHERE tenant_id = :tid AND id = :uid"),
                    {"tid": tenant_id, "uid": user_id},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def get_user_profile(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text("SELECT * FROM user_profile WHERE tenant_id = :tid AND user_id = :uid"),
                    {"tid": tenant_id, "uid": user_id},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def compute_managed_dept_ids(
        self,
        tenant_id: str,
        role: str,
        user_dept_id: str,
    ) -> tuple[str, ...]:
        async with tenant_db_session(tenant_id) as s:
            if role in ("FINANCE_ADMIN", "TENANT_ADMIN"):
                rows = (
                    await s.execute(
                        text("SELECT id FROM sys_dept WHERE tenant_id = :tid ORDER BY id"),
                        {"tid": tenant_id},
                    )
                ).scalars().all()
                return tuple(str(x) for x in rows)
            if role == "DEPT_MANAGER":
                own_path = (
                    await s.execute(
                        text("SELECT dept_path FROM sys_dept WHERE tenant_id = :tid AND id = :did"),
                        {"tid": tenant_id, "did": user_dept_id},
                    )
                ).scalar_one_or_none()
                if not own_path:
                    return (user_dept_id,)
                rows = (
                    await s.execute(
                        text(
                            "SELECT id FROM sys_dept WHERE tenant_id = :tid AND dept_path LIKE :prefix ORDER BY id"
                        ),
                        {"tid": tenant_id, "prefix": f"{own_path}%"},
                    )
                ).scalars().all()
                return tuple(str(x) for x in rows)
            return (user_dept_id,)

    async def get_active_credential(
        self,
        tenant_id: str,
        provider: str,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            if owner_user_id:
                row = (
                    await s.execute(
                        text(
                            "SELECT * FROM tenant_credential "
                            "WHERE tenant_id = :tid AND provider = :prov AND owner_user_id = :uid AND is_active = 1"
                        ),
                        {"tid": tenant_id, "prov": provider, "uid": owner_user_id},
                    )
                ).mappings().first()
                if row:
                    return dict(row)
            row = (
                await s.execute(
                    text(
                        "SELECT * FROM tenant_credential "
                        "WHERE tenant_id = :tid AND provider = :prov AND owner_user_id IS NULL AND is_active = 1"
                    ),
                    {"tid": tenant_id, "prov": provider},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def get_policy_rule(self, tenant_id: str, rank_no: int, city_tier: str) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT * FROM travel_policy_rule "
                        "WHERE tenant_id = :tid AND city_tier = :ct AND :rno BETWEEN rank_min AND rank_max "
                        "LIMIT 1"
                    ),
                    {"tid": tenant_id, "ct": city_tier, "rno": rank_no},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def list_policy_rules(self, tenant_id: str) -> list[dict[str, Any]]:
        async with tenant_db_session(tenant_id) as s:
            rows = (
                await s.execute(
                    text("SELECT * FROM travel_policy_rule WHERE tenant_id = :tid ORDER BY id"),
                    {"tid": tenant_id},
                )
            ).mappings().all()
            return [dict(r) for r in rows]

    async def list_user_trips(self, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        async with tenant_db_session(tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT * FROM travel_order WHERE tenant_id = :tid AND user_id = :uid "
                        "ORDER BY start_date DESC"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            ).mappings().all()
            return [dict(r) for r in rows]

    async def get_travel_order(self, tenant_id: str, order_id: int) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text("SELECT * FROM travel_order WHERE tenant_id = :tid AND id = :oid"),
                    {"tid": tenant_id, "oid": order_id},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def create_travel_order(self, data: dict[str, Any]) -> dict[str, Any]:
        tid = data["tenant_id"]
        now = datetime.now(UTC)
        order_no = data.get("order_no") or f"TRV-{now.strftime('%Y%m%d%H%M%S')}-{hashlib.md5(str(now.timestamp()).encode()).hexdigest()[:4].upper()}"
        async with tenant_db_session(tid) as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO travel_order "
                        "(tenant_id, order_no, user_id, dept_id, origin_city, dest_city, dest_city_tier, "
                        " start_date, end_date, purpose, estimated_amount, status, oa_external_id, version) "
                        "VALUES (:tid, :ono, :uid, :did, :orig, :dest, :tier, :sd, :ed, :purp, :amt, :st, :oa, 0) "
                        "RETURNING *"
                    ),
                    {
                        "tid": tid,
                        "ono": order_no,
                        "uid": data["user_id"],
                        "did": data["dept_id"],
                        "orig": data["origin_city"],
                        "dest": data["dest_city"],
                        "tier": data["dest_city_tier"],
                        "sd": data["start_date"],
                        "ed": data["end_date"],
                        "purp": data["purpose"],
                        "amt": Decimal(str(data["estimated_amount"])),
                        "st": data.get("status", "SUBMITTED"),
                        "oa": data.get("oa_external_id"),
                    },
                )
            ).mappings().one()
            return dict(row)

    async def update_travel_order_status(
        self,
        tenant_id: str,
        order_id: int,
        new_status: str,
        expected_version: int | None = None,
        oa_external_id: str | None = None,
    ) -> dict[str, Any]:
        async with tenant_db_session(tenant_id) as s:
            sql = (
                "UPDATE travel_order SET status = :st, version = version + 1"
                + (", oa_external_id = :oa" if oa_external_id is not None else "")
                + " WHERE tenant_id = :tid AND id = :oid"
                + (" AND version = :ver" if expected_version is not None else "")
                + " RETURNING *"
            )
            params: dict[str, Any] = {"tid": tenant_id, "oid": order_id, "st": new_status}
            if oa_external_id is not None:
                params["oa"] = oa_external_id
            if expected_version is not None:
                params["ver"] = expected_version
            row = (await s.execute(text(sql), params)).mappings().first()
            if not row:
                if expected_version is not None:
                    raise OptimisticLockError(f"差旅单 {order_id} 并发冲突（期望 version={expected_version}）")
                raise RepositoryError(f"差旅单 {order_id} 不存在或无权限")
            return dict(row)

    async def add_approval_record(
        self,
        tenant_id: str,
        order_id: int,
        approver_id: str,
        action: str,
        comment: str = "",
    ) -> dict[str, Any]:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO approval_record (tenant_id, order_id, approver_id, action, comment) "
                        "VALUES (:tid, :oid, :aid, :act, :cmt) RETURNING *"
                    ),
                    {"tid": tenant_id, "oid": order_id, "aid": approver_id, "act": action, "cmt": comment},
                )
            ).mappings().one()
            return dict(row)

    async def create_booking_pending(self, data: dict[str, Any]) -> dict[str, Any]:
        tid = data["tenant_id"]
        try:
            async with tenant_db_session(tid) as s:
                row = (
                    await s.execute(
                        text(
                            "INSERT INTO booking_record "
                            "(tenant_id, idempotent_key, travel_order_id, segment_no, user_id, dept_id, provider, "
                            " external_order_no, category, city, vendor_name, amount, policy_limit, is_policy_exceeded, "
                            " booked_at, depart_date, booking_status, raw_detail_json) "
                            "VALUES (:tid, :ikey, :oid, :seg, :uid, :did, :prov, NULL, :cat, :city, :vend, :amt, :plim, "
                            "        :ex, :bat, :ddate, 'PENDING', CAST(:raw AS jsonb)) "
                            "RETURNING *"
                        ),
                        {
                            "tid": tid,
                            "ikey": data["idempotent_key"],
                            "oid": data["travel_order_id"],
                            "seg": data.get("segment_no", 1),
                            "uid": data["user_id"],
                            "did": data["dept_id"],
                            "prov": data.get("provider", "TUNIU"),
                            "cat": data["category"],
                            "city": data["city"],
                            "vend": data["vendor_name"],
                            "amt": Decimal(str(data["amount"])),
                            "plim": Decimal(str(data["policy_limit"])) if data.get("policy_limit") is not None else None,
                            "ex": int(data.get("is_policy_exceeded", 0)),
                            "bat": data.get("booked_at", datetime.now(UTC)),
                            "ddate": data["depart_date"],
                            "raw": json.dumps(data.get("raw_detail_json", {}), ensure_ascii=False, default=str),
                        },
                    )
                ).mappings().one()
                return dict(row)
        except IntegrityError as exc:
            raise DuplicateIdempotentKeyError(f"幂等键 {data['idempotent_key'][:12]}... 已存在") from exc

    async def get_booking_by_idempotent_key(self, tenant_id: str, idempotent_key: str) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text("SELECT * FROM booking_record WHERE tenant_id = :tid AND idempotent_key = :ikey"),
                    {"tid": tenant_id, "ikey": idempotent_key},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def update_booking_status(
        self,
        tenant_id: str,
        booking_id: int,
        booking_status: str,
        external_order_no: str | None = None,
        raw_detail_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with tenant_db_session(tenant_id) as s:
            patch_json = json.dumps(raw_detail_patch or {}, ensure_ascii=False, default=str)
            row = (
                await s.execute(
                    text(
                        "UPDATE booking_record "
                        "SET booking_status = :st, "
                        "    external_order_no = COALESCE(:ext, external_order_no), "
                        "    raw_detail_json = raw_detail_json || CAST(:patch AS jsonb) "
                        "WHERE tenant_id = :tid AND id = :bid RETURNING *"
                    ),
                    {"tid": tenant_id, "bid": booking_id, "st": booking_status, "ext": external_order_no, "patch": patch_json},
                )
            ).mappings().first()
            if not row:
                raise RepositoryError(f"预订记录 {booking_id} 不存在或无权限")
            return dict(row)

    async def list_bookings(
        self,
        tenant_id: str,
        dept_ids: tuple[str, ...] | None = None,
        user_id: str | None = None,
        travel_order_id: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        if dept_ids is not None and len(dept_ids) == 0:
            return []
        clauses = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id}
        if dept_ids is not None:
            clauses.append("dept_id = ANY(:dids)")
            params["dids"] = list(dept_ids)
        if user_id is not None:
            clauses.append("user_id = :uid")
            params["uid"] = user_id
        if travel_order_id is not None:
            clauses.append("travel_order_id = :oid")
            params["oid"] = travel_order_id
        if start_date is not None:
            clauses.append("depart_date >= :sd")
            params["sd"] = start_date
        if end_date is not None:
            clauses.append("depart_date <= :ed")
            params["ed"] = end_date

        sql = f"SELECT * FROM booking_record WHERE {' AND '.join(clauses)} ORDER BY id"
        async with tenant_db_session(tenant_id) as s:
            rows = (await s.execute(text(sql), params)).mappings().all()
            return [dict(r) for r in rows]

    async def list_stale_pending_bookings(self, older_than_seconds: int = 120) -> list[dict[str, Any]]:
        """按租户逐一绑定 RLS 后扫描超时未回写的 PENDING 记录，绝不绕过 RLS。"""
        async with pg_session() as s:
            tids = (await s.execute(text("SELECT id FROM sys_tenant WHERE status = 1"))).scalars().all()
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        results: list[dict[str, Any]] = []
        for tid in tids:
            async with tenant_db_session(str(tid)) as s:
                rows = (
                    await s.execute(
                        text(
                            "SELECT * FROM booking_record "
                            "WHERE tenant_id = :tid AND booking_status = 'PENDING' AND updated_at <= :cutoff"
                        ),
                        {"tid": str(tid), "cutoff": cutoff},
                    )
                ).mappings().all()
                results.extend(dict(r) for r in rows)
        return results

    async def list_dept_budgets(
        self,
        tenant_id: str,
        allowed_dept_ids: tuple[str, ...],
        fiscal_year: int | None = None,
        fiscal_quarter: int | None = None,
    ) -> list[dict[str, Any]]:
        if not allowed_dept_ids:
            return []
        clauses = ["tenant_id = :tid", "dept_id = ANY(:dids)"]
        params: dict[str, Any] = {"tid": tenant_id, "dids": list(allowed_dept_ids)}
        if fiscal_year is not None:
            clauses.append("fiscal_year = :fy")
            params["fy"] = fiscal_year
        if fiscal_quarter is not None:
            clauses.append("fiscal_quarter = :fq")
            params["fq"] = fiscal_quarter
        sql = f"SELECT * FROM dept_budget WHERE {' AND '.join(clauses)} ORDER BY dept_id"
        async with tenant_db_session(tenant_id) as s:
            rows = (await s.execute(text(sql), params)).mappings().all()
            return [dict(r) for r in rows]

    async def create_expense_draft(self, data: dict[str, Any]) -> dict[str, Any]:
        tid = data["tenant_id"]
        now = datetime.now(UTC)
        draft_no = data.get("draft_no") or f"EXP-{now.strftime('%Y%m%d%H%M%S')}-{hashlib.md5(str(now.timestamp()).encode()).hexdigest()[:4].upper()}"
        async with tenant_db_session(tid) as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO expense_draft "
                        "(tenant_id, draft_no, user_id, dept_id, travel_order_id, total_amount, invoices_json, erp_external_id, status) "
                        "VALUES (:tid, :dno, :uid, :did, :oid, :amt, CAST(:inv AS jsonb), :ext, :st) RETURNING *"
                    ),
                    {
                        "tid": tid,
                        "dno": draft_no,
                        "uid": data["user_id"],
                        "did": data["dept_id"],
                        "oid": data.get("travel_order_id"),
                        "amt": Decimal(str(data["total_amount"])),
                        "inv": json.dumps(data.get("invoices_json", []), ensure_ascii=False, default=str),
                        "ext": data.get("erp_external_id"),
                        "st": data.get("status", "PUSHED_TO_ERP"),
                    },
                )
            ).mappings().one()
            return dict(row)

    async def get_or_create_conversation(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        title: str = "新对话",
    ) -> dict[str, Any]:
        async with tenant_db_session(tenant_id) as s:
            existing = (
                await s.execute(
                    text("SELECT * FROM chat_conversation WHERE id = :cid"),
                    {"cid": conversation_id},
                )
            ).mappings().first()
            if existing:
                if existing["tenant_id"] != tenant_id or existing["user_id"] != user_id:
                    raise CrossTenantAccessError("无权访问其他租户或用户的会话")
                return dict(existing)
            row = (
                await s.execute(
                    text(
                        "INSERT INTO chat_conversation (id, tenant_id, user_id, title, is_deleted) "
                        "VALUES (:cid, :tid, :uid, :title, 0) RETURNING *"
                    ),
                    {"cid": conversation_id, "tid": tenant_id, "uid": user_id, "title": title[:255]},
                )
            ).mappings().one()
            return dict(row)

    async def get_conversation_by_tenant(self, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text("SELECT * FROM chat_conversation WHERE tenant_id = :tid AND id = :cid AND is_deleted = 0"),
                    {"tid": tenant_id, "cid": conversation_id},
                )
            ).mappings().first()
            return dict(row) if row else None

    async def update_conversation_title(self, tenant_id: str, conversation_id: str, title: str) -> None:
        async with tenant_db_session(tenant_id) as s:
            await s.execute(
                text("UPDATE chat_conversation SET title = :title WHERE tenant_id = :tid AND id = :cid"),
                {"tid": tenant_id, "cid": conversation_id, "title": title[:255]},
            )

    async def list_conversations(self, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        async with tenant_db_session(tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT * FROM chat_conversation "
                        "WHERE tenant_id = :tid AND user_id = :uid AND is_deleted = 0 "
                        "ORDER BY updated_at DESC"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            ).mappings().all()
            return [dict(r) for r in rows]

    async def append_message(
        self,
        tenant_id: str,
        conversation_id: str,
        role: str,
        content: str,
        extra_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO chat_message (tenant_id, conversation_id, role, content, extra_json) "
                        "VALUES (:tid, :cid, :role, :content, CAST(:extra AS jsonb)) RETURNING *"
                    ),
                    {
                        "tid": tenant_id,
                        "cid": conversation_id,
                        "role": role,
                        "content": content,
                        "extra": json.dumps(extra_json or {}, ensure_ascii=False, default=str),
                    },
                )
            ).mappings().one()
            await s.execute(
                text("UPDATE chat_conversation SET updated_at = CURRENT_TIMESTAMP WHERE tenant_id = :tid AND id = :cid"),
                {"tid": tenant_id, "cid": conversation_id},
            )
            return dict(row)

    async def list_messages(self, tenant_id: str, conversation_id: str) -> list[dict[str, Any]]:
        async with tenant_db_session(tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT * FROM chat_message WHERE tenant_id = :tid AND conversation_id = :cid "
                        "ORDER BY created_at ASC, id ASC"
                    ),
                    {"tid": tenant_id, "cid": conversation_id},
                )
            ).mappings().all()
            return [dict(r) for r in rows]

    async def update_message_feedback(self, tenant_id: str, msg_id: int, feedback: int) -> bool:
        async with tenant_db_session(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE chat_message SET feedback = :fb WHERE tenant_id = :tid AND id = :mid RETURNING id"
                    ),
                    {"tid": tenant_id, "mid": msg_id, "fb": 1 if feedback >= 0 else -1},
                )
            ).first()
            return row is not None

    async def search_policy_chunks(
        self,
        tenant_id: str,
        query: str,
        top_k: int = 3,
        doc_type: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """
        pgvector HNSW Pre-filtering 检索（v1.1 P1 #18）：
        开启 `hnsw.iterative_scan = relaxed_order` 确保按租户过滤后依然召回足量 Top-K。
        """
        q_vec = query_embedding or compute_text_embedding(query, dim=1024)
        vec_str = _vector_literal(q_vec)
        async with tenant_db_session(tenant_id) as s:
            async with s.begin_nested():
                try:
                    await s.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                except Exception:
                    pass
            type_clause = "AND doc_type = :dtype" if doc_type else ""
            sql = (
                "SELECT id, tenant_id, doc_type, doc_scope, doc_id, doc_version, title_path, content, "
                "       ROUND((1 - (embedding <=> CAST(:qvec AS vector)))::numeric, 4)::float AS score "
                "FROM kb_policy_chunk "
                f"WHERE (doc_scope = 'PUBLIC' OR tenant_id = :tid) {type_clause} "
                "ORDER BY embedding <=> CAST(:qvec AS vector) ASC "
                "LIMIT :lim"
            )
            params: dict[str, Any] = {"tid": tenant_id, "qvec": vec_str, "lim": top_k}
            if doc_type:
                params["dtype"] = doc_type
            rows = (await s.execute(text(sql), params)).mappings().all()
            return [dict(r) for r in rows]

    async def list_user_preferences(self, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        async with tenant_db_session(tenant_id) as s:
            rows = (
                await s.execute(
                    text("SELECT * FROM user_preference WHERE tenant_id = :tid AND user_id = :uid"),
                    {"tid": tenant_id, "uid": user_id},
                )
            ).mappings().all()
            return [dict(r) for r in rows]

    async def upsert_user_preference(
        self,
        tenant_id: str,
        user_id: str,
        pref_key: str,
        pref_value: str,
        source: str = "INFERRED",
        confidence: float = 0.7,
    ) -> dict[str, Any]:
        emb_str = _vector_literal(compute_text_embedding(f"{pref_key}:{pref_value}", dim=1024))
        async with tenant_db_session(tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO user_preference (tenant_id, user_id, pref_key, pref_value, source, confidence, embedding) "
                    "VALUES (:tid, :uid, :pk, :pv, :src, :conf, CAST(:emb AS vector)) "
                    "ON CONFLICT (tenant_id, user_id, pref_key) DO UPDATE SET "
                    "  pref_value = EXCLUDED.pref_value, "
                    "  source = EXCLUDED.source, "
                    "  confidence = EXCLUDED.confidence, "
                    "  embedding = EXCLUDED.embedding "
                    "WHERE user_preference.source != 'EXPLICIT' OR EXCLUDED.source = 'EXPLICIT'"
                ),
                {
                    "tid": tenant_id,
                    "uid": user_id,
                    "pk": pref_key,
                    "pv": pref_value,
                    "src": source,
                    "conf": Decimal(str(confidence)),
                    "emb": emb_str,
                },
            )
            row = (
                await s.execute(
                    text(
                        "SELECT * FROM user_preference WHERE tenant_id = :tid AND user_id = :uid AND pref_key = :pk"
                    ),
                    {"tid": tenant_id, "uid": user_id, "pk": pref_key},
                )
            ).mappings().one()
            return dict(row)

    async def add_action_audit(self, entry: dict[str, Any]) -> dict[str, Any]:
        tid = entry["tenant_id"]
        async with tenant_db_session(tid) as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO agent_action_audit "
                        "(tenant_id, user_id, conversation_id, interrupt_id, tool_name, action_hash, "
                        " args_masked_json, decision, exec_status, trace_id) "
                        "VALUES (:tid, :uid, :cid, :iid, :tool, :ahash, CAST(:args AS jsonb), :dec, :est, :tr) "
                        "RETURNING *"
                    ),
                    {
                        "tid": tid,
                        "uid": entry["user_id"],
                        "cid": entry["conversation_id"],
                        "iid": entry["interrupt_id"],
                        "tool": entry["tool_name"],
                        "ahash": entry["action_hash"],
                        "args": json.dumps(entry["args_masked_json"], ensure_ascii=False, default=str),
                        "dec": entry["decision"],
                        "est": entry.get("exec_status"),
                        "tr": entry.get("trace_id"),
                    },
                )
            ).mappings().one()
            return dict(row)

    async def list_action_audits(self, tenant_id: str) -> list[dict[str, Any]]:
        async with tenant_db_session(tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT * FROM agent_action_audit WHERE tenant_id = :tid ORDER BY created_at DESC"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().all()
            return [dict(r) for r in rows]


# ==============================================================================
# 4. 零依赖内存仓储（与 PostgresRepository 保持 100% 同构的异步接口）
# ==============================================================================
class InMemoryRepository:
    """镜像 PostgreSQL 16 领域表的异步内存仓储，用于本地无依赖开发、单元测试与离线 Eval。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq: dict[str, int] = {}
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._seq.clear()
            self.sys_tenant: dict[str, dict[str, Any]] = {}
            self.sys_dept: dict[str, dict[str, Any]] = {}
            self.user_account: dict[str, dict[str, Any]] = {}
            self.user_profile: dict[str, dict[str, Any]] = {}
            self.tenant_credential: list[dict[str, Any]] = []
            self.travel_policy_rule: list[dict[str, Any]] = []
            self.travel_order: dict[int, dict[str, Any]] = {}
            self.approval_record: list[dict[str, Any]] = []
            self.booking_record: dict[int, dict[str, Any]] = {}
            self.booking_idempotent_idx: dict[str, int] = {}
            self.dept_budget: list[dict[str, Any]] = []
            self.expense_draft: dict[int, dict[str, Any]] = {}
            self.chat_conversation: dict[str, dict[str, Any]] = {}
            self.chat_message: list[dict[str, Any]] = []
            self.kb_policy_chunk: list[dict[str, Any]] = []
            self.user_preference: list[dict[str, Any]] = []
            self.agent_action_audit: list[dict[str, Any]] = []

    def _next_id(self, table: str) -> int:
        with self._lock:
            self._seq[table] = self._seq.get(table, 0) + 1
            return self._seq[table]

    async def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        row = self.sys_tenant.get(tenant_id)
        return deepcopy(row) if row else None

    async def get_user_by_username(self, tenant_id: str, username: str) -> dict[str, Any] | None:
        for acc in self.user_account.values():
            if acc["tenant_id"] == tenant_id and acc["username"] == username and acc["status"] == 1:
                return deepcopy(acc)
        return None

    async def get_user_account(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        acc = self.user_account.get(user_id)
        if not acc or acc["tenant_id"] != tenant_id:
            return None
        return deepcopy(acc)

    async def get_user_profile(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        prof = self.user_profile.get(user_id)
        if not prof or prof["tenant_id"] != tenant_id:
            return None
        return deepcopy(prof)

    async def compute_managed_dept_ids(
        self,
        tenant_id: str,
        role: str,
        user_dept_id: str,
    ) -> tuple[str, ...]:
        tenant_depts = [d for d in self.sys_dept.values() if d["tenant_id"] == tenant_id]
        if role in ("FINANCE_ADMIN", "TENANT_ADMIN"):
            return tuple(sorted(d["id"] for d in tenant_depts))
        if role == "DEPT_MANAGER":
            own_dept = self.sys_dept.get(user_dept_id)
            if not own_dept or own_dept["tenant_id"] != tenant_id:
                return (user_dept_id,)
            prefix = own_dept["dept_path"]
            matched = [d["id"] for d in tenant_depts if d["dept_path"].startswith(prefix)]
            return tuple(sorted(matched))
        return (user_dept_id,)

    async def get_active_credential(
        self,
        tenant_id: str,
        provider: str,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        if owner_user_id:
            for c in self.tenant_credential:
                if (
                    c["tenant_id"] == tenant_id
                    and c["provider"] == provider
                    and c["owner_user_id"] == owner_user_id
                    and c["is_active"] == 1
                ):
                    return deepcopy(c)
        for c in self.tenant_credential:
            if (
                c["tenant_id"] == tenant_id
                and c["provider"] == provider
                and c["owner_user_id"] is None
                and c["is_active"] == 1
            ):
                return deepcopy(c)
        return None

    async def get_policy_rule(self, tenant_id: str, rank_no: int, city_tier: str) -> dict[str, Any] | None:
        return self.get_policy_rule_sync(tenant_id, rank_no, city_tier)

    def get_policy_rule_sync(self, tenant_id: str, rank_no: int, city_tier: str) -> dict[str, Any] | None:
        for rule in self.travel_policy_rule:
            if (
                rule["tenant_id"] == tenant_id
                and rule["city_tier"] == city_tier
                and rule["rank_min"] <= rank_no <= rule["rank_max"]
            ):
                return deepcopy(rule)
        return None

    async def list_policy_rules(self, tenant_id: str) -> list[dict[str, Any]]:
        return [deepcopy(r) for r in self.travel_policy_rule if r["tenant_id"] == tenant_id]

    async def list_user_trips(self, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = [
            deepcopy(o)
            for o in self.travel_order.values()
            if o["tenant_id"] == tenant_id and o["user_id"] == user_id
        ]
        rows.sort(key=lambda x: x["start_date"], reverse=True)
        return rows

    async def get_travel_order(self, tenant_id: str, order_id: int) -> dict[str, Any] | None:
        row = self.travel_order.get(order_id)
        if not row or row["tenant_id"] != tenant_id:
            return None
        return deepcopy(row)

    async def create_travel_order(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.create_travel_order_sync(data)

    def create_travel_order_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            oid = self._next_id("travel_order")
            now = datetime.now(UTC)
            row = {
                "id": oid,
                "tenant_id": data["tenant_id"],
                "order_no": data.get("order_no") or f"TRV-{now.strftime('%Y%m%d')}-{oid:04d}",
                "user_id": data["user_id"],
                "dept_id": data["dept_id"],
                "origin_city": data["origin_city"],
                "dest_city": data["dest_city"],
                "dest_city_tier": data["dest_city_tier"],
                "start_date": data["start_date"],
                "end_date": data["end_date"],
                "purpose": data["purpose"],
                "estimated_amount": Decimal(str(data["estimated_amount"])),
                "status": data.get("status", "SUBMITTED"),
                "oa_external_id": data.get("oa_external_id"),
                "version": 0,
                "created_at": now,
                "updated_at": now,
            }
            self.travel_order[oid] = row
            return deepcopy(row)

    async def update_travel_order_status(
        self,
        tenant_id: str,
        order_id: int,
        new_status: str,
        expected_version: int | None = None,
        oa_external_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = self.travel_order.get(order_id)
            if not row or row["tenant_id"] != tenant_id:
                raise RepositoryError(f"差旅单 {order_id} 不存在或无权限")
            if expected_version is not None and row["version"] != expected_version:
                raise OptimisticLockError(
                    f"差旅单 {order_id} 并发冲突: 期望 version={expected_version}, 实际={row['version']}"
                )
            row["status"] = new_status
            if oa_external_id is not None:
                row["oa_external_id"] = oa_external_id
            row["version"] += 1
            row["updated_at"] = datetime.now(UTC)
            return deepcopy(row)

    async def add_approval_record(
        self,
        tenant_id: str,
        order_id: int,
        approver_id: str,
        action: str,
        comment: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            rec = {
                "id": self._next_id("approval_record"),
                "tenant_id": tenant_id,
                "order_id": order_id,
                "approver_id": approver_id,
                "action": action,
                "comment": comment,
                "created_at": datetime.now(UTC),
            }
            self.approval_record.append(rec)
            return deepcopy(rec)

    async def create_booking_pending(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.create_booking_pending_sync(data)

    def create_booking_pending_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            ikey = data["idempotent_key"]
            if ikey in self.booking_idempotent_idx:
                existing_id = self.booking_idempotent_idx[ikey]
                raise DuplicateIdempotentKeyError(f"幂等键 {ikey[:12]}... 已存在预订记录 id={existing_id}")
            bid = self._next_id("booking_record")
            now = datetime.now(UTC)
            row = {
                "id": bid,
                "tenant_id": data["tenant_id"],
                "idempotent_key": ikey,
                "travel_order_id": data["travel_order_id"],
                "segment_no": data.get("segment_no", 1),
                "user_id": data["user_id"],
                "dept_id": data["dept_id"],
                "provider": data.get("provider", "TUNIU"),
                "external_order_no": None,
                "category": data["category"],
                "city": data["city"],
                "vendor_name": data["vendor_name"],
                "amount": Decimal(str(data["amount"])),
                "policy_limit": Decimal(str(data["policy_limit"])) if data.get("policy_limit") is not None else None,
                "is_policy_exceeded": int(data.get("is_policy_exceeded", 0)),
                "booked_at": data.get("booked_at", now),
                "depart_date": data["depart_date"],
                "booking_status": "PENDING",
                "raw_detail_json": deepcopy(data.get("raw_detail_json", {})),
                "created_at": now,
                "updated_at": now,
            }
            self.booking_record[bid] = row
            self.booking_idempotent_idx[ikey] = bid
            return deepcopy(row)

    async def get_booking_by_idempotent_key(self, tenant_id: str, idempotent_key: str) -> dict[str, Any] | None:
        bid = self.booking_idempotent_idx.get(idempotent_key)
        if bid is None:
            return None
        row = self.booking_record.get(bid)
        if not row or row["tenant_id"] != tenant_id:
            return None
        return deepcopy(row)

    async def update_booking_status(
        self,
        tenant_id: str,
        booking_id: int,
        booking_status: str,
        external_order_no: str | None = None,
        raw_detail_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.update_booking_status_sync(
            tenant_id, booking_id, booking_status, external_order_no, raw_detail_patch
        )

    def update_booking_status_sync(
        self,
        tenant_id: str,
        booking_id: int,
        booking_status: str,
        external_order_no: str | None = None,
        raw_detail_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = self.booking_record.get(booking_id)
            if not row or row["tenant_id"] != tenant_id:
                raise RepositoryError(f"预订记录 {booking_id} 不存在或无权限")
            row["booking_status"] = booking_status
            if external_order_no is not None:
                row["external_order_no"] = external_order_no
            if raw_detail_patch:
                row["raw_detail_json"].update(raw_detail_patch)
            row["updated_at"] = datetime.now(UTC)
            return deepcopy(row)

    async def list_bookings(
        self,
        tenant_id: str,
        dept_ids: tuple[str, ...] | None = None,
        user_id: str | None = None,
        travel_order_id: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_bookings_sync(tenant_id, dept_ids, user_id, travel_order_id, start_date, end_date)

    def list_bookings_sync(
        self,
        tenant_id: str,
        dept_ids: tuple[str, ...] | None = None,
        user_id: str | None = None,
        travel_order_id: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for b in self.booking_record.values():
            if b["tenant_id"] != tenant_id:
                continue
            if dept_ids is not None and b["dept_id"] not in dept_ids:
                continue
            if user_id is not None and b["user_id"] != user_id:
                continue
            if travel_order_id is not None and b["travel_order_id"] != travel_order_id:
                continue
            if start_date is not None and b["depart_date"] < start_date:
                continue
            if end_date is not None and b["depart_date"] > end_date:
                continue
            rows.append(deepcopy(b))
        return rows

    async def list_stale_pending_bookings(self, older_than_seconds: int = 120) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        return [
            deepcopy(b)
            for b in self.booking_record.values()
            if b["booking_status"] == "PENDING" and b["updated_at"] <= cutoff
        ]

    async def list_dept_budgets(
        self,
        tenant_id: str,
        allowed_dept_ids: tuple[str, ...],
        fiscal_year: int | None = None,
        fiscal_quarter: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_dept_budgets_sync(tenant_id, allowed_dept_ids, fiscal_year, fiscal_quarter)

    def list_dept_budgets_sync(
        self,
        tenant_id: str,
        allowed_dept_ids: tuple[str, ...],
        fiscal_year: int | None = None,
        fiscal_quarter: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for b in self.dept_budget:
            if b["tenant_id"] != tenant_id:
                continue
            if b["dept_id"] not in allowed_dept_ids:
                continue
            if fiscal_year is not None and b["fiscal_year"] != fiscal_year:
                continue
            if fiscal_quarter is not None and b["fiscal_quarter"] != fiscal_quarter:
                continue
            rows.append(deepcopy(b))
        return rows

    async def create_expense_draft(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            did = self._next_id("expense_draft")
            now = datetime.now(UTC)
            row = {
                "id": did,
                "tenant_id": data["tenant_id"],
                "draft_no": data.get("draft_no") or f"EXP-{now.strftime('%Y%m%d')}-{did:04d}",
                "user_id": data["user_id"],
                "dept_id": data["dept_id"],
                "travel_order_id": data.get("travel_order_id"),
                "total_amount": Decimal(str(data["total_amount"])),
                "invoices_json": deepcopy(data.get("invoices_json", [])),
                "erp_external_id": data.get("erp_external_id"),
                "status": data.get("status", "PUSHED_TO_ERP"),
                "created_at": now,
                "updated_at": now,
            }
            self.expense_draft[did] = row
            return deepcopy(row)

    async def get_or_create_conversation(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        title: str = "新对话",
    ) -> dict[str, Any]:
        with self._lock:
            conv = self.chat_conversation.get(conversation_id)
            if conv:
                if conv["tenant_id"] != tenant_id or conv["user_id"] != user_id:
                    raise CrossTenantAccessError("无权访问其他租户或用户的会话")
                return deepcopy(conv)
            now = datetime.now(UTC)
            conv = {
                "id": conversation_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "title": title,
                "is_deleted": 0,
                "created_at": now,
                "updated_at": now,
            }
            self.chat_conversation[conversation_id] = conv
            return deepcopy(conv)

    async def get_conversation_by_tenant(self, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
        conv = self.chat_conversation.get(conversation_id)
        if not conv or conv["tenant_id"] != tenant_id or conv["is_deleted"] != 0:
            return None
        return deepcopy(conv)

    async def update_conversation_title(self, tenant_id: str, conversation_id: str, title: str) -> None:
        with self._lock:
            conv = self.chat_conversation.get(conversation_id)
            if conv and conv["tenant_id"] == tenant_id:
                conv["title"] = title[:255]
                conv["updated_at"] = datetime.now(UTC)

    async def list_conversations(self, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = [
            deepcopy(c)
            for c in self.chat_conversation.values()
            if c["tenant_id"] == tenant_id and c["user_id"] == user_id and c["is_deleted"] == 0
        ]
        rows.sort(key=lambda x: x["updated_at"], reverse=True)
        return rows

    async def append_message(
        self,
        tenant_id: str,
        conversation_id: str,
        role: str,
        content: str,
        extra_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(UTC)
            msg = {
                "id": self._next_id("chat_message"),
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
                "extra_json": deepcopy(extra_json or {}),
                "feedback": None,
                "created_at": now,
            }
            self.chat_message.append(msg)
            if conversation_id in self.chat_conversation:
                self.chat_conversation[conversation_id]["updated_at"] = now
            return deepcopy(msg)

    async def list_messages(self, tenant_id: str, conversation_id: str) -> list[dict[str, Any]]:
        return [
            deepcopy(m)
            for m in self.chat_message
            if m["tenant_id"] == tenant_id and m["conversation_id"] == conversation_id
        ]

    async def update_message_feedback(self, tenant_id: str, msg_id: int, feedback: int) -> bool:
        with self._lock:
            for m in self.chat_message:
                if m["id"] == msg_id and m["tenant_id"] == tenant_id:
                    m["feedback"] = 1 if feedback >= 0 else -1
                    return True
            return False

    async def search_policy_chunks(
        self,
        tenant_id: str,
        query: str,
        top_k: int = 3,
        doc_type: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        q_vec = query_embedding or compute_text_embedding(query, dim=1024)
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in self.kb_policy_chunk:
            if not (chunk["doc_scope"] == "PUBLIC" or chunk["tenant_id"] == tenant_id):
                continue
            if doc_type and chunk["doc_type"] != doc_type:
                continue
            sim = cosine_similarity(q_vec, chunk["embedding"])
            kw_bonus = 0.2 if any(w in chunk["content"] for w in query.split() if len(w) >= 2) else 0.0
            scored.append((sim + kw_bonus, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{**deepcopy(item), "score": round(score, 4)} for score, item in scored[:top_k]]

    async def list_user_preferences(self, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
        return [
            deepcopy(p)
            for p in self.user_preference
            if p["tenant_id"] == tenant_id and p["user_id"] == user_id
        ]

    async def upsert_user_preference(
        self,
        tenant_id: str,
        user_id: str,
        pref_key: str,
        pref_value: str,
        source: str = "INFERRED",
        confidence: float = 0.7,
    ) -> dict[str, Any]:
        return self.upsert_user_preference_sync(tenant_id, user_id, pref_key, pref_value, source, confidence)

    def upsert_user_preference_sync(
        self,
        tenant_id: str,
        user_id: str,
        pref_key: str,
        pref_value: str,
        source: str = "INFERRED",
        confidence: float = 0.7,
    ) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(UTC)
            for p in self.user_preference:
                if p["tenant_id"] == tenant_id and p["user_id"] == user_id and p["pref_key"] == pref_key:
                    if p["source"] == "EXPLICIT" and source == "INFERRED":
                        return deepcopy(p)
                    p["pref_value"] = pref_value
                    p["source"] = source
                    p["confidence"] = Decimal(str(confidence))
                    p["updated_at"] = now
                    return deepcopy(p)
            row = {
                "id": self._next_id("user_preference"),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "pref_key": pref_key,
                "pref_value": pref_value,
                "source": source,
                "confidence": Decimal(str(confidence)),
                "embedding": compute_text_embedding(f"{pref_key}:{pref_value}", dim=1024),
                "created_at": now,
                "updated_at": now,
            }
            self.user_preference.append(row)
            return deepcopy(row)

    async def add_action_audit(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            row = {
                "id": self._next_id("agent_action_audit"),
                "tenant_id": entry["tenant_id"],
                "user_id": entry["user_id"],
                "conversation_id": entry["conversation_id"],
                "interrupt_id": entry["interrupt_id"],
                "tool_name": entry["tool_name"],
                "action_hash": entry["action_hash"],
                "args_masked_json": deepcopy(entry["args_masked_json"]),
                "decision": entry["decision"],
                "exec_status": entry.get("exec_status"),
                "trace_id": entry.get("trace_id"),
                "created_at": datetime.now(UTC),
            }
            self.agent_action_audit.append(row)
            return deepcopy(row)

    async def list_action_audits(self, tenant_id: str) -> list[dict[str, Any]]:
        return [deepcopy(a) for a in self.agent_action_audit if a["tenant_id"] == tenant_id]


_mem_repo_singleton: InMemoryRepository | None = None
_mem_repo_seeded: bool = False
_pg_repo_singleton: PostgresRepository | None = None


def get_repository(auto_seed: bool = True) -> InMemoryRepository | PostgresRepository:
    """
    根据配置 `APP_STORAGE_MODE` 返回生产级 `PostgresRepository` 或零依赖 `InMemoryRepository`。
    """
    global _mem_repo_singleton, _mem_repo_seeded, _pg_repo_singleton
    settings = get_settings()
    if settings.app_storage_mode == "postgres":
        if _pg_repo_singleton is None:
            _pg_repo_singleton = PostgresRepository()
        return _pg_repo_singleton

    if _mem_repo_singleton is None:
        _mem_repo_singleton = InMemoryRepository()
    if auto_seed and not _mem_repo_seeded:
        _mem_repo_seeded = True
        from scripts.seed_mock_data import seed_into_repository

        seed_into_repository(_mem_repo_singleton)
    return _mem_repo_singleton


def reset_repository_for_tests(auto_seed: bool = True) -> InMemoryRepository:
    """测试专用：强制重置并可选重新灌入内存仿真数据。"""
    global _mem_repo_singleton, _mem_repo_seeded
    _mem_repo_singleton = InMemoryRepository()
    _mem_repo_seeded = False
    if auto_seed:
        _mem_repo_seeded = True
        from scripts.seed_mock_data import seed_into_repository

        seed_into_repository(_mem_repo_singleton)
    return _mem_repo_singleton
