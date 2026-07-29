"""Recurrence memory: close the loop after a successful run.

Evidence plane, post-run, per docs/01 — no LLM call here. `find_similar_incidents`
and `compute_fingerprint` (in `memory.py`) are the read half, called during the
run itself so the Writer's document can show recurrence context. This is the
write half: after a document is accepted, the bullets the Writer proposed in
its Corrective Actions section become `CorrectiveAction` nodes, so the *next*
structurally similar incident can ask whether this one's fix ever landed.

Extraction reuses the validator's sentence splitter rather than writing a
second markdown parser. The Writer's own section-exemption rule already
identifies exactly the text this module wants: "Corrective Actions" bullets are
proposals, not citable facts, which is why the validator never asked them to
carry a `[src:...]` tag in the first place.
"""

from __future__ import annotations

from agent.memory import Neo4jMemory
from agent.nodes.validator import split_sentences
from agent.state import make_corrective_action_id

#: Matches `EXEMPT_SECTIONS` in `validator.py` for the one section this module
#: cares about. Duplicated rather than imported as a single constant, because
#: coupling extraction to the validator's full exemption list would silently
#: start pulling in "Open Questions" bullets too if that list ever grows.
_CORRECTIVE_ACTIONS_SECTION: str = "corrective actions"


def extract_corrective_actions(document: str) -> list[str]:
    """Pull the Corrective Actions bullets out of a published document.

    Order-preserving, de-duplicated. A bullet that survived to publication
    already cleared validation, so nothing here re-checks it — this module
    only decides *which* sentences are corrective actions, not whether they
    were written well.
    """
    seen: dict[str, None] = {}
    for sentence in split_sentences(document):
        if _CORRECTIVE_ACTIONS_SECTION not in sentence.section:
            continue
        text = sentence.text.strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def persist_corrective_actions(
    memory: Neo4jMemory, incident_id: str, document: str
) -> list[str]:
    """Extract and write. Every new action starts `open` — completion is a
    fact about the world this pipeline cannot observe, so it is never guessed.

    Idempotent like everything else `memory.py` writes: re-publishing the same
    document `MERGE`s onto the same `CorrectiveAction` nodes rather than
    duplicating them, and `add_corrective_action` will not stomp a status a
    human has since marked done.
    """
    ids: list[str] = []
    for description in extract_corrective_actions(document):
        action_id = make_corrective_action_id(incident_id, description)
        ids.append(
            memory.add_corrective_action(
                incident_id, action_id, description, status="open"
            )
        )
    return ids
