# ==============================================================================
# PyTravel-Harness 后端全栈服务统一镜像
# 同时供 api-gateway / tuniu-mcp / oa-mcp / erp-expense-mcp / weather-mcp / mock-enterprise 复用
# ==============================================================================
FROM python:3.12-slim

# 构建时默认使用阿里云 PyPI 镜像（海外服务器可通过 --build-arg 覆盖）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}

# 国内阿里云 ECS 加速：将 Debian apt 源切换为阿里云镜像（使用 http 避免 slim 镜像缺少 ca-certificates）
# 安装 docker-cli 以便 api-gateway 通过 docker.sock 调度预热沙箱容器池，以及 curl 用于健康检查
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g; s|https://deb.debian.org|http://mirrors.aliyun.com|g; s|http://security.debian.org|http://mirrors.aliyun.com|g; s|https://security.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g; s|https://deb.debian.org|http://mirrors.aliyun.com|g; s|http://security.debian.org|http://mirrors.aliyun.com|g; s|https://security.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
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
