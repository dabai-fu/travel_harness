import React from "react";
import { Card, Col, Row, Tag, Typography } from "antd";
import type { TripOption } from "../types/sse";

const { Text } = Typography;

interface Props {
  options: TripOption[];
  selectedId?: string;
  onSelect?: (optionId: string) => void;
}

const LABEL_COLORS: Record<string, string> = {
  最省钱: "green",
  最省时: "blue",
  最合规: "gold",
  最舒适: "purple",
};

/**
 * 4 套代表性往返行程方案对比卡片（最省钱 / 最省时 / 最合规 / 最舒适）。
 * 对应文档《三、模块 1》与《九、9.2 travel_cards》。
 */
export const TravelOptionCards: React.FC<Props> = ({ options, selectedId, onSelect }) => {
  return (
    <Row gutter={[12, 12]} style={{ marginTop: 8, marginBottom: 12 }}>
      {options.map((opt) => {
        const isSelected = selectedId === opt.option_id;
        return (
          <Col xs={24} md={12} key={opt.option_id}>
            <Card
              size="small"
              hoverable={Boolean(onSelect)}
              onClick={() => onSelect?.(opt.option_id)}
              style={{
                borderColor: isSelected ? "#1677ff" : "#e5e7eb",
                borderWidth: isSelected ? 2 : 1,
                borderRadius: 10,
              }}
              title={
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <Tag color={LABEL_COLORS[opt.label] || "default"}>{opt.label}</Tag>
                  <Text strong style={{ color: "#cf1322", fontSize: 16 }}>
                    ¥{opt.total_price.toFixed(2)}
                  </Text>
                </div>
              }
            >
              <div style={{ fontSize: 13, lineHeight: 1.7 }}>
                <div>
                  <Text strong>去程：</Text>
                  {opt.outbound.carrier_or_train_no}（{opt.outbound.depart_time}-{opt.outbound.arrive_time}）¥
                  {opt.outbound.price}
                </div>
                <div>
                  <Text strong>酒店：</Text>
                  {opt.hotel.hotel_name}（{opt.hotel.nights}晚）¥{opt.hotel.total_price}
                </div>
                <div>
                  <Text strong>返程：</Text>
                  {opt.inbound.carrier_or_train_no}（{opt.inbound.depart_time}-{opt.inbound.arrive_time}）¥
                  {opt.inbound.price}
                </div>
                <div style={{ marginTop: 6 }}>
                  <Tag color={opt.is_policy_compliant ? "success" : "warning"}>
                    {opt.is_policy_compliant ? "100% 差标合规" : opt.compliance_notes[0]}
                  </Tag>
                </div>
              </div>
            </Card>
          </Col>
        );
      })}
    </Row>
  );
};
