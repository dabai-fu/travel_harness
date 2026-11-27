-- ==============================================================================
-- PyTravel-Harness PostgreSQL 16 + pgvector 完整数据库 DDL
-- 对应文档章节：八、数据库表结构 (8.1) & 十一、v1.1 设计评审优化记录
--
-- 核心工程特性（含 v1.1 全部 P0/P1 修复）：
--   1. 16 张领域表全面采用 TIMESTAMPTZ(3)，防止容器时区差异导致提前天数计算偏差；
--   2. sys_dept 物化路径组织树 + user_profile.rank_no 数值职级 + EXCLUDE USING gist 排他约束；
--   3. booking_record.idempotent_key + segment_no 分段幂等 + PENDING 预写状态机；
--   4. kb_policy_chunk 增加 doc_scope/doc_id/doc_version 与 HNSW 向量索引；
--   5. 所有含 tenant_id 的领域表均开启强制行级安全（ENABLE + FORCE ROW LEVEL SECURITY）；
--   6. 统一触发器自动维护 updated_at 字段。
-- ==============================================================================

-- 启用 pgvector 扩展（政策 RAG 与用户偏好向量检索）
CREATE EXTENSION IF NOT EXISTS vector;
-- 启用 btree_gist 扩展（支持差旅政策职级区间 int4range 排他约束）
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 通用 updated_at 自动更新触发器函数
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 1. 多租户主表
CREATE TABLE IF NOT EXISTS sys_tenant (
    id VARCHAR(64) PRIMARY KEY,
    tenant_name VARCHAR(128) NOT NULL,
    status SMALLINT NOT NULL DEFAULT 1, -- 1-启用, 0-停用
    monthly_token_quota BIGINT NOT NULL DEFAULT 10000000,
    config_json JSONB NOT NULL DEFAULT '{}', -- 租户级配置（默认预警阈值、启用 Skill 清单）
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. 部门组织树（v1.1 新增：管辖范围由物化路径实时计算，避免组织调整后权限陈旧）
CREATE TABLE IF NOT EXISTS sys_dept (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    parent_id VARCHAR(64),
    dept_name VARCHAR(128) NOT NULL,
    dept_path VARCHAR(1024) NOT NULL, -- 物化路径，如 /D001/D002/；子树查询：dept_path LIKE '/D001/%'
    manager_user_id VARCHAR(64),
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dept_tenant_path ON sys_dept (tenant_id, dept_path varchar_pattern_ops);

-- 3. 用户账号与 RBAC 角色表
CREATE TABLE IF NOT EXISTS user_account (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'EMPLOYEE', -- EMPLOYEE / DEPT_MANAGER / FINANCE_ADMIN / TENANT_ADMIN
    status SMALLINT NOT NULL DEFAULT 1,
    perm_version INT NOT NULL DEFAULT 1, -- 角色/组织变更时递增，用于失效旧缓存
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, username)
);

-- 4. 员工组织与差旅档案表
CREATE TABLE IF NOT EXISTS user_profile (
    user_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    dept_id VARCHAR(64) NOT NULL,
    dept_name VARCHAR(128) NOT NULL,
    real_name VARCHAR(64) NOT NULL,
    pinyin_name VARCHAR(64),
    rank_level VARCHAR(16) NOT NULL, -- P5 / P6 / P7 / P8
    rank_no SMALLINT NOT NULL,       -- v1.1：职级数值，避免 'P10' < 'P5' 字符串比较陷阱
    home_city VARCHAR(64) NOT NULL,
    phone_cipher VARCHAR(256),       -- AES-256-GCM 密文
    id_card_cipher VARCHAR(256),     -- AES-256-GCM 密文
    email VARCHAR(128),
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_profile_tenant_dept ON user_profile (tenant_id, dept_id);

-- 5. 租户/用户级外部凭证加密表（途牛 API Key / 企业账户）
CREATE TABLE IF NOT EXISTS tenant_credential (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(64), -- NULL 表示租户级共享途牛企业账户，非空表示个人凭证
    provider VARCHAR(32) NOT NULL, -- TUNIU / OA_WEBHOOK / ERP_API
    credential_cipher TEXT NOT NULL, -- AES-256-GCM 加密 JSON
    key_version SMALLINT NOT NULL DEFAULT 1, -- v1.1：主密钥版本，支持密钥轮换
    is_active SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- v1.1：利用 COALESCE 对租户级 NULL owner 生效部分唯一约束
CREATE UNIQUE INDEX IF NOT EXISTS uk_cred_active
    ON tenant_credential (tenant_id, provider, COALESCE(owner_user_id, ''))
    WHERE is_active = 1;

-- 6. 差旅政策规则表（租户 × 数值职级区间 × 城市等级）
CREATE TABLE IF NOT EXISTS travel_policy_rule (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    rank_min SMALLINT NOT NULL, -- v1.1：数值职级下限（含边界）
    rank_max SMALLINT NOT NULL, -- v1.1：数值职级上限（含边界）
    city_tier VARCHAR(16) NOT NULL, -- TIER_1 / TIER_2 / TIER_3
    flight_cabin VARCHAR(32) NOT NULL, -- ECONOMY / PREMIUM_ECONOMY / BUSINESS
    train_seat VARCHAR(32) NOT NULL,   -- SECOND_CLASS / FIRST_CLASS / BUSINESS
    hotel_limit_per_night DECIMAL(10, 2) NOT NULL,
    meal_allowance_per_day DECIMAL(10, 2) NOT NULL,
    local_transport_per_day DECIMAL(10, 2) NOT NULL,
    min_advance_booking_days INT NOT NULL DEFAULT 3, -- 提前预订天数软约束（亦用于临期归因）
    approval_threshold DECIMAL(12, 2) NOT NULL,      -- 超额需升级审批阈值
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (rank_min <= rank_max),
    -- v1.1：Gist 区间排他约束，禁止同一租户同一城市等级出现职级区间重叠
    EXCLUDE USING gist (
        tenant_id WITH =,
        city_tier WITH =,
        int4range(rank_min, rank_max, '[]') WITH &&
    )
);

-- 7. 差旅申请单主表（同步模拟 OA 状态机 + 乐观锁）
CREATE TABLE IF NOT EXISTS travel_order (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    order_no VARCHAR(32) NOT NULL, -- 如 TRV-20261127-0001
    user_id VARCHAR(64) NOT NULL,
    dept_id VARCHAR(64) NOT NULL,
    origin_city VARCHAR(64) NOT NULL,
    dest_city VARCHAR(64) NOT NULL,
    dest_city_tier VARCHAR(16) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    purpose VARCHAR(512) NOT NULL,
    estimated_amount DECIMAL(12, 2) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'DRAFT', -- DRAFT -> SUBMITTED -> APPROVED/REJECTED -> COMPLETED/CANCELLED
    oa_external_id VARCHAR(64),
    version INT NOT NULL DEFAULT 0, -- v1.1：乐观锁防 Webhook 与用户取消并发覆盖
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_travel_order_tenant_dept ON travel_order (tenant_id, dept_id, start_date);
CREATE UNIQUE INDEX IF NOT EXISTS uk_travel_order_no ON travel_order (tenant_id, order_no);
CREATE INDEX IF NOT EXISTS idx_travel_order_user_date ON travel_order (tenant_id, user_id, start_date, end_date);

-- 8. 审批记录表
CREATE TABLE IF NOT EXISTS approval_record (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    order_id BIGINT NOT NULL,
    approver_id VARCHAR(64) NOT NULL,
    action VARCHAR(24) NOT NULL, -- APPROVE / REJECT
    comment VARCHAR(512),
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_approval_order ON approval_record (tenant_id, order_id);

-- 9. 途牛外部预订统一落库表（机票/酒店/火车）
CREATE TABLE IF NOT EXISTS booking_record (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    idempotent_key VARCHAR(64) NOT NULL UNIQUE, -- SHA256(tenant|order|option|segment)
    travel_order_id BIGINT NOT NULL,
    segment_no SMALLINT NOT NULL DEFAULT 1,     -- v1.1：方案内分段序号（去程=1/酒店=2/返程=3）
    user_id VARCHAR(64) NOT NULL,
    dept_id VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'TUNIU',
    external_order_no VARCHAR(64),              -- v1.1：PENDING 预写阶段可空
    category VARCHAR(24) NOT NULL,              -- FLIGHT / HOTEL / TRAIN
    city VARCHAR(64) NOT NULL,
    vendor_name VARCHAR(128) NOT NULL,          -- 航司名 / 酒店名 / 车次
    amount DECIMAL(12, 2) NOT NULL,
    policy_limit DECIMAL(12, 2),                -- 当时对应差标限额
    is_policy_exceeded SMALLINT NOT NULL DEFAULT 0,
    booked_at TIMESTAMPTZ(3) NOT NULL,
    depart_date DATE NOT NULL,
    booking_status VARCHAR(24) NOT NULL DEFAULT 'PENDING', -- PENDING -> CONFIRMED / FAILED
    raw_detail_json JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_booking_tenant_dept_date ON booking_record (tenant_id, dept_id, depart_date);
CREATE INDEX IF NOT EXISTS idx_booking_order ON booking_record (tenant_id, travel_order_id);
CREATE INDEX IF NOT EXISTS idx_booking_pending ON booking_record (updated_at) WHERE booking_status = 'PENDING';

-- 10. 部门季度差旅预算表
CREATE TABLE IF NOT EXISTS dept_budget (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    dept_id VARCHAR(64) NOT NULL,
    dept_name VARCHAR(128) NOT NULL,
    fiscal_year INT NOT NULL,
    fiscal_quarter SMALLINT NOT NULL, -- 1 / 2 / 3 / 4
    budget_amount DECIMAL(14, 2) NOT NULL,
    warn_threshold DECIMAL(4, 2) NOT NULL DEFAULT 0.85,
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, dept_id, fiscal_year, fiscal_quarter)
);

-- 11. 差旅报销草稿与发票识别记录表
CREATE TABLE IF NOT EXISTS expense_draft (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    draft_no VARCHAR(32) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    dept_id VARCHAR(64) NOT NULL,
    travel_order_id BIGINT,
    total_amount DECIMAL(12, 2) NOT NULL,
    invoices_json JSONB NOT NULL,
    erp_external_id VARCHAR(64),
    status VARCHAR(24) NOT NULL DEFAULT 'DRAFT', -- DRAFT -> PUSHED_TO_ERP
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_expense_draft_no ON expense_draft (tenant_id, draft_no);
CREATE INDEX IF NOT EXISTS idx_expense_user ON expense_draft (tenant_id, user_id, created_at DESC);

-- 12. 对话会话表
CREATE TABLE IF NOT EXISTS chat_conversation (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    title VARCHAR(255) NOT NULL DEFAULT '新对话',
    is_deleted SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conv_tenant_user ON chat_conversation (tenant_id, user_id, updated_at DESC);

-- 13. 时间轴消息表
CREATE TABLE IF NOT EXISTS chat_message (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL, -- user / assistant
    content TEXT NOT NULL,
    extra_json JSONB NOT NULL DEFAULT '{}', -- progress / thinking / travel_cards / chart_data / hitl_form
    feedback SMALLINT,                      -- 1-点赞, -1-点踩
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON chat_message (conversation_id, created_at);

-- 14. 政策 RAG 向量切片表（支持 PUBLIC/TENANT 隔离与 HNSW 索引）
CREATE TABLE IF NOT EXISTS kb_policy_chunk (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    doc_type VARCHAR(32) NOT NULL, -- TRAVEL_POLICY / TRAVEL_GUIDELINE
    doc_scope VARCHAR(16) NOT NULL DEFAULT 'TENANT', -- PUBLIC / TENANT
    doc_id VARCHAR(64) NOT NULL,
    doc_version INT NOT NULL DEFAULT 1,
    title_path VARCHAR(256),
    content TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kb_tenant_type ON kb_policy_chunk (tenant_id, doc_type);
CREATE INDEX IF NOT EXISTS idx_kb_embedding_hnsw ON kb_policy_chunk USING hnsw (embedding vector_cosine_ops);

-- 15. 员工长期出行偏好表（v1.1 补齐）
CREATE TABLE IF NOT EXISTS user_preference (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    pref_key VARCHAR(64) NOT NULL,      -- AIRLINE / HOTEL_BRAND / TRAIN_SEAT / RED_EYE
    pref_value VARCHAR(256) NOT NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'INFERRED', -- INFERRED / EXPLICIT
    confidence DECIMAL(3, 2) NOT NULL DEFAULT 0.60,
    embedding VECTOR(1024),
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, user_id, pref_key)
);

-- 16. Agent 高危动作审计表（v1.1 新增：WRITE 工具 HITL 确认与执行留痕）
CREATE TABLE IF NOT EXISTS agent_action_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(64) NOT NULL,
    interrupt_id VARCHAR(64) NOT NULL,
    tool_name VARCHAR(64) NOT NULL,
    action_hash CHAR(64) NOT NULL,       -- SHA256(tool_name | canonical_json(args))
    args_masked_json JSONB NOT NULL,     -- 脱敏后的参数快照
    decision VARCHAR(16) NOT NULL,       -- CONFIRMED / REJECTED / EXPIRED
    exec_status VARCHAR(16),             -- SUCCESS / FAILED / UNKNOWN
    trace_id VARCHAR(64),
    created_at TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON agent_action_audit (tenant_id, created_at DESC);

-- ==============================================================================
-- 为含 updated_at 的表绑定自动更新触发器
-- ==============================================================================
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'sys_tenant', 'sys_dept', 'user_account', 'user_profile',
        'tenant_credential', 'travel_policy_rule', 'travel_order',
        'booking_record', 'dept_budget', 'expense_draft',
        'chat_conversation', 'user_preference'
    ]
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%I_updated_at ON %I;', tbl, tbl);
        EXECUTE format(
            'CREATE TRIGGER trg_%I_updated_at BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at();',
            tbl, tbl
        );
    END LOOP;
END $$;

-- ==============================================================================
-- v1.1 PostgreSQL 行级安全（RLS）全量租户表兜底策略
-- 说明：应用每个事务开头执行 SET LOCAL app.tenant_id = '<tenant_id>';
--       FORCE ROW LEVEL SECURITY 确保即使表 owner 连接也不绕过隔离。
-- ==============================================================================
DO $$
DECLARE
    rls_tbl TEXT;
BEGIN
    FOREACH rls_tbl IN ARRAY ARRAY[
        'sys_dept', 'user_account', 'user_profile', 'tenant_credential',
        'travel_policy_rule', 'travel_order', 'approval_record', 'booking_record',
        'dept_budget', 'expense_draft', 'chat_conversation', 'chat_message',
        'user_preference', 'agent_action_audit'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', rls_tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', rls_tbl);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I;', rls_tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true));',
            rls_tbl
        );
    END LOOP;
END $$;

-- 知识库表允许读取本租户私有文档与平台公共文档（PUBLIC）
ALTER TABLE kb_policy_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_policy_chunk FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON kb_policy_chunk;
CREATE POLICY tenant_isolation ON kb_policy_chunk
    USING (doc_scope = 'PUBLIC' OR tenant_id = current_setting('app.tenant_id', true));

-- ==============================================================================
-- 生产安全：创建非 superuser 应用账号 pytravel_app，确保 RLS 强制生效
-- 说明：PostgreSQL superuser 默认绕过一切 RLS；应用运行时必须使用 pytravel_app。
-- ==============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pytravel_app') THEN
        CREATE ROLE pytravel_app LOGIN PASSWORD 'pytravel_app_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

GRANT USAGE, CREATE ON SCHEMA public TO pytravel_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO pytravel_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO pytravel_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO pytravel_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO pytravel_app;

-- 为自部署 Langfuse 创建独立数据库（避免与业务表及 Checkpointer 表混用）
SELECT 'CREATE DATABASE langfuse OWNER pytravel'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
