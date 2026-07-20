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
    """Steam price points, as actually used by the storefront.

    These are charm-pricing tiers ($9.99, $14.99, $19.99 …), not evenly spaced
    bands. Steam publishers pick from this ladder rather than from a continuum,
    so a pitch's price is a rung, and the gap between rungs is a psychological
    step rather than a dollar amount.

    ⚠️ An earlier five-band split (budget / standard / premium / full) was drawn
    on AAA intuitions and did not survive contact with the corpus: successful
    titles run p25 $1.29, p50 $3.99, p75 $8.99, p90 $14.99. Anything over $15
    is already top-decile, so treating "$10-25" as one middle band lumped the
    common case together with the rare one.

    Ordering matters — `price_alignment` compares RUNG DISTANCE, not dollars.
    Use `.index` for that and `.price_point` when a number is needed.
    """

    FREE = "free"
    P0_99 = "0.99"
    P1_99 = "1.99"
    P3_99 = "3.99"
    P4_99 = "4.99"
    P7_49 = "7.49"
    P9_99 = "9.99"
    P11_99 = "11.99"
    P13_49 = "13.49"
    P14_99 = "14.99"
    P19_99 = "19.99"
    P24_99 = "24.99"
    P29_99 = "29.99"
    P39_99 = "39.99"
    P49_99 = "49.99"
    P99_99 = "99.99"
    P100_PLUS = "100+"

    @property
    def index(self) -> int:
        """Position on the ladder, 0 = free. The unit `price_alignment` uses."""
        return _TIER_ORDER.index(self)

    @property
    def price_point(self) -> float:
        """The tier's ceiling in dollars. FREE is 0; the top tier is open-ended."""
        return _TIER_PRICE[self]

    @classmethod
    def from_price(cls, price: float | None) -> "PriceTier | None":
        """Map an observed price onto its rung. None stays None."""
        if price is None:
            return None
        if price <= 0:
            return cls.FREE
        for tier in _TIER_ORDER:
            if price <= _TIER_PRICE[tier]:
                return tier
        return cls.P100_PLUS


#: Declaration order is the ladder order; kept explicit so a reordering of the
#: enum body cannot silently change what `.index` means.
_TIER_ORDER: tuple[PriceTier, ...] = (
    PriceTier.FREE,
    PriceTier.P0_99,
    PriceTier.P1_99,
    PriceTier.P3_99,
    PriceTier.P4_99,
    PriceTier.P7_49,
    PriceTier.P9_99,
    PriceTier.P11_99,
    PriceTier.P13_49,
    PriceTier.P14_99,
    PriceTier.P19_99,
    PriceTier.P24_99,
    PriceTier.P29_99,
    PriceTier.P39_99,
    PriceTier.P49_99,
    PriceTier.P99_99,
    PriceTier.P100_PLUS,
)

_TIER_PRICE: dict[PriceTier, float] = {
    PriceTier.FREE: 0.0,
    PriceTier.P0_99: 0.99,
    PriceTier.P1_99: 1.99,
    PriceTier.P3_99: 3.99,
    PriceTier.P4_99: 4.99,
    PriceTier.P7_49: 7.49,
    PriceTier.P9_99: 9.99,
    PriceTier.P11_99: 11.99,
    PriceTier.P13_49: 13.49,
    PriceTier.P14_99: 14.99,
    PriceTier.P19_99: 19.99,
    PriceTier.P24_99: 24.99,
    PriceTier.P29_99: 29.99,
    PriceTier.P39_99: 39.99,
    PriceTier.P49_99: 49.99,
    PriceTier.P99_99: 99.99,
    # Open-ended. The value is the floor of the band, not a ceiling.
    PriceTier.P100_PLUS: 100.0,
}


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
    where most clear the units bar is a healthy market; 200 where almost none do
    is a graveyard. Counting competitors conflates the two. What matters is the
    RATE at which entrants succeed and the SIZE of the wins available.

    ⛔ A fourth sub-score, `competitive_headroom` (inverse concentration — "is
    this space locked up by incumbents?"), was specified, built, and then
    removed. Validation against 120 known winners vs 120 random titles showed it
    carried no information: winners scored 18.2 against random's 19.7, i.e.
    marginally WORSE. The reason is structural — a winner is often the very
    title concentrating its niche, so the measure flags its comp set as locked
    up. The question is real; top-10 share does not answer it. Recorded here
    rather than silently dropped, because "we tried this and it did not work" is
    worth more than a gap. ➡️ docs/SCORING.md
    """

    #: Fraction of comparables clearing SUCCESS_UNITS. Separates winners from
    #: random titles 4.5x — the strongest signal measured.
    niche_hit_rate: float = Field(ge=0.0, le=100.0)
    #: p90 units among the successful comps: how big a win is available here.
    #: Rate is not size. Separates 3.6x.
    sales_potential: float = Field(ge=0.0, le=100.0)
    #: How distinct the pitch's position is within its own niche — the mean
    #: similarity of its closest comps, inverted.
    #: ⚠️ REPORTED BUT UNWEIGHTED (weight 0). Validation showed no separation
    #: between winners and random titles (23.7 vs 23.3), because tags cannot
    #: measure differentiation — two roguelike deckbuilders can share every tag
    #: and be different games. Kept because "your five closest comparables
    #: average 0.66 similarity" is a useful, checkable statement for a
    #: publisher; excluded from the grade because it does not predict outcomes.
    #: Real differentiation judgement is a Part 2 (LLM) job.
    differentiation: float = Field(ge=0.0, le=100.0)
    #: Pitch price rung against the comp set's median rung.
    #: ⚠️ Deliberately near-constant on well-priced pitches — it is a guard rail
    #: against mispricing, not a success predictor. Silence is correct.
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
