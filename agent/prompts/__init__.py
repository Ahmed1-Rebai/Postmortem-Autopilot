"""Prompt loading.

Prompts are `.md` files on disk, never inline strings, because in k3s they are
a ConfigMap: changing a prompt must not require rebuilding the image. The
directory is overridable so the mounted ConfigMap can replace the baked-in
copies without touching the code that loads them.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

_BUILTIN_DIR = Path(__file__).resolve().parent


class PromptError(RuntimeError):
    """A prompt is missing or empty."""


@cache
def load(name: str, directory: Path | None = None) -> str:
    """Read `<name>.md`. Cached — prompts do not change within a run."""
    base = directory or _BUILTIN_DIR
    path = base / f"{name}.md"
    if not path.is_file():
        raise PromptError(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise PromptError(f"prompt is empty: {path}")
    return text
