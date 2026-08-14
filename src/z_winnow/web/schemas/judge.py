"""Judge (LLM-as-judge) schema models.

Request/response models for quality evaluation using LLM-as-judge.
No direct table mapping — judge results are computed on-the-fly.

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JudgeDimensionScore(BaseModel):
    """Score for a single quality dimension."""

    model_config = ConfigDict(from_attributes=True)

    dimension: str
    score: float = Field(ge=0.0, le=1.0)
    evidence: str = ""
    passed: bool = True


class JudgeRequest(BaseModel):
    """Request body for triggering an LLM-as-judge evaluation."""

    report_id: str = Field(min_length=1, description="Report ID to evaluate")
    dimensions: list[str] | None = Field(
        default=None,
        description="Quality dimensions to evaluate (default: all)",
    )
    model: str | None = Field(default=None, description="LLM model to use for judging")


class JudgeResultOut(BaseModel):
    """Response model for a judge evaluation result."""

    model_config = ConfigDict(from_attributes=True)

    report_id: str
    overall_score: float = 0.0
    dimensions: list[JudgeDimensionScore] = []
    summary: str = ""
    judged_at: str | None = None
    model_used: str | None = None
