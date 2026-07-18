"""Contract tests for the messages exchanged between components.

These guard the interface between the three processes: if a message stops
round-tripping, one component will silently fail to understand another.
"""

from uuid import uuid4

from shared.schemas import (
    Comparable,
    FitmentResult,
    InvestmentTier,
    PitchProfile,
    PriceTier,
    ScoringCompleted,
    ScoringRequested,
    SubScores,
)


def _profile() -> PitchProfile:
    return PitchProfile(
        title="Hollow Reef",
        primary_genre="Metroidvania",
        sub_genres=["Action", "Adventure"],
        tags=["2D", "Hand-drawn", "Exploration"],
        core_mechanics=["double jump", "ability gating"],
        art_style="hand-drawn 2D",
        price_tier=PriceTier.STANDARD,
        target_platforms=["Windows", "Switch"],
        extracted_by="fixture",
    )


def _result() -> FitmentResult:
    return FitmentResult(
        score=78.5,
        grade="B",
        sub_scores=SubScores(
            market_saturation=62.0,
            niche_hit_rate=81.0,
            sales_potential=74.0,
            price_alignment=97.0,
        ),
        comparables=[
            Comparable(
                app_id=367520,
                name="Hollow Knight",
                review_count=180_000,
                positive_ratio=0.97,
                estimated_units=5_400_000,
                estimation_method="boxleiter-30x",
                similarity=0.91,
            )
        ],
        comps_considered=412,
        assumptions=["Units estimated from review counts; Steam does not publish sales."],
    )


def test_scoring_requested_round_trips():
    msg = ScoringRequested(profile=_profile())
    assert ScoringRequested.model_validate_json(msg.model_dump_json()) == msg


def test_scoring_completed_round_trips():
    msg = ScoringCompleted(pitch_id=uuid4(), profile=_profile(), result=_result())
    assert ScoringCompleted.model_validate_json(msg.model_dump_json()) == msg


def test_estimated_units_carry_their_method():
    """An estimate without a disclosed method must never reach a report."""
    for comp in _result().comparables:
        if comp.estimated_units is not None:
            assert comp.estimation_method, f"{comp.name} has units but no estimation_method"


def test_investment_tiers_are_stable():
    assert {t.value for t in InvestmentTier} == {
        "greenlight",
        "conditional",
        "de_risk",
        "pass",
    }
