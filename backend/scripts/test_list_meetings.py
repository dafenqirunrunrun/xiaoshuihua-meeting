#!/usr/bin/env python
"""Step 8B-6 Meeting List API integration tests (L-01..L-08).

Black-box HTTP against a subprocess uvicorn (fresh temp DB). Exercises the
real `GET /api/meetings` list endpoint end-to-end:

  L-01  create 3 meetings -> list returns them
  L-02  list is sorted by most recently updated first
  L-03  status=CREATED returns only CREATED
  L-04  status=IN_PROGRESS returns only IN_PROGRESS
  L-05  limit/offset pagination works
  L-06  invalid status -> 422 (FastAPI/Pydantic)
  L-07  existing GET /api/meetings/{id} unaffected
  L-08  start/finish unaffected

Run:  PYTHONPATH="backend" backend/.venv/Scripts/python scripts/test_list_meetings.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

_PASS = 0
_FAIL = 0


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    tag = "PASS" if ok else "FAIL"
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail else ""))


# ---------------------------------------------------------------------------
# Black-box server bootstrap (fresh temp DB, avoids port clash with dev)
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 8768
BASE = f"http://{HOST}:{PORT}"


def _http_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def _run_server() -> subprocess.Popen:
    tmp_db = tempfile.mkdtemp(prefix="step8b6_list_")
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{tmp_db}/test.db",
        "LLM_PROVIDER": "fake",
    }
    logf = open(os.path.join(tmp_db, "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
        cwd=_BACKEND, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            code, _ = _http_request("GET", "/api/health")
            if code == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("server did not start")


def _new_meeting(title: str) -> dict:
    code, body = _http_request("POST", "/api/meetings", {"title": title})
    assert code == 201, f"create failed {code}: {body}"
    return body


def _start(mid: str) -> None:
    code, body = _http_request("POST", f"/api/meetings/{mid}/start")
    assert code in (200, 201), f"start failed {code}: {body}"


def _finish(mid: str) -> None:
    code, body = _http_request("POST", f"/api/meetings/{mid}/finish")
    assert code in (200, 201), f"finish failed {code}: {body}"


def _part_b() -> int:
    # ---- L-01: create 3 meetings -> list finds them -------------------------
    a = _new_meeting("L-01 first")
    b = _new_meeting("L-01 second")
    c = _new_meeting("L-01 third")

    code, body = _http_request("GET", "/api/meetings")
    _record("L-01 GET /api/meetings -> 200 with all meetings",
            code == 200 and isinstance(body, dict)
            and body.get("total") == 3
            and len(body.get("items", [])) == 3,
            f"code={code} total={body.get('total') if isinstance(body, dict) else body!r}")
    _record("L-01b response shape {items,total,limit,offset}",
            isinstance(body, dict)
            and set(body.keys()) >= {"items", "total", "limit", "offset"}
            and body.get("limit") == 20 and body.get("offset") == 0,
            f"keys={sorted(body.keys()) if isinstance(body, dict) else body!r}")

    # ---- L-02: sorted most recently updated first ---------------------------
    # created in order a,b,c; SQLite CURRENT_TIMESTAMP has SECOND resolution,
    # so updates within the same second tie on updated_at. Space mutations
    # >1s apart so the sort is externally observable and deterministic:
    #   t0: a created            (updated = t0)
    #   t1: b started            (updated = t1)
    #   t2: c started + finished (updated = t2)  <- most recent
    # Expected list order: [c, b, a]
    time.sleep(1.1)  # separate b's start second from creation second
    _start(b["id"])
    time.sleep(1.1)
    _start(c["id"])
    _finish(c["id"])  # c -> FINALIZING
    code, body = _http_request("GET", "/api/meetings")
    ids = [m["id"] for m in body["items"]] if isinstance(body, dict) else []
    _record("L-02 recently-updated first (b/c updated > a)",
            code == 200 and ids[:3] == [c["id"], b["id"], a["id"]],
            f"order={ids[:3]}")

    # ---- L-03 / L-04: status filter ----------------------------------------
    code, body = _http_request("GET", "/api/meetings?status=CREATED")
    created = [m for m in body["items"]] if isinstance(body, dict) else []
    _record("L-03 status=CREATED -> only CREATED",
            code == 200 and created and all(m["status"] == "CREATED" for m in created)
            and len(created) == 1 and created[0]["id"] == a["id"],
            f"count={len(created)}")

    code, body = _http_request("GET", "/api/meetings?status=IN_PROGRESS")
    in_progress = [m for m in body["items"]] if isinstance(body, dict) else []
    _record("L-04 status=IN_PROGRESS -> only IN_PROGRESS",
            code == 200 and in_progress and all(m["status"] == "IN_PROGRESS" for m in in_progress)
            and len(in_progress) == 1 and in_progress[0]["id"] == b["id"],
            f"count={len(in_progress)}")

    # FINALIZING filter also works (four states all discoverable)
    code, body = _http_request("GET", "/api/meetings?status=FINALIZING")
    fin = [m for m in body["items"]] if isinstance(body, dict) else []
    _record("L-04b status=FINALIZING -> only FINALIZING",
            code == 200 and len(fin) == 1 and fin[0]["id"] == c["id"],
            f"count={len(fin)}")

    # ---- L-05: limit/offset ----
    # create one more so total=4; limit=2 offset=0 -> [c,b]; offset=2 -> [a,d]
    d = _new_meeting("L-05 fourth")
    code, body = _http_request("GET", "/api/meetings?limit=2&offset=0")
    page1 = [m["id"] for m in body["items"]] if isinstance(body, dict) else []
    _record("L-05 limit=2 offset=0 -> first 2",
            code == 200 and len(page1) == 2 and body.get("total") == 4,
            f"ids={page1} total={body.get('total') if isinstance(body, dict) else None}")
    code, body = _http_request("GET", "/api/meetings?limit=2&offset=2")
    page2 = [m["id"] for m in body["items"]] if isinstance(body, dict) else []
    _record("L-05b offset=2 -> next 2 (no overlap)",
            code == 200 and len(page2) == 2 and not (set(page1) & set(page2)),
            f"ids={page2}")
    code, body = _http_request("GET", "/api/meetings?limit=2&offset=4")
    page3 = [m["id"] for m in body["items"]] if isinstance(body, dict) else []
    _record("L-05c offset beyond end -> empty items, total still 4",
            code == 200 and page3 == [] and body.get("total") == 4,
            f"ids={page3}")

    # ---- L-06: invalid status -> 422 ----
    code, body = _http_request("GET", "/api/meetings?status=PAUSED")
    _record("L-06 status=PAUSED -> 422 (Pydantic/Literal)",
            code == 422, f"code={code}")
    code, body = _http_request("GET", "/api/meetings?limit=0")
    _record("L-06b limit=0 -> 422", code == 422, f"code={code}")
    code, body = _http_request("GET", "/api/meetings?limit=101")
    _record("L-06c limit=101 (>max 100) -> 422", code == 422, f"code={code}")
    code, body = _http_request("GET", "/api/meetings?offset=-1")
    _record("L-06d offset=-1 -> 422", code == 422, f"code={code}")

    # ---- L-07: GET /api/meetings/{id} unaffected ----
    code, body = _http_request("GET", f"/api/meetings/{a['id']}")
    _record("L-07 GET /{id} still 200 with full MeetingOut",
            code == 200 and isinstance(body, dict)
            and body.get("id") == a["id"] and body.get("title") == "L-01 first",
            f"code={code}")
    code, body = _http_request("GET", "/api/meetings/meeting_does_not_exist")
    _record("L-07b GET unknown id -> 404 unchanged", code == 404, f"code={code}")

    # ---- L-08: start/finish unaffected ----
    code, body = _http_request("POST", f"/api/meetings/{a['id']}/start")
    _record("L-08 start still works (CREATED -> IN_PROGRESS)",
            code in (200, 201) and body.get("status") == "IN_PROGRESS",
            f"code={code} status={body.get('status') if isinstance(body, dict) else body!r}")
    code, body = _http_request("POST", f"/api/meetings/{a['id']}/finish")
    _record("L-08b finish still works (IN_PROGRESS -> FINALIZING)",
            code in (200, 201) and body.get("status") == "FINALIZING",
            f"code={code} status={body.get('status') if isinstance(body, dict) else body!r}")

    return 0


def main() -> int:
    proc = _run_server()
    try:
        rc = _part_b()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    print(f"\nRESULT: {_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else rc


if __name__ == "__main__":
    sys.exit(main())
