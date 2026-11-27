"""
密码学与防篡改哈希工具模块。

对应文档章节：
  - 2.2 四层隔离之外部凭证隔离层（AES-256-GCM 加密 + key_version 轮换）
  - 6 章 Guardrails（action_hash 确认即绑定，防 TOCTOU 篡改）
  - 7.1 途牛防重复下单幂等键（SHA256 + '|' 分隔符 + segment_no）
  - 7.2 OA Webhook 回调（HMAC-SHA256 签名与防时序侧信道比对）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apps.common.config import get_settings


def encrypt_aes_gcm(plaintext: str, key_version: int | None = None) -> tuple[str, int]:
    """
    使用 AES-256-GCM 加密敏感明文（如途牛 API Key、员工手机号/身份证）。

    返回格式：("v{version}:{nonce_b64}:{ciphertext_b64}", version)
    设计亮点：
      - 每次加密生成 12 字节密码学随机 Nonce，同一明文多次加密密文完全不同；
      - 密文自带 key_version 前缀，后续主密钥轮换时无需停机即可按版本解密并重加密。
    """
    settings = get_settings()
    ver, key = settings.get_aes_key(key_version)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=f"v{ver}".encode())
    nonce_b64 = base64.urlsafe_b64encode(nonce).decode("ascii")
    ct_b64 = base64.urlsafe_b64encode(ct).decode("ascii")
    return f"v{ver}:{nonce_b64}:{ct_b64}", ver


def decrypt_aes_gcm(cipher_text: str) -> str:
    """
    解密 encrypt_aes_gcm 生成的带版本前缀密文。
    自动从密文前缀解析 key_version，防止密钥轮换期间旧密文无法读取。
    """
    parts = cipher_text.split(":", maxsplit=2)
    if len(parts) != 3 or not parts[0].startswith("v"):
        raise ValueError("非法 AES-GCM 密文格式，必须为 v{ver}:{nonce}:{ct}")

    ver = int(parts[0][1:])
    nonce = base64.urlsafe_b64decode(parts[1].encode("ascii"))
    ct = base64.urlsafe_b64decode(parts[2].encode("ascii"))

    settings = get_settings()
    _, key = settings.get_aes_key(ver)
    aesgcm = AESGCM(key)
    plain_bytes = aesgcm.decrypt(nonce, ct, associated_data=f"v{ver}".encode())
    return plain_bytes.decode("utf-8")


def canonical_json(data: Any) -> str:
    """
    规范化 JSON 序列化（按 key 排序、无多余空格、保留中文）。
    用于保证同一字典无论键插入顺序如何，都能算出完全一致的 SHA-256 哈希。
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def make_booking_idempotent_key(
    tenant_id: str,
    travel_order_id: int | str,
    option_hash: str,
    segment_no: int,
) -> str:
    """
    生成途牛分段预订唯一幂等键（落实 v1.1 P0 #1）。

    公式：SHA256("{tenant_id}|{travel_order_id}|{option_hash}|{segment_no}")
    关键坑点修复：
      1. 必须使用 '|' 分隔符，避免无分隔符拼接产生 ("ab"+"c" == "a"+"bc") 的哈希碰撞；
      2. 一个往返方案会拆分为去程(1)、酒店(2)、返程(3)多条 booking_record，
         必须引入 segment_no，否则第二段写入会直接触发 idempotent_key 唯一索引冲突！
    """
    raw = f"{tenant_id}|{travel_order_id}|{option_hash}|{segment_no}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_action_hash(tool_name: str, args: dict[str, Any]) -> str:
    """
    生成 WRITE 高危工具“确认即绑定”哈希（落实 v1.1 P0 #4，防止 TOCTOU 篡改）。

    公式：SHA256("{tool_name}|{canonical_json(args)}")
    流程：
      interrupt() 推送表单时生成 action_hash -> 用户点击确认 -> /resume 校验一致 ->
      独立后继节点真正调 WRITE 工具前再次计算实际参数哈希比对，模型无权悄悄改动金额或酒店。
    """
    raw = f"{tool_name}|{canonical_json(args)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sign_hmac_sha256(payload: bytes | str, secret: str) -> str:
    """计算 HMAC-SHA256 十六进制签名（用于 OA Webhook 与内部令牌）。"""
    msg = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_hmac_sha256(payload: bytes | str, signature: str, secret: str) -> bool:
    """使用 hmac.compare_digest 常量时间校验签名，防止时序攻击。"""
    expected = sign_hmac_sha256(payload, secret)
    return hmac.compare_digest(expected, signature.strip().lower())


def hash_password(raw_password: str, iterations: int = 260_000) -> str:
    """
    生产级口令哈希（PBKDF2-HMAC-SHA256 + 16 字节密码学随机盐）。
    存储格式：`pbkdf2_sha256${iterations}${salt_b64}${digest_b64}`
    """
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw_password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    dk_b64 = base64.urlsafe_b64encode(dk).decode("ascii")
    return f"pbkdf2_sha256${iterations}${salt_b64}${dk_b64}"


def verify_password(raw_password: str, stored_hash: str) -> bool:
    """
    常量时间口令验证（支持 PBKDF2-HMAC-SHA256 与历史静态加盐格式向下兼容）。
    """
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iter_str, salt_b64, dk_b64 = stored_hash.split("$", maxsplit=3)
            salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
            expected_dk = base64.urlsafe_b64decode(dk_b64.encode("ascii"))
            got_dk = hashlib.pbkdf2_hmac("sha256", raw_password.encode("utf-8"), salt, int(iter_str))
            return hmac.compare_digest(got_dk, expected_dk)
        except Exception:
            return False
    # 兼容旧演示哈希格式
    legacy = hashlib.sha256(f"pytravel_salt:{raw_password}".encode()).hexdigest()
    return hmac.compare_digest(legacy, stored_hash)
