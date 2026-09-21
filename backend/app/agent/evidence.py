"""Evidence selection for the Final Summary (Step AGENT-2 §4).

Normal Final Summary runs now include the whole transcript for completeness.
This module owns the capacity fallback for an oversized prompt: Meeting State
says **what mattered**, and this code decides **which transcript lines survive**
when the full transcript cannot fit safely.

The selection is deliberately simple, explainable and testable — no RAG, no
embeddings, no vector store (explicitly out of scope for this step):

  1. Take the `source_segment_seqs` the Meeting State already recorded on its
     decisions / action items / open questions / risks / topics.
  2. Optionally widen each cited seq by a small neighbour window (a decision
     cited at seq 8 is easier to judge with 7 and 9 beside it).
  3. Order the result by EVIDENCE PRIORITY then by segment_seq ASC, and cut at
     `max_evidence_segments` if the meeting was pathological.

Priority exists so the bound degrades gracefully: if the transcript is huge and
the cap bites, what survives is the grounding for the *load-bearing* items
(decisions and action items) rather than an arbitrary prefix. Ordering by seq
ASC *within* that keeps the quoted material readable in meeting order.

This module is pure: it takes segments in, returns segments out. No DB, no
settings lookups beyond what the caller passes, so it is trivially unit-testable
and the caller owns every policy knob.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.meeting_state.schemas import MeetingStateContent
from app.transcript.segment_model import TranscriptSegment

# Priority tiers, lowest number = most load-bearing grounding. A decision or an
# action item is a commitment the summary will assert, so its evidence is the
# last thing we would drop; a topic's is context.
PRIORITY_DECISION = 0
PRIORITY_ACTION_ITEM = 1
PRIORITY_RISK = 2
PRIORITY_OPEN_QUESTION = 3
PRIORITY_TOPIC = 4


@dataclass(frozen=True)
class EvidenceSelection:
    """The chosen evidence plus enough metadata to explain and assert on it."""

    segments: list[TranscriptSegment]
    cited_seqs: list[int]           # seqs cited by the state (before widening)
    truncated: bool                 # True when the cap dropped candidates
    dropped_count: int              # how many candidates the cap dropped


def _priority_of(content: MeetingStateContent, seq: int) -> int | None:
    """Best (lowest) priority tier in which `seq` is cited, else None."""
    best: int | None = None

    def consider(items, tier: int, extract) -> None:
        nonlocal best
        for item in items:
            if seq in extract(item):
                if best is None or tier < best:
                    best = tier

    consider(content.decisions, PRIORITY_DECISION, lambda i: i.source_segment_seqs)
    consider(content.action_items, PRIORITY_ACTION_ITEM, lambda i: i.source_segment_seqs)
    consider(content.risks, PRIORITY_RISK, lambda i: i.source_segment_seqs)
    consider(content.open_questions, PRIORITY_OPEN_QUESTION, lambda i: i.source_segment_seqs)
    consider(content.topics, PRIORITY_TOPIC, lambda i: i.source_segment_seqs)
    return best


def cited_segment_seqs(content: MeetingStateContent) -> list[int]:
    """Every seq the state cites, deduplicated, ASC.

    Deduplication matters for more than tidiness: the same line is routinely
    cited by two items (the decision AND the action item it implies), and
    quoting it twice would waste the evidence budget and read as two facts.
    """
    seen: set[int] = set()
    for items in (
        content.decisions,
        content.action_items,
        content.risks,
        content.open_questions,
        content.topics,
    ):
        for item in items:
            seen.update(item.source_segment_seqs)
    return sorted(seen)


def select_evidence(
    content: MeetingStateContent,
    segments: list[TranscriptSegment],
    *,
    max_evidence_segments: int,
    neighbor_window: int = 1,
) -> EvidenceSelection:
    """Choose the transcript evidence grounding `content`.

    `segments` is the full persisted FINAL list ASC (the caller reads it once).
    Only seqs that actually EXIST are quoted — a state citing seq 99 in a
    6-segment meeting must not conjure one, and a dangling citation is dropped
    silently here (the conflict rules in the prompt are what handle a state
    whose citation exists but does not support it).
    """
    by_seq = {seg.segment_seq: seg for seg in segments}
    cited = cited_segment_seqs(content)

    # --- candidate expansion: citations + neighbour window ------------------
    # priority is per-CANDIDATE and inherited from the citation that pulled it
    # in, so a neighbour of a decision outranks a directly-cited topic.
    priority: dict[int, int] = {}
    for seq in cited:
        tier = _priority_of(content, seq)
        if tier is None:  # unreachable (it was cited), kept for safety
            tier = PRIORITY_TOPIC
        priority[seq] = tier
        for delta in range(1, max(0, neighbor_window) + 1):
            for cand in (seq - delta, seq + delta):
                if cand in by_seq and cand not in priority:
                    # a neighbour is CONTEXT, so it ranks just after the item it
                    # supports rather than at its own (possibly absent) tier
                    priority[cand] = tier

    ordered = sorted(
        (seq for seq in priority if seq in by_seq),
        key=lambda s: (priority[s], s),
    )

    truncated = len(ordered) > max_evidence_segments
    dropped = len(ordered) - max_evidence_segments if truncated else 0
    chosen = ordered[:max_evidence_segments]

    return EvidenceSelection(
        # final read order is meeting order (seq ASC) so the model reads the
        # quoted material the way it was spoken, not in priority order
        segments=[by_seq[s] for s in sorted(chosen)],
        cited_seqs=cited,
        truncated=truncated,
        dropped_count=dropped,
    )


__all__ = [
    "EvidenceSelection",
    "cited_segment_seqs",
    "select_evidence",
    "PRIORITY_ACTION_ITEM",
    "PRIORITY_DECISION",
    "PRIORITY_OPEN_QUESTION",
    "PRIORITY_RISK",
    "PRIORITY_TOPIC",
]
