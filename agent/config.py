"""Single source of configuration truth.

Every environment variable and every YAML knob in this project is read *here*
and nowhere else. Modules take a `Config` (or the piece of it they need); they
never call `os.getenv` themselves. That is what makes the effective
configuration of a run inspectable in one place and recordable in the
`RunReport`.

Loading is strict on purpose. A malformed weight or a missing API key fails at
startup with a named error rather than silently producing a plausible-looking
confidence score — see invariant 8 in CLAUDE.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or self-inconsistent."""


#: Repo root — the directory containing ``pyproject.toml``. Relative paths in
#: the environment resolve against this rather than the CWD, so a run behaves
#: identically from a shell, from pytest, and from ``/app`` inside a container.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: The canonical signal names of the confidence model (docs/03). Declared here
#: because this module parses the weights file and must reject a typo loudly: a
#: misspelled key would otherwise drop a signal to zero weight and quietly skew
#: every score. `confidence.py` owns what the signals *mean*; this owns that the
#: set is exactly these five.
SIGNAL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "change_path_overlap",
        "log_signature_match",
        "metric_correlation",
        "temporal_proximity",
        "human_confirmation",
    }
)

LLMProvider = Literal["anthropic", "openrouter", "mock"]

#: Which env var holds the credential for each provider. `mock` is absent on
#: purpose — it must stay runnable with no secret at all, because CI and the
#: retry-loop tests depend on that (CLAUDE.md, "Testing expectations").
_API_KEY_VARS: Final[dict[LLMProvider, str]] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_DEFAULT_MODELS: Final[dict[LLMProvider, str]] = {
    "anthropic": "claude-sonnet-5",
    "openrouter": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "mock": "mock",
}

_OPENROUTER_BASE_URL: Final[str] = "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# environment helpers
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise ConfigError(f"{name} is not set and has no default")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def _env_provider(name: str, default: str) -> LLMProvider:
    raw = os.environ.get(name) or default
    if raw == "anthropic":
        return "anthropic"
    if raw == "openrouter":
        return "openrouter"
    if raw == "mock":
        return "mock"
    raise ConfigError(f"{name}={raw!r} must be one of: anthropic, openrouter, mock")


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name) or default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------
def _as_mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    bad = [k for k in value if not isinstance(k, str)]
    if bad:
        raise ConfigError(f"{where} has non-string keys: {bad!r}")
    return {str(k): v for k, v in value.items()}


def _as_float(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError(f"{where} must be finite, got {value!r}")
    return number


# ---------------------------------------------------------------------------
# config sections
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Which model answers, and over which wire.

    `base_url` is None for first-party providers and set for OpenAI-compatible
    gateways such as OpenRouter. Model *choice* stays config — the eval harness
    compares models, so swapping one must never require a code change.
    """

    provider: LLMProvider
    analyst_model: str
    writer_model: str
    api_key: str | None
    base_url: str | None = None

    def __post_init__(self) -> None:
        required = _API_KEY_VARS.get(self.provider)
        if required and not self.api_key:
            raise ConfigError(
                f"LLM_PROVIDER={self.provider} requires {required}. "
                "Use LLM_PROVIDER=mock to run without an API key."
            )
        if self.provider == "openrouter" and not self.base_url:
            raise ConfigError("LLM_PROVIDER=openrouter requires a base_url")


@dataclass(frozen=True, slots=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str


@dataclass(frozen=True, slots=True)
class ValkeyConfig:
    url: str


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    causal_window_minutes: int
    citation_coverage_threshold: float
    max_validation_retries: int
    max_events_per_run: int

    def __post_init__(self) -> None:
        if self.causal_window_minutes <= 0:
            raise ConfigError("CAUSAL_WINDOW_MINUTES must be > 0")
        if not 0.0 < self.citation_coverage_threshold <= 1.0:
            raise ConfigError("CITATION_COVERAGE_THRESHOLD must be in (0.0, 1.0]")
        if self.max_validation_retries < 0:
            raise ConfigError("MAX_VALIDATION_RETRIES must be >= 0")
        if self.max_events_per_run <= 0:
            raise ConfigError("MAX_EVENTS_PER_RUN must be > 0")


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    """Phase 1 collector inputs. Phase 4 swaps these for live API endpoints."""

    log_source_path: Path
    git_repo_path: Path
    alerts_fixture: Path


@dataclass(frozen=True, slots=True)
class ConfidenceConfig:
    """Parameters of the model in docs/03-confidence-model.md.

    Held as plain numbers; the arithmetic lives in `agent/confidence.py`, which
    is where it stays testable as pure functions.
    """

    weights: dict[str, float]
    contradiction_per_event: float
    contradiction_cap: float
    band_likely: float
    band_plausible: float

    def __post_init__(self) -> None:
        missing = SIGNAL_NAMES - self.weights.keys()
        unknown = self.weights.keys() - SIGNAL_NAMES
        if missing or unknown:
            raise ConfigError(
                f"confidence weights must be exactly {sorted(SIGNAL_NAMES)}; "
                f"missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        for name, weight in self.weights.items():
            if weight <= 0.0:
                raise ConfigError(f"weight {name}={weight} must be > 0")
        if self.contradiction_per_event <= 0.0:
            raise ConfigError("contradiction.per_event must be > 0")
        if not 0.0 < self.contradiction_cap <= 1.0:
            raise ConfigError("contradiction.cap must be in (0.0, 1.0]")
        if not 0.0 < self.band_plausible < self.band_likely <= 1.0:
            raise ConfigError(
                "bands must satisfy 0 < plausible < likely <= 1, got "
                f"plausible={self.band_plausible} likely={self.band_likely}"
            )

    @classmethod
    def from_yaml(cls, path: Path) -> ConfidenceConfig:
        if not path.is_file():
            raise ConfigError(f"confidence config not found: {path}")
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            # A parser traceback names a line, not the file it came from. In k3s
            # this file is a ConfigMap, so the path is the useful half.
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        root = _as_mapping(loaded, str(path))

        raw_weights = _as_mapping(root.get("weights"), f"{path}:weights")
        contradiction = _as_mapping(root.get("contradiction"), f"{path}:contradiction")
        bands = _as_mapping(root.get("bands"), f"{path}:bands")

        return cls(
            weights={
                name: _as_float(value, f"{path}:weights.{name}")
                for name, value in raw_weights.items()
            },
            contradiction_per_event=_as_float(
                contradiction.get("per_event"), f"{path}:contradiction.per_event"
            ),
            contradiction_cap=_as_float(
                contradiction.get("cap"), f"{path}:contradiction.cap"
            ),
            band_likely=_as_float(bands.get("likely"), f"{path}:bands.likely"),
            band_plausible=_as_float(bands.get("plausible"), f"{path}:bands.plausible"),
        )


@dataclass(frozen=True, slots=True)
class ServicesConfig:
    """Service-name canonicalization rules (`config/services.yaml`).

    Config rather than code because which names mean the same service is a fact
    about someone's infrastructure, not about this program.
    """

    aliases: dict[str, list[str]]
    strip_suffixes: list[str]

    @classmethod
    def from_yaml(cls, path: Path) -> ServicesConfig:
        if not path.is_file():
            # Canonicalization degrades to suffix-free identity rather than
            # failing the run: an absent alias map is a weaker signal, not a
            # broken pipeline.
            return cls(aliases={}, strip_suffixes=[])
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        root = _as_mapping(loaded or {}, str(path))

        raw_aliases = _as_mapping(root.get("aliases") or {}, f"{path}:aliases")
        aliases: dict[str, list[str]] = {}
        for canonical, listed in raw_aliases.items():
            if not isinstance(listed, list):
                raise ConfigError(
                    f"{path}:aliases.{canonical} must be a list, "
                    f"got {type(listed).__name__}"
                )
            aliases[canonical] = [str(alias) for alias in listed]

        suffixes = root.get("strip_suffixes") or []
        if not isinstance(suffixes, list):
            raise ConfigError(f"{path}:strip_suffixes must be a list")

        return cls(aliases=aliases, strip_suffixes=[str(s) for s in suffixes])


@dataclass(frozen=True, slots=True)
class Config:
    llm: LLMConfig
    neo4j: Neo4jConfig
    valkey: ValkeyConfig
    pipeline: PipelineConfig
    sources: SourcesConfig
    confidence: ConfidenceConfig
    services: ServicesConfig
    output_dir: Path


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_config(env_file: Path | None = None) -> Config:
    """Build a `Config` from the environment and `config/confidence.yaml`.

    Reads `.env` if present (existing environment variables win, so a shell
    export or a k3s Secret always overrides the file). Uncached — tests use this
    directly with `monkeypatch`; application code should call `get_config()`.
    """
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    provider = _env_provider("LLM_PROVIDER", "mock")
    key_var = _API_KEY_VARS.get(provider)

    return Config(
        llm=LLMConfig(
            provider=provider,
            analyst_model=_env_str("ANALYST_MODEL", _DEFAULT_MODELS[provider]),
            writer_model=_env_str("WRITER_MODEL", _DEFAULT_MODELS[provider]),
            api_key=(os.environ.get(key_var) if key_var else None) or None,
            base_url=(
                _env_str("OPENROUTER_BASE_URL", _OPENROUTER_BASE_URL)
                if provider == "openrouter"
                else None
            ),
        ),
        neo4j=Neo4jConfig(
            uri=_env_str("NEO4J_URI", "bolt://localhost:7687"),
            user=_env_str("NEO4J_USER", "neo4j"),
            password=_env_str("NEO4J_PASSWORD"),
        ),
        valkey=ValkeyConfig(url=_env_str("VALKEY_URL", "redis://localhost:6379/0")),
        pipeline=PipelineConfig(
            causal_window_minutes=_env_int("CAUSAL_WINDOW_MINUTES", 15),
            citation_coverage_threshold=_env_float("CITATION_COVERAGE_THRESHOLD", 0.95),
            max_validation_retries=_env_int("MAX_VALIDATION_RETRIES", 2),
            max_events_per_run=_env_int("MAX_EVENTS_PER_RUN", 5000),
        ),
        sources=SourcesConfig(
            log_source_path=_env_path("LOG_SOURCE_PATH", "./evals/fixtures/logs"),
            git_repo_path=_env_path("GIT_REPO_PATH", "./evals/fixtures/repo"),
            alerts_fixture=_env_path("ALERTS_FIXTURE", "./evals/fixtures/alerts.json"),
        ),
        confidence=ConfidenceConfig.from_yaml(
            _env_path("CONFIDENCE_CONFIG_PATH", "./config/confidence.yaml")
        ),
        services=ServicesConfig.from_yaml(
            _env_path("SERVICES_CONFIG_PATH", "./config/services.yaml")
        ),
        output_dir=_env_path("OUTPUT_DIR", "./out"),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide cached config. The entry point for application code."""
    return load_config()
