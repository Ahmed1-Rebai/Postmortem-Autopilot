"""Error fingerprinting — collapse log noise into distinct failure modes.

`ConnectionError: pool exhausted (32/32)` and `... (64/64)` are the same
failure. Stripping the parts that vary — UUIDs, hex, numbers, quoted strings,
paths, addresses — leaves a template that identifies the *kind* of error, so a
few thousand log lines become a handful of nodes.

Two things depend on getting this right, which is why the test table is
exhaustive:

- **Token cost is bounded by it.** The Analyst sees the collapsed graph. Without
  collapsing, what it costs to analyze an incident scales with how much the
  service logged during it.
- **`log_signature_match` is a 0.25-weight confidence signal.** Over-collapsing
  merges unrelated failures and manufactures corroboration; under-collapsing
  splits one failure into many and manufactures breadth. Both corrupt scores in
  a way nothing downstream can detect.

The output is a readable template rather than a hash, deliberately: when a
confidence score looks wrong, being able to read the signature is most of the
debugging story.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: Signatures are graph keys and appear in incident fingerprints, so they must
#: be bounded. Longer templates keep a readable prefix plus a hash of the whole,
#: which stays stable and unique without growing without limit.
MAX_SIGNATURE_LENGTH: Final[int] = 120

_LEVEL_TOKENS: Final[str] = (
    "trace|debug|info|notice|warn|warning|error|err|fatal|critical|crit"
)

_ISO_TIMESTAMP: Final[str] = (
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)

#: Stripped from the front of the raw line, before any substitution, so that
#: the same error logged at WARN and at ERROR shares a signature.
_LEADING_TIMESTAMP: Final[re.Pattern[str]] = re.compile(
    rf"^\s*{_ISO_TIMESTAMP}\s*", re.IGNORECASE
)

#: A level is only stripped when it *looks* like a level field: bracketed, or
#: colon-terminated, or bare uppercase. A bare lowercase word is left alone —
#: otherwise "trace id abc123 dropped" would silently lose its first word, and
#: two unrelated messages would collapse onto one signature.
_LEADING_LEVEL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:"
    rf"\[(?i:{_LEVEL_TOKENS})\]"
    rf"|(?i:{_LEVEL_TOKENS})\s*:"
    r"|(?:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|CRIT)\b"
    r")\s*[-:]?\s*"
)

# Order is load-bearing. Each pattern runs against the output of the previous
# one, so the more specific must go first: a UUID is also a run of hex, hex is
# also a run of digits, and a quoted string may contain any of them.
_SUBSTITUTIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # URLs before paths — a URL is full of slashes and dots.
    (re.compile(r"https?://\S+", re.IGNORECASE), "<url>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "<email>"),
    # UUID before hex: a UUID is hex runs joined by dashes.
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<uuid>",
    ),
    # Timestamps before IPs and numbers, or they'd shred into <num>-<num>-<num>.
    (re.compile(_ISO_TIMESTAMP, re.IGNORECASE), "<ts>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    # Hex needs at least one a-f, otherwise a long decimal (an epoch, a byte
    # count) would become <hex> instead of <num> — same collapse, wrong label,
    # and confusing to read six months later.
    (
        re.compile(
            r"\b(?:0x[0-9a-f]+|(?=[0-9a-f]{8,}\b)[0-9a-f]*[a-f][0-9a-f]*)\b",
            re.IGNORECASE,
        ),
        "<hex>",
    ),
    # Quoted strings before paths and numbers — whatever is inside varies.
    # The single-quote pattern requires non-word neighbours so `can't` is safe.
    (re.compile(r"\"[^\"\n]*\"|`[^`\n]*`|(?<!\w)'[^'\n]*'(?!\w)"), "<str>"),
    # Paths. The leading-slash branch has a lookbehind so `32/32` is not read as
    # a path — that ratio is two numbers, and it is the canonical example of a
    # thing that must collapse to <num>/<num>.
    (
        re.compile(
            r"(?<![\w])(?:\.{1,2})?/(?:[\w.\-]+/)*[\w.\-]+"
            r"|\b[\w\-]+(?:/[\w.\-]+)*\.[A-Za-z]{1,6}\b"
        ),
        "<path>",
    ),
    # No word boundaries: a number is just as much a number when it is glued to
    # its unit ("5000ms", "512MB") or to a version prefix ("v2.3"). Requiring a
    # boundary here silently left those unnormalized, so "timed out after
    # 5000ms" and "...after 8000ms" were two distinct failure modes.
    (re.compile(r"\d+(?:\.\d+)?"), "<num>"),
)

#: Belt-and-braces: a leading timestamp in a format the raw strip missed still
#: gets dropped once it has been normalized to `<ts>`.
_LEADING_PLACEHOLDER_TS: Final[re.Pattern[str]] = re.compile(r"^\s*<ts>\s*")

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def fingerprint(message: str) -> str:
    """Normalize a log message into a stable error template.

    Returns `""` for a blank message — callers store that as no signature
    rather than as an empty one.
    """
    if not message or not message.strip():
        return ""

    # Strip the log preamble first, while case still distinguishes a level
    # field from an ordinary word. Twice, because formats disagree about
    # whether the timestamp or the level comes first.
    text = message
    for _ in range(2):
        text = _LEADING_TIMESTAMP.sub("", text)
        text = _LEADING_LEVEL.sub("", text)

    for pattern, placeholder in _SUBSTITUTIONS:
        text = pattern.sub(placeholder, text)

    text = _LEADING_PLACEHOLDER_TS.sub("", text.lower())
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""

    if len(text) > MAX_SIGNATURE_LENGTH:
        digest = hashlib.sha256(text.encode()).hexdigest()[:8]
        keep = MAX_SIGNATURE_LENGTH - len(digest) - 1
        return f"{text[:keep]}~{digest}"
    return text


def same_failure(first: str, second: str) -> bool:
    """Whether two raw messages describe the same failure mode."""
    return fingerprint(first) == fingerprint(second)
