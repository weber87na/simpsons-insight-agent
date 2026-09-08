from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse


def canonical_dcard_url(value: str) -> str:
    """Validate a public article URL before removing known share tracking fields."""
    message = "只允許 https://www.dcard.tw/f/{forum}/p/{id} 公開文章網址"
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError(message) from exc
    match = re.fullmatch(r"/f/([A-Za-z0-9_-]{1,64})/p/([0-9]+)/?", parsed.path)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "www.dcard.tw"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or match is None
    ):
        raise ValueError(message)
    tracking_fields = {"cid", "ref", "referrer", "source", "share", "fbclid", "gclid"}
    if any(
        not key.lower().startswith("utm_") and key.lower() not in tracking_fields
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ValueError("Dcard 公開文章網址只允許分享追蹤參數")
    forum, post_id = match.groups()
    return f"https://www.dcard.tw/f/{forum.lower()}/p/{int(post_id)}"
