"""Config loading and, more importantly, config *rejection*.

Phase 0 has no pipeline yet, but the validation in `agent/config.py` is the
first place invariant 8 ("validation failure fails loudly") shows up: a bad
weight must stop the run, not silently skew every confidence score.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent.config import (
    SIGNAL_NAMES,
    ConfidenceConfig,
    ConfigError,
    PipelineConfig,
    load_config,
)

VALID_YAML = """\
weights:
  change_path_overlap: 0.30
  log_signature_match: 0.25
  metric_correlation: 0.20
  temporal_proximity: 0.15
  human_confirmation: 0.10
contradiction:
  per_event: 0.15
  cap: 0.40
bands:
  likely: 0.75
  plausible: 0.40
"""


def write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "confidence.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- the shipped config must be valid ---------------------------------------
def test_repo_confidence_yaml_loads():
    from agent.config import PROJECT_ROOT

    cfg = ConfidenceConfig.from_yaml(PROJECT_ROOT / "config" / "confidence.yaml")
    assert cfg.weights.keys() == SIGNAL_NAMES
    # The weights documented in docs/03-confidence-model.md.
    assert cfg.weights["change_path_overlap"] == pytest.approx(0.30)
    assert cfg.weights["temporal_proximity"] == pytest.approx(0.15)
    assert cfg.contradiction_cap == pytest.approx(0.40)
    assert cfg.band_likely == pytest.approx(0.75)


def test_valid_yaml_round_trips(tmp_path: Path):
    cfg = ConfidenceConfig.from_yaml(write_yaml(tmp_path, VALID_YAML))
    assert sum(cfg.weights.values()) == pytest.approx(1.0)


# --- rejection table --------------------------------------------------------
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        # a typo'd signal name would silently drop that signal to zero weight
        ("change_path_overlap", "chnge_path_overlap"),
        # an extra key means the file and the model disagree about the signal set
        ("human_confirmation: 0.10", "human_confirmation: 0.10\n  slack_vibes: 0.5"),
    ],
    ids=["typo'd signal name", "unknown signal"],
)
def test_weight_key_mismatch_is_rejected(tmp_path: Path, mutation: str, expected: str):
    body = VALID_YAML.replace(mutation, expected)
    with pytest.raises(ConfigError, match="weights must be exactly"):
        ConfidenceConfig.from_yaml(write_yaml(tmp_path, body))


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            VALID_YAML.replace("temporal_proximity: 0.15", "temporal_proximity: 0"),
            "> 0",
        ),
        (VALID_YAML.replace("cap: 0.40", "cap: 1.5"), "contradiction.cap"),
        (VALID_YAML.replace("per_event: 0.15", "per_event: -0.1"), "per_event"),
        # bands inverted: plausible above likely makes banding non-monotonic
        (
            VALID_YAML.replace("plausible: 0.40", "plausible: 0.90"),
            "bands must satisfy",
        ),
        (VALID_YAML.replace("likely: 0.75", "likely: notanumber"), "must be a number"),
        ("weights: []\ncontradiction: {}\nbands: {}\n", "weights must be a mapping"),
        ("- not\n- a\n- mapping\n", "must be a mapping"),
        # a broken file must name itself, not surface a bare yaml.ParserError
        ("weights:\n  a: 1\n bad_indent: 2\n", "not valid YAML"),
    ],
    ids=[
        "zero weight",
        "cap above 1.0",
        "negative per_event",
        "inverted bands",
        "non-numeric band",
        "weights not a mapping",
        "document not a mapping",
        "unparseable YAML",
    ],
)
def test_malformed_confidence_config_is_rejected(tmp_path: Path, body: str, match: str):
    with pytest.raises(ConfigError, match=match):
        ConfidenceConfig.from_yaml(write_yaml(tmp_path, body))


def test_missing_file_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        ConfidenceConfig.from_yaml(tmp_path / "nope.yaml")


# --- pipeline knobs ---------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"causal_window_minutes": 0}, "CAUSAL_WINDOW_MINUTES"),
        ({"citation_coverage_threshold": 1.5}, "CITATION_COVERAGE_THRESHOLD"),
        ({"citation_coverage_threshold": 0.0}, "CITATION_COVERAGE_THRESHOLD"),
        ({"max_validation_retries": -1}, "MAX_VALIDATION_RETRIES"),
        ({"max_events_per_run": 0}, "MAX_EVENTS_PER_RUN"),
    ],
)
def test_pipeline_config_rejects_nonsense(kwargs: dict[str, object], match: str):
    valid = {
        "causal_window_minutes": 15,
        "citation_coverage_threshold": 0.95,
        "max_validation_retries": 2,
        "max_events_per_run": 5000,
    }
    with pytest.raises(ConfigError, match=match):
        PipelineConfig(**{**valid, **kwargs})  # type: ignore[arg-type]


# --- environment loading ----------------------------------------------------
def test_mock_provider_needs_no_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cfg = load_config(env_file=Path("/nonexistent.env"))
    assert cfg.llm.provider == "mock"
    assert cfg.pipeline.citation_coverage_threshold == pytest.approx(0.95)


def test_anthropic_provider_without_key_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config(env_file=Path("/nonexistent.env"))


def test_unknown_provider_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")

    with pytest.raises(ConfigError, match="must be one of"):
        load_config(env_file=Path("/nonexistent.env"))


def test_openrouter_provider_gets_base_url_and_free_model_default(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.delenv("ANALYST_MODEL", raising=False)
    monkeypatch.delenv("WRITER_MODEL", raising=False)

    cfg = load_config(env_file=Path("/nonexistent.env"))
    assert cfg.llm.base_url == "https://openrouter.ai/api/v1"
    assert cfg.llm.analyst_model.endswith(":free")


def test_openrouter_without_key_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config(env_file=Path("/nonexistent.env"))


def test_anthropic_key_does_not_satisfy_openrouter(monkeypatch: pytest.MonkeyPatch):
    """Keys are per-provider — a stale ANTHROPIC_API_KEY must not look valid."""
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("NEO4J_PASSWORD", "test")

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config(env_file=Path("/nonexistent.env"))


def test_mock_provider_never_carries_a_key(monkeypatch: pytest.MonkeyPatch):
    """CI runs under mock; a real key must not leak into that config."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")

    cfg = load_config(env_file=Path("/nonexistent.env"))
    assert cfg.llm.api_key is None
    assert cfg.llm.base_url is None


def test_relative_source_paths_resolve_against_project_root(
    monkeypatch: pytest.MonkeyPatch,
):
    """A run must behave the same from any CWD — k3s starts it in /app."""
    from agent.config import PROJECT_ROOT

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.setenv("LOG_SOURCE_PATH", "./evals/fixtures/logs")

    cfg = load_config(env_file=Path("/nonexistent.env"))
    assert cfg.sources.log_source_path == PROJECT_ROOT / "evals" / "fixtures" / "logs"
    assert cfg.sources.log_source_path.is_absolute()


def test_absolute_source_paths_are_left_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.setenv("ALERTS_FIXTURE", "/var/data/alerts.json")

    cfg = load_config(env_file=Path("/nonexistent.env"))
    assert cfg.sources.alerts_fixture == Path("/var/data/alerts.json")


# --- PROMPTS_DIR: unset means "use the image's baked-in prompts" ----------
def test_prompts_dir_defaults_to_none(monkeypatch: pytest.MonkeyPatch):
    """Unset must mean "no override", not a project-relative guess — the
    baked-in prompts are the fallback, and there is nothing to resolve
    against PROJECT_ROOT when nothing was configured."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.delenv("PROMPTS_DIR", raising=False)

    cfg = load_config(env_file=Path("/nonexistent.env"))
    assert cfg.prompts_dir is None


def test_prompts_dir_set_resolves_absolute(monkeypatch: pytest.MonkeyPatch):
    """A k3s ConfigMap mounts to an absolute path; a relative one still must
    not depend on the process's CWD (k3s starts it in /app)."""
    from agent.config import PROJECT_ROOT

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.setenv("PROMPTS_DIR", "/config/prompts")

    cfg = load_config(env_file=Path("/nonexistent.env"))
    assert cfg.prompts_dir == Path("/config/prompts")

    monkeypatch.setenv("PROMPTS_DIR", "./local-prompts")
    cfg2 = load_config(env_file=Path("/nonexistent.env"))
    assert cfg2.prompts_dir == PROJECT_ROOT / "local-prompts"
