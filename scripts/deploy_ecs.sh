#!/usr/bin/env bash
# ==============================================================================
# PyTravel-Harness 阿里云 Ubuntu ECS 生产级一键部署脚本
# 适用配置：2核8G 经济型 e（已预装 Docker & Docker Compose）
#
# 功能：
#   1. 若不存在 .env，从 .env.production.example 生成并自动填充高熵随机密钥；
#   2. 构建并拉起全栈 10 个生产服务容器（可选 --with-langfuse 开启 Langfuse）；
#   3. 等待数据库就绪后自动执行一次幂等 PG 仿真造数（2 租户、1000+ 条差旅预订）；
#   4. 校验 /health 接口并打印访问地址。
# ==============================================================================
set -euo pipefail

WITH_LANGFUSE="false"
if [[ "${1:-}" == "--with-langfuse" ]]; then
  WITH_LANGFUSE="true"
fi

echo "==> [1/4] 检查环境变量文件 .env ..."
if [[ ! -f ".env" ]]; then
  cp .env.production.example .env
  python3 - <<'PY'
import base64, os, secrets
from pathlib import Path

env_path = Path(".env")
text = env_path.read_text(encoding="utf-8")
text = text.replace("__AUTO_GENERATE_B64_32__", base64.b64encode(os.urandom(32)).decode(), 1)
while "__AUTO_GENERATE__" in text:
    text = text.replace("__AUTO_GENERATE__", secrets.token_hex(32), 1)
env_path.write_text(text, encoding="utf-8")
print("已自动生成随机 JWT / INTERNAL_TOKEN / OA_WEBHOOK / AES-256-GCM 密钥并写入 .env")
PY
  echo "⚠️  请编辑 .env 填入你的 OPENAI_API_KEY（若暂不填可先改 LLM_MODE=mock 测试全栈）。"
fi

echo "==> [2/4] 先构建本地业务镜像（backend / sandbox / frontend），再启动全栈服务 ..."
docker compose build mock-enterprise sandbox-pool frontend
if [[ "$WITH_LANGFUSE" == "true" ]]; then
  docker compose --profile observability up -d
else
  docker compose up -d
fi

echo "==> [3/4] 向 PostgreSQL 16 灌入幂等仿真多租户测试数据 ..."
docker exec pytravel-api-gateway python scripts/seed_mock_data.py --mode postgres

echo "==> [4/4] 验证核心网关健康状态 ..."
curl -fsS http://127.0.0.1:8000/health
echo ""
echo "🎉 部署完成！"
echo "   - 前端工作台：http://<你的服务器公网IP>/"
echo "   - 后端 API 文档：http://<你的服务器公网IP>:8000/docs"
if [[ "$WITH_LANGFUSE" == "true" ]]; then
  echo "   - Langfuse 面板：http://<你的服务器公网IP>:3000"
fi
