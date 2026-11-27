import React, { useRef, useState } from "react";
import { Alert, Button, Card, Input, Layout, Select, Space, Tag, Typography } from "antd";
import { BudgetChartsPanel } from "./components/BudgetChartsPanel";
import { HitlConfirmCard } from "./components/HitlConfirmCard";
import { TravelOptionCards } from "./components/TravelOptionCards";
import { useChatStore } from "./store/chatStore";

const { Header, Content } = Layout;
const { Title, Text } = Typography;

const DEMO_USERS = [
  { label: "普通员工 林小雨 (P6 · 华东交付部)", value: "alice" },
  { label: "部门主管 陈志远 (P8 · 华东交付部)", value: "mgr_east" },
  { label: "研发总监 王建国 (P9 · 研发中心含子部门)", value: "vp_rd" },
  { label: "财务 BP 苏静 (FINANCE_ADMIN · 全公司)", value: "finance_bp" },
];

/**
 * PyTravel-Harness 企业级差旅协同与预算归因分析主工作台。
 */
export default function App() {
  const {
    session,
    messages,
    activeAgent,
    progressSteps,
    pendingHitl,
    isStreaming,
    errorBanner,
    login,
    sendQuery,
    submitHitlDecision,
    cancelRun,
  } = useChatStore();

  const [selectedUser, setSelectedUser] = useState("alice");
  const [inputText, setInputText] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<Array<{ filename: string; mime_type: string; image_base64: string }>>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    const loaded = await Promise.all(
      files.map(
        (file) =>
          new Promise<{ filename: string; mime_type: string; image_base64: string }>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () =>
              resolve({
                filename: file.name,
                mime_type: file.type || "image/jpeg",
                image_base64: String(reader.result || ""),
              });
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(file);
          })
      )
    );
    setPendingAttachments((prev) => [...prev, ...loaded]);
    e.target.value = "";
  };

  const handleSend = async (text?: string) => {
    const q = (text ?? inputText).trim();
    if (!q) return;
    if (!text) setInputText("");
    const extraSlots = pendingAttachments.length > 0 ? { attachments: pendingAttachments } : {};
    setPendingAttachments([]);
    await sendQuery(q, extraSlots);
  };

  if (!session) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#f5f7fa" }}>
        <Card style={{ width: 440, borderRadius: 12 }} title="PyTravel-Harness 多租户工作台登录">
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            <Text type="secondary">选择仿真企业（星云科技 T001）角色体验 RBAC 隔离与多智能体协同：</Text>
            <Select value={selectedUser} onChange={setSelectedUser} options={DEMO_USERS} style={{ width: "100%" }} />
            <Button type="primary" block size="large" onClick={() => login(selectedUser, "T001")}>
              登录进入工作台
            </Button>
          </Space>
        </Card>
      </div>
    );
  }

  return (
    <Layout style={{ minHeight: "100vh", background: "#f5f7fa" }}>
      <Header style={{ background: "#fff", borderBottom: "1px solid #eee", display: "flex", justifyContent: "space-between", alignItems: "center", padding: "0 24px" }}>
        <Space>
          <Title level={4} style={{ margin: 0 }}>PyTravel-Harness</Title>
          <Tag color="blue">{session.tenant_id}</Tag>
          <Tag color="geekblue">{session.role}</Tag>
          <Text>{session.real_name}（{session.dept_name} · {session.rank_level}）</Text>
        </Space>
        {activeAgent && <Tag color="processing">当前接管智能体: {activeAgent}</Tag>}
      </Header>

      <Content style={{ maxWidth: 1080, width: "100%", margin: "20px auto", padding: "0 16px" }}>
        {errorBanner && <Alert type="error" message={errorBanner} showIcon style={{ marginBottom: 12 }} />}

        <Space wrap style={{ marginBottom: 12 }}>
          <Button size="small" onClick={() => handleSend("下周二从上海去北京出差3天，住国贸附近，帮我规划并预订")}>
            示例：规划上海→北京出差
          </Button>
          <Button size="small" onClick={() => handleSend("帮我查一下北京和上海的差旅政策和住宿限额")}>
            示例：查差旅标准与天气
          </Button>
          <Button size="small" onClick={() => handleSend("我有几张出差发票和酒店水单，帮我整理成报销单草稿")}>
            示例：发票识别草拟报销
          </Button>
          <Button size="small" onClick={() => handleSend("分析一下2026年第四季度差旅预算执行与超支归因")}>
            示例：预算超支归因看板
          </Button>
        </Space>

        <Card style={{ borderRadius: 12, minHeight: 460 }}>
          {progressSteps.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              {progressSteps.map((p, i) => (
                <Tag key={i} color={p.status === "done" ? "success" : "processing"}>
                  {p.step}
                </Tag>
              ))}
            </div>
          )}

          {messages.map((m) => (
            <div key={m.id} style={{ marginBottom: 16, textAlign: m.role === "user" ? "right" : "left" }}>
              <div
                style={{
                  display: "inline-block",
                  maxWidth: "92%",
                  padding: "10px 14px",
                  borderRadius: 10,
                  background: m.role === "user" ? "#1677ff" : "#f6f8fa",
                  color: m.role === "user" ? "#fff" : "#1f2937",
                  whiteSpace: "pre-wrap",
                  textAlign: "left",
                }}
              >
                {m.content || (isStreaming && m.role === "assistant" ? "正在协同处理..." : "")}
              </div>

              {m.travelOptions && <TravelOptionCards options={m.travelOptions} />}
              {m.charts && <BudgetChartsPanel charts={m.charts} />}
              {m.suggestions && (
                <Space wrap style={{ marginTop: 8 }}>
                  {m.suggestions.map((q) => (
                    <Button key={q} size="small" type="dashed" onClick={() => handleSend(q)}>
                      {q}
                    </Button>
                  ))}
                </Space>
              )}
            </div>
          ))}

          {pendingHitl && <HitlConfirmCard hitl={pendingHitl} onConfirm={submitHitlDecision} />}
        </Card>

        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          style={{ display: "none" }}
          onChange={handleFileChange}
        />
        {pendingAttachments.length > 0 && (
          <Space wrap style={{ marginTop: 8 }}>
            {pendingAttachments.map((att, idx) => (
              <Tag
                key={idx}
                closable
                color="cyan"
                onClose={() => setPendingAttachments((prev) => prev.filter((_, i) => i !== idx))}
              >
                发票附件: {att.filename}
              </Tag>
            ))}
          </Space>
        )}
        <Space.Compact style={{ width: "100%", marginTop: 12 }}>
          <Button size="large" onClick={() => fileInputRef.current?.click()} disabled={isStreaming}>
            上传发票图
          </Button>
          <Input
            size="large"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onPressEnter={() => handleSend()}
            placeholder="输入差旅规划、政策查询、报销草拟或部门预算归因需求..."
            disabled={isStreaming}
          />
          {isStreaming ? (
            <Button size="large" danger onClick={cancelRun}>
              停止生成
            </Button>
          ) : (
            <Button size="large" type="primary" onClick={() => handleSend()}>
              发送
            </Button>
          )}
        </Space.Compact>
      </Content>
    </Layout>
  );
}
