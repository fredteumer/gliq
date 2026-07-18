"""Contracts exchanged between GreenlightIQ components.

These models are the interface between the three processes. Every message that
crosses a component boundary is one of these, serialised to JSON. Components
never pass ad-hoc dicts to each other.

Flow:
    A (intake)  --ScoringRequested-->  B (scoring)  --ScoringCompleted-->  C (report)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Component A output — extracted from the design document by an LLM
# --------------------------------------------------------------------------


class PriceTier(StrEnum):
    FREE = "free"
    BUDGET = "budget"  # < $10
    STANDARD = "standard"  # $10-25
    PREMIUM = "premium"  # $25-45
    FULL = "full"  # > $45


class PitchProfile(BaseModel):
    """Normalised, machine-comparable representation of a game pitch."""

    title: str
    primary_genre: str
    sub_genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    core_mechanics: list[str] = Field(default_factory=list)
    art_style: str | None = None
    price_tier: PriceTier
    target_platforms: list[str] = Field(default_factory=list)
    summary: str | None = None

    #: Which LLM produced this, for auditability. "fixture" when canned.
    extracted_by: str
    extraction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


# --------------------------------------------------------------------------
# Component B output — deterministic, rule-based market scoring
# --------------------------------------------------------------------------


class Comparable(BaseModel):
    """A released Steam title matched against the pitch."""

    app_id: int
    name: str
    release_date: str | None = None
    price_usd: float | None = None
    review_count: int | None = None
    positive_ratio: float | None = Field(default=None, ge=0.0, le=1.0)

    #: Estimated units. NOT audited sales — see estimation_method.
    estimated_units: int | None = None
    estimation_method: str | None = None

    #: How closely this title matches the pitch, 0-1.
    similarity: float = Field(ge=0.0, le=1.0)


class SubScores(BaseModel):
    """Weighted components of the overall fitment score, each 0-100."""

    market_saturation: float = Field(ge=0.0, le=100.0)
    niche_hit_rate: float = Field(ge=0.0, le=100.0)
    sales_potential: float = Field(ge=0.0, le=100.0)
    price_alignment: float = Field(ge=0.0, le=100.0)


class FitmentResult(BaseModel):
    """Result of scoring a pitch against the market comparables corpus."""

    score: float = Field(ge=0.0, le=100.0)
    grade: str  # A-F
    sub_scores: SubScores
    comparables: list[Comparable] = Field(default_factory=list)

    comps_considered: int
    #: Assumptions to surface in the report — estimation methods, corpus
    #: vintage, any matching fallbacks applied.
    assumptions: list[str] = Field(default_factory=list)
    scored_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Component C output — the publisher-facing decision
# --------------------------------------------------------------------------


class InvestmentTier(StrEnum):
    GREENLIGHT = "greenlight"
    CONDITIONAL = "conditional"
    DE_RISK = "de_risk"
    PASS = "pass"


class Recommendation(BaseModel):
    tier: InvestmentTier
    rationale: str
    de_risk_actions: list[str] = Field(default_factory=list)
    report_uri: str | None = None
    generated_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Messages on the wire
# --------------------------------------------------------------------------


class ScoringRequested(BaseModel):
    """A -> B. Published to PUBSUB_TOPIC_SCORING_REQUESTED."""

    pitch_id: UUID = Field(default_factory=uuid4)
    profile: PitchProfile
    requested_at: datetime = Field(default_factory=_now)


class ScoringCompleted(BaseModel):
    """B -> C. Published to PUBSUB_TOPIC_SCORING_COMPLETED."""

    pitch_id: UUID
    profile: PitchProfile
    result: FitmentResult
    completed_at: datetime = Field(default_factory=_now)
