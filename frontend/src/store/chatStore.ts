/**
 * Zustand 全局对话与 SSE 状态管理仓库。
 *
 * 对应文档章节：九、9.1 / 9.2 SSE 流式协议
 * 核心功能：
 *   1. 管理 JWT 登录态与当前用户角色（EMPLOYEE / DEPT_MANAGER / FINANCE_ADMIN）；
 *   2. 消费 POST /api/chat/stream 的 SSE 帧，实时更新 timeline 进度、4套行程方案卡片、
 *      Recharts 3 预算图表、HITL 确认表单与追问胶囊；
 *   3. 维护 `lastEventId` 支持断线重连续传；
 *   4. 提供 `submitHitlDecision` 调用 /api/chat/resume（携带 interrupt_id + action_hash）。
 */

import { create } from "zustand";
import type { ChartPayload, HitlPayload, TripOption } from "../types/sse";

export interface ChatTimelineItem {
  id: string;
  role: "user" | "assistant";
  content: string;
  travelOptions?: TripOption[];
  charts?: ChartPayload[];
  suggestions?: string[];
}

interface UserSession {
  token: string;
  tenant_id: string;
  user_id: string;
  role: string;
  real_name: string;
  dept_name: string;
  rank_level: string;
}

interface ChatStore {
  session: UserSession | null;
  conversationId: string;
  messages: ChatTimelineItem[];
  activeAgent: string | null;
  progressSteps: Array<{ step: string; status: string }>;
  pendingHitl: HitlPayload | null;
  isStreaming: boolean;
  lastEventId: string;
  errorBanner: string | null;

  login: (username: string, tenantId?: string) => Promise<void>;
  sendQuery: (query: string, extraSlots?: Record<string, unknown>) => Promise<void>;
  submitHitlDecision: (decision: "CONFIRMED" | "REJECTED", selectedOptionId?: string) => Promise<void>;
  cancelRun: () => Promise<void>;
}

function parseSseFrames(buffer: string): { frames: Array<{ id?: string; event: string; data: string }>; rest: string } {
  const chunks = buffer.split("\n\n");
  const rest = chunks.pop() ?? "";
  const frames: Array<{ id?: string; event: string; data: string }> = [];

  for (const raw of chunks) {
    if (!raw.trim()) continue;
    let id: string | undefined;
    let event = "message";
    const dataLines: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("id:")) id = line.slice(3).trim();
      else if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    frames.push({ id, event, data: dataLines.join("\n") });
  }
  return { frames, rest };
}

export const useChatStore = create<ChatStore>((set, get) => ({
  session: null,
  conversationId: `conv_${Math.random().toString(36).slice(2, 10)}`,
  messages: [],
  activeAgent: null,
  progressSteps: [],
  pendingHitl: null,
  isStreaming: false,
  lastEventId: "0-0",
  errorBanner: null,

  login: async (username: string, tenantId = "T001") => {
    const resp = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_id: tenantId, username, password: "123456" }),
    });
    if (!resp.ok) throw new Error("登录失败");
    const data = await resp.json();
    set({
      session: {
        token: data.access_token,
        tenant_id: data.tenant_id,
        user_id: data.user_id,
        role: data.role,
        real_name: data.real_name,
        dept_name: data.dept_name,
        rank_level: data.rank_level,
      },
      messages: [],
      pendingHitl: null,
      errorBanner: null,
    });
  },

  sendQuery: async (query: string, extraSlots: Record<string, unknown> = {}) => {
    const { session, conversationId } = get();
    if (!session) return;

    const userMsgId = `u_${Date.now()}`;
    const botMsgId = `a_${Date.now() + 1}`;
    set((s) => ({
      isStreaming: true,
      errorBanner: null,
      pendingHitl: null,
      progressSteps: [],
      messages: [
        ...s.messages,
        { id: userMsgId, role: "user", content: query },
        { id: botMsgId, role: "assistant", content: "" },
      ],
    }));

    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session.token}`,
      },
      body: JSON.stringify({ conversation_id: conversationId, query, extra_slots: extraSlots }),
    });

    if (!resp.body) {
      set({ isStreaming: false, errorBanner: "SSE 流不可用" });
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const { frames, rest } = parseSseFrames(buf);
      buf = rest;

      for (const f of frames) {
        if (f.id) set({ lastEventId: f.id });
        let payload: Record<string, unknown> = {};
        try {
          payload = JSON.parse(f.data);
        } catch {
          continue;
        }

        if (f.event === "agent_switch") {
          set({ activeAgent: String(payload.to || "") });
        } else if (f.event === "progress") {
          set((s) => ({
            progressSteps: [...s.progressSteps, { step: String(payload.step), status: String(payload.status) }],
          }));
        } else if (f.event === "travel_cards") {
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === botMsgId ? { ...m, travelOptions: payload.options as TripOption[] } : m
            ),
          }));
        } else if (f.event === "chart_data") {
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === botMsgId ? { ...m, charts: payload.charts as ChartPayload[] } : m
            ),
          }));
        } else if (f.event === "hitl_form") {
          set({ pendingHitl: payload as unknown as HitlPayload });
        } else if (f.event === "message") {
          const delta = String(payload.delta || "");
          set((s) => ({
            messages: s.messages.map((m) => (m.id === botMsgId ? { ...m, content: m.content + delta } : m)),
          }));
        } else if (f.event === "suggestions") {
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === botMsgId ? { ...m, suggestions: payload.questions as string[] } : m
            ),
          }));
        } else if (f.event === "error") {
          set({ errorBanner: String(payload.message || "执行出错") });
        } else if (f.event === "done" || f.event === "interrupted") {
          set({ isStreaming: false });
        }
      }
    }
    set({ isStreaming: false });
  },

  submitHitlDecision: async (decision: "CONFIRMED" | "REJECTED", selectedOptionId?: string) => {
    const { session, conversationId, pendingHitl } = get();
    if (!session || !pendingHitl) return;

    const resp = await fetch("/api/chat/resume", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session.token}`,
      },
      body: JSON.stringify({
        conversation_id: conversationId,
        interrupt_id: pendingHitl.interrupt_id,
        action_hash: pendingHitl.action_hash,
        decision,
        selected_option_id: selectedOptionId,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      set({ errorBanner: data.detail || "HITL 恢复失败" });
      return;
    }

    if (data.status === "interrupted" && data.pending_hitl) {
      set({ pendingHitl: data.pending_hitl as HitlPayload });
    } else {
      set((s) => ({
        pendingHitl: null,
        messages: [
          ...s.messages,
          { id: `res_${Date.now()}`, role: "assistant", content: String(data.final_answer || "操作已确认完成。") },
        ],
      }));
    }
  },

  cancelRun: async () => {
    const { session, conversationId } = get();
    if (!session) return;
    await fetch("/api/chat/cancel", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session.token}`,
      },
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    set({ isStreaming: false });
  },
}));
