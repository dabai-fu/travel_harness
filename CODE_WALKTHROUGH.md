# PyTravel-Harness 代码导读地图（从零看懂全链路）

本文档旨在帮你按**最短理解路径**通读整个代码库，每一步都对应设计文档中的核心工程难点。

---

## 第一站：数据与安全底座（先理解“多租户隔离与防篡改”）
1. **全局配置与运行开关**：[apps/common/config.py](file:///d:/java%20programs/travel_harness/apps/common/config.py)
   - 看懂如何通过 `APP_STORAGE_MODE / REDIS_MODE / SANDBOX_MODE / LLM_MODE / TUNIU_MODE` 实现“零外部依赖本地运行”和“PG16 + Redis7 + Docker 生产部署”双模式切换。
2. **PostgreSQL 16 + RLS 建表脚本**：[scripts/init_db.sql](file:///d:/java%20programs/travel_harness/scripts/init_db.sql)
   - 重点看第 6 张表 `travel_policy_rule` 的 `EXCLUDE USING gist` 职级区间排他约束（v1.1 P0 #9）、第 9 张表 `booking_record` 的分段幂等与 `PENDING` 预写设计，以及文末全部租户表的 `FORCE ROW LEVEL SECURITY` 行级安全策略。
3. **密码学与脱敏**：[apps/common/crypto.py](file:///d:/java%20programs/travel_harness/apps/common/crypto.py) & [apps/api_gateway/security/masker.py](file:///d:/java%20programs/travel_harness/apps/api_gateway/security/masker.py)
   - 重点看 `encrypt_aes_gcm`（带 `key_version` 支持密钥轮换）、`make_booking_idempotent_key`（带 `|` 分隔符与 `segment_no`）、`make_action_hash`（防 TOCTOU）。
4. **仿真造数与 Decimal 真值核算**：[scripts/seed_mock_data.py](file:///d:/java%20programs/travel_harness/scripts/seed_mock_data.py)
   - 生成 2 个隔离租户、6 个部门（`sys_dept` 物化路径）、1000+ 条跨越 Q3/Q4 的差旅与途牛预订数据。

---

## 第二站：Agent Harness 工程底座（7 大可插拔中间件 + 护栏）
全部位于 [apps/agent_harness/middlewares/](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/__init__.py)，建议依次阅读：
1. [tenant_context.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/tenant_context.py)：`ContextVar` 协程透传 + 线程池复制 + 60s 内部令牌 `X-PyTravel-Ctx` 跨进程签发/验签。
2. [stream_progress.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/stream_progress.py)：图执行写入 Redis Stream，SSE 端按 `Last-Event-ID` 续读 + 32 字符/50ms 合批。
3. [circuit_breaker.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/circuit_breaker.py)：Redis Lua 原子三态熔断（全局故障 vs 租户凭证故障两级隔离、`HALF_OPEN` 单探活锁、WRITE 超时不重试）。
4. [context_engineering.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/context_engineering.py)：>4KB 大结果落盘卸载（只返 `artifact_id` + 前 5 行）、途牛大 JSON 裁剪、历史 `SKILL.md` 折叠、Token 硬止损。
5. [cluster_cancel.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/cluster_cancel.py)：单连接 `PSUBSCRIBE` + `cancel_flag` 双保险跨副本优雅停止。
6. [credential_persist.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/credential_persist.py)：凭证动态解密缓存 + `PENDING -> CONFIRMED/FAILED` 状态机 + [scheduled_jobs.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/runtime/scheduled_jobs.py) 对账补偿。
7. [langfuse_observability.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/middlewares/langfuse_observability.py)：Trace 脱敏回调 + 月度 Token 配额拦截。
8. **读写分级护栏**：[apps/agent_harness/runtime/guardrails.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/runtime/guardrails.py) —— `action_hash` 确认即绑定与执行前二次校验。

---

## 第三站：三层路由与核心算法
1. **三层意图路由**：[l1_regex.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/router/l1_regex.py) -> [l2_vector.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/router/l2_vector.py) -> [l3_llm.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/router/l3_llm.py)
   - 注意看 v1.1 的 4 个短路前置条件（断点粘性路由、L1 唯一命中、指代词不跳过改写、L2 `Top1 - Top2 >= 0.05`）。
2. **多目标往返优化**：[roundtrip_planner.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/graphs/roundtrip_planner.py)
   - 看懂分段 Top-K 预裁剪、P5/P95 截尾防极端价格、以及“最合规”如何通过 `PolicyPenalty == 0` 硬约束选出。
3. **沙箱精算引擎**：[budget_calc.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/skills/budget-fpa-attribution/scripts/budget_calc.py) & [docker_runner.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/sandbox/docker_runner.py)
   - 看懂“CSV 字符串 -> `int64` 分向量化聚合 -> 输出层 `Decimal`”高精度方案，以及无残差两因素量价公式 $\Delta C = (Q_1-Q_0)P_0 + (P_1-P_0)Q_1$。

---

## 第四站：五大 LangGraph 图与接口网关
1. **行程规划与预订子图**：[trip_plan_book.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/graphs/trip_plan_book.py) —— 重点看“纯 `interrupt()` 节点无副作用”与“OA 审批后下单前实时价格复核”。
2. **预算并行归因子图**：[budget_insight.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/graphs/budget_insight.py) —— 重点看 `Send` API 并行扇出 3 路沙箱 Worker、`{{占位符}}` 报告防幻觉、以及 `verification_node` 零容差勾稽。
3. **政策双通道 & 报销草稿子图**：[policy_info.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/graphs/policy_info.py) & [expense_draft.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/graphs/expense_draft.py)。
4. **后台调度与 API 网关**：[run_manager.py](file:///d:/java%20programs/travel_harness/apps/agent_harness/runtime/run_manager.py) -> [auth.py](file:///d:/java%20programs/travel_harness/apps/api_gateway/auth.py) -> [chat.py](file:///d:/java%20programs/travel_harness/apps/api_gateway/routes/chat.py) -> [webhook.py](file:///d:/java%20programs/travel_harness/apps/api_gateway/routes/webhook.py)。

---

## 第五站：前端工作台与自动化验证
- **React 19 工作台**：[App.tsx](file:///d:/java%20programs/travel_harness/frontend/src/App.tsx) & [chatStore.ts](file:///d:/java%20programs/travel_harness/frontend/src/store/chatStore.ts)
- **离线评测与单测**：[eval/run_eval.py](file:///d:/java%20programs/travel_harness/eval/run_eval.py) & [tests/](file:///d:/java%20programs/travel_harness/tests/test_graphs_and_api.py)
