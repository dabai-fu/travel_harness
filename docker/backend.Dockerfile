# ==============================================================================
# PyTravel-Harness 后端全栈服务统一镜像
# 同时供 api-gateway / tuniu-mcp / oa-mcp / erp-expense-mcp / weather-mcp / mock-enterprise 复用
# ==============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 安装 docker-cli 以便 api-gateway 通过 docker.sock 调度预热沙箱容器池，以及 curl 用于健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY apps ./apps
COPY scripts ./scripts
COPY eval ./eval

RUN pip install --no-cache-dir -e . \
    && mkdir -p /workspace

EXPOSE 8000
CMD ["uvicorn", "apps.api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
