"""Deterministic scoring rules — the implementation of docs/SCORING.md.

⚠️ Pure functions only. No database, no network, no Pub/Sub. Everything here
takes plain data and returns plain data, so the rules can be tested and
calibrated without infrastructure — and so `data/etl/calibrate.py` and
`validate_scoring.py` can import the SAME code that runs in production. A
calibration that measures a reimplementation of the rules measures the wrong
thing.

➡️ docs/SCORING.md is the specification. Change it there first.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Iterable, Sequence

from shared.schemas import (
    COVERAGE_FIELDS,
    LOAD_BEARING_FIELDS,
    Comparable,
    FitmentResult,
    InvestmentTier,
    PitchProfile,
    PriceTier,
    SubScores,
)

# --------------------------------------------------------------------------
# Constants. ✅ = calibrated against the corpus, ⏳ = editorial judgement.
# --------------------------------------------------------------------------

W_TAGS = 0.55        # ✅ raised from 0.35 — see docs/SCORING.md step 1
W_GENRE = 0.20       # ✅ lowered from 0.40; a flat genre match cleared any floor
W_SUBGENRE = 0.15
W_PLATFORM = 0.10

SIMILARITY_FLOOR = 0.45   # ✅ p90 of observed similarity
MAX_COMPS = 50            # ✅ 149/150 sample pitches fill it
MIN_COMPS_WARN = 5

RECENCY_FULL_YEARS = 3    # ⏳
RECENCY_FLOOR_YEARS = 8   # ⏳
RECENCY_FLOOR = 0.25

SUCCESS_UNITS = 10_000              # ✅ the success bar, in units moved
POTENTIAL_UNITS_FLOOR = 23_250      # ✅ observed p05 of real comp sets
POTENTIAL_UNITS_CEILING = 1_449_000  # ✅ observed p95

PRICE_PENALTY_PER_RUNG = 12.5
#: No price on the pitch or no priced comps — neither reward nor punish.
PRICE_UNKNOWN = 50.0

WEIGHT_HIT_RATE = 0.45     # ⏳ editorial
WEIGHT_POTENTIAL = 0.40    # ⏳ tilted toward ceiling: the brief is winners who win BIG
WEIGHT_PRICE = 0.15        # ⏳

#: Grade floors. ✅ anchored to the validation distributions.
GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (80.0, "A"),
    (68.0, "B"),
    (55.0, "C"),
    (40.0, "D"),
    (0.0, "F"),
)

TIER_FOR_GRADE = {
    "A": InvestmentTier.GREENLIGHT,
    "B": InvestmentTier.GREENLIGHT,
    "C": InvestmentTier.CONDITIONAL,
    "D": InvestmentTier.DE_RISK,
    "F": InvestmentTier.PASS,
}

#: Completeness ceilings, expressed as the TOP of a grade band rather than as
#: round numbers.
#:
#: ⚠️ These were originally 85/75/65 against the old 90/80/70/60 thresholds. The
#: thresholds moved to 80/68/55/40 after validation, which silently turned a
#: "cap at D" into a cap at C. Deriving them from GRADE_THRESHOLDS keeps the two
#: from drifting apart again.
CAP_B = 79.0   # just under A
CAP_C = 67.0   # just under B
CAP_D = 54.0   # just under C

ESTIMATION_DISCLOSURE = (
    "Units are ESTIMATES (Boxleiter review-count multiplier, cross-checked "
    "against SteamSpy owner bands), not audited sales — Steam does not publish "
    "unit sales."
)


# --------------------------------------------------------------------------
# Similarity
# --------------------------------------------------------------------------


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _normalise(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in weights.items()}


def tag_overlap(pitch_tags: Sequence[str], cand_votes: dict[str, int]) -> float:
    """Vote-weighted overlap between a pitch's tags and a candidate's.

    Steam tags carry vote counts, so a title tagged 'Turn-Based Strategy' 86
    times genuinely is one while a single stray vote is noise. Plain Jaccard
    treats those identically, which is how ubiquitous tags like 'Indie' come to
    dominate a comp set.

    ⚠️ A pitch has no votes — the extractor either found a tag or it did not —
    so its tags are weighted uniformly. BOTH sides are then normalised to sum
    to 1 before intersecting, which is what makes the comparison meaningful:
    without it, a candidate carrying thousands of votes would score as less
    similar than a sparsely-tagged one purely because its denominator is
    bigger. The result is the fraction of tag-weight the two share.
    """
    if not pitch_tags or not cand_votes:
        return 0.0
    p = _normalise({t: 1.0 for t in pitch_tags})
    c = _normalise({k: float(v) for k, v in cand_votes.items() if v >= 0})
    if not p or not c:
        return 0.0
    return sum(min(p[t], c[t]) for t in p.keys() & c.keys())


def recency_factor(release_date: date | None, today: date) -> float:
    """Older comparables are weaker evidence about today's market.

    Applied to similarity rather than scored separately, so a niche with a good
    hit rate but nothing successful in five years falls away on its own.
    Never reaches zero: a genre's history still informs it.
    """
    if release_date is None:
        return RECENCY_FLOOR
    years = (today - release_date).days / 365.25
    if years <= RECENCY_FULL_YEARS:
        return 1.0
    if years >= RECENCY_FLOOR_YEARS:
        return RECENCY_FLOOR
    span = RECENCY_FLOOR_YEARS - RECENCY_FULL_YEARS
    return 1.0 - 0.5 * ((years - RECENCY_FULL_YEARS) / span)


def similarity(profile: PitchProfile, cand: dict, today: date) -> float:
    """Weighted overlap, scaled by recency. See docs/SCORING.md step 1."""
    cand_genres = set(cand.get("genres") or [])
    genre = 1.0 if profile.primary_genre and profile.primary_genre in cand_genres else 0.0

    raw = (
        W_TAGS * tag_overlap(profile.tags, cand.get("tag_votes") or {})
        + W_GENRE * genre
        + W_SUBGENRE * jaccard(set(profile.sub_genres), cand_genres)
        + W_PLATFORM * jaccard(set(profile.target_platforms), set(cand.get("platforms") or []))
    )
    return raw * recency_factor(cand.get("release_date"), today)


def select_comps(
    profile: PitchProfile, candidates: Iterable[dict], today: date
) -> list[tuple[float, dict]]:
    """Top MAX_COMPS above SIMILARITY_FLOOR, most similar first."""
    scored = ((similarity(profile, c, today), c) for c in candidates)
    kept = [pair for pair in scored if pair[0] >= SIMILARITY_FLOOR]
    kept.sort(key=lambda pair: -pair[0])
    return kept[:MAX_COMPS]


# --------------------------------------------------------------------------
# Sub-scores
# --------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(q / 100 * len(s)), len(s) - 1)]


def niche_hit_rate(units: Sequence[int]) -> float:
    """Fraction of comps clearing SUCCESS_UNITS. The strongest signal (4.5x).

    ⚠️ Absolute, never normalised to the corpus. The median real comp set has
    an 8% hit rate, so most pitches score low — which is correct. Most Steam
    releases do not clear 10,000 units. Rescaling to make the median pitch look
    average would be grade inflation and would destroy the discrimination the
    whole tool exists to provide.
    """
    if not units:
        return 0.0
    return 100.0 * sum(1 for u in units if u >= SUCCESS_UNITS) / len(units)


def sales_potential(units: Sequence[int]) -> float:
    """How big a win is available here — p90 of the winners, log-scaled.

    p90 rather than median: a median over a set already thresholded at >=10k is
    structurally stable (1.33x spread across niches, scoring everything ~55).
    p90 spreads 62x and asks the better question — how big can a hit get?
    """
    winners = [u for u in units if u >= SUCCESS_UNITS]
    if not winners:
        return 0.0
    p90 = max(percentile(winners, 90), 1)
    lo, hi = math.log10(POTENTIAL_UNITS_FLOOR), math.log10(POTENTIAL_UNITS_CEILING)
    return max(0.0, min(100.0, 100 * (math.log10(p90) - lo) / (hi - lo)))


def price_alignment(pitch_tier: PriceTier | None, comp_prices: Sequence[float]) -> float:
    """Rung distance from the comp set's median price tier.

    Scored in ladder rungs, not dollars: Steam prices are charm-pricing points,
    and $9.99 -> $11.99 is a psychological step a $2.00 delta understates.

    ⚠️ A GUARD RAIL, NOT A SUCCESS PREDICTOR. It scores ~87 for both winning and
    random real titles, because shipped games are priced sensibly for their
    niches. It fires when a pitch asks $49.99 in a $3.99 niche. Its low variance
    is correct behaviour — do not "fix" it.
    """
    rungs = [PriceTier.from_price(p).index for p in comp_prices if p is not None]
    if pitch_tier is None or not rungs:
        return PRICE_UNKNOWN
    median_rung = percentile(rungs, 50)
    distance = abs(pitch_tier.index - median_rung)
    return max(0.0, 100.0 - PRICE_PENALTY_PER_RUNG * distance)


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------


def _is_populated(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, str)) and len(value) == 0:
        return False
    return True


def completeness(profile: PitchProfile) -> float:
    """Fraction of scoring-relevant fields the extractor populated, 0-1.

    Counts COVERAGE_FIELDS only — title/summary/art_style are report-only and
    do not affect the arithmetic, so they must not dilute the measure.
    """
    populated = sum(1 for f in COVERAGE_FIELDS if _is_populated(getattr(profile, f, None)))
    return populated / len(COVERAGE_FIELDS)


def missing_load_bearing(profile: PitchProfile) -> list[str]:
    return [f for f in LOAD_BEARING_FIELDS if not _is_populated(getattr(profile, f, None))]


def completeness_ceiling(profile: PitchProfile) -> tuple[float, str | None]:
    """The grade ceiling this profile's completeness imposes.

    A CEILING, not an averaged-in penalty: it keeps "we cannot tell" distinct
    from "we can tell, and it is mediocre". Averaging destroys that, and the two
    warrant different recommendations.
    """
    missing = missing_load_bearing(profile)
    if missing:
        return CAP_D, f"missing load-bearing field(s): {', '.join(missing)}"

    coverage = completeness(profile)
    if coverage < 0.40:
        return CAP_D, f"only {coverage:.0%} of scoring fields were extracted"
    if coverage < 0.60:
        return CAP_C, f"only {coverage:.0%} of scoring fields were extracted"
    if coverage < 0.80:
        return CAP_B, f"only {coverage:.0%} of scoring fields were extracted"
    return 100.0, None


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


def grade_for(score: float) -> str:
    for floor, letter in GRADE_THRESHOLDS:
        if score >= floor:
            return letter
    return "F"


def tier_for(grade: str) -> InvestmentTier:
    return TIER_FOR_GRADE.get(grade, InvestmentTier.PASS)


def has_comp_basis(profile: PitchProfile) -> bool:
    """False when there is nothing to build a comp set from at all.

    Distinct from a low grade: "we cannot evaluate this" and "we evaluated it
    and it is bad" both land on F but warrant different reports.
    """
    return bool(profile.primary_genre) or bool(profile.tags)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def score_pitch(profile: PitchProfile, candidates: Iterable[dict], today: date) -> FitmentResult:
    """Score a pitch against candidate comparables. The whole of docs/SCORING.md."""
    assumptions: list[str] = [ESTIMATION_DISCLOSURE]
    coverage = completeness(profile)

    if not has_comp_basis(profile):
        return FitmentResult(
            score=0.0,
            grade="F",
            sub_scores=SubScores(niche_hit_rate=0.0, sales_potential=0.0, price_alignment=0.0),
            comparables=[],
            comps_considered=0,
            completeness=coverage,
            uncapped_score=0.0,
            missing_fields=missing_load_bearing(profile),
            insufficient_information=True,
            assumptions=assumptions
            + ["No genre and no tags were extracted, so no comparable set exists."],
        )

    comps = select_comps(profile, candidates, today)
    if len(comps) < MIN_COMPS_WARN:
        assumptions.append(
            f"Only {len(comps)} comparable(s) cleared the similarity floor; "
            "the market read is correspondingly weak."
        )

    units = [c.get("estimated_units") or 0 for _, c in comps]
    prices = [c["price_usd"] for _, c in comps if c.get("price_usd") is not None]

    subs = SubScores(
        niche_hit_rate=niche_hit_rate(units),
        sales_potential=sales_potential(units),
        price_alignment=price_alignment(profile.price_tier, prices),
    )

    uncapped = (
        WEIGHT_HIT_RATE * subs.niche_hit_rate
        + WEIGHT_POTENTIAL * subs.sales_potential
        + WEIGHT_PRICE * subs.price_alignment
    )

    ceiling, reason = completeness_ceiling(profile)
    score = min(uncapped, ceiling)
    if reason:
        # State what the incompleteness COST, so the report shows it rather
        # than silently absorbing the difference.
        assumptions.append(
            f"Grade capped at {ceiling:.0f} — {reason}. "
            f"Uncapped score was {uncapped:.1f}."
        )

    return FitmentResult(
        score=score,
        grade=grade_for(score),
        sub_scores=subs,
        comparables=[
            Comparable(
                app_id=c["app_id"],
                name=c["name"],
                release_date=str(c["release_date"]) if c.get("release_date") else None,
                price_usd=float(c["price_usd"]) if c.get("price_usd") is not None else None,
                review_count=c.get("review_count"),
                positive_ratio=float(c["positive_ratio"])
                if c.get("positive_ratio") is not None
                else None,
                estimated_units=c.get("estimated_units"),
                estimation_method=c.get("estimation_method"),
                similarity=round(sim, 4),
            )
            for sim, c in comps
        ],
        comps_considered=len(comps),
        completeness=coverage,
        uncapped_score=uncapped,
        missing_fields=missing_load_bearing(profile),
        insufficient_information=False,
        assumptions=assumptions,
    )
