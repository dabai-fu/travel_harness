import React from "react";
import { Card, Col, Row } from "antd";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartPayload } from "../types/sse";

const PIE_COLORS = ["#1677ff", "#ff4d4f", "#faad14", "#52c41a"];

/**
 * Recharts 3 预算执行与超支归因看板组件。
 * 对应文档《三、模块 4》与《九、9.2 chart_data》。
 */
export const BudgetChartsPanel: React.FC<{ charts: ChartPayload[] }> = ({ charts }) => {
  return (
    <Row gutter={[12, 12]} style={{ marginTop: 12, marginBottom: 12 }}>
      {charts.map((chart) => (
        <Col xs={24} lg={chart.type === "bar" ? 24 : 12} key={chart.chart_id}>
          <Card size="small" title={chart.title} style={{ borderRadius: 10 }}>
            <div style={{ width: "100%", height: 260 }}>
              <ResponsiveContainer>
                {chart.type === "bar" ? (
                  <BarChart data={chart.data}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey={chart.x_key} />
                    <YAxis />
                    <Tooltip />
                    <Legend />
                    {chart.series.map((s) => (
                      <Bar key={s.dataKey} dataKey={s.dataKey} name={s.name} fill={s.fill || "#1677ff"} />
                    ))}
                  </BarChart>
                ) : chart.type === "line" ? (
                  <LineChart data={chart.data}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey={chart.x_key} />
                    <YAxis />
                    <Tooltip />
                    <Legend />
                    {chart.series.map((s) => (
                      <Line
                        key={s.dataKey}
                        type="monotone"
                        dataKey={s.dataKey}
                        name={s.name}
                        stroke={s.stroke || "#13c2c2"}
                        strokeWidth={2}
                      />
                    ))}
                  </LineChart>
                ) : (
                  <PieChart>
                    <Pie data={chart.data} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={85} label>
                      {chart.data.map((_, idx) => (
                        <Cell key={idx} fill={PIE_COLORS[idx % PIE_COLORS.length]} />
                      ))}
                    </Pie>
                    <Tooltip />
                    <Legend />
                  </PieChart>
                )}
              </ResponsiveContainer>
            </div>
          </Card>
        </Col>
      ))}
    </Row>
  );
};
