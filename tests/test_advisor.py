"""Behaviours the LLM analyst commits to.

The analyst is a subjective second opinion, so almost nothing here checks *what*
it concludes — that is the model's job. What is pinned instead is the contract
around it: that it never touches the deterministic grade, that a missing key or
a broken call DEGRADES rather than breaks the pipeline, that the model is asked
to read the actual prose and the deterministic figures, and that provenance is
stamped by us rather than trusted from the model.

⚠️ No live API call anywhere. The Gemini and Anthropic clients are stubbed, so
this runs offline like the rest of the suite.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from components.reporting.advisor import (
    SYSTEM_PROMPT,
    AnthropicAdvisor,
    GeminiAdvisor,
    _AnalystOutput,
    _clean_grade,
    _finalise,
    get_advisor,
)
from components.reporting.recommend import recommend
from components.reporting.render import render_report
from shared.config import Config
from shared.schemas import (
    AdvisoryOpinion,
    AdvisoryStance,
    Comparable,
    FitmentResult,
    InvestmentTier,
    PitchProfile,
    PriceTier,
    SubScores,
)

DOCUMENT = "# Hollow Reef\n\nA hand-drawn 2D metroidvania. No microtransactions. Priced at $19.99."


def config(**over) -> Config:
    """A Config with sane defaults, overriding only what a test cares about."""
    base = dict(
        gcp_project="p", gcp_region="us",
        topic_scoring_requested="t", sub_scoring_requested="s",
        topic_scoring_completed="t2", sub_scoring_completed="s2",
        dead_letter_topic="dl", artifacts_bucket="",
        db_host="h", db_port=5432, db_name="d", db_user="u", db_password="",
        redis_host="r", redis_port=6379, cache_ttl_seconds=3600,
        llm_provider="deterministic", session_secret="", admin_password_hash="",
        admin_user="admin", advisor_provider="fixture",
        gemini_api_key="", gemini_model="gemini-flash-lite-latest",
        anthropic_api_key="", anthropic_model="claude-sonnet-5",
    )
    base.update(over)
    return Config(**base)


def profile(**kw) -> PitchProfile:
    base = dict(title="Hollow Reef", primary_genre="Action", tags=["Metroidvania", "2D"],
                price_tier=PriceTier.P19_99, extracted_by="deterministic")
    base.update(kw)
    return PitchProfile(**base)


def result(**kw) -> FitmentResult:
    base = dict(
        score=46.7, grade="D",
        sub_scores=SubScores(niche_hit_rate=56, sales_potential=39.6,
                             differentiation=50.8, price_alignment=37.5),
        comparables=[Comparable(app_id=1, name="Voidwrought", price_usd=6.79,
                                estimated_units=18_330, similarity=0.56)],
        comps_considered=50, completeness=1.0, uncapped_score=46.7,
        scored_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    base.update(kw)
    return FitmentResult(**base)


def analyst_json(**over) -> str:
    """A plausible model response, as the JSON string the provider would parse."""
    fields = dict(category="premium 2D metroidvania", grade="B", tier="greenlight",
                  rationale="$19.99 is correct for the segment.", stance="dissents",
                  key_points=["shipped team", "priced right"], confidence=0.72)
    fields.update(over)
    return _AnalystOutput(**fields).model_dump_json()


# ----------------------------------------------------------------- selection


def test_selection_covers_every_provider_and_rejects_unknown():
    assert get_advisor(config(advisor_provider="fixture")).name == "fixture"
    assert get_advisor(config(advisor_provider="gemini")).name == "gemini"
    assert get_advisor(config(advisor_provider="anthropic")).name == "anthropic"
    with pytest.raises(RuntimeError, match="gemini"):  # the message lists the valid set
        get_advisor(config(advisor_provider="gpt5"))


def test_construction_never_needs_a_key():
    """⚠️ A missing key must not stop Component C from STARTING.

    The key is only touched in `opine`, which degrades — so a misconfigured key
    is a per-request degradation, not a crash-on-boot.
    """
    get_advisor(config(advisor_provider="gemini", gemini_api_key=""))
    get_advisor(config(advisor_provider="anthropic", anthropic_api_key=""))


# ------------------------------------------------------------------- fixture


def test_the_fixture_opinion_is_valid_and_self_disclosing():
    """It returns a real AdvisoryOpinion and labels itself so a report can tell.

    `model="fixture"` is how the rendered report discloses that no live model
    was consulted — the fixture is for offline tests and no-key deploys, never a
    real read.
    """
    op = get_advisor(config()).opine(DOCUMENT, profile(), result())
    assert isinstance(op, AdvisoryOpinion)
    assert op.model == "fixture"


# --------------------------------------------------------------- degradation


def test_a_missing_key_degrades_to_none_rather_than_raising():
    """🔒 The analyst is decision support, never a dependency of the pipeline."""
    assert GeminiAdvisor(config(gemini_api_key="")).opine(DOCUMENT, profile(), result()) is None
    assert AnthropicAdvisor(config(anthropic_api_key="")).opine(DOCUMENT, profile(), result()) is None


def test_a_broken_provider_call_degrades_to_none():
    """A rate limit, a network drop, an unparseable reply — all → None, logged.

    Component C then renders the full deterministic report; a pitch is always
    reported even when the model is down.
    """
    with patch("openai.OpenAI", side_effect=RuntimeError("network down")):
        assert GeminiAdvisor(config(gemini_api_key="k")).opine(DOCUMENT, profile(), result()) is None

    garbage = MagicMock()
    garbage.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="not json at all"))]
    )
    with patch("openai.OpenAI", return_value=garbage):
        assert GeminiAdvisor(config(gemini_api_key="k")).opine(DOCUMENT, profile(), result()) is None


# ------------------------------------------------------- the request it builds


def test_the_gemini_request_reads_the_prose_and_the_deterministic_figures():
    """⚠️ Built without a live call. The whole point is that the model READS.

    It must receive the raw document (the extractor cannot see negation like
    "no microtransactions") and the deterministic result to react to.
    """
    fake = MagicMock()
    fake.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=analyst_json()))]
    )
    with patch("openai.OpenAI", return_value=fake) as ctor:
        op = GeminiAdvisor(config(gemini_api_key="k", gemini_model="gemini-flash-lite-latest")).opine(
            DOCUMENT, profile(), result()
        )

    ctor.assert_called_once()
    assert ctor.call_args.kwargs["api_key"] == "k"
    call = fake.chat.completions.create.call_args
    assert call.kwargs["model"] == "gemini-flash-lite-latest"
    assert call.kwargs["response_format"] == {"type": "json_object"}
    user_turn = call.kwargs["messages"][-1]["content"]
    assert "no microtransactions" in user_turn.lower()       # the prose
    assert "Grade: D" in user_turn                            # the deterministic read
    assert "Voidwrought" in user_turn                         # a comparable to react to
    assert op is not None and op.grade == "B"


def test_the_system_prompt_frames_the_four_lenses_and_the_analyst_edge():
    for lens in ("hit rate", "Sales potential", "Differentiation", "Price alignment"):
        assert lens.lower() in SYSTEM_PROMPT.lower(), lens
    # its stated edge over the deterministic system: it reads the prose
    assert "read" in SYSTEM_PROMPT.lower()
    assert "cannot measure" in SYSTEM_PROMPT.lower()  # differentiation is the LLM's contribution


# ------------------------------------------------------------ output handling


def test_provenance_is_stamped_by_us_not_trusted_from_the_model():
    """The model returns its opinion; WE record which model produced it.

    A future panel of several models must be poolable without ambiguity about
    the source, so `model` is set from config, never from the model's output.
    """
    op = _finalise(_AnalystOutput.model_validate_json(analyst_json()), "gemini:x")
    assert op.model == "gemini:x"
    assert op.stance is AdvisoryStance.DISSENTS
    assert op.tier is InvestmentTier.GREENLIGHT


def test_grade_is_coerced_to_a_letter_without_grabbing_word_letters():
    """"a solid B" is B, not A; "Grade: C" is C, not the A in GRADE."""
    cases = {"B": "B", "B+": "B", "b": "B", "a solid B": "B", "Grade: C": "C", "F": "F"}
    for raw, want in cases.items():
        assert _clean_grade(raw) == want, raw


def test_the_anthropic_path_returns_a_finalised_opinion():
    fake = MagicMock()
    fake.messages.parse.return_value = MagicMock(
        parsed_output=_AnalystOutput.model_validate_json(analyst_json())
    )
    with patch("anthropic.Anthropic", return_value=fake):
        op = AnthropicAdvisor(config(anthropic_api_key="k", anthropic_model="claude-sonnet-5")).opine(
            DOCUMENT, profile(), result()
        )
    assert op is not None and op.model == "anthropic:claude-sonnet-5"
    assert fake.messages.parse.call_args.kwargs["model"] == "claude-sonnet-5"


# ------------------------------------------------ the report never bends to it


def test_a_dissenting_opinion_never_overrides_the_deterministic_grade():
    """The two verdicts stand side by side. This is the load-bearing guarantee.

    A B/greenlight analyst opinion on a deterministic D pitch must leave the
    header grade D and label the analyst section as separate and subjective.
    """
    prof, res = profile(), result(grade="D", score=46.7)
    opinion = AdvisoryOpinion(category="premium metroidvania", grade="B",
                              tier=InvestmentTier.GREENLIGHT, rationale="price is right",
                              stance=AdvisoryStance.DISSENTS, key_points=["shipped team"],
                              confidence=0.72, model="gemini:x")
    md = render_report("demo", prof, res, recommend(prof, res), opinion)

    assert "Grade **D**" in md                              # header unchanged
    assert "Analyst second opinion — B / Greenlight" in md  # the analyst's own verdict
    assert "separate from the grade above" in md
    assert "does **not** change the letter grade" in md
    assert "dissents" in md


def test_no_opinion_omits_the_section_and_leaves_the_report_whole():
    prof, res = profile(), result()
    md = render_report("demo", prof, res, recommend(prof, res), None)
    assert "Analyst second opinion" not in md
    assert "Fitment breakdown" in md  # the deterministic report is intact
