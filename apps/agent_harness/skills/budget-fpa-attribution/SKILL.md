---
name: budget-fpa-attribution
trigger: 当部门主管或财务 BP 询问部门预算执行率、超支归因分析（量价拆分/临期溢价）与降本测算时加载。
---

# 技能：企业差旅预算执行与超支归因（FP&A Attribution）

## 1. 强制工程铁律
1. **数值计算 100% 下沉沙箱脚本** `scripts/budget_calc.py`，禁止模型口算汇总金额与同比/环比；
2. **精度方案（v1.1 P1 #11）**：CSV 金额以字符串读入，转为 **`int64` 分（cent）** 进行 Pandas 向量化聚合，仅在最终输出层转换为 `Decimal`（`ROUND_HALF_UP` 保留 2 位）；
3. **无残差两因素量价拆分公式（v1.1 P1 #12）**：
   $$\Delta C = (Q_1 - Q_0) \cdot P_0 + (P_1 - P_0) \cdot Q_1$$
   保证“量增效应 + 价升效应 = 总支出差额”恒成立，支持 `VerificationNode` 零容差勾稽；
4. **临期阈值读取规则表**：从 `travel_policy_rule.min_advance_booking_days` 动态读取，禁止硬编码 3 天；
5. **报告防幻觉**：大模型写报告只写 `{{...}}` 占位符，不直接书写任何指标数字。
