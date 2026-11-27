---
name: expense-drafting
trigger: 当员工上传机票行程单、酒店水单、打车或餐饮发票，请求整理并草拟 ERP 报销单时加载。
---

# 技能：发票识别与报销单智能草拟（Expense Drafting）

## 1. 多模态发票解析规范
使用视觉模型提取每张单据的：`invoice_no`、`invoice_date`、`merchant`、`category`（FLIGHT/HOTEL/TRAIN/TAXI/MEAL）、`amount`、`tax_amount`。

## 2. 订单关联与柔性提示模板
- 自动查询该员工近期 `COMPLETED` / `APPROVED` 差旅单及 `booking_record`；
- 定位原则：**高容错辅助，绝不越权扣款或自动入账**；
- 柔性提示示例：
  - 超标提示：`酒店每晚 730 元，超出 P6 一线城市限额（650 元）80 元，建议在事由中填写“展会期间周边房源紧张”说明。`
  - 缺票提示：`已匹配到去程航班与酒店发票，暂未检测到返程交通或市内交通票据，确认是否先行提交草稿？`

## 3. HITL 确认与推送 ERP
- 通过 `interrupt()` 展示预填明细表单并绑定 `action_hash`；
- 用户确认后在独立后继节点调用 `erp-expense-mcp.submit_expense_draft` 生成 ERP 草稿单号。
