from sqlalchemy import func, select


def test_delete_created_meeting(client):
    meeting = client.post("/api/meetings", json={"title": "delete draft"}).json()

    response = client.delete(f"/api/meetings/{meeting['id']}")

    assert response.status_code == 204
    assert client.get(f"/api/meetings/{meeting['id']}").status_code == 404
    assert client.get("/api/meetings").json()["total"] == 0


def test_delete_rejects_active_meeting(client):
    meeting = client.post("/api/meetings", json={"title": "keep active"}).json()
    assert client.post(f"/api/meetings/{meeting['id']}/start").status_code == 200

    response = client.delete(f"/api/meetings/{meeting['id']}")

    assert response.status_code == 409
    assert client.get(f"/api/meetings/{meeting['id']}").status_code == 200


def test_delete_completed_meeting_removes_all_durable_children(client):
    from app.agent.summary_model import MeetingSummary
    from app.db import SessionLocal
    from app.meeting.meeting_model import Meeting
    from app.meeting_state.model import MeetingState
    from app.meeting_state.snapshot_model import (
        MeetingMemorySnapshot,
        MeetingStateHealth,
    )
    from app.transcript.segment_model import TranscriptSegment
    from app.transcript.stream_model import TranscriptStream

    meeting = client.post("/api/meetings", json={"title": "delete complete"}).json()
    meeting_id = meeting["id"]
    with SessionLocal() as db:
        row = db.get(Meeting, meeting_id)
        assert row is not None
        row.status = "COMPLETED"
        db.add_all(
            [
                MeetingSummary(meeting_id=meeting_id, version=1, status="COMPLETED"),
                MeetingState(meeting_id=meeting_id),
                MeetingMemorySnapshot(
                    meeting_id=meeting_id,
                    from_segment_seq=1,
                    to_segment_seq=1,
                    state_json="{}",
                ),
                MeetingStateHealth(meeting_id=meeting_id),
                TranscriptSegment(
                    meeting_id=meeting_id,
                    segment_seq=1,
                    utterance_id="utterance-delete",
                    event_id="event-delete",
                    speaker_id="speaker-1",
                    text="需要随会议删除的转写。",
                ),
                TranscriptStream(meeting_id=meeting_id, last_segment_seq=1),
            ]
        )
        db.commit()

    response = client.delete(f"/api/meetings/{meeting_id}")
    assert response.status_code == 204

    models = (
        Meeting,
        MeetingSummary,
        MeetingState,
        MeetingMemorySnapshot,
        MeetingStateHealth,
        TranscriptSegment,
        TranscriptStream,
    )
    with SessionLocal() as db:
        for model in models:
            column = model.id if model is Meeting else model.meeting_id
            count = db.scalar(
                select(func.count()).select_from(model).where(column == meeting_id)
            )
            assert count == 0, model.__name__
