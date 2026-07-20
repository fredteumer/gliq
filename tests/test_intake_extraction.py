"""Behaviours Component A's deterministic extractor commits to.

The extractor's job is not to be clever, it is to emit values the corpus can
actually match. Almost everything here guards that: a tag with the wrong casing,
an invented platform, or a genre that only exists in the extractor's head does
not score badly — it fails to RETRIEVE, and the pitch is scored against the
wrong comp set or none at all, with no error anywhere to say so.

Where a decision was made against an obvious alternative — inferring genres from
tags instead of matching them literally, emitting compound tags AND their
constituents — the test says why, so a future change that quietly reverses it
fails here rather than in a report.
"""

from pathlib import Path

import pytest

from components.intake import vocabulary as vocab
from components.intake.extract import (
    MAX_SUB_GENRES,
    NON_GENRE_LABELS,
    extract,
    extract_price_tier,
    extract_title,
)
from components.intake.providers import get_extractor
from shared.schemas import COVERAGE_FIELDS, LOAD_BEARING_FIELDS, PriceTier

REPO_ROOT = Path(__file__).resolve().parents[1]

STRONG = (REPO_ROOT / "samples" / "strong-pitch.md").read_text()
WEAK = (REPO_ROOT / "samples" / "weak-pitch.md").read_text()

#: Both shipped samples, so a case that must hold for any complete document is
#: written once and checked against both.
SAMPLES = (("strong", STRONG), ("weak", WEAK))


def unmatchable(profile) -> list[str]:
    """Values the corpus cannot match — the failure this suite exists to catch."""
    bad = [t for t in profile.tags if t not in vocab.TAG_COUNTS]
    bad += [g for g in profile.sub_genres if g not in vocab.GENRE_COUNTS]
    bad += [p for p in profile.target_platforms if p not in vocab.PLATFORM_COUNTS]
    if profile.primary_genre and profile.primary_genre not in vocab.GENRE_COUNTS:
        bad.append(profile.primary_genre)
    return bad


# ----------------------------------------------------------------- vocabulary


def test_every_extracted_value_exists_in_the_corpus():
    """The load-bearing guarantee of the whole component.

    `rules.tag_overlap` intersects raw `set[str]` and `corpus.fetch_candidates`
    prefilters with the Postgres `&&` operator, both exact and case-sensitive.
    One character of drift costs the pitch its comp set silently.
    """
    for name, document in SAMPLES:
        assert unmatchable(extract(document)) == [], name


def test_aliases_resolve_to_the_corpus_spelling():
    """Nobody writes "Rogue-like" in a design document; they write "roguelike"."""
    tags = extract("A roguelike deckbuilder with science fiction themes").tags
    assert "Rogue-like" in tags
    assert "Deckbuilding" in tags
    assert "Sci-fi" in tags


def test_a_compound_tag_also_emits_its_constituents():
    """Non-consuming matching, decided by measurement rather than taste.

    Corpus titles tagged `Action-Adventure` also carry `Action` 78% of the time
    and `Adventure` 77%. Longest-match-wins would emit only the compound and
    lose overlap with the very comps the pitch most resembles.
    """
    tags = extract("An action adventure game").tags
    assert {"Action-Adventure", "Action", "Adventure"} <= set(tags)


def test_matching_is_by_token_not_substring():
    """`Dog` must not fire on "dogged", nor `War` on "warehouse".

    The corpus vocabulary contains short common words (`Dog`, `Elf`, `War`,
    `Jet`), so substring matching would tag most documents with nonsense.
    """
    tags = extract("A dogged protagonist explores a warehouse").tags
    assert "Dog" not in tags
    assert "War" not in tags


# ---------------------------------------------------------------------- genre


def test_genre_is_inferred_from_tags_when_the_document_names_none():
    """A design document describes a game; it does not file it.

    The weak sample calls itself "a free-to-play 100-player battle royale
    shooter" — which names no Steam genre at all, since `Shooter` and `Battle
    Royale` are tags. Literal matching yields only `Free To Play`. The corpus
    supplies the answer instead: 91.6% of `Shooter` titles are filed as Action.
    """
    assert extract(WEAK).primary_genre == "Action"


def test_a_business_model_label_is_never_the_primary_genre():
    """`Free To Play` and `Indie` are true of the weak pitch and say nothing.

    `Indie` alone matches 59,379 corpus titles — a comp set too broad to
    discriminate. They remain eligible as sub-genres, where they are real
    information.
    """
    profile = extract(WEAK)
    assert profile.primary_genre not in NON_GENRE_LABELS
    assert "Free To Play" in profile.sub_genres


def test_sub_genres_stay_within_the_corpus_shape():
    """Corpus titles carry 2-3 genres.

    `rules.similarity` scores sub-genres by Jaccard, whose denominator is the
    UNION — so an over-broad pitch is penalised against every comp it meets.
    More genres is not more matching.
    """
    assert len(extract(STRONG).sub_genres) <= MAX_SUB_GENRES


# ---------------------------------------------------------------------- price


def test_the_funding_ask_is_not_read_as_the_price():
    """Both samples quote a budget before they quote a price.

    "Requested funding: $850,000" precedes "Intended price $19.99", so a naive
    first-dollar-figure rule reads the budget as the asking price and prices the
    pitch off the top of the ladder.
    """
    assert extract_price_tier(STRONG) is PriceTier.P19_99


def test_free_to_play_wins_over_a_dollar_figure():
    """The weak pitch's only dollar figure is its $4,200,000 budget.

    Free-to-play is checked first and independently, because a F2P pitch still
    asks for money — just not from the player.
    """
    assert extract_price_tier(WEAK) is PriceTier.FREE


def test_price_ladder_rungs_come_from_the_shared_enum():
    """Reuses `PriceTier.from_price` rather than reimplementing the ladder."""
    cases = [
        ("Priced at $14.99", PriceTier.P14_99),
        ("It will retail for $29.99", PriceTier.P29_99),
        ("No price is given anywhere", None),
    ]
    for text, expected in cases:
        assert extract_price_tier(text) is expected, text


# ------------------------------------------------------------------ platforms


def test_console_platforms_are_dropped_rather_than_invented():
    """Steam knows exactly three platforms; a `Switch` value matches nothing.

    ⚠️ This is a real loss — the strong pitch ships on Switch — but emitting an
    unmatchable value would zero the platform term against every comp, which is
    worse than scoring against the subset Steam can see.
    """
    profile = extract(STRONG)
    assert profile.target_platforms == ["Windows"]
    assert not {"Switch", "Nintendo Switch", "PlayStation"} & set(profile.target_platforms)


def test_pc_maps_onto_windows():
    """The weak pitch says "PC and consoles"; the corpus says "Windows"."""
    assert extract("Shipping on PC").target_platforms == ["Windows"]


# --------------------------------------------------------------- completeness


def test_a_full_pitch_populates_every_scoring_field():
    """Any missing load-bearing field hard-caps the grade at D.

    Both samples are complete documents, so a cap here would be the extractor's
    failure and not the pitch's.
    """
    for name, document in SAMPLES:
        profile = extract(document)
        assert [f for f in COVERAGE_FIELDS if not getattr(profile, f)] == [], name
        assert [f for f in LOAD_BEARING_FIELDS if not getattr(profile, f)] == [], name


def test_title_strips_the_document_boilerplate():
    """"Hollow Reef", not "Hollow Reef — Game Design Document".

    `title` is load-bearing and goes on the report as the title of record.
    """
    assert extract_title(STRONG) == "Hollow Reef"
    assert extract_title("# Neon Drift - Design Document") == "Neon Drift"
    assert extract_title("no heading here") is None


def test_the_summary_is_the_concept_paragraph():
    """Both samples open their concept with an italicised title.

    ⚠️ `*Hollow Reef* is a hand-drawn...` reads as a markdown bullet unless the
    bullet pattern requires a following space, and the concept paragraph — the
    one paragraph describing the game — gets skipped for whatever prose is next.
    This fired on 2 of 2 samples.
    """
    summary = extract(STRONG).summary
    assert summary is not None
    assert summary.startswith("Hollow Reef is a hand-drawn 2D metroidvania")


# ------------------------------------------------------------- thin documents


def test_a_thin_document_returns_a_profile_rather_than_raising():
    """The decisive behaviour of the whole component.

    `PitchProfile` is optional by design: a required field would mean a sparse
    pitch raises in Component A and never reaches scoring, which is exactly the
    outcome the completeness cap exists to avoid. A thin pitch must earn a low
    grade, not a stack trace. ➡️ docs/SCORING.md
    """
    for document in ("", "   ", "# Untitled", "zzz qqq", "\n\n\n"):
        profile = extract(document)
        assert profile.extracted_by == "deterministic", repr(document)
        assert unmatchable(profile) == [], repr(document)


def test_a_document_matching_nothing_leaves_no_comp_basis():
    """`rules.has_comp_basis` reads exactly this shape as insufficient information.

    Distinct from a low score — "we cannot evaluate this" and "we evaluated it
    and it is bad" warrant different reports.
    """
    profile = extract("zzz qqq")
    assert not profile.primary_genre
    assert not profile.tags


# ------------------------------------------------------------------ providers


def test_the_fixture_provider_ignores_the_document():
    """That is the point of it: a constant input for testing B and C.

    It is NOT a substitute for the deterministic extractor — same profile
    regardless of input makes it useless for testing extraction itself.
    """
    fixture = get_extractor("fixture")
    assert fixture.extract(STRONG) == fixture.extract("something else entirely")


def test_the_fixture_profile_is_made_of_real_corpus_values():
    """A fixture that cannot retrieve candidates makes B look broken when it is fine."""
    assert unmatchable(get_extractor("fixture").extract("")) == []


def test_part_two_providers_fail_at_selection_not_at_request_time():
    """A misconfigured provider must kill startup, where systemd reports it.

    Failing on the first producer's submission instead would surface as a 500
    long after the deploy that caused it.
    """
    for name in ("anthropic", "gemini"):
        with pytest.raises(NotImplementedError):
            get_extractor(name)


def test_an_unknown_provider_names_the_valid_options():
    with pytest.raises(RuntimeError, match="deterministic"):
        get_extractor("gpt4")


# ------------------------------------------------------------ worked example


def test_the_example_pitch_shown_in_the_ui_extracts_without_a_cap():
    """⚠️ The UI shows `samples/minimal-pitch.md` as "what good looks like".

    If that document does not itself populate every scoring field, the UI is
    teaching producers a shape that gets their pitch capped — worse than showing
    no example at all. Pinned here so an edit to the sample cannot quietly make
    the guidance wrong.
    """
    from components.intake.web import EXAMPLE_PITCH, SAMPLES_DIR

    profile = extract((SAMPLES_DIR / EXAMPLE_PITCH).read_text())

    assert [f for f in COVERAGE_FIELDS if not getattr(profile, f)] == []
    assert [f for f in LOAD_BEARING_FIELDS if not getattr(profile, f)] == []
    assert unmatchable(profile) == []
    assert profile.extraction_confidence == 1.0


def test_the_example_demonstrates_the_features_it_claims_to():
    """Each annotation beside the example asserts something. Check they hold."""
    from components.intake.web import EXAMPLE_PITCH, SAMPLES_DIR

    profile = extract((SAMPLES_DIR / EXAMPLE_PITCH).read_text())

    assert profile.title == "Tidal Rift"                      # the # heading
    assert "Metroidvania" in profile.tags                     # prose -> corpus tags
    assert profile.primary_genre                              # genre inferred from tags
    assert profile.core_mechanics                             # bullets under a heading
    assert profile.price_tier is PriceTier.P14_99             # "Priced at $14.99"
    assert profile.target_platforms == ["Windows", "macOS"]   # Steam's platform vocabulary
