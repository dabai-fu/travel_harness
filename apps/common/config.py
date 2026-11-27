"""
全局配置模块（Pydantic v2 Settings）。

对应文档章节：九、技术栈清单与环境变量配置 (.env.example)
设计说明：
  通过开关变量（APP_STORAGE_MODE / REDIS_MODE / SANDBOX_MODE / LLM_MODE / TUNIU_MODE）
  实现同一套代码在“零外部依赖本地仿真模式”与“全套生产中间件（PG16+Redis7+Docker+LLM）”
  之间无缝切换，既满足面试/本地调试开箱即用，又具备企业级交付能力。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """系统全局配置对象，自动从环境变量及项目根目录 .env 文件加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # 1. 运行模式开关
    # ------------------------------------------------------------------
    app_storage_mode: Literal["memory", "postgres"] = Field(
        default="memory",
        description="持久化模式：memory 为零依赖内存仿真库；postgres 为 PG16+pgvector+RLS",
    )
    redis_mode: Literal["memory", "redis"] = Field(
        default="memory",
        description="缓存与流控模式：memory 为协程安全内存模拟；redis 为真实 Redis 7",
    )
    sandbox_mode: Literal["local", "docker"] = Field(
        default="local",
        description="沙箱模式：local 为隔离目录子进程；docker 为 Docker SDK 容器池",
    )
    llm_mode: Literal["mock", "openai"] = Field(
        default="mock",
        description="模型调用模式：mock 为确定性本地响应；openai 为通义千问/DeepSeek 兼容接口",
    )
    tuniu_mode: Literal["mock", "real"] = Field(
        default="mock",
        description="途牛商旅接入模式：mock 为确定性仿真运价；real 为真实途牛开放接口/CLI",
    )
    mcp_mode: Literal["inprocess", "http"] = Field(
        default="inprocess",
        description="MCP 调用模式：inprocess 为进程内直调（单测/单机）；http 为独立 FastMCP Streamable HTTP 容器调用",
    )

    # ------------------------------------------------------------------
    # 2. PostgreSQL 与 Redis 连接配置
    # ------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://pytravel_app:pytravel_app_pass@localhost:5432/pytravel"
    database_admin_url: str = "postgresql+asyncpg://pytravel:pytravel_pass@localhost:5432/pytravel"
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""

    # ------------------------------------------------------------------
    # 3. 安全密钥与令牌配置（对应第 2.2 / 6 / 7.2 / 8.1 节）
    # ------------------------------------------------------------------
    jwt_secret: str = "pytravel-dev-jwt-secret-change-in-prod-32bytes"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 天滑动过期，与 Redis Session 对齐

    # Harness 调用独立 MCP Server 时签发的 60s 短时内部令牌密钥（Header: X-PyTravel-Ctx）
    internal_token_secret: str = "pytravel-internal-mcp-token-secret-32bytes"
    internal_token_ttl_seconds: int = 60

    # OA 审批异步回调 Webhook 的 HMAC-SHA256 验签密钥
    oa_webhook_secret: str = "pytravel-oa-webhook-hmac-secret-32bytes"
    oa_webhook_max_skew_seconds: int = 300  # 允许 ±5 分钟时间戳窗口

    # AES-256-GCM 主密钥（Base64 编码的 32 字节密钥，支持 key_version 轮换）
    aes_master_key_v1: str = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    aes_active_key_version: int = 1

    # ------------------------------------------------------------------
    # 4. 大模型与视觉模型配置
    # ------------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_main_model: str = "qwen-max"
    llm_fast_model: str = "qwen-turbo"
    llm_vision_model: str = "qwen-vl-max"
    embedding_model: str = "text-embedding-v3"
    request_token_budget: int = 24000  # 单次请求 Token 硬止损阈值（Middleware 4）

    # ------------------------------------------------------------------
    # 5. 途牛、独立 MCP 服务地址与模拟企业服务地址
    # ------------------------------------------------------------------
    tuniu_api_base_url: str = "https://api.tuniu.com/corporate/v1"
    mock_ent_base_url: str = "http://localhost:8010"
    tuniu_mcp_url: str = "http://localhost:8021/mcp"
    oa_mcp_url: str = "http://localhost:8022/mcp"
    erp_mcp_url: str = "http://localhost:8023/mcp"
    weather_mcp_url: str = "http://localhost:8024/mcp"

    # ------------------------------------------------------------------
    # 6. 沙箱隔离工作区、命名卷与预热容器池配置
    # ------------------------------------------------------------------
    sandbox_workspace_root: str = "./.sandbox_workspaces"
    sandbox_docker_image: str = "pytravel-sandbox:py312"
    sandbox_docker_volume: str = ""  # 生产 Compose 挂载的命名卷名，解决容器内套容器路径错位
    sandbox_warm_container: str = ""  # 预热常驻沙箱容器名，通过 docker exec 零冷启动执行
    sandbox_timeout_seconds: int = 60

    # ------------------------------------------------------------------
    # 7. Langfuse 可观测性（对应 Middleware 7）
    # ------------------------------------------------------------------
    langfuse_enabled: bool = False
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = "pk-lf-dev"
    langfuse_secret_key: str = "sk-lf-dev"

    @property
    def sandbox_root_path(self) -> Path:
        """返回规范化后的沙箱根目录绝对路径，并确保目录存在。"""
        root = Path(self.sandbox_workspace_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def get_aes_key(self, version: int | None = None) -> tuple[int, bytes]:
        """
        根据密钥版本号获取 32 字节 AES-256-GCM 密钥。
        支持未来扩展 v2/v3 密钥用于平滑轮换（对应 v1.1 P1 #21）。
        """
        import base64
        import hashlib

        ver = version or self.aes_active_key_version
        raw_b64 = self.aes_master_key_v1 if ver == 1 else f"{self.aes_master_key_v1}:v{ver}"
        try:
            decoded = base64.b64decode(raw_b64)
            if len(decoded) == 32:
                return ver, decoded
        except Exception:
            pass
        # 兜底通过 SHA-256 派生固定 32 字节密钥，避免环境变量格式轻微偏差导致启动失败
        return ver, hashlib.sha256(raw_b64.encode("utf-8")).digest()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例缓存读取全局配置。测试中修改环境变量后可调用 get_settings.cache_clear()。"""
    return Settings()
