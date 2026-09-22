import re

import pytest
from sqlalchemy.exc import IntegrityError


def _create(client, title: str) -> dict:
    response = client.post("/api/meetings", json={"title": title})
    assert response.status_code == 201
    return response.json()


def test_created_meeting_has_distinct_numeric_id_and_nine_digit_code(client):
    first = _create(client, "First")
    second = _create(client, "Second")

    assert re.fullmatch(r"\d+", first["id"])
    assert int(first["id"]) < 2**63
    assert re.fullmatch(r"\d{9}", first["meeting_code"])
    assert first["id"] != second["id"]
    assert first["meeting_code"] != second["meeting_code"]


def test_meeting_code_is_database_unique(client):
    from app.db import SessionLocal
    from app.meeting.meeting_model import Meeting

    existing = _create(client, "Existing")
    with SessionLocal() as db:
        db.add(
            Meeting(
                id="900000000000000001",
                meeting_code=existing["meeting_code"],
                title="Duplicate",
                status="CREATED",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_create_retries_when_meeting_code_collides(client, monkeypatch):
    from app.meeting import service

    codes = iter(("123456789", "123456789", "987654321"))
    monkeypatch.setattr(service, "generate_meeting_code", lambda: next(codes))

    first = _create(client, "First")
    second = _create(client, "Second")

    assert first["meeting_code"] == "123456789"
    assert second["meeting_code"] == "987654321"


def test_snowflake_sequence_is_unique_within_same_millisecond(monkeypatch):
    from app.meeting.id_generator import SnowflakeGenerator

    generator = SnowflakeGenerator(worker_id=7)
    monkeypatch.setattr(generator, "_now_ms", lambda: 1_800_000_000_000)

    values = [generator.next_id() for _ in range(100)]

    assert len(values) == len(set(values))
    assert all(value.isdigit() for value in values)
