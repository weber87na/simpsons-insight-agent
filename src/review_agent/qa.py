from __future__ import annotations

import asyncio
import re

from sqlalchemy import select

from .cloud import GroundedAnswer, OpenAIService
from .config import Settings, get_settings
from .db import SessionLocal
from .embeddings import EmbeddingService
from .models import (
    ChatMessage,
    ChatSession,
    JobReview,
    Report,
    Review,
    ReviewAnalysis,
    ReviewEmbedding,
)
from .privacy import redact_pii


class QuestionService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.openai = OpenAIService(self.settings)
        self.embeddings = EmbeddingService(self.settings)

    async def answer(
        self,
        *,
        report_id: str,
        question: str,
        session_id: str | None,
        model: str | None,
    ) -> tuple[str, GroundedAnswer]:
        if not self.openai.available:
            raise RuntimeError("尚未設定 OPENAI_API_KEY，無法使用報告問答。")
        async with SessionLocal() as session:
            report = await session.get(Report, report_id)
            if report is None:
                raise LookupError("找不到報告")
            selected_model = model or report.model_id or self.settings.openai_model_default
            if selected_model not in self.settings.allowed_llm_models:
                raise ValueError("報告使用了不允許的模型")
            if session_id:
                chat = await session.get(ChatSession, session_id)
                if chat is None or chat.report_id != report_id:
                    raise LookupError("找不到問答工作階段")
            else:
                chat = ChatSession(report_id=report_id)
                session.add(chat)
                await session.flush()
            chat_id = chat.id
            session.add(ChatMessage(session_id=chat_id, role="user", content=question))
            await session.commit()
            history_result = await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == chat_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(8)
            )
            history = [
                {"role": item.role, "content": item.content}
                for item in reversed(list(history_result.all()))
            ]

        evidence = await self._retrieve(
            report.job_id,
            question,
            set(report.payload.get("review_ids", [])),
        )
        answer = await self.openai.answer_question(
            question=question,
            report=report.payload,
            evidence=evidence,
            model=selected_model,
            history=history,
        )
        valid_ids = {item["review_id"] for item in evidence}
        answer.evidence_review_ids = [
            review_id for review_id in answer.evidence_review_ids if review_id in valid_ids
        ]
        if not answer.evidence_review_ids and evidence:
            answer.limitations.append("模型未能將回答連結到具體評論，請人工檢視證據清單。")

        async with SessionLocal() as session:
            session.add(
                ChatMessage(
                    session_id=chat_id,
                    role="assistant",
                    content=answer.answer,
                    evidence_review_ids=answer.evidence_review_ids,
                    limitations=answer.limitations,
                )
            )
            await session.commit()
        return chat_id, answer

    async def _retrieve(
        self,
        job_id: str,
        question: str,
        allowed_ids: set[str],
    ) -> list[dict]:
        async with SessionLocal() as session:
            statement = (
                select(ReviewEmbedding.review_id, ReviewEmbedding.vector, ReviewEmbedding.dimension)
                .join(Review, Review.id == ReviewEmbedding.review_id)
                .join(JobReview, JobReview.review_id == Review.id)
                .where(
                    JobReview.job_id == job_id,
                    ReviewEmbedding.model_id == self.settings.embedding_model,
                )
            )
            if allowed_ids:
                statement = statement.where(Review.id.in_(allowed_ids))
            result = await session.execute(statement)
            vectors = list(result.all())

        ranked_ids: list[str] = []
        if vectors:
            try:
                ranked = await asyncio.to_thread(self.embeddings.rank, question, vectors, 20)
                ranked_ids = [item.review_id for item in ranked]
            except Exception:
                ranked_ids = []

        async with SessionLocal() as session:
            if ranked_ids:
                result = await session.scalars(
                    select(Review)
                    .where(Review.id.in_(ranked_ids))
                )
                by_id = {item.id: item for item in result.all()}
                reviews = [by_id[item] for item in ranked_ids if item in by_id]
            else:
                statement = (
                    select(Review)
                    .join(JobReview, JobReview.review_id == Review.id)
                    .where(JobReview.job_id == job_id, Review.text != "")
                    .limit(200)
                )
                if allowed_ids:
                    statement = statement.where(Review.id.in_(allowed_ids))
                result = await session.scalars(statement)
                reviews = _keyword_rank(question, list(result.all()), 20)

            review_ids = [review.id for review in reviews]
            analyses = list(
                (
                    await session.scalars(
                        select(ReviewAnalysis).where(
                            ReviewAnalysis.job_id == job_id,
                            ReviewAnalysis.review_id.in_(review_ids),
                        )
                    )
                ).all()
            )
            analysis_by_review = {analysis.review_id: analysis for analysis in analyses}

        return [
            {
                "review_id": review.id,
                "source": review.source,
                "content_type": review.content_type,
                "title": review.title,
                "board": review.board,
                "rating": review.rating,
                "sentiment": analysis_by_review[review.id].sentiment
                if review.id in analysis_by_review
                else None,
                "aspects": analysis_by_review[review.id].aspects
                if review.id in analysis_by_review
                else [],
                "text": review.redacted_text or redact_pii(review.text),
            }
            for review in reviews
        ]


def _keyword_rank(question: str, reviews: list[Review], limit: int) -> list[Review]:
    terms = set(re.findall(r"[\w\u3400-\u9fff]{2,}", question.lower()))

    def score(review: Review) -> int:
        text = review.text.lower()
        return sum(text.count(term) for term in terms)

    ranked = sorted(reviews, key=score, reverse=True)
    useful = [item for item in ranked if score(item) > 0]
    return (useful or ranked)[:limit]
