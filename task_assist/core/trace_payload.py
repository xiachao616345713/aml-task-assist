"""Trace 输入输出的序列化与秘密字段遮盖；不依赖模型或业务包。"""

from __future__ import annotations

import json
import re
from typing import Any

SECRET_KEYS = {"token", "access_token", "x_access_token", "authorization", "password",
               "passwd", "secret", "api_key", "apikey", "client_secret"}


def safe_payload(value: Any, secrets: tuple[str, ...] = (), max_chars: int = 12000) -> Any:
    """保留工具的可审计输入输出，同时限制深度、长度并遮盖已知密钥。"""
    if hasattr(value, "content") and not isinstance(value, (str, bytes, dict, list, tuple)):
        value = value.content
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            value = parsed

    def clean(item: Any, depth: int) -> Any:
        if depth > 6:
            return "[已截断嵌套内容]"
        if isinstance(item, dict):
            return {str(key): ("[已遮盖]" if str(key).lower().replace("-", "_") in SECRET_KEYS else clean(val, depth + 1))
                    for key, val in list(item.items())[:100]}
        if isinstance(item, (list, tuple)):
            return [clean(val, depth + 1) for val in item[:100]]
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        if isinstance(item, bytes):
            return f"[二进制数据 {len(item)} 字节]"
        text = str(item)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[已遮盖]")
        text = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [已遮盖]", text)
        return text[:max_chars] + ("…[已截断]" if len(text) > max_chars else "")

    cleaned = clean(value, 0)
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return cleaned
    return encoded[:max_chars] + "…[已截断]"
