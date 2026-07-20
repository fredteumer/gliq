"""The LLM analyst — Component C's subjective second opinion.

⚠️ This does NOT feed the grade. Component B's fitment score stays a pure,
deterministic function of the profile and the corpus; a grade change must be
attributable to extraction or comp selection, never to model drift. The analyst
is an INDEPENDENT read, prompted as the publisher's investment arm: it reads the
raw pitch prose the tag extractor cannot, consults the deterministic result, and
casts its own vote. It is decision support shown alongside the deterministic
recommendation — the disagreement between the two is the point. ➡️ shared.schemas
.AdvisoryOpinion, docs/ARCHITECTURE.md.

## Swappable, and safe by default

Provider, model and key are all config-driven. `fixture` (the default) returns a
canned opinion with no key and no network, so the whole component runs and tests
offline and an unconfigured stack spends nothing. `gemini` is the real path, via
its OpenAI-compatible endpoint; `anthropic` is a real alternate.

⚠️ NOT the Anthropic SDK for Gemini. Gemini is reached through the OpenAI-
compatible endpoint (`openai` lib), matching how Component A's extractor config
is set up. Only the `anthropic` provider uses the Anthropic SDK.

## Degrade, never break

Every provider call is wrapped: any failure — missing key, rate limit, network,
a refusal, an unparseable response — returns None. Component C then renders the
full DETERMINISTIC report with an "analyst unavailable" note. The analyst is
never a dependency of the pipeline; a pitch is always reported.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from pydantic import BaseModel, Field

from shared.config import Config
from shared.schemas import (
    AdvisoryOpinion,
    AdvisoryStance,
    FitmentResult,
    InvestmentTier,
    PitchProfile,
)

log = logging.getLogger("gliq-report")

#: Headroom for the structured opinion plus any thinking. The opinion itself is
#: small; this leaves room without inviting an essay.
MAX_OUTPUT_TOKENS = 4000

#: Gemini's OpenAI-compatible base URL. Documented by Google; kept here so the
#: provider needs only a model name and a key from config.
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

SYSTEM_PROMPT = """\
You are a senior analyst on the investment committee of a video-game publisher's \
acquisitions team. A producer has submitted a game pitch. An automated, \
deterministic system has already scored it against a corpus of ~83,000 released \
Steam titles; you are the human-style second opinion that sits beside that score.

Your edge over the deterministic system is that you READ THE ACTUAL PITCH. The \
deterministic score is built from extracted tags, so it is blind to things only \
prose reveals: how genuinely differentiated the concept is, the strength of the \
team and their track record, the coherence of the plan, whether a stated price \
fits the ambition, and claims like "no microtransactions" that a tag match \
inverts or ignores.

Evaluate the pitch against the same four lenses the deterministic system uses, \
but judge them with your reading of the document:
  1. Niche hit rate — do comparable titles in this space actually succeed, or is \
     it a graveyard?
  2. Sales potential — how large is the win realistically available here?
  3. Differentiation — how distinct is THIS pitch within its niche? (The \
     deterministic system explicitly cannot measure this; it is your strongest \
     contribution.)
  4. Price alignment — is the asking price sane for the segment and the ambition?

Then take a clear position. Categorise the pitch in a short phrase, assign your \
own letter grade A-F and an investment tier, and state plainly whether you AGREE \
with the deterministic read, DISSENT from it, or land somewhere MIXED — and why. \
Dissent is valuable when you can see something the tags cannot; say so directly. \
Be decisive and concrete; a committee cannot act on hedging.

Investment tiers:
  greenlight  — fund it
  conditional — fund it if specific conditions are met
  de_risk     — promising but needs work before it is fundable
  pass        — decline

Return ONLY a JSON object with exactly these fields:
  category      string   — your short categorisation of the pitch
  grade         string   — a single letter, one of A B C D E F
  tier          string   — one of greenlight conditional de_risk pass
  rationale     string   — 2-4 sentences defending your call
  stance        string   — one of agrees dissents mixed (vs the deterministic read)
  key_points    string[] — 2-5 short bullet points a committee member could skim
  confidence    number   — your confidence in this read, 0.0 to 1.0
"""


class _AnalystOutput(BaseModel):
    """Exactly what the model is asked to return.

    Distinct from `AdvisoryOpinion`: `model` (provenance) and `generated_at` are
    set by us, not the LLM, so they are not in the schema handed to it.
    """

    category: str
    grade: str
    tier: InvestmentTier
    rationale: str
    stance: AdvisoryStance
    key_points: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


def _render_deterministic(profile: PitchProfile, result: FitmentResult) -> str:
    """The deterministic result, compact, for the analyst to react to."""
    lines = [
        "## The deterministic system's read (for you to react to)",
        f"Grade: {result.grade}  Score: {result.score:.1f}/100  "
        f"(comps considered: {result.comps_considered})",
    ]
    if result.insufficient_information:
        lines.append("It could not build a comp set — no genre and no tags were extracted.")
    else:
        s = result.sub_scores
        lines += [
            f"Niche hit rate: {s.niche_hit_rate:.0f}  Sales potential: {s.sales_potential:.0f}  "
            f"Price alignment: {s.price_alignment:.0f}  "
            f"Differentiation: {s.differentiation:.0f} (reported, unweighted)",
            "Closest comparables (most similar first):",
        ]
        for c in result.comparables[:8]:
            units = f"{c.estimated_units:,}" if c.estimated_units is not None else "?"
            price = f"${c.price_usd:.2f}" if c.price_usd is not None else "?"
            lines.append(f"  - {c.name} — {price}, ~{units} est. units, similarity {c.similarity:.2f}")
    if result.assumptions:
        lines.append("Disclosed assumptions: " + " ".join(result.assumptions))
    return "\n".join(lines)


def _build_user_prompt(document: str, profile: PitchProfile, result: FitmentResult) -> str:
    """The document plus the deterministic read — what the analyst weighs."""
    title = profile.title or "Untitled pitch"
    return (
        f"# Pitch under review: {title}\n\n"
        "## The submitted design document (verbatim)\n\n"
        f"{document.strip()}\n\n"
        f"{_render_deterministic(profile, result)}\n\n"
        "Now return your JSON opinion."
    )


def _finalise(raw: _AnalystOutput, model: str) -> AdvisoryOpinion:
    """Stamp provenance onto the model's output."""
    return AdvisoryOpinion(
        category=raw.category,
        grade=_clean_grade(raw.grade),
        tier=raw.tier,
        rationale=raw.rationale,
        stance=raw.stance,
        key_points=raw.key_points,
        confidence=raw.confidence,
        model=model,
    )


#: A single A-F grade letter standing alone (start/end or bounded by non-letters),
#: so it fires on the "B" in "a solid B" but not the "a" itself or letters buried
#: in words like "GRADE" or "pass".
_GRADE_TOKEN = re.compile(r"(?:^|[^A-Z])([A-F])(?:$|[^A-Z])")


def _clean_grade(grade: str) -> str:
    """Coerce a returned grade to a single A-F letter, else pass it through.

    The model is asked for a bare letter, but occasionally answers "B+", "Grade:
    B", or "a solid B". Take the LAST standalone grade token — grades trail the
    prose — rather than degrade the whole opinion over formatting, and rather
    than grab the leading "a"/"GRADE" letter a naive scan would.
    """
    matches = _GRADE_TOKEN.findall(grade.strip().upper())
    return matches[-1] if matches else (grade.strip()[:8] or "?")


class Advisor(Protocol):
    """One subjective opinion, or None if it could not be produced."""

    name: str

    def opine(
        self, document: str, profile: PitchProfile, result: FitmentResult
    ) -> AdvisoryOpinion | None: ...


class FixtureAdvisor:
    """A canned opinion, ignoring input — for offline tests and no-key deploys.

    ⚠️ Like the extractor's fixture, it returns the SAME opinion regardless of
    the pitch. Useful for exercising Component C and the report end to end with
    no key and no spend; useless as an actual analyst. Its `model` is "fixture",
    which is how the report discloses that it is not a real read.
    """

    name = "fixture"

    def opine(
        self, document: str, profile: PitchProfile, result: FitmentResult
    ) -> AdvisoryOpinion | None:
        del document, profile  # canned by design
        return AdvisoryOpinion(
            category="fixture — analyst not enabled",
            grade=result.grade,
            tier=InvestmentTier.CONDITIONAL,
            stance=AdvisoryStance.AGREES,
            rationale=(
                "Placeholder opinion from the fixture provider. Configure "
                "ADVISOR_PROVIDER=gemini (or anthropic) with a key for a real read."
            ),
            key_points=["Fixture provider — no live model was consulted."],
            confidence=None,
            model=self.name,
        )


class GeminiAdvisor:
    """The real analyst, via Gemini's OpenAI-compatible endpoint.

    ⚠️ Uses the `openai` client pointed at Gemini — NOT the Anthropic SDK.
    """

    name = "gemini"

    def __init__(self, config: Config) -> None:
        self.api_key = config.gemini_api_key
        self.model = config.gemini_model
        self.provenance = f"gemini:{self.model}"

    def opine(
        self, document: str, profile: PitchProfile, result: FitmentResult
    ) -> AdvisoryOpinion | None:
        if not self.api_key:
            log.warning("🔒 ADVISOR_PROVIDER=gemini but GEMINI_API_KEY is unset — skipping analyst")
            return None
        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key, base_url=GEMINI_OPENAI_BASE)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(document, profile, result)},
                ],
                response_format={"type": "json_object"},
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            content = response.choices[0].message.content
            return _finalise(_AnalystOutput.model_validate_json(content), self.provenance)
        except Exception as exc:  # noqa: BLE001 — degrade, never break (incl. ValidationError)
            log.warning("⚠️ gemini analyst unavailable, reporting without it: %s", exc)
            return None


class AnthropicAdvisor:
    """The real analyst, via the Anthropic SDK. A configured alternate to Gemini."""

    name = "anthropic"

    def __init__(self, config: Config) -> None:
        self.api_key = config.anthropic_api_key
        self.model = config.anthropic_model
        self.provenance = f"anthropic:{self.model}"

    def opine(
        self, document: str, profile: PitchProfile, result: FitmentResult
    ) -> AdvisoryOpinion | None:
        if not self.api_key:
            log.warning(
                "🔒 ADVISOR_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset — skipping analyst"
            )
            return None
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.parse(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": _build_user_prompt(document, profile, result)}
                ],
                output_format=_AnalystOutput,
            )
            return _finalise(response.parsed_output, self.provenance)
        except Exception as exc:  # noqa: BLE001 — degrade, never break
            log.warning("⚠️ anthropic analyst unavailable, reporting without it: %s", exc)
            return None


_PROVIDERS: dict[str, Any] = {
    "fixture": lambda _config: FixtureAdvisor(),
    "gemini": GeminiAdvisor,
    "anthropic": AnthropicAdvisor,
}


def get_advisor(config: Config) -> Advisor:
    """Build the configured analyst, or fail loudly with the valid options.

    ⚠️ Construction never reaches the network or needs a key — that happens in
    `opine`, which degrades to None. So a misconfigured KEY does not stop
    Component C from starting; only an unknown PROVIDER name is fatal, and that
    is a deploy error worth failing on.
    """
    key = (config.advisor_provider or "").strip().casefold()
    factory = _PROVIDERS.get(key)
    if factory is None:
        valid = ", ".join(sorted(_PROVIDERS))
        raise RuntimeError(f"❌ unknown ADVISOR_PROVIDER={config.advisor_provider!r}. Valid: {valid}")
    return factory(config)
