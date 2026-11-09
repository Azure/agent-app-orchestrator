"""Stable application names and business types shared by both native hosts."""

from typing import Literal, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

AGENT_NAME = "agent-lz-conversation"
WORKFLOW_NAME = "agent-lz-research-review"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PresentationResult(Contract):
    presentation: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("presentation")
    @classmethod
    def validate_presentation(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            from .presentation import validator
            validator("turn-presentation").validate(value)
        return value


class ResearchReviewStart(Contract):
    topic: str = Field(min_length=1, max_length=8000, pattern=r"\S")


class ReviewDecision(Contract):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=4000)


class ResearchReviewResult(PresentationResult):
    status: Literal["approved", "rejected"]
    topic: str
    draft: str
    review_comment: str
    configuration_revision: str


class ReviewRequest(PresentationResult):
    """Business payload; the native runtime supplies the pending request ID."""

    topic: str
    draft: str
    configuration_revision: str
