"""Service name canonicalization.

`checkout-svc` in a deploy, `checkout_service` in a log line and `checkout` in
an alert label are one service. If they stay distinct, the service-overlap
heuristic never fires, every hypothesis is scored on temporal proximity alone,
and the whole ranking flattens — the failure docs/06 lists as "every hypothesis
scores identically".

Deliberately a small rule set plus an explicit alias map, not fuzzy matching.
Guessing that `checkout` and `checkout-v2` are the same service is exactly the
kind of silent wrongness this project is built to avoid; if two names really
are one service, someone writes it down in `config/services.yaml`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Separators are normalized before anything else, so `checkout_service`,
#: `checkout.service` and `checkout service` reduce to the same shape.
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[\s_.]+")
_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9-]+")
_DASHES: Final[re.Pattern[str]] = re.compile(r"-{2,}")


@dataclass(frozen=True, slots=True)
class ServiceCanonicalizer:
    """Maps raw service names onto canonical ones.

    Built once from config and passed to the normalizer — no module-level
    mutable state, so two incidents with different alias maps cannot interfere.
    """

    #: alias (already normalized) -> canonical name
    alias_to_canonical: dict[str, str]
    #: longest-first, so `-service` wins over `-srv` when both could match
    strip_suffixes: tuple[str, ...]

    @classmethod
    def from_config(
        cls,
        aliases: dict[str, list[str]],
        strip_suffixes: list[str],
    ) -> ServiceCanonicalizer:
        table: dict[str, str] = {}
        for canonical, alias_list in aliases.items():
            canonical_name = _basic_normalize(canonical)
            # A canonical name is an alias of itself, so lookups need one path.
            table[canonical_name] = canonical_name
            for alias in alias_list:
                table[_basic_normalize(alias)] = canonical_name
        ordered = tuple(
            sorted(
                (
                    suffix
                    for raw in strip_suffixes
                    if (suffix := _normalize_suffix(raw))
                ),
                key=len,
                reverse=True,
            )
        )
        return cls(alias_to_canonical=table, strip_suffixes=ordered)

    def canonical(self, name: str | None) -> str | None:
        """Canonical form of `name`, or None if there is nothing to canonicalize.

        Unknown services pass through in normalized form rather than being
        dropped — an unrecognized name is still a usable overlap key, and
        discarding it would lose a real signal.
        """
        if name is None:
            return None
        normalized = _basic_normalize(name)
        if not normalized:
            return None

        # An exact alias hit wins before any suffix surgery, so an alias that
        # legitimately ends in "-api" is not mangled on the way in.
        direct = self.alias_to_canonical.get(normalized)
        if direct is not None:
            return direct

        stripped = self._strip_suffix(normalized)
        return self.alias_to_canonical.get(stripped, stripped)

    def _strip_suffix(self, normalized: str) -> str:
        for suffix in self.strip_suffixes:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                return normalized[: -len(suffix)]
        return normalized


def _normalize_suffix(suffix: str) -> str:
    """Normalize a suffix while keeping its leading separator.

    `_basic_normalize` strips leading dashes — correct for a service name,
    wrong here: it turned `-svc` into `svc`, so `checkout-svc` was stripped to
    `checkout-` and never matched the canonical `checkout`. A suffix is always
    a trailing *segment*, so the separator is re-attached unconditionally and
    `svc`, `-svc` and `_svc` all mean the same rule.
    """
    core = _basic_normalize(suffix)
    return f"-{core}" if core else ""


def _basic_normalize(name: str) -> str:
    lowered = _SEPARATORS.sub("-", name.strip().lower())
    safe = _UNSAFE.sub("-", lowered)
    return _DASHES.sub("-", safe).strip("-")
