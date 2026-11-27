/**
 * SSE 标准事件与核心业务卡片 TypeScript 类型定义。
 * 严格对应文档《九、9.2 SSE 流式协议标准事件表》。
 */

export type SseEventType =
  | "agent_switch"
  | "thinking"
  | "progress"
  | "plan_update"
  | "travel_cards"
  | "chart_data"
  | "hitl_form"
  | "message"
  | "suggestions"
  | "interrupted"
  | "done"
  | "error";

export interface TravelLeg {
  leg_id: string;
  category: "FLIGHT" | "TRAIN";
  carrier_or_train_no: string;
  depart_time: string;
  arrive_time: string;
  duration_minutes: number;
  cabin_or_seat: string;
  price: number;
}

export interface HotelOption {
  hotel_id: string;
  hotel_name: string;
  brand: string;
  city: string;
  district: string;
  nightly_price: number;
  total_price: number;
  nights: number;
  is_corporate_agreed: boolean;
  rating: number;
}

export interface TripOption {
  option_id: string;
  option_hash: string;
  label: "最省钱" | "最省时" | "最合规" | "最舒适";
  outbound: TravelLeg;
  hotel: HotelOption;
  inbound: TravelLeg;
  total_price: number;
  total_duration_minutes: number;
  is_policy_compliant: boolean;
  compliance_notes: string[];
}

export interface ChartPayload {
  chart_id: string;
  type: "bar" | "line" | "pie";
  title: string;
  x_key: string;
  series: Array<{ dataKey: string; name: string; fill?: string; stroke?: string }>;
  data: Array<Record<string, string | number>>;
}

export interface HitlPayload {
  interrupt_id: string;
  ui_type: "select" | "confirm" | "form";
  title: string;
  description: string;
  tool_name: string;
  tool_args: Record<string, unknown>;
  action_hash: string;
  options?: TripOption[];
}
