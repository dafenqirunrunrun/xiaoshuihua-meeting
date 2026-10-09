"""Wake word detection for 水华队长 (ADR-002 §6).

A PURE function over FINAL text. No IO, no DB, no LLM, no import of any other
business module — which is what makes it exhaustively unit-testable and keeps
the referee's activation rule in exactly ONE place.

Why detection lives server-side
-------------------------------
The frontend must never decide whether the wake word was said. If it did, the
rule would exist twice (browser + server) and the two copies would drift — and
the drift would be invisible, because a missed detection is silence, not an
error. The client only ever calls the trigger endpoint; the decision is here.

Why FINAL only
--------------
PARTIAL is not a fact source (ADR D4): it is a mutable live-cache frame that is
never persisted and is dropped on process restart. Triggering a report from
text that may still be revised would let a mis-transcribed "水华队长" start an
LLM call, and would make the anchor un-referenceable afterwards — the segment it
pointed at would not exist in the transcript. FINAL segments are durable, so the
anchor survives a refresh and the dedupe key is stable.
"""

from __future__ import annotations

import re
import unicodedata

from app.config import settings

# Anything that is not a letter/number in ANY script. Punctuation and spacing
# around the wake word are noise: ASR inserts commas and the speaker may pause
# mid-phrase, so "水华队长，你怎么看" and "水华队长你怎么看" must match the same way.
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize(text: str) -> str:
    """Casefold, width-fold and strip punctuation/whitespace for comparison.

    NFKC is the load-bearing step: it maps full-width forms (common in Chinese
    input and in some ASR engines) onto their ASCII equivalents, so a wake word
    typed or recognised as "ｓｈｕｉ" or with full-width brackets still matches.

    Stripping ALL non-word characters means a match cannot be broken by a stray
    comma, a space, or the trailing period an ASR engine likes to append.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _NON_WORD.sub("", folded)


def configured_wake_words() -> list[str]:
    """The configured wake phrases, normalised and de-duplicated.

    Read from settings rather than hardcoded so a deployment can retune the
    phrasing without a code change. A blank entry is dropped instead of matching
    everything (an empty needle is a substring of every haystack).
    """
    raw = settings.captain_wake_words or ""
    seen: list[str] = []
    for part in raw.split(","):
        needle = normalize(part)
        if needle and needle not in seen:
            seen.append(needle)
    return seen


def match(text: str) -> bool:
    """True iff `text` contains a configured wake word.

    Substring, not equality: the wake word is spoken INSIDE a sentence
    ("那水华队长你觉得呢"), so the trigger has to survive surrounding words. The
    false-positive surface is bounded by the phrase being long and specific.
    """
    haystack = normalize(text)
    if not haystack:
        return False
    return any(needle in haystack for needle in configured_wake_words())


__all__ = ["match", "normalize", "configured_wake_words"]
