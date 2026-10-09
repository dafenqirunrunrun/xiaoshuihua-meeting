"""Citation validation for referee reports (ADR-002 §5).

PURE: report content in, report content out. No DB, no LLM, no settings.

The prompt asks the model to cite segment numbers and quote verbatim. Asking is
not enforcing. This module is the enforcement, and it is a plain function
precisely because anti-hallucination must be DETERMINISTIC — a second model call
to check the first model's citations would add a new failure mode rather than
remove one.

Three checks, in order of how badly a violation would mislead a reader:

  1. **The citation exists.** Floors are the window bounds, not 1: a model that
     cites segment 3 when the window starts at 40 is referencing something it
     was never shown. A citation outside the window is dropped.
  2. **The quote is verbatim.** `quote` must be a substring of the cited
     segment's text, modulo whitespace and the punctuation ASR inserts
     inconsistently. A fabricated quote is the most convincing possible
     hallucination — it reads as primary-source evidence — so a non-matching
     quote is dropped, never softened into a paraphrase.
  3. **Dangling seqs are pruned.** Every `source_segment_seqs` entry must fall
     inside the window. Dropping the dangling numbers is better than dropping
     the whole claim: the claim may be legitimate and merely over-cited.

Nothing here is silently "repaired". Over-citation is pruned, and the pruning is
REPORTED in the returned `dropped` counts so the caller can record that the
report was degraded rather than passing it off as fully grounded.

Deliberately NOT done here: deciding whether a citation actually SUPPORTS its
claim. That is a judgement call, not a fact check — it would need another model,
and it is out of scope for V0 (see ADR-002 §9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.captain.schemas import CaptainReportContent, EvidenceItem, PointItem, Position
from app.transcript.segment_model import TranscriptSegment

# Runs of whitespace, and the CJK/ASCII punctuation ASR varies between runs.
# Matched loosely on purpose: "我们，下周上线" and "我们下周上线" are the same
# utterance and must not fail the substring check over a comma.
_LOOSE = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】]+")


def _loose(text: str) -> str:
    """Normalised comparison form: punctuation and whitespace removed."""
    return _LOOSE.sub("", text or "")


@dataclass
class EvidenceValidation:
    """The cleaned report plus an audit trail of what was removed."""

    content: CaptainReportContent
    dropped_evidence: int = 0
    dropped_citations: int = 0
    # Non-empty when evidence was removed; the caller folds this into the
    # report's own `uncertainty` so the user is told the grounding is partial.
    notes: list[str] = field(default_factory=list)


def _valid_seqs(seqs: list[int], window_seqs: set[int]) -> list[int]:
    """Keep only seqs that exist inside the window, order-preserving, unique."""
    seen: list[int] = []
    for seq in seqs:
        if seq in window_seqs and seq not in seen:
            seen.append(seq)
    return seen


def _prune_point(item: PointItem, window_seqs: set[int]) -> tuple[PointItem, bool]:
    kept = _valid_seqs(item.source_segment_seqs, window_seqs)
    changed = kept != item.source_segment_seqs
    return item.model_copy(update={"source_segment_seqs": kept}), changed


def validate_evidence(
    content: CaptainReportContent,
    segments: list[TranscriptSegment],
) -> EvidenceValidation:
    """Prune citations that do not resolve, and quotes that are not verbatim.

    `segments` is the SAME window handed to the model, so the set of legal seqs
    is exactly what the model could have read.
    """
    by_seq = {seg.segment_seq: seg for seg in segments}
    window_seqs = set(by_seq)

    dropped_citations = 0
    dropped_evidence = 0
    notes: list[str] = []

    # --- positions ----------------------------------------------------------
    positions: list[Position] = []
    for item in content.positions:
        pruned, changed = _prune_point(item, window_seqs)
        dropped_citations += int(changed)
        positions.append(pruned)

    # --- agreement / dispute points ----------------------------------------
    agreements: list[PointItem] = []
    for item in content.agreement_points:
        pruned, changed = _prune_point(item, window_seqs)
        dropped_citations += int(changed)
        agreements.append(pruned)

    disputes: list[PointItem] = []
    for item in content.dispute_points:
        pruned, changed = _prune_point(item, window_seqs)
        dropped_citations += int(changed)
        disputes.append(pruned)

    # --- evidence: existence AND verbatim quote ----------------------------
    evidence: list[EvidenceItem] = []
    for item in content.evidence:
        seg = by_seq.get(item.segment_seq)
        if seg is None:
            dropped_evidence += 1
            continue
        if _loose(item.quote) not in _loose(seg.text):
            # Fabricated or paraphrased quote: drop the item entirely. Keeping
            # it with an edited quote would present our own words as the
            # speaker's.
            dropped_evidence += 1
            continue
        evidence.append(item)

    if dropped_evidence:
        notes.append(f"{dropped_evidence} 条证据引用未能在原文中核对，已剔除")
    if dropped_citations:
        notes.append(f"{dropped_citations} 条论据的转写编号不在本次分析窗口内，已清理")

    cleaned = content.model_copy(
        update={
            "positions": positions,
            "agreement_points": agreements,
            "dispute_points": disputes,
            "evidence": evidence,
        }
    )
    return EvidenceValidation(
        content=cleaned,
        dropped_evidence=dropped_evidence,
        dropped_citations=dropped_citations,
        notes=notes,
    )


__all__ = ["EvidenceValidation", "validate_evidence"]
