from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import quote, urljoin, urlparse

import httpx

from .author_privacy import AuthorHasher
from .config import Settings, get_settings
from .privacy import normalize_text
from .schemas import ContentType, SourceKind
from .sources import (
    CollectedItem,
    SourceBlockedError,
    SourceCallbacks,
    SourceCanceledError,
    SourceCheckpoint,
    SourceCollectionResult,
)

_PTT_BASE = "https://www.ptt.cc"
_DCARD_HOST = "www.dcard.tw"
_DCARD_PATH = re.compile(r"/f/(?P<forum>[A-Za-z0-9_-]+)/p/(?P<id>\d+)/?")


def _hash(*parts: object) -> str:
    rendered = "\0".join(normalize_text(str(part)) if part is not None else "" for part in parts)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _is_new(item: CollectedItem, known_keys: set[str]) -> bool:
    return item.source_item_id not in known_keys and item.content_hash not in known_keys


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (TypeError, ValueError):
        return None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, normalize_text("".join(self._text))))
            self._href = None
            self._text = []


def parse_ptt_search(html: str, board: str) -> tuple[list[str], str | None]:
    parser = _LinkParser()
    parser.feed(html)
    article_pattern = re.compile(rf"^/bbs/{re.escape(board)}/M\.[^/]+\.html$")
    articles: list[str] = []
    previous: str | None = None
    for href, text in parser.links:
        if article_pattern.fullmatch(href) and href not in articles:
            articles.append(href)
        if "上頁" in text and href.startswith(f"/bbs/{board}/"):
            previous = href
    return articles, previous


class _PttArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.meta_values: list[str] = []
        self.body: list[str] = []
        self.pushes: list[dict[str, str]] = []
        self._meta: list[str] | None = None
        self._push: dict[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        role: str | None = None
        if tag == "div" and values.get("id") == "main-content":
            role = "main"
        elif self._in("main") and tag == "div" and "push" in classes:
            role = "push"
            self._push = {"tag": [], "author": [], "text": [], "time": []}
        elif self._in("main") and classes.intersection(
            {"article-metaline", "article-metaline-right"}
        ):
            role = "ignore"
        elif self._in("main") and "article-meta-value" in classes:
            role = "meta"
            self._meta = []
        elif self._in("push") and "push-tag" in classes:
            role = "push_tag"
        elif self._in("push") and "push-userid" in classes:
            role = "push_author"
        elif self._in("push") and "push-content" in classes:
            role = "push_text"
        elif self._in("push") and "push-ipdatetime" in classes:
            role = "push_time"
        self.stack.append((tag, role))
        if tag == "br" and self._in("main") and not self._in("push"):
            self.body.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._push is not None:
            role = self._nearest_role()
            mapping = {
                "push_tag": "tag",
                "push_author": "author",
                "push_text": "text",
                "push_time": "time",
            }
            if role in mapping:
                self._push[mapping[role]].append(data)
            return
        if self._meta is not None and self._nearest_role() == "meta":
            self._meta.append(data)
            return
        if self._in("main") and not self._in("ignore"):
            self.body.append(data)

    def handle_endtag(self, tag: str) -> None:
        index = next(
            (i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag),
            None,
        )
        if index is None:
            return
        _tag, role = self.stack[index]
        del self.stack[index:]
        if role == "meta" and self._meta is not None:
            self.meta_values.append(normalize_text("".join(self._meta)))
            self._meta = None
        elif role == "push" and self._push is not None:
            self.pushes.append(
                {key: normalize_text("".join(value)) for key, value in self._push.items()}
            )
            self._push = None

    def _in(self, role: str) -> bool:
        return any(current == role for _tag, current in self.stack)

    def _nearest_role(self) -> str | None:
        return next((role for _tag, role in reversed(self.stack) if role), None)


def _clean_ptt_body(value: str) -> str:
    lines: list[str] = []
    for raw in value.replace("\r", "").splitlines():
        line = raw.strip()
        if line == "--":
            break
        if not line or line.startswith((">", "※ 發信站", "※ 文章網址", "※ 編輯")):
            continue
        lines.append(line)
    return normalize_text("\n".join(lines))


def _ptt_push_datetime(value: str, article_date: datetime | None) -> datetime | None:
    match = re.search(r"(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})", value)
    if not match or article_date is None:
        return None
    values = {key: int(number) for key, number in match.groupdict().items()}
    candidates = [
        datetime(article_date.year + offset, tzinfo=UTC, **values) for offset in (0, 1)
    ]
    now = datetime.now(UTC) + timedelta(days=1)
    return min(
        (item for item in candidates if item >= article_date - timedelta(days=1) and item <= now),
        default=candidates[0],
    )


def parse_ptt_article(
    html: str,
    source_url: str,
    hasher: AuthorHasher,
) -> list[CollectedItem]:
    parser = _PttArticleParser()
    parser.feed(html)
    values = parser.meta_values + ["", "", "", ""]
    raw_author, board, title, raw_date = values[:4]
    author = raw_author.split(" ", 1)[0]
    published_at = _parse_datetime(raw_date)
    body = _clean_ptt_body("".join(parser.body))
    match = re.search(r"/bbs/[^/]+/(?P<id>M\.[^/]+)\.html", source_url)
    if not match or not body:
        return []
    thread_id = match.group("id")
    post = CollectedItem(
        source="ptt",
        content_type="post",
        source_item_id=thread_id,
        thread_source_id=thread_id,
        content_hash=_hash("ptt", thread_id, body),
        author_hash=hasher.hash("ptt", author),
        title=title or None,
        board=board or None,
        text=body,
        published_at=published_at,
        date_precision="day" if published_at else "unknown",
        source_url=source_url,
    )
    items = [post]
    for index, push in enumerate(parser.pushes, start=1):
        text = push["text"].lstrip(":： ")
        if not text or text in {"(本文已被刪除)", "[deleted]"}:
            continue
        signal = {"推": "push", "噓": "boo", "→": "neutral"}.get(
            push["tag"].strip(), "neutral"
        )
        comment_date = _ptt_push_datetime(push["time"], published_at)
        author_hash = hasher.hash("ptt", push["author"])
        source_id = _hash(
            "ptt-comment", thread_id, index, signal, push["time"], text
        )
        items.append(
            CollectedItem(
                source="ptt",
                content_type="comment",
                source_item_id=source_id,
                thread_source_id=thread_id,
                parent_source_id=thread_id,
                content_hash=_hash("ptt", source_id, text),
                author_hash=author_hash,
                title=title or None,
                board=board or None,
                text=text,
                relative_date=push["time"] or None,
                published_at=comment_date,
                date_precision="day" if comment_date else "unknown",
                source_url=source_url,
                platform_data={"signal": signal, "floor": index},
            )
        )
    return items


class _DcardDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.scripts: list[tuple[str, str]] = []
        self._script_type: str | None = None
        self._script: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta[key] = content
        elif tag == "script":
            self._script_type = values.get("type") or ""
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script_type is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_type is not None:
            self.scripts.append((self._script_type, "".join(self._script)))
            self._script_type = None
            self._script = []


def _walk_json(value: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for item in value.values():
            found.extend(_walk_json(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_json(item))
    return found


def _record_text(record: dict[str, Any]) -> str:
    for key in ("articleBody", "content", "text", "description", "excerpt"):
        value = record.get(key)
        if isinstance(value, str) and normalize_text(value):
            return normalize_text(value)
    return ""


def _author_value(record: dict[str, Any]) -> str | None:
    value = record.get("author")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("id", "name", "nickname"):
            if value.get(key):
                return str(value[key])
    for key in ("school", "department", "anonymousSchool"):
        if record.get(key):
            return str(record[key])
    return None


def parse_dcard_article(
    html: str,
    source_url: str,
    hasher: AuthorHasher,
) -> tuple[list[CollectedItem], bool]:
    url_match = _DCARD_PATH.fullmatch(urlparse(source_url).path)
    if not url_match:
        return [], False
    forum = url_match.group("forum")
    post_id = url_match.group("id")
    parser = _DcardDocumentParser()
    parser.feed(html)
    records: list[dict[str, Any]] = []
    for _script_type, raw in parser.scripts:
        stripped = raw.strip()
        if not stripped or stripped[0] not in "[{":
            continue
        try:
            records.extend(_walk_json(json.loads(stripped)))
        except json.JSONDecodeError:
            continue

    article_record: dict[str, Any] | None = None
    for record in records:
        identifier = str(record.get("id") or record.get("postId") or record.get("identifier") or "")
        if identifier == post_id and _record_text(record):
            article_record = record
            break
        if record.get("headline") and _record_text(record):
            article_record = article_record or record

    text = _record_text(article_record or {}) or normalize_text(
        parser.meta.get("og:description") or parser.meta.get("description")
    )
    raw_title = (
        (article_record or {}).get("title")
        or (article_record or {}).get("headline")
        or parser.meta.get("og:title")
    )
    title = normalize_text(str(raw_title)) if raw_title else ""
    if not text:
        return [], False
    published_at = _parse_datetime(
        (article_record or {}).get("createdAt")
        or (article_record or {}).get("datePublished")
        or parser.meta.get("article:published_time")
    )
    items = [
        CollectedItem(
            source="dcard",
            content_type="post",
            source_item_id=post_id,
            thread_source_id=post_id,
            content_hash=_hash("dcard", post_id, text),
            author_hash=hasher.hash("dcard", _author_value(article_record or {})),
            title=title or None,
            board=forum,
            text=text,
            published_at=published_at,
            date_precision="day" if published_at else "unknown",
            source_url=source_url,
            platform_data={
                "reaction_count": int((article_record or {}).get("likeCount") or 0),
            },
        )
    ]

    seen_comments: set[str] = set()
    declared_comment_count = 0
    for record in records:
        declared_comment_count = max(
            declared_comment_count,
            int(record.get("commentCount") or 0) if str(record.get("commentCount") or "").isdigit() else 0,
        )
        is_comment = "floor" in record or "commentId" in record or record.get("postId") == post_id
        comment_text = _record_text(record)
        if not is_comment or not comment_text or record is article_record:
            continue
        raw_id = record.get("id") or record.get("commentId")
        floor = record.get("floor")
        comment_id = str(raw_id or _hash("dcard-comment", post_id, floor, comment_text))
        if comment_id == post_id or comment_id in seen_comments:
            continue
        seen_comments.add(comment_id)
        comment_date = _parse_datetime(record.get("createdAt") or record.get("dateCreated"))
        items.append(
            CollectedItem(
                source="dcard",
                content_type="comment",
                source_item_id=comment_id,
                thread_source_id=post_id,
                parent_source_id=str(record.get("parentId") or post_id),
                content_hash=_hash("dcard", comment_id, comment_text),
                author_hash=hasher.hash("dcard", _author_value(record)),
                title=title or None,
                board=forum,
                text=comment_text,
                published_at=comment_date,
                date_precision="day" if comment_date else "unknown",
                source_url=source_url,
                platform_data={
                    "floor": floor,
                    "reaction_count": int(record.get("likeCount") or 0),
                },
            )
        )
    incomplete_marker = any(
        marker in html for marker in ("View more comments", "查看更多留言", "載入更多留言")
    )
    complete = not incomplete_marker and (
        declared_comment_count == 0 or len(seen_comments) >= declared_comment_count
    )
    return items, complete


def dcard_import_item(record: dict[str, Any], hasher: AuthorHasher) -> CollectedItem:
    source_url = str(record["source_url"])
    path_match = _DCARD_PATH.fullmatch(urlparse(source_url).path)
    if not path_match:
        raise ValueError("Dcard 匯入資料包含不允許的 URL")
    content_type = cast(ContentType, str(record["item_type"]))
    text = normalize_text(record["text"])
    thread_id = str(record.get("thread_id") or path_match.group("id"))
    source_item_id = str(
        record.get("source_item_id")
        or _hash("dcard-import", source_url, content_type, record.get("published_at"), text)
    )
    published_at = _parse_datetime(record.get("published_at"))
    return CollectedItem(
        source="dcard",
        content_type=content_type,
        source_item_id=source_item_id,
        thread_source_id=thread_id,
        parent_source_id=str(record.get("parent_id") or thread_id)
        if content_type == "comment"
        else None,
        content_hash=_hash("dcard", source_item_id, text),
        author_hash=str(record.get("author_hash")) if record.get("author_hash") else None,
        title=normalize_text(str(record.get("title"))) if record.get("title") else None,
        board=(normalize_text(str(record.get("forum"))) if record.get("forum") else path_match.group("forum")),
        text=text,
        published_at=published_at,
        date_precision="day" if published_at else "unknown",
        source_url=source_url,
        platform_data={"reaction_count": int(record.get("reaction_count") or 0)},
    )


@dataclass(slots=True)
class _HttpFetcher:
    settings: Settings
    interval: float
    allowed_hosts: set[str]
    client: httpx.AsyncClient | None = None
    _last_request: float = 0.0

    async def get(self, url: str) -> str:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.port not in {None, 443}
            or (parsed.hostname or "").lower() not in self.allowed_hosts
        ):
            raise SourceBlockedError("來源網址不在允許清單")
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=self.settings.source_http_timeout_seconds,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"User-Agent": "LocalReviewResearch/0.2 (+single-user; respectful-rate-limit)"},
        )
        try:
            current = url
            redirects = 0
            rate_limit_retried = False
            while redirects <= 2:
                delay = self.interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                response = await client.get(current)
                self._last_request = time.monotonic()
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceBlockedError("來源重新導向缺少目標")
                    target = urljoin(current, location)
                    target_parts = urlparse(target)
                    target_host = (target_parts.hostname or "").lower()
                    if (
                        target_parts.scheme != "https"
                        or target_parts.port not in {None, 443}
                        or target_host not in self.allowed_hosts
                    ):
                        raise SourceBlockedError("來源重新導向至未允許網域")
                    if target_parts.path.startswith("/ask/over18"):
                        raise SourceBlockedError("PTT 限制級看板不在蒐集範圍")
                    current = target
                    redirects += 1
                    continue
                if response.status_code == 429 and not rate_limit_retried:
                    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                    if retry_after > 60:
                        raise SourceBlockedError("來源要求等待超過 60 秒，已停止本次蒐集")
                    await asyncio.sleep(retry_after)
                    rate_limit_retried = True
                    continue
                if response.status_code in {401, 403, 429}:
                    raise SourceBlockedError(f"來源拒絕請求（HTTP {response.status_code}）")
                response.raise_for_status()
                body = response.text
                lowered = body.lower()
                if any(
                    marker in lowered
                    for marker in (
                        "access denied",
                        "service temporarily unavailable",
                        "just a moment...",
                        "verify you are human",
                        "系統負荷過重",
                    )
                ):
                    raise SourceBlockedError("來源回傳阻擋頁，已停止本次蒐集")
                return body
            raise SourceBlockedError("來源重新導向次數過多")
        except httpx.HTTPError as exc:
            raise SourceBlockedError(f"來源連線失敗：{type(exc).__name__}") from exc
        finally:
            if owns_client:
                await client.aclose()


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 1.0
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 1.0
        target = target.replace(tzinfo=target.tzinfo or UTC).astimezone(UTC)
        return max((target - datetime.now(UTC)).total_seconds(), 0.0)


class PttSource:
    source: SourceKind = "ptt"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.hasher = AuthorHasher(self.settings)
        self.fetcher = _HttpFetcher(
            self.settings,
            self.settings.ptt_request_interval_seconds,
            {"www.ptt.cc", "ptt.cc"},
            client,
        )

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult:
        known_keys = checkpoint.known_keys
        date_from = date.fromisoformat(config["date_from"])
        date_to = date.fromisoformat(config["date_to"])
        max_posts = int(config["max_posts"])
        max_comments = int(config["max_comments"])
        per_thread = int(config["max_comments_per_thread"])
        posts = checkpoint.post_count
        comments = checkpoint.comment_count
        seen_urls: set[str] = set()
        exhausted = True

        def limits_reached() -> bool:
            return posts >= max_posts and comments >= max_comments

        for board in config["boards"]:
            for keyword in config["keywords"]:
                next_url: str | None = f"{_PTT_BASE}/bbs/{board}/search?q={quote(keyword)}"
                for _page in range(20):
                    if not next_url:
                        break
                    if limits_reached():
                        exhausted = False
                        break
                    if await callbacks.is_canceled():
                        raise SourceCanceledError("任務已取消")
                    html = await self.fetcher.get(next_url)
                    links, previous = parse_ptt_search(html, board)
                    if not links:
                        break
                    for href in links:
                        canonical = urljoin(_PTT_BASE, href)
                        if canonical in seen_urls:
                            continue
                        seen_urls.add(canonical)
                        article_html = await self.fetcher.get(canonical)
                        items = parse_ptt_article(article_html, canonical, self.hasher)
                        if not items:
                            continue
                        post = items[0]
                        if post.published_at:
                            published = post.published_at.date()
                            if published > date_to:
                                continue
                            if published < date_from:
                                continue
                        post_is_new = _is_new(post, known_keys)
                        if post_is_new and posts >= max_posts:
                            continue
                        new_posts = [post] if post_is_new else []
                        eligible_comments = items[1 : 1 + per_thread]
                        new_comments = [
                            item for item in eligible_comments if _is_new(item, known_keys)
                        ][: max(max_comments - comments, 0)]
                        new_items = [*new_posts, *new_comments]
                        if new_items:
                            await callbacks.on_batch(new_items)
                            known_keys.update(item.source_item_id for item in new_items)
                            known_keys.update(item.content_hash for item in new_items)
                        posts += len(new_posts)
                        comments += len(new_comments)
                        await callbacks.on_progress(
                            posts,
                            comments,
                            max_posts + max_comments,
                            f"PTT 已讀取 {posts} 篇文章、{comments} 則推文",
                        )
                        if limits_reached():
                            exhausted = False
                            break
                    next_url = urljoin(_PTT_BASE, previous) if previous else None
                else:
                    if next_url:
                        exhausted = False

        reached = limits_reached()
        return SourceCollectionResult(
            source="ptt",
            collected_count=posts + comments,
            post_count=posts,
            comment_count=comments,
            complete=exhausted or reached,
            stop_reason=(
                "target_reached"
                if reached
                else ("search_exhausted" if exhausted else "page_limit")
            ),
            checkpoint={"seen_urls": len(seen_urls)},
        )


class DcardSource:
    source: SourceKind = "dcard"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.hasher = AuthorHasher(self.settings)
        self.fetcher = _HttpFetcher(
            self.settings,
            self.settings.dcard_request_interval_seconds,
            {_DCARD_HOST},
            client,
        )

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult:
        known_keys = checkpoint.known_keys
        date_from = date.fromisoformat(config["date_from"])
        date_to = date.fromisoformat(config["date_to"])
        max_posts = int(config["max_posts"])
        max_comments = int(config["max_comments"])
        per_thread = int(config["max_comments_per_thread"])
        posts = checkpoint.post_count
        comments = checkpoint.comment_count
        complete = True

        def limits_reached() -> bool:
            return posts >= max_posts and comments >= max_comments

        async def process_group(items: list[CollectedItem]) -> None:
            nonlocal posts, comments
            posts_in_group = [item for item in items if item.content_type == "post"]
            thread_date = posts_in_group[0].published_at if posts_in_group else None
            if thread_date and not (date_from <= thread_date.date() <= date_to):
                return
            post = posts_in_group[0] if posts_in_group else None
            post_is_new = bool(post and _is_new(post, known_keys))
            if post_is_new and posts >= max_posts:
                return
            new_posts = [post] if post is not None and post_is_new else []
            eligible_comments = [
                item for item in items if item.content_type == "comment"
            ][:per_thread]
            new_comments = [
                item for item in eligible_comments if _is_new(item, known_keys)
            ][: max(max_comments - comments, 0)]
            new_items = [*new_posts, *new_comments]
            if new_items:
                await callbacks.on_batch(new_items)
                known_keys.update(item.source_item_id for item in new_items)
                known_keys.update(item.content_hash for item in new_items)
            posts += len(new_posts)
            comments += len(new_comments)
            await callbacks.on_progress(
                posts,
                comments,
                max_posts + max_comments,
                f"Dcard 已讀取 {posts} 篇文章、{comments} 則留言",
            )

        for raw_url in config.get("urls", []):
            if limits_reached():
                break
            if await callbacks.is_canceled():
                raise SourceCanceledError("任務已取消")
            url = str(raw_url).rstrip("/")
            url_match = _DCARD_PATH.fullmatch(urlparse(url).path)
            if (
                posts >= max_posts
                and url_match
                and url_match.group("id") not in known_keys
            ):
                continue
            html = await self.fetcher.get(url)
            items, page_complete = parse_dcard_article(html, url, self.hasher)
            complete = complete and page_complete
            if items:
                await process_group(items)

        imported_by_thread: dict[str, list[CollectedItem]] = {}
        for record in config.get("import_records", []):
            item = dcard_import_item(record, self.hasher)
            imported_by_thread.setdefault(item.thread_source_id or item.source_item_id, []).append(item)
        for items in imported_by_thread.values():
            if limits_reached():
                break
            await process_group(items)

        reached = limits_reached()
        return SourceCollectionResult(
            source="dcard",
            collected_count=posts + comments,
            post_count=posts,
            comment_count=comments,
            complete=complete or reached,
            stop_reason="target_reached" if reached else ("input_exhausted" if complete else "public_page_partial"),
            checkpoint={
                "url_count": len(config.get("urls", [])),
                "import_thread_count": len(imported_by_thread),
            },
        )
