"""Small deterministic redaction helpers for persisted extraction values."""
from __future__ import annotations

import math
import re
import urllib.parse

_SECRET_LABEL = "[REDACTED:secret-key]"
_SECRET_NAMES = {"password", "passwd", "secret", "token", "apikey", "accesstoken", "accesskey", "clientsecret", "privatekey"}
_SECRET_SUFFIXES = ("password", "passwd", "secret", "token", "apikey", "accesskey", "privatekey")
_CREDENTIAL_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+@[^\s]+", re.I)
_AUTHORIZATION = re.compile(r"(?im)^(\s*(?:proxy-)?authorization\s*:\s*)\S[^\r\n]*")
_PRIVATE_KEY = re.compile(r"(?m)^-----BEGIN [^\r\n]* PRIVATE KEY-----[\s\S]*?(?:^-----END [^\r\n]* PRIVATE KEY-----[^\r\n]*|\Z)")
_KEY_ASSIGNMENT = re.compile(r"(?im)([\"']?([A-Za-z0-9_. -]+)[\"']?\s*[:=]\s*)([^,}\]\r\n]+)")
_TOKEN = re.compile(r"(?<![A-Za-z0-9_+/=-])([A-Za-z0-9_+/=-]{24,256})(?![A-Za-z0-9_+/=-])")


def is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[-_. ]", "", key.casefold())
    return normalized in _SECRET_NAMES or normalized.endswith(_SECRET_SUFFIXES)


def _high_entropy(value: str) -> bool:
    if not value.isascii() or not re.fullmatch(r"[A-Za-z0-9_+/=-]{24,256}", value):
        return False
    groups = sum(bool(re.search(pattern, value)) for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[_+/=-]"))
    if groups < 3:
        return False
    counts = {char: value.count(char) for char in set(value)}
    entropy = -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())
    return entropy >= 3.5


def redact_value(key: object, value: object) -> object:
    """Redact typed values under secret-like keys, otherwise redact string material."""
    if is_secret_key(key):
        return _SECRET_LABEL
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(None, item) for item in value]
    if isinstance(value, dict):
        return {name: redact_value(name, item) for name, item in value.items()}
    return value


def redact_text(value: str) -> str:
    value = _PRIVATE_KEY.sub("[REDACTED:private-key]", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED:authorization]", value)
    value = _CREDENTIAL_URL.sub(lambda match: _redact_url(match.group(0)), value)
    value = _KEY_ASSIGNMENT.sub(
        lambda match: match.group(0)
        if not is_secret_key(match.group(2)) or match.group(3).strip().startswith("[REDACTED:")
        else match.group(1) + _SECRET_LABEL,
        value,
    )
    return _TOKEN.sub(lambda match: "[REDACTED:high-entropy]" if _high_entropy(match.group(1)) else match.group(1), value)


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, f"[REDACTED:credential-url]@{host}", parsed.path, parsed.query, parsed.fragment))
