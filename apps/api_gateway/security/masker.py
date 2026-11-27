"""
敏感信息全链路脱敏拦截器。

对应文档章节：
  - 六、核心中间件 1（TenantAuthContextMiddleware）：日志与响应中的手机号、身份证、银行卡、API Key 掩码
  - 六、核心中间件 7（LangfuseObservabilityMiddleware）：上报 Langfuse 前复用本脱敏器，防止 PII 外泄
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

# ----------------------------------------------------------------------
# 预编译脱敏正则（按精确度排序：长证件号/Key 优先于短数字串）
# ----------------------------------------------------------------------
# 18 位中国居民身份证号（前6后4保留，中间8位掩码；用数字负向环视兼容中文紧邻场景）
_ID_CARD_RE = re.compile(r"(?<!\d)([1-9]\d{5})(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(\d{3}[\dXx])(?![\dA-Za-z])")
# 11 位中国大陆手机号（前3后4保留）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
# 16~19 位银行卡号（前4后4保留）
_BANK_CARD_RE = re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)")
# 常见 API Key / Bearer Token / 途牛凭证前缀（兼容中文紧邻场景）
_SECRET_KEY_RE = re.compile(r"(?<![A-Za-z0-9])((?:sk|pk|tn|ak|tok)[-_][A-Za-z0-9]{4})[A-Za-z0-9_-]{6,}(?![A-Za-z0-9_-])")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._-]{12,}", re.IGNORECASE)

# 字段名命中敏感关键词时，直接整体掩码其字符串值
_SENSITIVE_FIELD_NAMES = {
    "api_key",
    "api_secret",
    "app_secret",
    "token",
    "access_token",
    "password",
    "password_hash",
    "id_card",
    "phone",
    "mobile",
    "bank_card",
    "credential",
    "credential_cipher",
    "authorization",
}


class SensitiveDataMasker:
    """统一脱敏器：支持纯文本、嵌套 JSON 字典/列表，以及 Langfuse `mask` 回调。"""

    @staticmethod
    def mask_text(text: str) -> str:
        """对任意字符串中的手机号、身份证、银行卡与 API Key 执行正则掩码。"""
        if not text:
            return text
        masked = _ID_CARD_RE.sub(r"\1********\2", text)
        masked = _PHONE_RE.sub(r"\1****\2", masked)
        masked = _BANK_CARD_RE.sub(r"\1********\2", masked)
        masked = _SECRET_KEY_RE.sub(r"\1****", masked)
        masked = _BEARER_RE.sub(r"\1****", masked)
        return masked

    @classmethod
    def mask_value(cls, value: Any, field_name: str | None = None) -> Any:
        """
        递归脱敏任意 Python 对象（dict / list / tuple / str）。
        若父级字典键名属于 _SENSITIVE_FIELD_NAMES，则对其字符串值做保守截断掩码。
        """
        if field_name and field_name.lower() in _SENSITIVE_FIELD_NAMES:
            if isinstance(value, str) and value:
                text_masked = cls.mask_text(value)
                if text_masked != value:
                    return text_masked
                if len(value) <= 6:
                    return "***"
                return f"{value[:3]}****{value[-4:] if len(value) >= 11 else value[-2:]}"
        if isinstance(value, str):
            return cls.mask_text(value)
        if isinstance(value, dict):
            return {k: cls.mask_value(v, field_name=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [cls.mask_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls.mask_value(item) for item in value)
        return value

    @classmethod
    def langfuse_mask_callback(cls, data: Any) -> Any:
        """
        Langfuse SDK 专用脱敏回调函数（对应 v1.1 P1 #22）。
        先深拷贝再递归脱敏，确保不污染业务内存对象。
        """
        return cls.mask_value(deepcopy(data))
