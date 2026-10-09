"""External-evidence boundary enforcement (Phase 3-2 §7/§3).

PURE: provider results in, report-shaped external evidence out. No DB, no LLM,
no network, no settings.

This module exists to make one thing structurally impossible: mixing the two
evidence layers. The two have DIFFERENT guarantees and must never be merged:

    transcript evidence   verified against persisted segments. The system is
                          the authority. Validated by `captain/evidence.py`,
                          which can prove a quote is real because it has the
                          transcript.
    external evidence     NOT verifiable by this system. The search provider is
                          the authority. There is nothing to check a web
                          snippet against, so the honesty mechanism is not
                          "verify it" but "only ever emit what a provider
                          actually returned".

The consequence, and the reason this is a function rather than a comment:

  * `ExternalEvidence` entries are constructed HERE from `SearchResult`s, never
    parsed out of model output. The model is given `[web_1]`..`[web_n]` blocks
    in its prompt and may REFER to them, but it can never produce one: the
    report's `external_evidence` list is overwritten from this function's
    output after generation. A hallucinated `web_9` therefore has nowhere to go.
  * `source_id` values are assigned by position, so they always start at
    `web_1` and are contiguous — the model cannot be handed a gap it might
    "helpfully" fill.
  * Nothing here touches `segment_seq`. An external source has no segment
    number, which is what keeps the transcript validator from ever seeing one.

De-duplication happens upstream (in `search/service.py`, before the prompt is
built) so that the ids the model reads are the ids stored here. If this module
renumbered, the model's citations would silently point at the wrong source.
"""

from __future__ import annotations

from app.captain.schemas import ExternalEvidence
from app.captain.search.schemas import SearchResult

# Local handle prefix. Kept as a constant so the prompt builder and this module
# cannot disagree about what the model is looking at.
SOURCE_ID_PREFIX = "web_"


def source_id_for(index: int) -> str:
    """1-based local handle for the `index`-th (0-based) result."""
    return f"{SOURCE_ID_PREFIX}{index + 1}"


def to_external_evidence(results: list[SearchResult]) -> list[ExternalEvidence]:
    """Provider results -> report-ready external evidence.

    Every provider result becomes exactly one entry, in provider order, with a
    contiguous id. Results that carry neither a title nor a context are skipped:
    an entry with no content would occupy a `[web_n]` slot in the prompt while
    telling the model nothing, and would let a later source's citation appear to
    point at an empty one.

    Never raises. `SearchResult` is already validated, so the only work here is
    projection — but it stays a total function so a caller never needs a guard.
    """
    evidence: list[ExternalEvidence] = []
    for result in results:
        if not (result.title or result.snippet):
            continue
        evidence.append(
            ExternalEvidence(
                source_id=source_id_for(len(evidence)),
                title=result.title,
                url=result.url,
                context=result.snippet,
                domain=result.domain,
                retrieved_at=result.retrieved_at,
                provider=result.provider,
                source_quality=result.source_quality,
                answers_question=result.answers_question,
                # Phase C.5 fields, projected straight across. They are DERIVED
                # in `search/service.py::_stamp` (from the URL, the snippet and
                # the claim) and never here: this module is a projection, and a
                # second derivation site is a second thing that can disagree.
                source_type=result.source_type,
                relation=result.relation,
                query=result.query,
                evidence_summary=result.evidence_summary,
                published_at=result.published_at,
            )
        )
    return evidence


def render_for_prompt(evidence: list[ExternalEvidence]) -> str:
    """The `<external_evidence>` body shown to the model.

    Format is deliberately parallel to the `<discussion_window>` renderer
    (`[seq] text`) so the two blocks are visually distinguishable at a glance:
    transcript lines are `[12] …`, web sources are `[web_1] …`. The model is
    told which block is which and that they carry different authority.

    Empty input renders an explicit "none" line rather than an empty block, so
    the model is never left guessing whether the block was omitted by mistake.
    """
    if not evidence:
        return "(本次没有可用的外部资料)"
    blocks: list[str] = []
    for item in evidence:
        lines = [f"[{item.source_id}]"]
        if item.title:
            lines.append(f"title: {item.title}")
        if item.domain or item.url:
            lines.append(f"source: {item.domain or item.url}")
        # Quality and the claim being answered are shown because they change how
        # the model should WEIGH the source. Without them the model sees a
        # snippet with no provenance and treats every page as equally
        # authoritative, which is exactly the failure `source_quality` exists to
        # prevent. Both are pipeline-assigned, never provider-or-model-supplied.
        if item.source_quality:
            lines.append(f"source_quality: {item.source_quality}")
        # `source_type` is a SECOND, independent axis (Phase C.5): what the
        # source IS, not how much it is trusted. Rendered next to quality so the
        # model cannot read one as the other — a community post quoting the
        # standard is `community` by type and `reputable` by trust, and the
        # reader needs both facts to weigh it.
        if item.source_type:
            lines.append(f"source_type: {item.source_type}")
        # How the source bears on the claim. Usually `contextual`, which is the
        # honest answer for a passage that merely mentions the topic; the model
        # is told so it does not upgrade a mention into corroboration.
        if item.relation:
            lines.append(f"relation: {item.relation}")
        if item.answers_question:
            lines.append(f"answers_claim: {item.answers_question}")
        # Only rendered when the PROVIDER supplied a date. Never inferred here:
        # an empty line is the truthful state and a guessed one is a fabricated
        # credential the reader cannot detect.
        if item.published_at:
            lines.append(f"published_at: {item.published_at}")
        if item.context:
            lines.append(f"context: {item.context}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


__all__ = [
    "SOURCE_ID_PREFIX",
    "render_for_prompt",
    "source_id_for",
    "to_external_evidence",
]
