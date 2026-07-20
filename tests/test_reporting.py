"""Behaviours Component C's report commits to.

The report is the only artefact a publisher actually reads, so most of what is
pinned here is about honesty rather than correctness: that an estimate is
labelled as one, that a signal carrying no weight is not presented as if it
does, that "we cannot evaluate this" never renders as "this is bad", and that no
dollar figure is ever derived from an estimated unit count.

Where a decision was made against an obvious alternative — importing the tier
map instead of restating it, computing the closest-comp similarity from the
comps instead of un-inverting a sub-score — the test says why, so a future
change that quietly reverses it fails here rather than in front of a producer.
"""

from datetime import datetime, timezone

from components.reporting.recommend import (
    HIT_RATE_CONCERN,
    PRICE_CONCERN,
    de_risk_actions,
    recommend,
)
from components.reporting.render import STANDING_CAVEATS, mean_closest_similarity, render_report
from components.scoring.rules import TIER_FOR_GRADE
from shared.schemas import Comparable, FitmentResult, PitchProfile, PriceTier, SubScores

PITCH_ID = "11111111-2222-3333-4444-555555555555"
SCORED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def profile(**kw) -> PitchProfile:
    base = dict(
        title="Test Pitch",
        primary_genre="Action",
        sub_genres=["Indie"],
        tags=["Metroidvania", "2D"],
        core_mechanics=["Exploration"],
        price_tier=PriceTier.P19_99,
        target_platforms=["Windows"],
        extracted_by="deterministic",
    )
    base.update(kw)
    return PitchProfile(**base)


def comp(app_id=1, name="Comp Title", similarity=0.55, units=50_000, price=9.99) -> Comparable:
    return Comparable(
        app_id=app_id,
        name=name,
        release_date="2024-01-01",
        price_usd=price,
        review_count=1_000,
        positive_ratio=0.9,
        estimated_units=units,
        estimation_method="boxleiter",
        similarity=similarity,
    )


def result(**kw) -> FitmentResult:
    subs = kw.pop("sub_scores", None) or SubScores(
        niche_hit_rate=60.0, sales_potential=55.0, differentiation=50.0, price_alignment=70.0
    )
    base = dict(
        score=62.0,
        grade="C",
        sub_scores=subs,
        comparables=[comp(app_id=i, name=f"Comp {i}", similarity=0.6 - i / 100) for i in range(6)],
        comps_considered=6,
        completeness=1.0,
        uncapped_score=62.0,
        missing_fields=[],
        insufficient_information=False,
        assumptions=["Units are ESTIMATES (Boxleiter review-count multiplier)."],
        scored_at=SCORED_AT,
    )
    base.update(kw)
    return FitmentResult(sub_scores=subs, **{k: v for k, v in base.items() if k != "sub_scores"})


def report_for(prof, res) -> str:
    return render_report(PITCH_ID, prof, res, recommend(prof, res))


# ----------------------------------------------------------------------- tier


def test_tier_comes_from_the_scoring_module_not_a_second_copy():
    """The grade -> tier map must have exactly one definition.

    `rules.TIER_FOR_GRADE` sits beside `GRADE_THRESHOLDS`, which is what it is
    derived from. A copy here would be a second thing to forget when the
    thresholds move — which already happened once in this project, when the
    completeness caps silently became a grade too generous.
    """
    for grade, expected in TIER_FOR_GRADE.items():
        got = recommend(profile(), result(grade=grade)).tier
        assert got is expected, grade


# --------------------------------------------------------------------- honesty


def test_differentiation_is_labelled_as_not_affecting_the_grade():
    """It carries weight 0.00 and must never read as a scoring signal.

    Validation found no separation between winners and random titles, because
    tags cannot measure how different a game is. Showing the number without the
    caveat would imply the grade responds to it.
    """
    md = report_for(profile(), result())
    assert "unweighted" in md
    assert "does not affect the grade" in md


def test_the_closest_comp_similarity_is_computed_from_the_comps():
    """⚠️ NOT by un-inverting `differentiation`.

    That sub-score is normalised against CROWDING_FLOOR/CEILING, so
    `1 - differentiation/100` is not the crowding figure. An earlier draft did
    exactly that and printed 0.49 where the true mean was 0.55 — a fabricated
    number presented as "a checkable statement".
    """
    comps = [comp(app_id=i, similarity=s) for i, s in enumerate([0.9, 0.8, 0.7, 0.6, 0.5, 0.1])]
    res = result(comparables=comps, sub_scores=SubScores(
        niche_hit_rate=60.0, sales_potential=55.0, differentiation=50.0, price_alignment=70.0
    ))
    # Mean of the closest CROWDING_SAMPLE (5), not of all six.
    assert mean_closest_similarity(res) == 0.7
    assert "0.70" in report_for(profile(), res)


def test_estimation_disclosure_and_standing_caveats_reach_the_report():
    """AGENTS.md: every report must disclose the estimation method.

    B emits the per-run disclosure; the corpus-level caveats are standing
    properties that docs/SCORING.md also requires be disclosed. Both appear,
    and the per-run ones are never replaced by the standing ones.
    """
    md = report_for(profile(), result())
    assert "Units are ESTIMATES" in md
    for caveat in STANDING_CAVEATS:
        assert caveat in md


def test_the_report_never_derives_revenue_from_units():
    """⚠️ Units moved, never revenue.

    Multiplying an estimated unit count by list price stacks a second guess on
    the first and produces a figure that looks precise and is not. The only
    dollar figures allowed are prices, which are observed rather than inferred.
    """
    units, price = 50_000, 9.99
    md = report_for(profile(), result(comparables=[comp(units=units, price=price)]))
    for forbidden in (f"{units * price:,.2f}", f"{units * price:,.0f}", "revenue of", "grossed"):
        assert forbidden not in md, forbidden
    assert "never revenue" in md


# ------------------------------------------------- insufficient information


def test_insufficient_information_renders_a_different_report_not_a_low_grade():
    """"We cannot evaluate this" and "we evaluated it and it is bad" must not merge.

    They warrant different responses from a publisher, and only one of them is
    fixed by resubmitting.
    """
    res = result(
        score=0.0,
        grade="F",
        comparables=[],
        comps_considered=0,
        uncapped_score=0.0,
        insufficient_information=True,
        sub_scores=SubScores(
            niche_hit_rate=0.0, sales_potential=0.0, differentiation=0.0, price_alignment=0.0
        ),
    )
    md = report_for(profile(primary_genre=None, tags=[]), res)

    assert "Not evaluated" in md
    assert "absence of evidence" in md
    # The scoring furniture must be absent — showing zeroed sub-scores would
    # read as "it scored zero on everything", which is the wrong claim.
    assert "Fitment breakdown" not in md
    assert "Comparable titles" not in md


def test_an_unevaluable_pitch_is_told_how_to_become_evaluable():
    res = result(insufficient_information=True, comparables=[], comps_considered=0)
    actions = de_risk_actions(profile(primary_genre=None, tags=[]), res)
    assert len(actions) == 1
    assert "Resubmit" in actions[0]


# ------------------------------------------------------------ de-risk actions


def test_actions_respond_to_the_data_rather_than_being_canned():
    """An action appears only when the result supports it."""
    healthy = result(sub_scores=SubScores(
        niche_hit_rate=80.0, sales_potential=80.0, differentiation=50.0, price_alignment=90.0
    ))
    assert de_risk_actions(profile(), healthy) == []

    mispriced = result(sub_scores=SubScores(
        niche_hit_rate=80.0, sales_potential=80.0,
        differentiation=50.0, price_alignment=PRICE_CONCERN - 1,
    ))
    assert any("pricing" in a.lower() for a in de_risk_actions(profile(), mispriced))

    bad_niche = result(sub_scores=SubScores(
        niche_hit_rate=HIT_RATE_CONCERN - 1, sales_potential=80.0,
        differentiation=50.0, price_alignment=90.0,
    ))
    assert any("niche" in a.lower() for a in de_risk_actions(profile(), bad_niche))


def test_a_capped_pitch_is_told_what_the_cap_cost_it():
    """The report shows the cost of an incomplete submission, per docs/SCORING.md."""
    res = result(score=54.0, grade="D", uncapped_score=71.0, completeness=0.5,
                 missing_fields=["price_tier"])
    actions = de_risk_actions(profile(), res)
    assert any("price_tier" in a for a in actions)
    assert any("71.0" in a for a in actions)


# ----------------------------------------------------------------- rendering


def test_comparables_are_rendered_in_the_order_received():
    """Order IS the ranking — B sorts by similarity and C must not re-sort."""
    comps = [comp(app_id=i, name=f"Comp {i}", similarity=0.6 - i / 100) for i in range(4)]
    md = report_for(profile(), result(comparables=comps, comps_considered=4))
    positions = [md.index(f"Comp {i}") for i in range(4)]
    assert positions == sorted(positions)


def test_untrusted_text_cannot_break_the_table_or_carry_html():
    """Titles come from a producer's document and comp names from Steam.

    A single `|` silently destroys a table row, and the stored Markdown is
    rendered to HTML by a UI later. Escaping happens here so the artefact is
    safe on its own rather than depending on every future consumer.
    """
    nasty = "Evil | <script>alert(1)</script> | Game"
    md = report_for(profile(title=nasty), result(comparables=[comp(name=nasty)]))
    assert "<script>" not in md
    # The title line must remain a single line with no injected cell breaks.
    title_line = md.splitlines()[0]
    assert title_line.startswith("# ")
    assert "|" not in title_line


def test_an_empty_comp_set_does_not_break_the_template():
    """A scored pitch can still have zero comps clear the floor."""
    md = report_for(profile(), result(comparables=[], comps_considered=0))
    assert "No comparable titles" in md
