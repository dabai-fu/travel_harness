---
name: trip-planning
trigger: 当员工请求差旅出行规划、机票/高铁/酒店比价、冲突检测或通过途牛预订时加载。
---

# 技能：智能差旅行程规划与途牛预订（Trip Planning & Booking）

## 1. 槽位抽取与主动澄清规范
必须提取以下核心要素：
- `origin_city`（出发城市，缺省时从员工档案 `home_city` 自动补全）
- `dest_city`（目的地城市，缺失必须追问）
- `start_date` / `days`（出发日期与出差天数，支持“下周二”“后天”“3天”等相对表达）
- `hotel_preference`（商圈或品牌偏好，结合长期记忆如“国贸附近”“亚朵/全季”）

## 2. 六维软审核与冲突检测优先级
1. **时间重叠冲突（HIGH）**：同一员工历史 `travel_order` 在重叠日期已有未取消行程；
2. **跨城物理不可达（HIGH）**：前后行程结束与开始间隔过短，无法跨城衔接；
3. **差标软约束（MEDIUM/LOW）**：对比 `travel_policy_rule` 的舱位、席别、每晚住宿限额、提前预订天数、审批升级阈值。

## 3. 4 套代表方案展示格式
通过 `RoundtripPlanner` 统一输出 4 套方案（推送至 `travel_cards` SSE 事件）：
1. **最省钱**（价格权重优先）
2. **最省时**（总行程耗时最短）
3. **最合规**（硬约束 `PolicyPenalty == 0`，若无完全合规方案则明确提示）
4. **最舒适**（综合个人偏好与酒店评分）

## 4. 下单前二次核对清单（v1.1 资金安全铁律）
1. 通过 LangGraph `interrupt()` 弹出 `hitl_form`，绑定 `action_hash`；
2. OA 审批通过后，下单前**必须**调 `tuniu-mcp` 复核实时价格与余票，涨价超过 `max(50 元, 3%)` 或售罄必须再次 `interrupt()` 确认；
3. 遵循“预写 `PENDING` -> 调途牛下单 -> 回写 `CONFIRMED`/`FAILED`”，每段行程携带独立幂等键 `segment_no`。
