from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RepoCreate(BaseModel):
    url: str = Field(max_length=200)


class AskRequest(BaseModel):
    repo_id: UUID
    question: str = Field(max_length=5000)   # real limit enforced by guardrail (config)
    mode: Literal["fast", "agent"] | None = None   # default comes from DEFAULT_ASK_MODE


class DecisionRequest(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    edited_payload: dict | None = None


class FeedbackRequest(BaseModel):
    query_id: UUID
    rating: Literal[-1, 1]
    correction: str | None = Field(default=None, max_length=2000)
