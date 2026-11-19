from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit


class Fault(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self):
        return {"code": self.code, "message": self.message}


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    raw = value if isinstance(value, bytes) else compact(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def timestamp(value: str) -> int:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("缺少时区")
        return int(moment.timestamp())
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise Fault("invalid_time", "时间必须包含时区且符合ISO8601", 422) from exc


def iso(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def bounded_int(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Fault("invalid_number", f"{name}范围应为{minimum}至{maximum}", 422)
    return value


def text(value, name: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Fault("invalid_text", f"{name}为空或超过{limit}字符", 422)
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise Fault("invalid_text", f"{name}含控制字符", 422)
    return value.strip()


def string_set(value, name: str, allowed=None) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise Fault("invalid_list", f"{name}必须是非空且不超过100项的列表", 422)
    result = sorted({text(item, name, 80) for item in value})
    if allowed and not set(result) <= set(allowed):
        raise Fault("invalid_choice", f"{name}有不支持的值", 422)
    return result


FACILITIES = {"road", "power", "water", "heat", "telecom"}
LEVELS = {"info": 0, "yellow": 1, "orange": 2, "red": 3}


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(text(url, "来源地址", 2048))
        port = parts.port
    except ValueError as exc:
        raise Fault("invalid_url", "来源地址无效", 422) from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise Fault("invalid_url", "仅支持HTTP或HTTPS地址", 422)
    if parts.username or parts.password or parts.fragment:
        raise Fault("invalid_url", "地址不得含凭据或片段", 422)
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    default = 80 if parts.scheme == "http" else 443
    authority = host + (f":{port}" if port and port != default else "")
    return urlunsplit((parts.scheme.lower(), authority, parts.path or "/", parts.query, ""))


def same_origin(base: str, link: str) -> str:
    candidate = canonical_url(urljoin(base, link))
    if urlsplit(candidate)[:2] != urlsplit(base)[:2]:
        raise Fault("foreign_link", "分页或重定向越出已授权来源")
    return candidate


def require_fields(value: dict, allowed: set[str], required: set[str]):
    if not isinstance(value, dict):
        raise Fault("invalid_object", "请求内容必须是对象", 422)
    if set(value) - allowed:
        raise Fault("unknown_field", "不支持的字段：" + ",".join(sorted(set(value) - allowed)), 422)
    if required - set(value):
        raise Fault("missing_field", "缺少字段：" + ",".join(sorted(required - set(value))), 422)
