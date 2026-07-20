"""Turning a fitment score into a decision — the pure half of Component C.

⚠️ Pure functions only. No database, no network, no Pub/Sub, no templates. The
same separation `components/scoring/rules.py` keeps, and for the same reason:
the decision logic is testable and reviewable without any of the transport
above it.

## What is deliberately NOT here

The grade → tier mapping. It already exists as `rules.TIER_FOR_GRADE` /
`rules.tier_for`, sits beside `GRADE_THRESHOLDS` in the module that owns them,
and is specified in docs/SCORING.md step 6. A second copy here would be a
second thing to forget when the thresholds move — which has already happened
once in this project, when the completeness caps silently became a grade too
generous. ➡️ docs/SCORING.md.

Importing across component packages is safe in exactly this direction:
`rules.py` depends on nothing but the standard library and `shared.schemas`, so
C picks up no database driver, no pandas, and no Pub/Sub client — none of which
the `reporting` extra installs.

## Rationale and actions are TEMPLATED, not generated

docs/SCORING.md step 0 puts "generated prose" in the Part 2 column. What this
module produces is assembled from the sub-scores by fixed rules, so the same
result always yields the same sentence — the reproducible control the LLM
version gets measured against.
"""

from __future__ import annotations

from components.scoring.rules import MIN_COMPS_WARN, tier_for
from shared.schemas import FitmentResult, InvestmentTier, PitchProfile, Recommendation

#: Below this, `price_alignment` is saying the asking price sits far from what
#: the comp set charges. It is a guard rail rather than a predictor, so it only
#: earns a de-risk action when it is genuinely out of line.
PRICE_CONCERN = 50.0

#: Below this, most comparable titles fail to clear the success bar. The niche
#: itself is the risk, not the execution.
HIT_RATE_CONCERN = 35.0

#: Below this, even the successful comps in this niche are small. A pitch can be
#: perfectly executed here and still not return.
POTENTIAL_CONCERN = 35.0


def rationale_for(profile: PitchProfile, result: FitmentResult) -> str:
    """One paragraph explaining the grade in terms of what drove it.

    Leads with the strongest and weakest weighted signals, because "C, 58.3" is
    not a reason and an acquisitions read has to be defensible to leadership.

    ⚠️ `differentiation` is excluded from this sentence. It carries weight 0.00,
    so naming it as a driver of the grade would be false.
    """
    if result.insufficient_information:
        return (
            "This pitch could not be evaluated. No genre and no tags were extracted "
            "from the document, so there is no basis for a comparable set — this is "
            "an absence of evidence, not evidence of a weak game."
        )

    weighted = {
        "market hit rate": result.sub_scores.niche_hit_rate,
        "sales potential": result.sub_scores.sales_potential,
        "price alignment": result.sub_scores.price_alignment,
    }
    strongest = max(weighted, key=lambda k: weighted[k])
    weakest = min(weighted, key=lambda k: weighted[k])

    parts = [
        f"Scored {result.score:.1f} ({result.grade}) against {result.comps_considered} "
        f"comparable title(s) drawn from the Steam corpus.",
        f"The strongest signal is {strongest} at {weighted[strongest]:.0f}; "
        f"the weakest is {weakest} at {weighted[weakest]:.0f}.",
    ]

    if result.score < result.uncapped_score:
        parts.append(
            f"⚠️ The grade is capped: an uncapped {result.uncapped_score:.1f} was "
            f"reduced to {result.score:.1f} because the submission was incomplete."
        )
    if result.comps_considered < MIN_COMPS_WARN:
        parts.append(
            "⚠️ Few comparables cleared the similarity floor, so this is a weak "
            "market read rather than a confident one."
        )
    return " ".join(parts)


def de_risk_actions(profile: PitchProfile, result: FitmentResult) -> list[str]:
    """Concrete things that would change the answer.

    Every action is derived from a value in the result rather than drawn from a
    canned list, so an action only appears when the data supports it. An empty
    list is a legitimate outcome and means the pitch has no identified,
    addressable weakness — which is not the same as it being a good bet.
    """
    actions: list[str] = []

    if result.insufficient_information:
        return [
            "Resubmit with an explicit genre, or with tags describing the game's "
            "mechanics and setting — without either there is nothing to compare against.",
        ]

    if result.missing_fields:
        fields = ", ".join(result.missing_fields)
        actions.append(
            f"Supply the missing field(s): {fields}. Their absence caps the grade "
            f"regardless of how the pitch scores on the market."
        )

    if result.completeness < 1.0:
        actions.append(
            f"Complete the submission — {result.completeness:.0%} of the scoring-relevant "
            f"fields were extracted. Uncapped, this pitch scored {result.uncapped_score:.1f}."
        )

    if result.sub_scores.price_alignment < PRICE_CONCERN:
        asking = profile.price_tier.price_point if profile.price_tier else None
        asking_text = f"The asking price is ${asking:.2f}. " if asking is not None else ""
        actions.append(
            f"Revisit pricing. {asking_text}Price alignment scored "
            f"{result.sub_scores.price_alignment:.0f} against the comparable set, meaning "
            f"the asking price sits well away from what titles in this niche charge."
        )

    if result.sub_scores.niche_hit_rate < HIT_RATE_CONCERN:
        actions.append(
            f"Reconsider the niche. Only a small share of comparable titles clear the "
            f"success threshold (hit rate {result.sub_scores.niche_hit_rate:.0f}), so the "
            f"risk here is the market rather than the execution."
        )

    if result.sub_scores.sales_potential < POTENTIAL_CONCERN:
        actions.append(
            f"Check the ceiling. Even successful comparables in this niche move modest "
            f"volumes (sales potential {result.sub_scores.sales_potential:.0f}); a strong "
            f"execution may still not return the requested budget."
        )

    if result.comps_considered < MIN_COMPS_WARN:
        actions.append(
            f"Broaden or sharpen the pitch's tags — only {result.comps_considered} "
            f"comparable(s) cleared the similarity floor, which makes every figure in "
            f"this report a weak estimate."
        )

    return actions


def recommend(profile: PitchProfile, result: FitmentResult) -> Recommendation:
    """The decision: tier, why, and what would change it."""
    return Recommendation(
        tier=tier_for(result.grade),
        rationale=rationale_for(profile, result),
        de_risk_actions=de_risk_actions(profile, result),
    )


#: Re-exported so the renderer can label the tier without importing the enum
#: from two places.
TIER_LABELS: dict[InvestmentTier, str] = {
    InvestmentTier.GREENLIGHT: "Greenlight",
    InvestmentTier.CONDITIONAL: "Conditional",
    InvestmentTier.DE_RISK: "De-risk",
    InvestmentTier.PASS: "Pass",
}
