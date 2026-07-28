"""Candidate causal links — the deterministic half of the interesting part.

Three cheap, independent heuristics propose `POSSIBLY_CAUSED` edges. Each edge
records *which* heuristics fired, because that decomposition is what the
confidence model consumes and what lets a score be explained rather than
asserted.

**No LLM here, on purpose** (ADR-003). Generating candidates is a recall
problem: be generous, propose 20-50 links, and let ranking do precision. Doing
recall with a model would be slow, expensive, and non-reproducible — and
reproducibility is what makes the eval suite mean anything.

`temporal_proximity` — cause precedes effect by 0 < Δt ≤ window. Weak on its
own: during an incident, everything correlates in time.

`service_overlap` — both events touch the same canonical service. Medium.

`change_path_overlap` — the change touched the code that failed. Strong, and
the one that separates "a deploy happened" from "*this* deploy".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Final

from agent.config import PipelineConfig
from agent.normalize.services import ServiceCanonicalizer
from agent.state import CandidateLink, Event, EventType

#: Only symptom events are treated as effects. A postmortem explains failures,
#: not fixes: allowing a deploy to be an effect would let the linker propose
#: that a rollback was caused by the outage it resolved, which is true but
#: useless, and it would crowd out the chains that explain anything.
#: Remediation events still appear in the timeline — they are just not
#: candidates for "what went wrong".
EFFECT_TYPES: Final[frozenset[EventType]] = frozenset(
    {EventType.LOG_ERROR, EventType.ALERT_FIRED}
)

#: File stems too generic to be evidence. `utils.py` changing tells you nothing
#: about which service broke, and matching on it would fire
#: `change_path_overlap` — the strongest signal — on almost every commit.
GENERIC_MODULE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "base",
        "common",
        "config",
        "constants",
        "core",
        "helpers",
        "index",
        "init",
        "main",
        "misc",
        "setup",
        "shared",
        "test",
        "tests",
        "types",
        "util",
        "utils",
    }
)

#: Below this length a stem is too likely to collide with an ordinary English
#: word appearing in an error message by coincidence.
MIN_MODULE_TOKEN_LENGTH: Final[int] = 4

#: Ranking weights for the candidate edge's `score`. This orders candidates for
#: the Analyst; it is **not** the confidence number. `confidence.py` owns that
#: (invariant 7), computes it over a different and larger signal set, and is
#: the only thing whose output reaches the reader.
_RANKING_WEIGHTS: Final[dict[str, float]] = {
    "change_path_overlap": 0.5,
    "service_overlap": 0.3,
    "temporal_proximity": 0.2,
}

_TOKEN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class LinkerConfig:
    """Tuning for candidate generation."""

    window_minutes: int = 15
    #: A backstop, not a target. Candidate generation is meant to be generous,
    #: but a pathological window (hundreds of uncollapsed symptoms) would
    #: otherwise produce a quadratic edge count and blow up the Analyst's input.
    max_candidates: int = 200
    generic_module_names: frozenset[str] = GENERIC_MODULE_NAMES
    min_module_token_length: int = MIN_MODULE_TOKEN_LENGTH

    def __post_init__(self) -> None:
        if self.window_minutes <= 0:
            raise ValueError("window_minutes must be > 0")
        if self.max_candidates <= 0:
            raise ValueError("max_candidates must be > 0")

    @classmethod
    def from_pipeline_config(cls, pipeline: PipelineConfig) -> LinkerConfig:
        """Take the window from `CAUSAL_WINDOW_MINUTES` rather than keeping a
        second default that could drift away from the configured one."""
        return cls(window_minutes=pipeline.causal_window_minutes)


@dataclass(frozen=True, slots=True)
class CandidateLinker:
    """Proposes `POSSIBLY_CAUSED` edges over a set of events.

    Pure: events in, links out, no graph access. That keeps the whole heuristic
    surface unit-testable against a fixture list, which is where off-by-one
    window bugs actually get caught.
    """

    config: LinkerConfig = field(default_factory=LinkerConfig)
    canonicalizer: ServiceCanonicalizer | None = None

    def link(self, events: Sequence[Event]) -> list[CandidateLink]:
        ordered = sorted(events, key=lambda e: (e.timestamp, e.id))
        window = timedelta(minutes=self.config.window_minutes)
        effects = [event for event in ordered if event.type in EFFECT_TYPES]

        links: list[CandidateLink] = []
        for cause in ordered:
            touched_services = self._services_touched(cause)
            module_tokens = self._module_tokens(cause)

            for effect in effects:
                if effect.id == cause.id:
                    continue
                delta = effect.timestamp - cause.timestamp
                # Strictly positive: simultaneous events have no direction, and
                # inventing one would let an effect explain its own cause.
                if delta <= timedelta(0) or delta > window:
                    continue

                heuristics = self._heuristics(
                    cause, effect, touched_services, module_tokens
                )
                if not heuristics:
                    continue
                links.append(
                    CandidateLink(
                        cause_id=cause.id,
                        effect_id=effect.id,
                        heuristics=heuristics,
                        delta_seconds=int(delta.total_seconds()),
                        score=round(
                            sum(_RANKING_WEIGHTS[name] for name in heuristics), 4
                        ),
                    )
                )

        # Strongest first, then tightest; the cap trims the weak tail.
        links.sort(key=lambda link: (-link.score, link.delta_seconds, link.cause_id))
        return links[: self.config.max_candidates]

    # -- heuristics ----------------------------------------------------------
    def _heuristics(
        self,
        cause: Event,
        effect: Event,
        touched_services: frozenset[str],
        module_tokens: frozenset[str],
    ) -> tuple[str, ...]:
        fired = ["temporal_proximity"]

        if cause.service and effect.service and cause.service == effect.service:
            fired.append("service_overlap")

        if self._change_path_overlaps(effect, touched_services, module_tokens):
            fired.append("change_path_overlap")

        return tuple(fired)

    def _change_path_overlaps(
        self,
        effect: Event,
        touched_services: frozenset[str],
        module_tokens: frozenset[str],
    ) -> bool:
        """Did the change touch the code that failed?

        Two ways to be convinced, both requiring the *change* to name something
        the *failure* also names:

        1. a path segment canonicalizes to the failing service, or
        2. a changed file's stem appears as a token in the failure's signature
           — the case that catches `app/pool.py` against "pool exhausted".
        """
        if not touched_services and not module_tokens:
            return False
        if effect.service and effect.service in touched_services:
            return True
        return bool(module_tokens & _tokens_of(effect))

    # -- extraction ----------------------------------------------------------
    def _services_touched(self, event: Event) -> frozenset[str]:
        """Services a change touched, from its file paths.

        Every directory segment is considered, not just the first: monorepos
        nest (`services/checkout/app.py`) and flat repos do not, and guessing
        which layout is in use would silently drop the signal for one of them.
        """
        paths = _files_changed(event)
        if not paths:
            return frozenset()
        segments = {
            segment
            for path in paths
            for segment in PurePosixPath(path).parts[:-1]
            if segment not in {"/", "."}
        }
        if self.canonicalizer is None:
            return frozenset(segment.lower() for segment in segments)
        return frozenset(
            canonical
            for segment in segments
            if (canonical := self.canonicalizer.canonical(segment))
        )

    def _module_tokens(self, event: Event) -> frozenset[str]:
        """Distinctive file stems from a change, e.g. `pool` from `app/pool.py`."""
        return frozenset(
            stem
            for path in _files_changed(event)
            if (stem := PurePosixPath(path).stem.lower())
            if len(stem) >= self.config.min_module_token_length
            if stem not in self.config.generic_module_names
        )


def _files_changed(event: Event) -> tuple[str, ...]:
    value = event.attributes.get("files_changed")
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def _tokens_of(event: Event) -> frozenset[str]:
    """Words in an event's signature and summary, for module matching.

    Tokenized rather than substring-matched: `pool` must not match `poolside`,
    or the strongest signal in the model would fire on a coincidence.
    """
    haystack = f"{event.signature or ''} {event.summary}".lower()
    return frozenset(_TOKEN.findall(haystack))


def summarize(links: Iterable[CandidateLink]) -> dict[str, int]:
    """How often each heuristic fired. Goes in the `RunReport`.

    If this shows `temporal_proximity` alone, the run's hypotheses will all
    score identically and the cause is almost always service canonicalization
    (docs/06, "common problems").
    """
    counts: dict[str, int] = {}
    for link in links:
        for name in link.heuristics:
            counts[name] = counts.get(name, 0) + 1
    return counts
