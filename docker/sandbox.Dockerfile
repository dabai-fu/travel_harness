# ==============================================================================
# PyTravel-Harness Python 预算精算沙箱镜像
# 对应文档章节：四层隔离之沙箱物理隔离层 / 十一、v1.1 P1 #23（预热容器池）
# ==============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir pandas==2.2.3 openpyxl==3.1.5 \
    && useradd -m -u 10001 sandbox \
    && mkdir -p /workspace /opt \
    && chown -R sandbox:sandbox /workspace

# 将受信任财务精算脚本只读预置到 /opt/budget_calc.py（供 docker exec 预热池执行）
COPY apps/agent_harness/skills/budget-fpa-attribution/scripts/budget_calc.py /opt/budget_calc.py
RUN chmod 444 /opt/budget_calc.py

WORKDIR /workspace
USER sandbox

CMD ["sleep", "infinity"]
