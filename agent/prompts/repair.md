Your previous draft failed mechanical validation. Below are the specific
defects found by code, and the same evidence you had before.

Fix **only** what is listed. Do not rewrite sections that were not complained
about — an unrelated rewrite risks breaking citations that already passed.

How to fix each kind of complaint:

- **missing_citation** — the sentence states a fact with no `[src:...]` tag.
  Either add a tag for an event that actually supports it, or delete the
  sentence. Do not attach a nearby ID that does not support the claim: that
  turns a visible defect into an invisible one.
- **hallucinated_id** — the cited ID does not exist in this incident's graph.
  It cannot be repaired by guessing a similar ID. Replace it with an ID from
  the evidence list, or remove the claim.
- **timestamp_mismatch** — the time stated in the sentence disagrees with the
  cited event's actual timestamp. Use the event's timestamp, exactly as given.
- **coverage_below_threshold** — too many factual sentences are uncited. Cite
  them, or remove the ones you cannot support. Removing an unsupportable claim
  is always an acceptable fix.

The same hard rules still apply: only IDs from the evidence, no facts that are
not in the evidence, no Timeline section, and hedging that matches each
hypothesis's confidence band.

Return the complete corrected markdown document, with no preamble and no
explanation of the changes.
