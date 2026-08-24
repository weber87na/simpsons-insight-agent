from __future__ import annotations

import re
import unicodedata
from typing import Any

EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?886[-\s]?)?(?:0?9\d{2}|0\d{1,2})[-\s]?\d{3,4}[-\s]?\d{3,4}(?!\d)"
)
HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_.-]{2,}")
LONG_ID_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.replace("\u200b", "").split())


def redact_pii(text: str | None) -> str:
    redacted = normalize_text(text)
    substitutions = (
        (URL_RE, "[網址已遮罩]"),
        (EMAIL_RE, "[電子郵件已遮罩]"),
        (PHONE_RE, "[電話已遮罩]"),
        (HANDLE_RE, "[帳號已遮罩]"),
        (LONG_ID_RE, "[識別碼已遮罩]"),
    )
    for pattern, replacement in substitutions:
        redacted = pattern.sub(replacement, redacted)
    return redacted


_OPENAI_FORBIDDEN_KEYS = {
    "author",
    "author_hash",
    "author_name",
    "source_url",
    "maps_url",
    "address",
    "board",
    "forum",
    "business",
    "subject",
    "aliases",
}


def sanitize_for_openai(value: Any) -> Any:
    """Deep-copy an OpenAI payload while removing local identity and source locators."""

    if isinstance(value, dict):
        return {
            key: sanitize_for_openai(item)
            for key, item in value.items()
            if key not in _OPENAI_FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [sanitize_for_openai(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_openai(item) for item in value]
    if isinstance(value, str):
        return redact_pii(value)
    return value
