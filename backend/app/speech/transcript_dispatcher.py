"""Dispatch Tencent sentence results into the existing Transcript Domain."""

from __future__ import annotations

import asyncio
import logging

from app.db import SessionLocal
from app.device.meeting_binding import get_active_meeting
from app.speech.result_parser import TencentSentenceResult
from app.speech.transcript_bridge import to_asr_transcript_event
from app.transcript import asr_service

logger = logging.getLogger(__name__)

NO_ACTIVE_MEETING_LOG_INTERVAL = 25


def dispatch_sentence_result(
    device_id: str, sentence: TencentSentenceResult
) -> str | None:
    """Resolve the current meeting and ingest one result on a fresh DB session."""
    db = SessionLocal()
    try:
        meeting_id = get_active_meeting(db, device_id)
        if meeting_id is None:
            return None
        event = to_asr_transcript_event(sentence, meeting_id=meeting_id)
        asr_service.ingest_asr_event(db, meeting_id, event)
        return meeting_id
    finally:
        db.close()


async def transcript_dispatch_worker(
    queue: asyncio.Queue[TencentSentenceResult | None],
    device_id: str,
) -> None:
    """Drain one connection's Tencent result queue without blocking its loop."""
    skipped_without_meeting = 0
    while True:
        sentence = await queue.get()
        try:
            if sentence is None:
                return
            try:
                meeting_id = await asyncio.to_thread(
                    dispatch_sentence_result, device_id, sentence
                )
            except Exception:
                logger.exception(
                    "[TRANSCRIPT] ingest failed device=%s voice_id=%s sentence_id=%s",
                    device_id,
                    sentence.voice_id,
                    sentence.sentence_id,
                )
                continue

            if meeting_id is None:
                skipped_without_meeting += 1
                if (
                    skipped_without_meeting == 1
                    or skipped_without_meeting % NO_ACTIVE_MEETING_LOG_INTERVAL == 0
                ):
                    logger.info(
                        "[TRANSCRIPT] no active meeting device=%s, sentence skipped",
                        device_id,
                    )
            elif sentence.sentence_type == 1:
                # The Tencent callback path does not have FastAPI
                # BackgroundTasks. Notify the coalescing scheduler only after
                # the FINAL is durably committed by dispatch_sentence_result.
                from app.meeting_state.scheduler import meeting_state_scheduler

                meeting_state_scheduler.schedule(meeting_id)
        finally:
            queue.task_done()


__all__ = ["dispatch_sentence_result", "transcript_dispatch_worker"]
