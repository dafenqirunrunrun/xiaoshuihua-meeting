"""Context window assembly for a referee report (ADR-002 §4/§6).

PURE with respect to the DB: the caller reads the persisted segments once and
hands them in. No session, no settings lookups beyond the values passed, so the
whole selection policy is trivially unit-testable — the same stance
`agent/evidence.py` takes for the summary pipeline.

Two hard rules, both of which exist to protect the product promise
-----------------------------------------------------------------

**Nothing after the anchor.** The window is `segment_seq <= anchor_segment_seq`
inclusive of the past, never the future. The trigger word is spoken and the
conversation continues while the model runs; every one of those later segments
would be content the referee was never asked about. Feeding them in would make
the report describe a discussion that had not happened yet when the user asked
for it.

**Whether the anchor itself is content depends on WHY it was triggered.** The
anchor segment carries two different meanings that were originally conflated
into one field, and the conflation silently dropped material:

  - **Wake-word anchor.** The segment *is* the wake phrase ("水华队长，你说说").
    It is not discussion content, and leaving it in is how it ends up
    paraphrased into `discussion_summary` as somebody's argument. Excluded.

  - **Button anchor.** The segment is the current FINAL high-water mark
    (`_resolve_anchor` falls back to `max_seq`). It is an ordinary participant
    utterance — often the MOST relevant one, being the newest. Excluding it
    dropped the latest thing said, every time, with no signal to the reader.

That second case is not hypothetical. On a five-segment discussion whose last
line was "…不过这个问题我们上周已经改了连接池参数", the button path handed the
model seqs 1..4 and the referee's report never mentioned the connection-pool
change — the single fact that decides whether the historical incident still
applies. The evidence validator then had nothing to validate against, because
the segment was never in the window to begin with.

So exclusion is a property of the TRIGGER, not of the field. `exclude_anchor`
must be stated by whoever resolves the anchor, because only they know which
kind it is.

Why `start_ms` and not wall clock
---------------------------------
`start_ms` is ASR-relative meeting time, which is the axis the audio actually
lives on. Wall clock would need a second anchor (the meeting's `started_at`
plus the current time) and would break on a paused/restarted meeting. Using
`start_ms` also keeps the window query entirely inside columns that already
exist — no schema change, and no new index: `meeting_id` is already indexed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.transcript.segment_model import TranscriptSegment


@dataclass(frozen=True)
class ContextWindow:
    """The selected window plus enough metadata to explain and audit it."""

    segments: list[TranscriptSegment]
    # Inclusive seq bounds of what was actually selected (0/0 when empty).
    from_seq: int
    window_to_seq: int
    # True when the char bound dropped older candidates.
    truncated: bool
    # How many candidate segments the char bound dropped.
    dropped_count: int
    # True when a segment had no `start_ms` and position-based selection was
    # used instead; surfaced so the report can disclose reduced precision.
    used_position_fallback: bool

    @property
    def is_empty(self) -> bool:
        return not self.segments


def select_window(
    segments: list[TranscriptSegment],
    *,
    anchor_segment_seq: int,
    window_seconds: float,
    max_chars: int,
    min_segments: int,
    exclude_anchor: bool = True,
) -> ContextWindow:
    """Choose the referee's context window.

    `segments` is the full persisted FINAL list ASC (the caller reads it once
    with `transcript_repository.list_sorted`). Selection runs in three passes:

      1. BOUND the past: keep `segment_seq <= anchor`, optionally drop the
         anchor itself, and drop anything with seq <= 0.
      2. BOUND the time: keep segments whose `start_ms` falls inside
         `[anchor_start - window_seconds, anchor_start]`.
      3. BOUND the size: walking BACKWARDS from the anchor, accumulate until
         `max_chars` is reached, then stop.

    `exclude_anchor` defaults to True because the wake-word path is the stricter
    of the two and the one where a mistake corrupts the report's CONTENT (the
    wake phrase becoming a "position"). Callers that resolved the anchor to a
    plain high-water mark must pass False — see the module docstring for why.

    Pass 3 walks backwards on purpose. When the transcript is too big to fit,
    what survives must be the discussion IMMEDIATELY before the trigger — that
    is the material the user was thinking of when they called the referee.
    Keeping the oldest lines instead would produce a report about a
    conversation that had already moved on.

    If the anchor has no `start_ms` (some ASR paths omit timing), pass 2 is
    skipped entirely and the newest `min_segments`-worth of material is used
    from the seq-bounded set, with `used_position_fallback=True` recorded so the
    caller can disclose it. Refusing to referee at all because one timestamp is
    missing would be a worse failure than a slightly wider window.
    """
    if anchor_segment_seq <= 0 or not segments:
        return ContextWindow([], 0, 0, False, 0, False)

    # --- pass 1: the anchor (per `exclude_anchor`) and everything after -----
    past = [
        seg
        for seg in segments
        if seg.segment_seq <= anchor_segment_seq and seg.segment_seq > 0
    ]
    anchor = next(
        (seg for seg in past if seg.segment_seq == anchor_segment_seq), None
    )
    if exclude_anchor:
        # The trigger utterance is never context.
        candidates = [seg for seg in past if seg.segment_seq != anchor_segment_seq]
    else:
        # A high-water-mark anchor is ordinary discussion content and must stay.
        candidates = list(past)

    # --- pass 2: time bound ------------------------------------------------
    used_position_fallback = False
    if anchor is not None and anchor.start_ms is not None:
        floor_ms = anchor.start_ms - int(max(0.0, window_seconds) * 1000)
        candidates = [
            seg
            for seg in candidates
            if seg.start_ms is not None and floor_ms <= seg.start_ms <= anchor.start_ms
        ]
    else:
        # No timing on the anchor: fall back to pure recency.
        used_position_fallback = True

    # --- pass 3: size bound, newest-first accumulation ---------------------
    budget = max(0, max_chars)
    kept: list[TranscriptSegment] = []
    used = 0
    for seg in sorted(candidates, key=lambda s: s.segment_seq, reverse=True):
        cost = len(seg.text or "")
        # Always let the first segment in, even if it alone exceeds the budget:
        # an empty window is useless, and one long utterance is a legitimate
        # thing to be asked about.
        if kept and used + cost > budget:
            break
        kept.append(seg)
        used += cost

    selected = sorted(kept, key=lambda s: s.segment_seq)
    dropped = len(candidates) - len(selected)

    if len(selected) < max(1, min_segments):
        # Not enough material to referee. Reported as an EMPTY window rather
        # than a thin one, so the caller refuses the trigger instead of asking
        # the model for a verdict on one sentence.
        return ContextWindow([], 0, anchor_segment_seq, dropped > 0, dropped,
                             used_position_fallback)

    return ContextWindow(
        segments=selected,
        from_seq=selected[0].segment_seq,
        window_to_seq=anchor_segment_seq,
        truncated=dropped > 0,
        dropped_count=dropped,
        used_position_fallback=used_position_fallback,
    )


__all__ = ["ContextWindow", "select_window"]
