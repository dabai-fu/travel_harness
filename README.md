# PyTravel-Harness：企业级多智能体差旅协同与预算归因分析系统（生产部署完整手册）

> **一句话看懂这个项目**：这是一个基于 **Python 3.12 + FastAPI + LangGraph + FastMCP + PostgreSQL 16 (pgvector + RLS) + Redis 7 + Docker 沙箱 + React 19** 构建的企业级多智能体（Agent Harness）系统。它把“员工出差比价下单、政策问答、发票识别报销、部门预算超支归因”串联成一条可审计、防越权、防篡改的生产级全链路。

- 系统需求与架构蓝图：[PyTravel-Harness_系统需求与架构设计文档.md](./PyTravel-Harness_系统需求与架构设计文档.md)
- 从零读懂全部代码顺序：[CODE_WALKTHROUGH.md](./CODE_WALKTHROUGH.md)
- 远程连接与断点调试指南（8GB 本机 + 云服务器推荐）：[REMOTE_DEBUG_GUIDE.md](./REMOTE_DEBUG_GUIDE.md)
- 生产容器编排文件：[docker-compose.yml](./docker-compose.yml)
- 调试覆盖编排文件：[docker-compose.debug.yml](./docker-compose.debug.yml)
- 生产环境变量模板：[.env.production.example](./.env.production.example)

---

## 一、 生产环境容器拓扑全景（你服务器里到底会跑哪些东西？）

生产环境启动后，你的服务器上会运行 **10 个核心 Docker 容器**（加可选的第 11 个可观测平台 `langfuse`），它们之间通过 Docker 内网通信：

| 序号 | 容器名 | 作用通俗解释 | 对外端口 | 是否依赖本地卷 |
| :---: | :--- | :--- | :---: | :--- |
| **1** | `pytravel-frontend` | **Nginx + React 19 前端页面**。负责展示网页，并把 `/api/*` 请求转发给网关（已关闭缓冲，确保流式打字机不卡顿） | **80**（公网访问） | 无 |
| **2** | `pytravel-api-gateway` | **FastAPI + LangGraph 主脑**。负责 JWT 登录、三层意图路由、7 大中间件、断点恢复与 SSE 流推送 | **8000**（调试用） | `sandbox_data`、`docker.sock` |
| **3** | `pytravel-tuniu-mcp` | **途牛商旅 MCP 服务**。负责机票/高铁/酒店查价、实时复核与幂等下单（支持 `mock` 仿真与 `real` 真接口） | 内网 `8021` | 无 |
| **4** | `pytravel-oa-mcp` | **企业 OA MCP 服务**。负责查员工职级、行程冲突检测、发起出差审批与发送部门预警通知 | 内网 `8022` | 无 |
| **5** | `pytravel-erp-expense-mcp` | **费控 ERP MCP 服务**。负责按权限导出脱敏消费明细 CSV 到沙箱、以及推送报销草稿 | 内网 `8023` | `sandbox_data` |
| **6** | `pytravel-weather-mcp` | **城市天气 MCP 服务**。白名单提供目的地近期天气与商务出行穿衣提醒 | 内网 `8024` | 无 |
| **7** | `pytravel-mock-enterprise` | **模拟企业老 OA/ERP 系统**。仿真真实公司已有的审批和费控接口 | 内网 `8010` | 无 |
| **8** | `pytravel-sandbox-pool` | **Python 预算精算隔离沙箱池**。**彻底断网**、限制 1 核 512MB 内存，专门运行 Pandas + `Decimal` 财务归因脚本 | 无（纯内网隔离） | `sandbox_data` |
| **9** | `pytravel-postgres` | **PostgreSQL 16 + pgvector 数据库**。存 16 张业务表、1024 维向量索引、LangGraph 断点表，并强制开启行级安全（RLS） | 仅内网 `5432` | `pgdata` |
| **10** | `pytravel-redis` | **Redis 7 内存数据库**。存登录 Session、权限缓存、Lua 三态熔断器、SSE 事件流、集群取消标记与断点元数据 | 仅内网 `6379` | `redisdata` |
| **11** | `pytravel-langfuse` *(可选)* | **自部署 Langfuse 可观测看板**。实时查看每次对话的 Trace 树、耗时、Token 成本（已内置 PII 脱敏拦截） | **3000**（按需开放） | 复用 `postgres` |

> **为什么没有 GPU 也能跑？**  
> 本项目的大模型推理（主推理、轻量意图分类、发票图片多模态识别、文本向量化）全部通过标准 HTTP 接口调用云端 API（如阿里云百炼通义千问或 DeepSeek），服务器不需要显卡。

---

## 二、 上线前准备（在阿里云控制台与本地电脑做 3 件事）

### 2.1 在阿里云 ECS 控制台配置「安全组」放行端口
登录阿里云控制台 → 找到你的 ECS 实例 → 点击「安全组」→「配置规则」→「入方向」→ 手动添加以下规则：

| 端口范围 | 协议 | 授权对象 | 用途与安全说明 |
| :--- | :--- | :--- | :--- |
| **`22`** | TCP | `0.0.0.0/0`（或你本机 IP） | SSH 远程登录服务器敲命令 |
| **`80`** | TCP | `0.0.0.0/0` | 访问网页工作台（前端 Nginx 会自动转发 `/api/`，**生产其实只需开 22 和 80**） |
| `8000` *(可选)* | TCP | 你的 IP 或 `0.0.0.0/0` | 想直接打开后端 Swagger 接口文档 `http://公网IP:8000/docs` 时才需要放行 |
| `3000` *(可选)* | TCP | 你的 IP 或 `0.0.0.0/0` | 如果开启了 `--with-langfuse` 看板才需要放行 |
| ❌ **`5432` / `6379`** | — | **绝对不要放行！** | PostgreSQL 和 Redis 绝对不能暴露给公网，否则极易被扫库或勒索 |

### 2.2 在你本地 Windows 电脑用 SSH 连上服务器
打开 Windows PowerShell，把下面命令中的 `你的服务器公网IP` 替换掉（通常在阿里云控制台实例列表能看到）：

```powershell
ssh root@你的服务器公网IP
```
> 首次连接会提示 `Are you sure you want to continue connecting (yes/no)?`，输入 `yes` 回车，再输入你在阿里云设置的服务器 root 密码（输入密码时屏幕上不会显示星号，输完直接回车即可）。

### 2.3 准备云端大模型 API Key（以阿里云百炼 / DeepSeek 为例）
为了在生产环境真实体验多轮对话、语义理解与**上传发票图片的多模态视觉识票**，建议准备一个云端 API Key：
- **推荐：阿里云百炼（DashScope）**
  - 登录阿里云百炼控制台开通「模型服务灵积」并创建 API Key（格式如 `sk-xxxxxxxxxxxxxxxx`）。
  - 对应模型全部开箱即用：`qwen-max`（主推理）、`qwen-turbo`（L3 路由）、`qwen-vl-max`（发票图片识票）、`text-embedding-v3`（1024 维向量）。
- **或者：DeepSeek**
  - 填 DeepSeek API Key，并把 `OPENAI_BASE_URL` 改为 `https://api.deepseek.com/v1`，`LLM_MAIN_MODEL` 和 `LLM_FAST_MODEL` 均改为 `deepseek-chat`。

---

## 三、 把项目代码上传到服务器（避坑版）

下面提供两种方式，**不要把你本地 Windows 里的 `.venv` 和 `frontend/node_modules` 传上去**（体积大且 Windows 二进制文件在 Linux 上无法运行，Docker 会自动在 Linux 容器内重新构建）。

### 方式 1：本地打包干净压缩包并用 `scp` 上传（最稳妥，无需在服务器配 Git 密码）
在**你本地电脑的 PowerShell** 中（进入项目根目录 `d:\java programs\travel_harness`）执行：

```powershell
# 1. 在项目根目录打包（只打包源码、Docker配置、脚本和前端src，自动排除 .venv 和 node_modules）
tar -czf travel_harness.tar.gz --exclude=".venv" --exclude="frontend/node_modules" --exclude="frontend/dist" --exclude=".sandbox_workspaces" --exclude=".pytest_cache" --exclude="__pycache__" .

# 2. 上传到服务器的 /opt 目录
scp travel_harness.tar.gz root@你的服务器公网IP:/opt/
```

然后回到 **SSH 登录的服务器终端** 解压：
```bash
mkdir -p /opt/pytravel-harness
tar -xzf /opt/travel_harness.tar.gz -C /opt/pytravel-harness
cd /opt/pytravel-harness
```

### 方式 2：从 Git 仓库拉取
如果你已经把项目推送到了 GitHub / Gitee，直接在服务器上：
```bash
cd /opt
git clone <你的仓库地址> pytravel-harness
cd /opt/pytravel-harness
```

### ⚠️ Windows 上传到 Linux 必做的一步：转换脚本换行符
Windows 编辑的文件默认带 `\r\n`（CRLF），Linux 执行 `.sh` 脚本时会报错。在服务器目录执行一次转换即可：
```bash
sed -i 's/\r$//' scripts/deploy_ecs.sh
chmod +x scripts/deploy_ecs.sh
```

---

## 四、 检查服务器 Docker 与配置国内镜像加速

### 4.1 确认 Docker 已就绪
在服务器终端执行：
```bash
docker --version
docker compose version
```
如果前面买服务器时勾选了「预装 Docker」，这里会直接输出版本号。如果提示找不到命令，执行官方/阿里云镜像一键安装：
```bash
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
systemctl enable --now docker
```

### 4.2 （杭州/北京/上海等中国内地服务器必配）配置 Docker 镜像加速
如果不配镜像加速，拉取 `pgvector`、`redis`、`python:3.12-slim`、`node:22-alpine` 会非常慢甚至超时；**同时注意公共免费镜像站限流严重（部分站点还会触发白名单拦截）**。阿里云 ECS 最快最稳的方式是使用**个人免费阿里云 ACR 专属加速地址**：

1. 浏览器登录阿里云容器镜像服务控制台：<https://cr.console.aliyun.com/>
2. 左侧菜单点击 **镜像工具 -> 镜像加速器**，复制你的专属加速地址（形如 `https://xxxxxxxx.mirror.aliyuncs.com`）。
3. 将 `<你的专属前缀>` 替换为刚复制的地址，在服务器执行：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://<你的专属前缀>.mirror.aliyuncs.com",
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
}
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

> 说明：项目内的 `docker/backend.Dockerfile`、`docker/sandbox.Dockerfile`、`docker/frontend.Dockerfile` 已默认内置阿里云 `apt` / `PyPI` 及淘宝 `npmmirror` 加速源，无需再手动配置容器内包管理器镜像。

---

## 五、 生产环境 `.env` 配置文件逐行详解

项目根目录下的 [.env.production.example](./.env.production.example) 是生产配置模板。你先复制一份为 `.env`（如果使用一键部署脚本 `./scripts/deploy_ecs.sh`，它会自动帮你复制并随机生成所有安全密钥）：

```bash
cp .env.production.example .env
```

下面逐行解释 `.env` 里每一项是什么、要不要改：

```ini
# ==============================================================================
# 1. 核心架构开关（生产环境全部开启真实基础设施，不要改动）
# ==============================================================================
APP_STORAGE_MODE=postgres   # 真实 PostgreSQL 16 + pgvector + RLS 行级安全
REDIS_MODE=redis            # 真实 Redis 7（Lua 原子熔断、SSE Stream、集群中断）
SANDBOX_MODE=docker         # 真实 Docker 禁网沙箱容器池（docker exec pytravel-sandbox-pool）
MCP_MODE=http               # 跨进程真实 FastMCP Streamable HTTP 微服务调用
LLM_MODE=openai             # 调用云端真实大模型（如果暂未买 Key 想先测试，可临时改 mock）
TUNIU_MODE=mock             # 途牛仿真报价开关（本地与演示用 mock；对接真实途牛企业账号时改 real）

# ==============================================================================
# 2. 数据库与 Redis 密码（强烈建议改成你自己的复杂密码）
# ==============================================================================
POSTGRES_USER=pytravel
POSTGRES_PASSWORD=pytravel_pass         # ⚠️ 建议改为强密码（注意首次初始化后修改密码需重建卷）
POSTGRES_DB=pytravel
REDIS_PASSWORD=pytravel_redis_pass      # Redis 连接密码（docker-compose 已设为不暴露公网）

# ==============================================================================
# 3. 安全密钥（使用 scripts/deploy_ecs.sh 会自动替换为 64 位随机高熵密钥！）
#    如果手动配置，不要保留 __AUTO_GENERATE__ 占位符
# ==============================================================================
JWT_SECRET=__AUTO_GENERATE__            # 登录 JWT 签名密钥
INTERNAL_TOKEN_SECRET=__AUTO_GENERATE__ # 网关调用 4 个 MCP 容器的 60s 内部令牌签名密钥
OA_WEBHOOK_SECRET=__AUTO_GENERATE__     # OA 异步审批回调的 HMAC-SHA256 验签密钥
AES_MASTER_KEY_V1=__AUTO_GENERATE_B64_32__ # AES-256-GCM 主密钥（加密途牛 Key 与员工身份证/手机号）

# ==============================================================================
# 4. 云端大模型与视觉模型配置（✅ 你唯一必须手动填入的一块）
# ==============================================================================
OPENAI_API_KEY=sk-请替换为你的真实API_KEY
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MAIN_MODEL=qwen-max                 # 主推理、预算报告撰写
LLM_FAST_MODEL=qwen-turbo               # L3 意图分类与多轮指代改写
LLM_VISION_MODEL=qwen-vl-max            # 多模态发票图片识别
EMBEDDING_MODEL=text-embedding-v3       # 1024 维政策知识库向量

# ==============================================================================
# 5. Langfuse 全链路可观测看板（可选，2核8G 服务器推荐开启）
# ==============================================================================
LANGFUSE_ENABLED=false                  # 配好 Langfuse Public/Secret Key 后改为 true
LANGFUSE_HOST=http://langfuse:3000      # 容器内网直连地址，不要改
LANGFUSE_PUBLIC_KEY=pk-lf-dev           # 在 Langfuse 网页后台新建项目后复制填入
LANGFUSE_SECRET_KEY=sk-lf-dev           # 在 Langfuse 网页后台新建项目后复制填入
```

> **提示**：如果你手头暂时还没有 `OPENAI_API_KEY`，只想先看整个 10 容器生产架构跑通，只需把 `.env` 中的 `LLM_MODE=openai` 临时改成 `LLM_MODE=mock` 即可 0 费用跑通全流程；后续拿到 Key 再改回 `openai` 重启网关即可。

---

## 六、 生产环境启动流程（一键脚本 + 手动分步拆解）

### 方式 A：一键全自动部署（推荐）
在服务器 `/opt/pytravel-harness` 目录执行：

```bash
# 选项 1：启动 10 个核心生产服务（PG16 + Redis7 + 沙箱池 + 4 MCP + 模拟OA/ERP + 网关 + Nginx前端）
./scripts/deploy_ecs.sh

# 选项 2：在以上基础上，额外启动第 11 个服务 —— 自部署 Langfuse 可观测看板（2核8G 推荐）
./scripts/deploy_ecs.sh --with-langfuse
```

脚本执行完毕后，编辑 `.env` 填入你真实的 `OPENAI_API_KEY`（因为首次运行脚本时自动生成 `.env`，还没有你的 Key）：
```bash
nano .env
# 找到 OPENAI_API_KEY= 那一行，替换成你的 sk-xxx
# 按 Ctrl+O 回车保存，再按 Ctrl+X 退出

# 让网关容器加载新写入的 API Key（1秒完成，无需重新构建镜像）
docker compose up -d api-gateway
```

### 方式 B：如果你想彻底看懂每一步在做什么（手动分步等价命令）
`deploy_ecs.sh` 本质上只干了下面 4 件事，你也可以手动一条一条敲：

1. **生成 `.env` 并随机生成安全密钥**（如果不想用脚本，手写 `.env` 时可运行 `openssl rand -base64 32` 生成 `AES_MASTER_KEY_V1`）。
2. **构建镜像并后台启动全部容器**：
   ```bash
   docker compose up -d --build
   # 首次构建需要下载 python:3.12-slim、node:22-alpine、pgvector 等基础镜像并打包前端，大约需要 3~6 分钟
   ```
3. **检查数据库表是否由 `scripts/init_db.sql` 自动初始化成功**：
   ```bash
   docker exec -it pytravel-postgres psql -U pytravel -d pytravel -c "\dt"
   ```
   正常会列出 `sys_tenant`、`sys_dept`、`user_account`、`booking_record` 等全部 **16 张表**。
4. **向 PostgreSQL 16 灌入生产仿真数据（2 个租户、6 个部门、1000+ 条差旅与预订数据）**：
   ```bash
   docker exec pytravel-api-gateway python scripts/seed_mock_data.py --mode postgres
   ```
   看到输出 `"status": "seeded_postgres"` 即表示灌数成功（脚本具备幂等保护，重复执行不会插重复数据）。

---

## 七、 上线验收与全链路功能测试指南

### 7.1 第一步：检查全部容器与网关健康状态
```bash
docker compose ps
```
确认所有服务状态均为 `Up` 或 `Up (healthy)`，然后测一下网关健康接口：
```bash
curl http://127.0.0.1:8000/health
```
你应该看到以下 JSON（注意 4 个 mode 全部已是生产值）：
```json
{
  "status": "ok",
  "service": "pytravel-api-gateway",
  "version": "1.1.0",
  "storage_mode": "postgres",
  "redis_mode": "redis",
  "sandbox_mode": "docker",
  "mcp_mode": "http"
}
```

### 7.2 第二步：亲手验证 PostgreSQL 非特权账号 RLS 行级安全是否真生效
你可以直接在服务器上用应用账号 `pytravel_app` 连进 PostgreSQL 测试：
```bash
docker exec -it pytravel-postgres psql -U pytravel_app -d pytravel
```
进入 `psql` 后依次执行：
```sql
-- 1. 不设置租户上下文直接查订单 -> 受 FORCE ROW LEVEL SECURITY 拦截，返回 0 行！
SELECT count(*) FROM booking_record;

-- 2. 在事务内绑定 T001 上下文 -> 只能看到 T001 的预订记录
BEGIN;
SELECT set_config('app.tenant_id', 'T001', true);
SELECT count(*) FROM booking_record;
COMMIT;

-- 3. 绑定 T002 上下文 -> 只能看到 T002 的记录，绝不串库
BEGIN;
SELECT set_config('app.tenant_id', 'T002', true);
SELECT count(*) FROM booking_record;
COMMIT;

\q
```

### 7.3 第三步：打开浏览器全链路体验系统
在本地电脑浏览器打开：**`http://<你的服务器公网IP>/`**（如果安全组没开 80 端口，也可用本地 PowerShell `ssh -L 8080:127.0.0.1:80 root@公网IP` 后访问 `http://localhost:8080`）。

系统已预置 5 个不同权限的演示账号（统一登录密码为 `123456`）：

| 登录用户名 | 租户 | 角色 | 职级 / 部门 | 推荐必测场景 |
| :--- | :--- | :--- | :--- | :--- |
| **`alice`** | `T001` | 普通员工 (`EMPLOYEE`) | P6 · 华东业务交付部 | **场景 1（规划+HITL下单）**：输入「下周二从上海去北京出差3天，住国贸附近，帮我规划并预订」，查看 4 套方案卡片并点击确认下单；<br>**场景 2（VLM 发票草拟）**：点击左下角「上传发票图」选一张机票/酒店发票图片，输入「帮我整理报销单草稿」；<br>**场景 3（越权拦截）**：输入「查看公司全部部门预算」，验证普通员工被拦截 |
| **`mgr_east`** | `T001` | 部门主管 (`DEPT_MANAGER`) | P8 · 华东业务交付部 | **场景 4（部门预算归因）**：输入「分析2026年第四季度华东业务交付部差旅预算与超支归因」，查看 **3 张 Recharts 图表**与零误差报告；<br>**场景 5（平行部门越权拦截）**：输入「查看基础架构部(D003)第四季度预算」，验证被 100% 拦截 |
| **`vp_rd`** | `T001` | 部门主管 (`DEPT_MANAGER`) | P9 · 研发与交付中心 | 输入「分析本季度预算执行」，验证通过 `sys_dept` 物化路径 `/D001/` 自动汇总查看 `D001 + D002 + D003` 子部门树 |
| **`finance_bp`** | `T001` | 财务 BP (`FINANCE_ADMIN`) | P8 · 财务与行政部 | 输入「分析2026年第四季度全公司差旅预算与超支归因」，查看全公司 4 部门对比柱状图、趋势折线图与量价拆分饼图 |
| **`carol`** | `T002` | 普通员工 (`EMPLOYEE`) | P6 · 智能装备事业部 | 验证多租户隔离（只能查出 `YUANHANG-260` 制度，与 `T001` 完全隔离） |

### 7.4 第四步（如开启了 `--with-langfuse`）：绑定 Langfuse 可观测看板
1. 浏览器打开 **`http://<你的服务器公网IP>:3000`**；
2. 注册一个管理员账号 → 新建 Organization & Project（如 `pytravel-prod`）；
3. 进入 **Settings → API Keys → Create new API keys**，复制生成的 `Public Key`（`pk-lf-...`）和 `Secret Key`（`sk-lf-...`）；
4. 回到服务器修改 `.env` 并重启网关：
   ```bash
   sed -i 's/^LANGFUSE_ENABLED=.*/LANGFUSE_ENABLED=true/' .env
   # 编辑 .env 把 LANGFUSE_PUBLIC_KEY 和 LANGFUSE_SECRET_KEY 填进去
   nano .env
   docker compose up -d api-gateway
   ```
5. 再去网页端聊几句，回到 Langfuse `:3000` 即可看到完整的每次请求 Trace、耗时与 Token 消耗（且手机号/身份证号已被自动掩码脱敏）。

---

## 八、 生产日常运维、省钱停机与常见报错排查

### 8.1 常用运维命令速查
```bash
# 查看所有容器运行状态
docker compose ps

# 实时查看主网关或某个 MCP 容器的日志（排查报错最常用）
docker compose logs -f --tail=100 api-gateway
docker compose logs -f --tail=100 tuniu-mcp

# 单独更新并重启某个修改了代码的服务（无需重建 PG/Redis）
docker compose up -d --build api-gateway

# 停止全部服务（保留数据库数据卷）
docker compose stop

# 彻底停止并清空全部容器与数据库数据（慎用！重装洗库时用）
docker compose down -v
```

### 8.2 阿里云 300 元免费试用省钱技巧
- 平时不测试也不给别人演示时，去阿里云 ECS 控制台点**「停止」→ 选择「节省停机模式」**，停止期间实例不扣算力小时费（只扣极少量系统盘费，3 个月完全够用）；
- 再次开机后，由于所有容器均已配置 `restart: unless-stopped`，Docker 会自动把 10 个容器全部拉起，且因为断点存在 PostgreSQL + Redis 中，之前未点完的 HITL 审批卡片都不会丢。

### 8.3 常见报错排查 FAQ

1. **`./scripts/deploy_ecs.sh: /usr/bin/env: ‘bash\r’: No such file or directory`**
   - **原因**：从 Windows 上传保留了 `\r\n` 换行符。
   - **解决**：执行 `sed -i 's/\r$//' scripts/deploy_ecs.sh && chmod +x scripts/deploy_ecs.sh`。

2. **`docker compose build` 或 `docker pull` 报 `context deadline exceeded` / 网络超时**
   - **原因**：中国内地机房未配置 Docker 镜像加速。
   - **解决**：按本文「第四章 4.2」配置 `/etc/docker/daemon.json` 后执行 `systemctl restart docker`。

3. **打开网页访问正常，但发送消息提示 `502 Bad Gateway` 或连接失败**
   - **原因**：`api-gateway` 还在初始化或启动异常。
   - **解决**：执行 `docker compose logs --tail=80 api-gateway` 查看 Python 报错信息；常见原因是第一次启动 PG 还没建表完，等待 10 秒后执行 `docker compose restart api-gateway`。

4. **沙箱预算分析报错或 `docker exec` 权限拒绝**
   - **原因**：`pytravel-sandbox-pool` 未启动，或网关未挂载 `/var/run/docker.sock`。
   - **解决**：检查 `docker compose ps sandbox-pool` 是否为 `Up`，并确认 `docker-compose.yml` 中 `api-gateway` 挂载了 `/var/run/docker.sock`。

5. **对话能回复，但回复的是模板化话术而非大模型智能推理**
   - **原因**：`.env` 中 `LLM_MODE=mock` 或 `OPENAI_API_KEY` 未替换成真实 Key。
   - **解决**：检查 `.env` 确保 `LLM_MODE=openai` 且 `OPENAI_API_KEY` 有效，然后运行 `docker compose up -d api-gateway`。

6. **浏览器一直转圈打不开 `http://<公网IP>/`**
   - **原因**：阿里云安全组未放行 `80` 端口。
   - **解决**：去阿里云 ECS 控制台 → 安全组 → 入方向添加 `80` 端口放行。

---

## 九、 附：本地二次开发与自动化测试 / Eval 速查

如果你后续在本地（或服务器测试环境）修改了代码，可以用以下命令一键做回归验证（在零外部依赖仿真模式下运行，不花模型钱也不污染生产库）：

```bash
# 1. 运行全部 19 个单元测试与集成测试（覆盖多租户隔离、RLS、熔断、HITL防TOCTOU、并行沙箱、PBKDF2、Redis断点）
pytest -v

# 2. 运行离线自动化评测（输出 6 项量化指标并生成 eval/last_eval_report.json）
python eval/run_eval.py

# 3. 代码静态规范检查
ruff check apps scripts eval tests
```
