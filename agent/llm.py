"""The only place that talks to a model.

Three providers behind one protocol: `mock` (no network, deterministic),
`anthropic`, and `openrouter` (any OpenAI-compatible gateway). Keeping the
surface this narrow is what lets the eval harness compare models, and what
keeps every CI path runnable with `LLM_PROVIDER=mock`.

Two things this layer refuses to paper over:

- **Truncation is an error, not a short answer.** Most of the free reasoning
  models spend their budget on hidden reasoning tokens; a low `max_tokens`
  yields a postmortem that stops mid-sentence, which the validator would then
  report as a coverage failure. Wrong diagnosis, wasted retry.
- **Token usage is always reported**, because cost per run is an eval metric
  and an unmeasured cost is one nobody manages.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from agent.config import LLMConfig

#: Generous by default. Reasoning tokens count against this on most models, so
#: a tight budget truncates the visible answer rather than shortening it.
DEFAULT_MAX_TOKENS: Final[int] = 8000

_EVENT_ID: Final[re.Pattern[str]] = re.compile(r"\b[0-9a-f]{16}\b")
_JSON_FENCE: Final[re.Pattern[str]] = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """The model could not be reached, or answered unusably."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# parsing helpers — shared, because both nodes need them
# ---------------------------------------------------------------------------
def extract_json(text: str) -> Any:
    """Pull a JSON object out of a model response.

    Models fence JSON, prepend "Here is the analysis:", or emit reasoning
    before the payload. Rather than forbidding that in the prompt and trusting
    it, the parser tolerates it — the structural guarantee comes from
    validating what is inside, not from hoping about the wrapper.
    """
    candidates: list[str] = []
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    for start, end in (("{", "}"), ("[", "]")):
        first, last = text.find(start), text.rfind(end)
        if first != -1 and last > first:
            candidates.append(text[first : last + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except json.JSONDecodeError:
            continue
    raise LLMError(f"no parseable JSON in model response: {text[:300]!r}")


# ---------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------
@dataclass
class MockProvider:
    """Deterministic canned responses. No network, no key, no tokens.

    This exists so the whole pipeline — including the validator's retry loop —
    is exercisable in CI. Skipping it is trap 2 in TODO.md: without a mock,
    tests need an API key, so they do not get run, so the retry loop stays
    untested.

    Responses are synthesized from the event IDs found in the prompt, so the
    mock produces *valid citations* by construction. `scripted` overrides that
    for tests that need a specific — including deliberately broken — reply.
    """

    name: str = "mock"
    scripted: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        self.calls.append((system, user))
        if self.scripted:
            text = self.scripted.pop(0)
        elif "postmortem" in system.lower() or "citation" in system.lower():
            text = self._draft(user)
        else:
            text = self._analysis(user)
        return LLMResponse(
            text=text,
            input_tokens=len(user) // 4,
            output_tokens=len(text) // 4,
            model="mock",
        )

    @staticmethod
    def _ids(prompt: str) -> list[str]:
        return list(dict.fromkeys(_EVENT_ID.findall(prompt)))

    def _analysis(self, prompt: str) -> str:
        ids = self._ids(prompt)
        if not ids:
            return json.dumps({"hypotheses": []})
        primary, *rest = ids
        hypotheses = [
            {
                "statement": "The change is the most likely cause of the failure",
                "supporting_event_ids": ids[:3],
                "contradicting_event_ids": [],
            }
        ]
        if rest:
            hypotheses.append(
                {
                    "statement": "An unrelated concurrent change may be responsible",
                    "supporting_event_ids": rest[:2],
                    "contradicting_event_ids": [primary],
                }
            )
        return json.dumps({"hypotheses": hypotheses}, indent=2)

    def _draft(self, prompt: str) -> str:
        ids = self._ids(prompt)
        if not ids:
            return "## Summary\n\nNo evidence was available for this incident.\n"
        cite = f"[src:{ids[0]}]"
        second = f"[src:{ids[min(1, len(ids) - 1)]}]"
        return (
            f"## Summary\n\nThe service began failing during the incident "
            f"window {cite}.\n\n"
            f"## Impact\n\nErrors were observed for the affected service "
            f"{second}.\n\n"
            f"## Contributing Factors\n\nA change landed shortly before the "
            f"failure {cite}.\n\n"
            f"## Corrective Actions\n\n- Review the change process.\n\n"
            f"## Open Questions\n\n- Was the change reviewed before release?\n"
        )


# ---------------------------------------------------------------------------
# anthropic
# ---------------------------------------------------------------------------
@dataclass
class AnthropicProvider:
    api_key: str
    name: str = "anthropic"

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMError("the anthropic package is not installed") from exc

        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # the SDK raises many shapes
            raise LLMError(f"anthropic call failed: {exc}") from exc

        if message.stop_reason == "max_tokens":
            raise LLMError(
                f"response hit max_tokens ({max_tokens}) and is truncated; "
                "raise the budget rather than publishing a partial document"
            )
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=model,
        )


# ---------------------------------------------------------------------------
# openrouter (and anything else speaking the OpenAI chat API)
# ---------------------------------------------------------------------------
@dataclass
class OpenRouterProvider:
    api_key: str
    base_url: str
    name: str = "openrouter"
    timeout_seconds: float = 180.0

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        import httpx

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            response = httpx.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"openrouter call failed: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(
                f"openrouter returned {response.status_code}: {response.text[:300]}"
            )
        body = response.json()
        if "error" in body:
            raise LLMError(f"openrouter error: {body['error']}")

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"openrouter returned no choices: {str(body)[:300]}")
        choice = choices[0]
        text = (choice.get("message") or {}).get("content") or ""

        if choice.get("finish_reason") == "length":
            raise LLMError(
                f"response hit max_tokens ({max_tokens}) and is truncated. "
                "Free reasoning models spend this budget on hidden reasoning "
                "tokens — raise it rather than publishing a partial document"
            )
        if not text.strip():
            raise LLMError("openrouter returned an empty completion")

        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=model,
        )


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------
def build_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "mock":
        return MockProvider()
    if config.provider == "anthropic":
        if not config.api_key:
            raise LLMError("anthropic provider requires an API key")
        return AnthropicProvider(api_key=config.api_key)
    if not config.api_key or not config.base_url:
        raise LLMError("openrouter provider requires an API key and base URL")
    return OpenRouterProvider(api_key=config.api_key, base_url=config.base_url)


def deterministic_shuffle(items: Sequence[str], seed: int = 0) -> list[str]:
    """Shuffle reproducibly — used by evals to test order sensitivity without
    making a run non-reproducible."""
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    return shuffled
