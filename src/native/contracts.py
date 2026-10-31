"""Stable application names and business types shared by both native hosts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AGENT_NAME = "alz-conversation"
WORKFLOW_NAME = "alz-research-review"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchReviewStart(Contract):
    topic: str = Field(min_length=1, max_length=8000, pattern=r"\S")


class ReviewDecision(Contract):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=4000)


class ResearchReviewResult(Contract):
    status: Literal["approved", "rejected"]
    topic: str
    draft: str
    review_comment: str
    configuration_revision: str


class ReviewRequest(Contract):
    """Business payload; the native runtime supplies the pending request ID."""

    topic: str
    draft: str
    configuration_revision: str
