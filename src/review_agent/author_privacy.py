from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from .config import Settings, get_settings
from .privacy import normalize_text


class AuthorHasher:
    """Produces stable local pseudonyms without retaining source usernames."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._key: bytes | None = None

    def hash(self, source: str, author: str | None) -> str | None:
        normalized = normalize_text(author)
        if not normalized:
            return None
        return hmac.new(
            self._load_key(),
            f"{source}\0{normalized}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        path = Path(self.settings.author_hash_key_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, os.urandom(32))
            finally:
                os.close(descriptor)
        key = path.read_bytes()
        if len(key) < 32:
            raise RuntimeError("作者匿名金鑰格式錯誤")
        self._key = key
        return key
