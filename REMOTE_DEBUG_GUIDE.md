# PyTravel-Harness 远程开发与断点调试指南（8GB 本地电脑 + 云服务器黄金工作流）

> **为什么要看这篇文档？**  
> 你的本地电脑是 **8GB 内存 Windows**，而云端有一台 **2核8G Linux ECS 服务器**。如果采用业界最成熟的 **`VS Code Remote - SSH` + `Docker Compose Debug`** 模式：
> - 本地 8GB 电脑**只负责显示代码编辑器界面和打开浏览器**（几乎不占内存，风扇都不转）；
> - 全部 10+ 个生产容器（PG16、Redis 7、禁网沙箱池、4 个 FastMCP、Langfuse）都在云服务器运行；
> - 你在编辑器里点 **F5** 就能直接**单步断点调试跑在 Docker 容器里的 Python 代码**，改完代码 1 秒生效，还能自动把服务器端口安全映射回本地浏览器！

---

## 一、 整体工作原理图

```text
┌───────────────────────────┐            SSH 加密通道 (只开 22 端口)            ┌─────────────────────────────────────────┐
│   本地 8GB Windows 电脑    │ ══════════════════════════════════════════════════> │        2核 8G 阿里云 ECS 服务器          │
│                           │                                                   │                                         │
│  1. VS Code (仅 UI 界面)   │ <── 自动端口转发 (80 / 8000 / 5678 / 3000) ────── │  Docker Compose (Debug 模式):            │
│  2. 浏览器访问:            │                                                   │   • frontend (:80)                      │
│     • http://localhost    │                                                   │   • api-gateway (:8000 + debugpy :5678) │
│     • http://localhost:3000│                                                  │   • 4 个 FastMCP 容器 + 禁网沙箱池        │
│     • F5 断点单步调试       │                                                   │   • postgres (:5432) + redis (:6379)    │
└───────────────────────────┘                                                   └─────────────────────────────────────────┘
```

---

## 二、 操作步骤（共 5 步，首次配置约 5 分钟）

### Step 1：配置 SSH 免密登录（强烈推荐，不用每次连接输密码）

在**本地 Windows PowerShell** 中执行：

1. 检查或生成本地 SSH 密钥（一路回车即可）：
   ```powershell
   if (-not (Test-Path "$env:USERPROFILE\.ssh\id_ed25519.pub")) {
       ssh-keygen -t ed25519 -N '""' -f "$env:USERPROFILE\.ssh\id_ed25519"
   }
   ```
2. 把公钥上传到你的阿里云服务器（这次需要输一次 root 密码）：
   ```powershell
   type "$env:USERPROFILE\.ssh\id_ed25519.pub" | ssh root@你的服务器公网IP "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```
3. 在本地写入 SSH 别名配置（编辑 `C:\Users\<你的用户名>\.ssh\config`，不存在就新建）：
   ```sshconfig
   Host pytravel-ecs
       HostName 你的服务器公网IP
       User root
       IdentityFile ~/.ssh/id_ed25519
       ServerAliveInterval 30
       ServerAliveCountMax 5
   ```
   > 验证一下：在 PowerShell 敲 `ssh pytravel-ecs`，如果**不用输密码直接登录进服务器**，这一步就成功了！

---

### Step 2：用 VS Code `Remote - SSH` 连上服务器

1. 在本地打开 **VS Code**（或者 Cursor / Antigravity IDE），在扩展商店搜索安装官方扩展：**`Remote - SSH`**（以及在远程窗口内安装 **`Python`**、**`Python Debugger`** 扩展）。
2. 按快捷键 `Ctrl + Shift + P`，输入并选择：**`Remote-SSH: Connect to Host...`** → 选择 **`pytravel-ecs`**。
3. 新窗口打开并连上服务器后，点击左侧 **`打开文件夹（Open Folder）`**，输入服务器上的项目路径：
   ```text
   /opt/pytravel-harness
   ```
4. 打开 VS Code 内置终端（`` Ctrl + ` ``），此时你敲的所有命令都已经是在**云端 Linux 服务器**里执行了！

---

### Step 3：以「调试模式」启动全栈 Docker 容器

确保你已经按 [README.md](./README.md) 跑过一次 `./scripts/deploy_ecs.sh`（镜像已构建好且 PG16 已灌入仿真数据）。

现在，在 VS Code 远程终端里用 **`docker-compose.yml` + `docker-compose.debug.yml`** 叠加启动：

```bash
cd /opt/pytravel-harness

# 调试模式启动（同时开启 Langfuse 可选 --profile observability）
docker compose -f docker-compose.yml -f docker-compose.debug.yml --profile observability up -d --build
```

查看 `api-gateway` 日志，确认 `debugpy` 已经在 `5678` 端口等待连接：
```bash
docker compose logs -f --tail=30 api-gateway
```
看到 `Uvicorn running on http://0.0.0.0:8000` 即说明服务与调试器均已就绪！

> **为什么修改代码不用重新 `docker build`？**  
> 因为 [docker-compose.debug.yml](./docker-compose.debug.yml) 已经把服务器的 `./apps`、`./scripts` 目录直接挂载进了容器里的 `/app/apps`。你在 VS Code 里改完代码保存后，只需 1 秒重启对应容器即可生效：
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.debug.yml restart api-gateway
> ```

---

### Step 4：按 F5 附加调试 & 最值得下断点的 5 个核心位置

项目已经内置了 [.vscode/launch.json](./.vscode/launch.json)。

1. 在 VS Code 左侧点击 **「运行和调试（Run and Debug）」图标**（快捷键 `Ctrl + Shift + D`）；
2. 顶部下拉框选择：**`1. Docker 容器附加调试: api-gateway (debugpy :5678)`**；
3. 点击绿色三角形（或直接按 **`F5`**），底部状态栏变成橙色即表示已经成功挂上容器内的 Python 进程！

#### 💡 推荐你亲手打断点“单步跟踪”的 5 个关键源码位置（边调试边看懂架构）

| 序号 | 文件路径与函数 | 你能在这里看到什么？ |
| :---: | :--- | :--- |
| **1** | `apps/api_gateway/routes/chat.py` → `chat_stream()` | 用户请求如何携带 JWT 进入网关，启动后台 `RunManager` 并从 Redis Stream 实时流式输出 SSE |
| **2** | `apps/agent_harness/router/l3_llm.py` → `ThreeTierIntentRouter.route()` | 亲眼看到 L1 正则如何在 `<1ms` 内短路直跳子图，以及复合意图如何转交 `MasterSupervisorGraph` |
| **3** | `apps/agent_harness/graphs/trip_plan_book.py` → `price_recheck_node()` & `execute_booking_node()` | 看到 OA 审批后下单前如何做**实时价格复核**，以及如何通过 `CredentialAndPersistenceMiddleware` 预写 `PENDING` 状态机防双写不一致 |
| **4** | `apps/agent_harness/graphs/budget_insight.py` → `fan_out_sandbox_workers()` & `verification_node()` | 看到 LangGraph `Send` 如何同时扇出 3 路禁网 Docker 沙箱精算，以及 `verification_node` 如何做零容差数值勾稽校验 |
| **5** | `apps/mcp_servers/erp_expense_mcp.py` → `export_expense_csv_to_sandbox()` | 看到 `X-PyTravel-Ctx` 内部令牌如何验签重建 `TenantContext`，并拦截平行部门越权查账 |

---

### Step 5：利用 VS Code 自动端口转发（零公网端口暴露）

连着 `Remote - SSH` 时，VS Code 底部面板的 **`端口（PORTS）`** 会自动把容器端口安全映射回你的本地电脑（即使你在阿里云安全组里**没有放行 `8000`、`5678`、`3000`、`5432`、`6379`**，本地也能直接连！）：

- 前端工作台：本地浏览器打开 `http://localhost:80`（或 `http://localhost`）
- 后端 Swagger 文档：本地浏览器打开 `http://localhost:8000/docs`
- Langfuse Trace 看板：本地浏览器打开 `http://localhost:3000`
- 数据库可视化客户端（如 DBeaver / DataGrip）：主机填 `127.0.0.1`、端口 `5432`、用户名 `pytravel_app`（测试 RLS）或 `pytravel`（管理员）、密码 `pytravel_app_pass` / `pytravel_pass`

> 如果某个端口没有自动出现在 `PORTS` 面板，在 VS Code 底部 `PORTS` 标签页点 **「添加端口（Forward a Port）」** 输入端口号回车即可。

---

## 三、 进阶优化与常见调试排错

### 3.1 服务器 Linux 文件监听数调优（防止 VS Code 报 `ENOSPC`）
当代码和容器卷较多时，建议在服务器上调高 `inotify` 上限（只需执行一次）：
```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### 3.2 进阶：使用 Tailscale 零信任内网替代公网 SSH（可选）
如果你经常切换家里/公司/咖啡馆 Wi-Fi，担心阿里云安全组开放 `22` 端口被暴力扫描：
1. 在服务器与本地 Windows 各自安装免费的 **Tailscale** 并登录同一账号；
2. 服务器会获得一个固定的内网加密 IP（`100.x.x.x`）；
3. 把 `~/.ssh/config` 里的 `HostName` 改成这个 `100.x.x.x`；
4. 去阿里云安全组**直接删掉 `22` 端口放行规则**，从此你的服务器对公网几乎隐身，笔记本合盖休眠再打开也能秒重连！

### 3.3 调试常见问题 FAQ
1. **按 F5 提示 `connect ECONNREFUSED 127.0.0.1:5678`？**
   - 检查容器是否是用叠加命令启动的：`docker compose -f docker-compose.yml -f docker-compose.debug.yml ps`；
   - 如果确认启动了但还没看到 `5678`，看一眼 VS Code 底部 `PORTS` 面板里是否把 `5678` 转发到了本地。
2. **断点显示空心圆（Unverified Breakpoint）不停下来？**
   - 确认选的是 `1. Docker 容器附加调试: api-gateway (debugpy :5678)`，并且 `pathMappings` 中 `remoteRoot` 为 `/app`；
   - 如果要调试 `tuniu-mcp` / `oa-mcp` / `erp-expense-mcp` 等独立 MCP 容器里的断点，临时想单步进 MCP 时，最简单的方法是在本地 `.env` 把 `MCP_MODE=inprocess` 并使用 `launch.json` 第 2 项 `宿主机直跑调试` 直接跑在服务器宿主机进程内。
3. **不想调试了，切回纯生产模式怎么切？**
   ```bash
   docker compose up -d
   ```
   一条命令就会自动移除调试端口和源码挂载，恢复纯生产镜像模式。
