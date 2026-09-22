from datetime import datetime


def _create(client, title: str) -> dict:
    response = client.post("/api/meetings", json={"title": title})
    assert response.status_code == 201
    return response.json()


def test_list_supports_search_date_range_and_sort(client):
    from app.db import SessionLocal
    from app.meeting.meeting_model import Meeting

    alpha = _create(client, "Alpha planning")
    beta = _create(client, "Beta review")
    gamma = _create(client, "Gamma retro")

    with SessionLocal() as db:
        dates = {
            alpha["id"]: datetime(2026, 9, 20, 9, 0),
            beta["id"]: datetime(2026, 9, 21, 9, 0),
            gamma["id"]: datetime(2026, 9, 22, 9, 0),
        }
        for meeting_id, created_at in dates.items():
            meeting = db.get(Meeting, meeting_id)
            assert meeting is not None
            meeting.created_at = created_at
            meeting.updated_at = created_at
        db.commit()

    by_title = client.get("/api/meetings", params={"q": "alpha"}).json()
    assert by_title["total"] == 1
    assert by_title["items"][0]["id"] == alpha["id"]

    by_id = client.get("/api/meetings", params={"q": beta["id"][-5:]}).json()
    assert by_id["total"] == 1
    assert by_id["items"][0]["id"] == beta["id"]

    by_code = client.get(
        "/api/meetings", params={"q": gamma["meeting_code"]}
    ).json()
    assert by_code["total"] == 1
    assert by_code["items"][0]["id"] == gamma["id"]

    one_day = client.get(
        "/api/meetings",
        params={"created_from": "2026-09-21", "created_to": "2026-09-21"},
    ).json()
    assert one_day["total"] == 1
    assert one_day["items"][0]["id"] == beta["id"]

    oldest = client.get("/api/meetings", params={"sort": "created_asc"}).json()
    newest = client.get("/api/meetings", params={"sort": "created_desc"}).json()
    assert [item["id"] for item in oldest["items"]] == [alpha["id"], beta["id"], gamma["id"]]
    assert [item["id"] for item in newest["items"]] == [gamma["id"], beta["id"], alpha["id"]]

    invalid = client.get(
        "/api/meetings",
        params={"created_from": "2026-09-22", "created_to": "2026-09-20"},
    )
    assert invalid.status_code == 422


def test_meeting_title_can_be_renamed_without_changing_status(client):
    meeting = _create(client, "Before rename")

    response = client.patch(
        f"/api/meetings/{meeting['id']}",
        json={"title": "  Product weekly  "},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Product weekly"
    assert response.json()["status"] == "CREATED"
    assert client.get(f"/api/meetings/{meeting['id']}").json()["title"] == "Product weekly"

    blank = client.patch(f"/api/meetings/{meeting['id']}", json={"title": "   "})
    assert blank.status_code == 422
    assert client.patch("/api/meetings/missing", json={"title": "Nope"}).status_code == 404
