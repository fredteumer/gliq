"""Tests for the deterministic scoring rules.

These pin the BEHAVIOURS docs/SCORING.md commits to, not just arithmetic. Where
a decision was made against an obvious alternative — capping instead of
averaging, absolute instead of normalised — the test says why, so a future
change that quietly reverses it fails here rather than in a report.
"""

from datetime import date

import pytest

from components.scoring import rules
from shared.schemas import PitchProfile, PriceTier

TODAY = date(2026, 7, 20)


def comp(app_id=1, units=50_000, price=9.99, tags=None, genres=None, released="2025-01-01"):
    return {
        "app_id": app_id,
        "name": f"Comp {app_id}",
        "genres": genres if genres is not None else ["Strategy"],
        "tags": list((tags or {"Strategy": 100, "Turn-Based": 80}).keys()),
        "tag_votes": tags or {"Strategy": 100, "Turn-Based": 80},
        "platforms": ["Windows"],
        "release_date": date.fromisoformat(released) if released else None,
        "price_usd": price,
        "review_count": max(units // 30, 1),
        "positive_ratio": 0.85,
        "estimated_units": units,
        "estimation_method": "boxleiter-30x",
    }


def profile(**kw):
    base = dict(
        title="Test Pitch",
        primary_genre="Strategy",
        tags=["Strategy", "Turn-Based"],
        price_tier=PriceTier.P9_99,
        target_platforms=["Windows"],
        extracted_by="test",
    )
    base.update(kw)
    return PitchProfile(**base)


# ---------------------------------------------------------------- similarity


def test_tag_overlap_is_dominated_by_strongly_voted_tags():
    """A tag voted 200 times should outweigh one voted twice.

    This is the whole reason tag_votes is stored. Plain set overlap treats them
    identically, which is how ubiquitous tags like 'Indie' come to dominate a
    comp set.
    """
    strong = rules.tag_overlap(["Roguelike"], {"Roguelike": 200, "Indie": 2})
    weak = rules.tag_overlap(["Indie"], {"Roguelike": 200, "Indie": 2})
    assert strong > weak


def test_tag_overlap_does_not_punish_heavily_tagged_candidates():
    """Both sides are normalised, so vote VOLUME must not change the result.

    Without normalisation a popular title carrying thousands of votes would
    look less similar than a sparsely-tagged one purely because its denominator
    is larger — an artefact, not a signal.
    """
    small = rules.tag_overlap(["A", "B"], {"A": 10, "B": 10})
    large = rules.tag_overlap(["A", "B"], {"A": 10_000, "B": 10_000})
    assert small == pytest.approx(large)


def test_recency_decays_but_never_reaches_zero():
    """An old comp is weaker evidence, not no evidence."""
    fresh = rules.recency_factor(date(2025, 1, 1), TODAY)
    mid = rules.recency_factor(date(2020, 1, 1), TODAY)
    ancient = rules.recency_factor(date(2005, 1, 1), TODAY)
    assert fresh == 1.0
    assert 0.5 < mid < 1.0
    assert ancient == rules.RECENCY_FLOOR > 0


def test_unknown_release_date_is_treated_as_old():
    """Absent evidence of recency must not be rewarded as if it were recent."""
    assert rules.recency_factor(None, TODAY) == rules.RECENCY_FLOOR


# ----------------------------------------------------------------- subscores


def test_hit_rate_is_absolute_not_normalised():
    """A weak niche must score weakly, not average-for-the-corpus.

    Percentile-normalising was considered and rejected: most Steam releases do
    not clear 10k units, and a publisher passing on most of its inbox is the
    tool working. Normalising is grade inflation.
    """
    weak = rules.niche_hit_rate([100] * 95 + [50_000] * 5)
    assert weak == pytest.approx(5.0)


def test_sales_potential_uses_the_ceiling_not_the_typical_win():
    """Two niches with identical hit rates but different ceilings must differ.

    A median over a set already thresholded at >=10k is structurally stable,
    which is why p90 is used instead.
    """
    modest = [30_000] * 20
    blockbuster = [30_000] * 10 + [2_000_000] * 10
    assert rules.sales_potential(blockbuster) > rules.sales_potential(modest)


def test_a_single_outlier_does_not_move_p90():
    """p90 is a PERCENTILE, not a maximum — one breakout does not re-rate a niche.

    Deliberate: an acquisitions read should not be swung by the existence of a
    single Stardew Valley in an otherwise unremarkable space. If the intent were
    "how big is the biggest win", the statistic would be max().
    """
    flat = [30_000] * 20
    one_giant = [30_000] * 19 + [5_000_000]
    assert rules.sales_potential(one_giant) == rules.sales_potential(flat)


def test_a_niche_below_the_calibrated_floor_scores_zero():
    """Winners that only just clear the bar are not 'potential'.

    POTENTIAL_UNITS_FLOOR is the observed p05 of real comp sets, so a niche
    whose winners all sit beneath it is genuinely in the bottom few percent.
    """
    barely = [12_000] * 20  # clears SUCCESS_UNITS, under POTENTIAL_UNITS_FLOOR
    assert rules.sales_potential(barely) == 0.0


def test_sales_potential_is_zero_without_winners():
    assert rules.sales_potential([100, 200, 300]) == 0.0


def test_price_alignment_penalises_by_rung_not_dollars():
    """$9.99 -> $11.99 is one rung; the dollar gap understates the step."""
    comps = [9.99] * 10
    on_point = rules.price_alignment(PriceTier.P9_99, comps)
    one_rung = rules.price_alignment(PriceTier.P11_99, comps)
    way_off = rules.price_alignment(PriceTier.P49_99, comps)
    assert on_point == 100.0
    assert on_point > one_rung > way_off


def test_price_alignment_is_neutral_when_unknown():
    """No price on the pitch is a completeness problem, not a pricing one.

    Scoring it as 0 would punish the same omission twice — once here and again
    through the completeness cap.
    """
    assert rules.price_alignment(None, [9.99]) == rules.PRICE_UNKNOWN
    assert rules.price_alignment(PriceTier.P9_99, []) == rules.PRICE_UNKNOWN


# -------------------------------------------------------------- completeness


def test_completeness_ignores_report_only_fields():
    """title/summary/art_style do not enter the arithmetic, so they must not
    dilute the measure that caps it."""
    with_extras = profile(summary="a summary", art_style="pixel")
    without = profile(summary=None, art_style=None)
    assert rules.completeness(with_extras) == rules.completeness(without)


def test_missing_load_bearing_field_caps_the_grade():
    """Absent price_tier must LOWER the grade, never raise ValidationError."""
    p = profile(price_tier=None)
    ceiling, reason = rules.completeness_ceiling(p)
    assert ceiling == rules.CAP_D
    assert "price_tier" in reason


def test_caps_sit_just_below_their_grade_boundaries():
    """The caps and the grade thresholds must move together.

    They drifted apart once already: caps of 85/75/65 were written against
    thresholds of 90/80/70/60, and when those became 80/68/55/40 a "cap at D"
    silently became a cap at C.
    """
    assert rules.grade_for(rules.CAP_B) == "B"
    assert rules.grade_for(rules.CAP_C) == "C"
    assert rules.grade_for(rules.CAP_D) == "D"


def test_a_capped_pitch_reports_what_it_lost():
    """The report must show the cost of incompleteness, not absorb it."""
    strong = [comp(i, units=2_000_000) for i in range(30)]
    result = rules.score_pitch(profile(price_tier=None), strong, TODAY)
    assert result.score < result.uncapped_score
    assert result.score <= rules.CAP_D
    assert any("capped" in a.lower() for a in result.assumptions)


# ------------------------------------------------------------------ grading


def test_no_comp_basis_is_insufficient_information_not_a_low_score():
    """"We cannot evaluate this" and "we evaluated it and it is bad" both land
    on F but warrant different reports, so the flag is carried explicitly."""
    blank = PitchProfile(extracted_by="test")
    result = rules.score_pitch(blank, [comp()], TODAY)
    assert result.insufficient_information
    assert result.grade == "F"
    assert result.comps_considered == 0


def test_a_bad_niche_scores_f_without_claiming_ignorance():
    """The opposite case: comps exist, they are just bad."""
    losers = [comp(i, units=200) for i in range(40)]
    result = rules.score_pitch(profile(), losers, TODAY)
    assert result.grade == "F"
    assert not result.insufficient_information
    assert result.comps_considered > 0


def test_a_strong_niche_can_reach_the_top_of_the_scale():
    """If A were unreachable the tool could not flag the winners it exists for."""
    winners = [comp(i, units=3_000_000) for i in range(50)]
    result = rules.score_pitch(profile(), winners, TODAY)
    assert result.grade in {"A", "B"}
    assert result.score > 65


def test_estimation_method_is_always_disclosed():
    """AGENTS.md: no unit figure reaches a report without its method."""
    result = rules.score_pitch(profile(), [comp(i) for i in range(20)], TODAY)
    assert any("ESTIMATE" in a for a in result.assumptions)


def test_thin_comp_sets_are_flagged():
    result = rules.score_pitch(profile(), [comp(1)], TODAY)
    assert any("comparable" in a.lower() for a in result.assumptions)


def test_comps_are_returned_most_similar_first():
    """Component C renders these as evidence; order is the ranking."""
    mixed = [
        comp(1, tags={"Strategy": 100, "Turn-Based": 100}),
        comp(2, tags={"Strategy": 100, "Racing": 100}),
        comp(3, tags={"Strategy": 100, "Turn-Based": 100, "Hex Grid": 50}),
    ]
    result = rules.score_pitch(profile(), mixed, TODAY)
    sims = [c.similarity for c in result.comparables]
    assert sims == sorted(sims, reverse=True)


def test_grades_cover_the_whole_range_without_gaps():
    for score, expected in [(100, "A"), (80, "A"), (79.9, "B"), (68, "B"),
                            (67.9, "C"), (55, "C"), (54.9, "D"), (40, "D"),
                            (39.9, "F"), (0, "F")]:
        assert rules.grade_for(score) == expected, score
