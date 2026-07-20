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


#: Fields whose absence is decisive. See docs/SCORING.md, step 5.
LOAD_BEARING_FIELDS = ("title", "primary_genre", "price_tier")

#: Fields that feed the scoring arithmetic, and so count toward coverage.
#: title/summary/art_style are report-only and deliberately excluded.
COVERAGE_FIELDS = (
    "primary_genre",
    "sub_genres",
    "tags",
    "core_mechanics",
    "price_tier",
    "target_platforms",
)


class PitchProfile(BaseModel):
    """Normalised, machine-comparable representation of a game pitch.

    ⚠️ Almost everything is optional, deliberately. The design document is
    open-ended, so extraction will often come up empty on a field — and a
    missing field must produce a LOW GRADE, not a validation error. Requiring
    `primary_genre` here would mean a sparse pitch raised ValidationError in
    Component A and never reached scoring at all, which is precisely the
    outcome the completeness cap exists to avoid.

    Decisiveness is enforced in scoring (docs/SCORING.md), not in validation.
    """

    title: str | None = None
    primary_genre: str | None = None
    sub_genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    core_mechanics: list[str] = Field(default_factory=list)
    art_style: str | None = None
    price_tier: PriceTier | None = None
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
    """Weighted components of the overall fitment score, each 0-100.

    ⚠️ Raw crowding is deliberately NOT one of these. 10,000 titles in a genre
    where most clear the revenue bar is a healthy market; 200 where almost none
    do is a graveyard. Counting competitors conflates the two. What matters is
    the rate of success, the size of the wins, and whether the space is
    winnable — which is what the first three measure.
    """

    #: Fraction of comparables clearing the revenue bar. The core signal.
    niche_hit_rate: float = Field(ge=0.0, le=100.0)
    #: Median revenue among the successful comps. Rate is not size.
    sales_potential: float = Field(ge=0.0, le=100.0)
    #: Inverse revenue concentration. Rate is not winnability: most comps may
    #: clear the bar while a few titles hold most of the units.
    competitive_headroom: float = Field(ge=0.0, le=100.0)
    #: Pitch price tier against what the niche actually sustains.
    price_alignment: float = Field(ge=0.0, le=100.0)


class FitmentResult(BaseModel):
    """Result of scoring a pitch against the market comparables corpus."""

    score: float = Field(ge=0.0, le=100.0)
    grade: str  # A-F
    sub_scores: SubScores
    comparables: list[Comparable] = Field(default_factory=list)

    comps_considered: int

    #: Fraction of scoring-relevant fields the extractor populated, 0-1.
    completeness: float = Field(ge=0.0, le=1.0)
    #: What the weighted total was BEFORE the completeness cap. Equal to
    #: `score` when no cap applied. Reported so an incomplete submission shows
    #: what it cost rather than silently absorbing the difference.
    uncapped_score: float = Field(ge=0.0, le=100.0)
    #: Which load-bearing fields were missing, if any. Empty when complete.
    missing_fields: list[str] = Field(default_factory=list)
    #: True when there was no comp set at all (no genre AND no tags), which is
    #: "we cannot evaluate this" rather than "we evaluated it and it is bad".
    #: The two warrant different reports and must not be conflated.
    insufficient_information: bool = False
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
