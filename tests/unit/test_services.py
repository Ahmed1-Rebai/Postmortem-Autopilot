"""Service canonicalization.

If this is wrong, the service-overlap heuristic never fires and every
hypothesis scores identically on temporal proximity alone — the exact symptom
docs/06 lists under "common problems".
"""

from __future__ import annotations

import pytest

from agent.normalize.services import ServiceCanonicalizer

ALIASES = {
    "checkout": ["checkout-frontend", "co"],
    "payment-gateway": ["payment-gw", "paymentgw", "payments"],
}
SUFFIXES = ["-svc", "-service", "-srv", "-api", "-app"]


@pytest.fixture
def canonicalizer() -> ServiceCanonicalizer:
    return ServiceCanonicalizer.from_config(ALIASES, SUFFIXES)


# ---------------------------------------------------------------------------
# the case this exists for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "checkout",
        "checkout-svc",
        "checkout_service",
        "checkout-service",
        "Checkout",
        "  CHECKOUT  ",
        "checkout.service",
        "checkout srv",
        "checkout-frontend",
        "co",
    ],
)
def test_all_spellings_of_checkout_canonicalize_together(
    canonicalizer: ServiceCanonicalizer, raw: str
):
    assert canonicalizer.canonical(raw) == "checkout"


@pytest.mark.parametrize(
    "raw", ["payment-gw", "payment_gw", "paymentgw", "payments", "payment-gateway"]
)
def test_alias_map_handles_what_no_rule_could(
    canonicalizer: ServiceCanonicalizer, raw: str
):
    assert canonicalizer.canonical(raw) == "payment-gateway"


# ---------------------------------------------------------------------------
# discrimination
# ---------------------------------------------------------------------------
def test_distinct_services_stay_distinct(canonicalizer: ServiceCanonicalizer):
    assert canonicalizer.canonical("checkout") != canonicalizer.canonical("inventory")


def test_similar_names_are_not_guessed_together(canonicalizer: ServiceCanonicalizer):
    """No fuzzy matching. If `checkout-v2` really is `checkout`, someone writes
    it in the alias map — guessing is how silent wrongness gets in."""
    assert canonicalizer.canonical("checkout-v2") == "checkout-v2"


def test_unknown_services_pass_through_normalized(
    canonicalizer: ServiceCanonicalizer,
):
    """An unrecognized name is still a usable overlap key; dropping it would
    lose a real signal."""
    assert canonicalizer.canonical("Recommendation_Engine") == "recommendation-engine"


def test_unknown_service_still_gets_suffix_stripped(
    canonicalizer: ServiceCanonicalizer,
):
    assert canonicalizer.canonical("shipping-svc") == "shipping"


# ---------------------------------------------------------------------------
# edges
# ---------------------------------------------------------------------------
def test_none_stays_none(canonicalizer: ServiceCanonicalizer):
    assert canonicalizer.canonical(None) is None


@pytest.mark.parametrize("blank", ["", "   ", "---", "_"])
def test_blank_names_become_none(canonicalizer: ServiceCanonicalizer, blank: str):
    assert canonicalizer.canonical(blank) is None


def test_a_name_that_is_only_a_suffix_is_not_stripped_to_nothing(
    canonicalizer: ServiceCanonicalizer,
):
    assert canonicalizer.canonical("api") == "api"


def test_longest_suffix_wins():
    """`-service` must win over `-srv` so `foo-service` isn't left as `foo-`."""
    canonicalizer = ServiceCanonicalizer.from_config({}, ["-svc", "-service"])
    assert canonicalizer.canonical("billing-service") == "billing"


def test_exact_alias_beats_suffix_stripping():
    """An alias that legitimately ends in a strippable suffix must survive."""
    canonicalizer = ServiceCanonicalizer.from_config(
        {"gateway": ["edge-api"]}, ["-api"]
    )
    assert canonicalizer.canonical("edge-api") == "gateway"


def test_canonicalization_is_idempotent(canonicalizer: ServiceCanonicalizer):
    once = canonicalizer.canonical("checkout_service")
    assert canonicalizer.canonical(once) == once


def test_empty_config_degrades_to_normalization_only():
    canonicalizer = ServiceCanonicalizer.from_config({}, [])
    assert canonicalizer.canonical("Checkout_SVC") == "checkout-svc"


def test_repo_services_yaml_loads():
    from agent.config import PROJECT_ROOT, ServicesConfig

    config = ServicesConfig.from_yaml(PROJECT_ROOT / "config" / "services.yaml")
    canonicalizer = ServiceCanonicalizer.from_config(
        config.aliases, config.strip_suffixes
    )
    assert canonicalizer.canonical("checkout-svc") == "checkout"
    assert canonicalizer.canonical("payment-gw") == "payment-gateway"
