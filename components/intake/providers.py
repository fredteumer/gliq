"""Swappable extraction backends.

Every stage where a model could help has a deterministic implementation and,
optionally, an LLM one behind the same interface — both emitting the same
`PitchProfile`. ➡️ AGENTS.md.

⚠️ The deterministic provider is NOT a placeholder. It is the reproducible
CONTROL the LLM path gets measured against, and it is what makes Part 1
demonstrable with no API key, no network and no spend. A provider that cannot
be reached must fail at selection time with a clear message rather than at
request time with a stack trace.

## Providers

| Name            | Needs a key | What it does                                  |
| :-------------- | :---------- | :-------------------------------------------- |
| `deterministic` | no          | Dictionary match against the corpus vocabulary |
| `fixture`       | no          | A canned profile — for exercising B and C      |
| `anthropic`     | yes         | ⏳ Part 2                                       |
| `gemini`        | yes         | ⏳ Part 2                                       |

`fixture` and `deterministic` are different tools and neither replaces the
other: `fixture` returns the SAME profile regardless of input, which is what
makes it useful for testing the transport downstream, and useless for testing
extraction.
"""

from __future__ import annotations

from typing import Protocol

from components.intake import extract as deterministic_extract
from shared.schemas import PitchProfile, PriceTier


class Extractor(Protocol):
    """The one thing every provider must do."""

    name: str

    def extract(self, text: str) -> PitchProfile: ...


class DeterministicExtractor:
    """Dictionary match against the tags/genres present in the corpus."""

    name = "deterministic"

    def extract(self, text: str) -> PitchProfile:
        return deterministic_extract.extract(text, extracted_by=self.name)


class FixtureExtractor:
    """A canned profile, ignoring the document entirely.

    Exists so Components B and C are fully developable and testable with no API
    key and no network. The values are deliberately ordinary — a mid-market
    indie strategy title — so a fixture run exercises the normal scoring path
    rather than an edge case.

    ⚠️ Every value here must be a real corpus string, for the same reason the
    vocabulary is committed byte-for-byte: a fixture that cannot retrieve
    candidates would make B look broken when it is fine.
    """

    name = "fixture"

    def extract(self, text: str) -> PitchProfile:
        del text  # canned by design — the document is deliberately ignored
        return PitchProfile(
            title="Fixture Tactics",
            primary_genre="Strategy",
            sub_genres=["Indie", "Simulation"],
            tags=["Turn-Based Strategy", "Strategy", "Tactical", "Singleplayer", "2D"],
            core_mechanics=["Turn-based tactical combat", "Squad progression between missions"],
            art_style="Stylised 2D",
            price_tier=PriceTier.P19_99,
            target_platforms=["Windows", "macOS"],
            summary="A canned pitch profile used to exercise the pipeline without an LLM.",
            extracted_by=self.name,
            extraction_confidence=1.0,
        )


class _NotYetImplemented:
    """A Part 2 provider that has been selected before it exists.

    ⏳ Deferred, not abandoned: LLM extraction from text and images is Part 2,
    and it is where real differentiation judgement belongs. Raising at
    construction means a misconfigured `LLM_PROVIDER` fails when the process
    starts, not on the first producer's submission.
    """

    def __init__(self, name: str) -> None:
        raise NotImplementedError(
            f"🛑 LLM_PROVIDER={name!r} is a Part 2 provider and is not implemented yet. "
            "Use 'deterministic' for real extraction or 'fixture' for a canned profile."
        )


#: Selection table. `deterministic` is the Part 1 default.
PROVIDERS = {
    "deterministic": DeterministicExtractor,
    "fixture": FixtureExtractor,
    "anthropic": lambda: _NotYetImplemented("anthropic"),
    "gemini": lambda: _NotYetImplemented("gemini"),
}


def get_extractor(provider: str) -> Extractor:
    """Build the configured extractor, or fail loudly with the valid options."""
    key = (provider or "").strip().casefold()
    if key not in PROVIDERS:
        valid = ", ".join(sorted(PROVIDERS))
        raise RuntimeError(f"❌ unknown LLM_PROVIDER={provider!r}. Valid providers: {valid}")
    return PROVIDERS[key]()
