import React, { useState } from "react";
import { Alert, Button, Card, Descriptions, Space, Tag } from "antd";
import type { HitlPayload } from "../types/sse";
import { TravelOptionCards } from "./TravelOptionCards";

interface Props {
  hitl: HitlPayload;
  onConfirm: (decision: "CONFIRMED" | "REJECTED", selectedOptionId?: string) => Promise<void>;
}

/**
 * HITL 人工拍板交互确认卡片（展示防篡改 action_hash 摘要并触发 /api/chat/resume）。
 */
export const HitlConfirmCard: React.FC<Props> = ({ hitl, onConfirm }) => {
  const defaultOptId = hitl.options?.[2]?.option_id || hitl.options?.[0]?.option_id;
  const [selectedOptId, setSelectedOptId] = useState<string | undefined>(defaultOptId);
  const [submitting, setSubmitting] = useState(false);

  const handleAction = async (decision: "CONFIRMED" | "REJECTED") => {
    setSubmitting(true);
    try {
      await onConfirm(decision, selectedOptId);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card
      size="small"
      style={{ marginTop: 12, borderColor: "#faad14", background: "#fffbe6", borderRadius: 10 }}
      title={
        <Space>
          <Tag color="warning">HITL 人工确认</Tag>
          <span>{hitl.title}</span>
        </Space>
      }
    >
      <Alert type="info" showIcon message={hitl.description} style={{ marginBottom: 12 }} />

      {hitl.options && hitl.options.length > 0 && (
        <TravelOptionCards
          options={hitl.options}
          selectedId={selectedOptId}
          onSelect={setSelectedOptId}
        />
      )}

      <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="高危工具">{hitl.tool_name}</Descriptions.Item>
        <Descriptions.Item label="参数防篡改哈希">{hitl.action_hash.slice(0, 16)}...</Descriptions.Item>
      </Descriptions>

      <Space>
        <Button type="primary" loading={submitting} onClick={() => handleAction("CONFIRMED")}>
          确认并执行
        </Button>
        <Button danger loading={submitting} onClick={() => handleAction("REJECTED")}>
          拒绝 / 取消
        </Button>
      </Space>
    </Card>
  );
};
