from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from .config import get_settings

SourceKind = Literal["google_maps", "ptt", "dcard"]
ContentType = Literal["review", "post", "comment"]
SourceRunStatus = Literal[
    "PENDING",
    "COLLECTING",
    "COMPLETE",
    "PARTIAL",
    "BLOCKED",
    "FAILED",
]


def _validate_maps_url(value: HttpUrl) -> HttpUrl:
    parsed = urlparse(str(value))
    hostname = (parsed.hostname or "").lower()
    is_google = hostname in {"google.com", "google.com.tw"} or hostname.endswith(
        (".google.com", ".google.com.tw")
    )
    is_short_link = hostname in {"goo.gl", "maps.app.goo.gl"}
    if not (is_short_link or (is_google and parsed.path.startswith("/maps"))):
        raise ValueError("只允許 Google Maps URL")
    return value


def _validate_dcard_url(value: HttpUrl) -> HttpUrl:
    parsed = urlparse(str(value))
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "www.dcard.tw"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or not re.fullmatch(r"/f/[A-Za-z0-9_-]+/p/\d+/?", parsed.path)
    ):
        raise ValueError("只允許 https://www.dcard.tw/f/{forum}/p/{id} 公開文章網址")
    return value


class BusinessCandidate(BaseModel):
    name: str
    maps_url: str
    address: str | None = None
    average_rating: float | None = None
    total_review_count: int | None = None


class BusinessSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    headless: bool | None = None


class BusinessSearchResponse(BaseModel):
    candidates: list[BusinessCandidate]
    direct_url: bool = False


class SubjectInput(BaseModel):
    kind: Literal["business", "brand"] = "business"
    name: str = Field(min_length=1, max_length=500)
    address: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = value.strip()
            if not item or len(item) > 200:
                raise ValueError("別名不得為空且不可超過 200 字")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class GoogleMapsSourceConfig(BaseModel):
    source: Literal["google_maps"] = "google_maps"
    maps_url: HttpUrl
    max_reviews: int = Field(default=500, ge=1, le=500)
    sort: Literal["newest", "relevant"] = "newest"
    headless: bool = False
    average_rating: float | None = Field(default=None, ge=0, le=5)
    total_review_count: int | None = Field(default=None, ge=0)

    _maps_url = field_validator("maps_url")(_validate_maps_url)


class PttSourceConfig(BaseModel):
    source: Literal["ptt"] = "ptt"
    boards: list[str] = Field(min_length=1, max_length=10)
    keywords: list[str] = Field(min_length=1, max_length=10)
    date_from: date = Field(default_factory=lambda: date.today() - timedelta(days=365))
    date_to: date = Field(default_factory=date.today)
    max_posts: int = Field(default=50, ge=1, le=200)
    max_comments: int = Field(default=500, ge=0, le=2000)
    max_comments_per_thread: int = Field(default=100, ge=0, le=100)

    @field_validator("boards")
    @classmethod
    def validate_boards(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) for value in cleaned):
            raise ValueError("PTT 看板名稱格式錯誤")
        return cleaned

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not value or len(value) > 100 for value in cleaned):
            raise ValueError("PTT 關鍵字不得為空且不可超過 100 字")
        return cleaned

    @model_validator(mode="after")
    def validate_dates(self) -> PttSourceConfig:
        if self.date_from > self.date_to:
            raise ValueError("PTT 起始日不可晚於結束日")
        return self


class DcardSourceConfig(BaseModel):
    source: Literal["dcard"] = "dcard"
    urls: list[HttpUrl] = Field(default_factory=list, max_length=50)
    import_ids: list[str] = Field(default_factory=list, max_length=20)
    date_from: date = Field(default_factory=lambda: date.today() - timedelta(days=365))
    date_to: date = Field(default_factory=date.today)
    max_posts: int = Field(default=50, ge=1, le=100)
    max_comments: int = Field(default=500, ge=0, le=2000)
    max_comments_per_thread: int = Field(default=100, ge=0, le=100)
    acknowledge_terms: bool = False

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, values: list[HttpUrl]) -> list[HttpUrl]:
        result: list[HttpUrl] = []
        seen: set[str] = set()
        for value in values:
            checked = _validate_dcard_url(value)
            rendered = str(checked).rstrip("/")
            if rendered not in seen:
                result.append(checked)
                seen.add(rendered)
        return result

    @model_validator(mode="after")
    def validate_config(self) -> DcardSourceConfig:
        if self.date_from > self.date_to:
            raise ValueError("Dcard 起始日不可晚於結束日")
        if not self.urls and not self.import_ids:
            raise ValueError("Dcard 至少需要一個公開文章 URL 或匯入批次")
        if not self.acknowledge_terms:
            raise ValueError("必須確認 Dcard 來源條款提醒")
        return self


SourceConfig = Annotated[
    GoogleMapsSourceConfig | PttSourceConfig | DcardSourceConfig,
    Field(discriminator="source"),
]


class CreateJobRequest(BaseModel):
    """V2 multi-source request with the legacy Google-only fields kept compatible."""

    subject: SubjectInput | None = None
    sources: list[SourceConfig] = Field(default_factory=list, max_length=3)
    llm_model: str | None = None

    # Legacy Google-only contract.
    maps_url: HttpUrl | None = None
    name: str | None = Field(default=None, min_length=1, max_length=500)
    address: str | None = Field(default=None, max_length=1000)
    average_rating: float | None = Field(default=None, ge=0, le=5)
    total_review_count: int | None = Field(default=None, ge=0)
    max_reviews: int = Field(default=500, ge=1, le=500)
    sort: Literal["newest", "relevant"] = "newest"
    headless: bool = False

    @field_validator("maps_url")
    @classmethod
    def validate_legacy_maps_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        return _validate_maps_url(value) if value is not None else None

    @field_validator("llm_model")
    @classmethod
    def validate_model(cls, value: str | None) -> str:
        settings = get_settings()
        selected = value or settings.openai_model_default
        if selected not in settings.allowed_llm_models:
            raise ValueError(f"未知模型；允許值：{', '.join(settings.allowed_llm_models)}")
        return selected

    @model_validator(mode="after")
    def validate_shape(self) -> CreateJobRequest:
        if self.sources:
            if self.subject is None:
                raise ValueError("多來源任務必須提供 subject")
            kinds = [item.source for item in self.sources]
            if len(kinds) != len(set(kinds)):
                raise ValueError("同一來源只能設定一次")
        elif self.maps_url is None or not self.name:
            raise ValueError("至少選擇一個來源")
        return self

    def subject_input(self) -> SubjectInput:
        if self.subject is not None:
            return self.subject
        return SubjectInput(kind="business", name=self.name or "未命名", address=self.address)

    def source_configs(self) -> list[SourceConfig]:
        if self.sources:
            return self.sources
        assert self.maps_url is not None
        return [
            GoogleMapsSourceConfig(
                maps_url=self.maps_url,
                max_reviews=self.max_reviews,
                sort=self.sort,
                headless=self.headless,
                average_rating=self.average_rating,
                total_review_count=self.total_review_count,
            )
        ]


class StartAnalysisRequest(BaseModel):
    llm_model: str | None = None
    accept_partial_collection: bool = False

    @field_validator("llm_model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is not None and value not in get_settings().allowed_llm_models:
            raise ValueError("未知模型")
        return value


class JobSourceResponse(BaseModel):
    source: str
    status: str
    collected_count: int
    post_count: int
    comment_count: int
    target_count: int
    collection_complete: bool
    stop_reason: str | None
    error: str | None
    attempt_count: int
    config: dict


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    business_id: str
    subject_id: str
    status: str
    max_reviews: int
    sort_order: str
    headless: bool
    llm_model: str
    collected_count: int
    processed_count: int
    progress: float
    message: str | None
    error: str | None
    cancel_requested: bool
    collection_complete: bool
    collection_stop_reason: str | None
    can_resume_collection: bool
    can_start_analysis: bool
    last_completed_stage: str | None
    degraded_reasons: list[str]
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    report_id: str | None = None
    sources: list[JobSourceResponse] = Field(default_factory=list)


class ReviewResponse(BaseModel):
    id: str
    source: str = "google_maps"
    content_type: str = "review"
    title: str | None = None
    board: str | None = None
    thread_source_id: str | None = None
    source_url: str | None = None
    platform_data: dict = Field(default_factory=dict)
    rating: int | None
    text: str
    relative_date: str | None
    published_at_estimated: datetime | None
    date_precision: str
    owner_reply: str | None
    language: str | None
    sentiment: str | None
    confidence: float | None
    rating_sentiment: str | None
    rating_text_conflict: bool
    aspects: list[str]
    key_points: list[str]


class PaginatedReviews(BaseModel):
    items: list[ReviewResponse]
    total: int
    page: int
    page_size: int


class SourceImportResponse(BaseModel):
    import_id: str
    filename: str
    row_count: int
    sha256: str
    validation_status: str = "VALID"


class ReportResponse(BaseModel):
    id: str
    job_id: str
    business_id: str
    model_id: str | None
    status: str
    payload: dict
    created_at: datetime


class QuestionRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    session_id: str | None = None
    llm_model: str | None = None

    @field_validator("llm_model")
    @classmethod
    def validate_question_model(cls, value: str | None) -> str | None:
        if value is not None and value not in get_settings().allowed_llm_models:
            raise ValueError("未知模型")
        return value


class QuestionResponse(BaseModel):
    session_id: str
    answer: str
    evidence_review_ids: list[str]
    limitations: list[str]
