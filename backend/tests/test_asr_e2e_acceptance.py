"""End-to-end acceptance: Mock ASR -> Adapter -> Transcript -> SSE -> Summary.

This is the ASR-0B acceptance suite. It drives the REAL mock producer script as
a subprocess (a genuine external HTTP client that never imports Backend
internals), against a REAL uvicorn server on a scratch port + scratch SQLite, so
the full chain is exercised:

    mock_transcript_producer.py  (external collaborator wire format)
      -> POST /api/internal/.../asr-events      (ASR-0B ingress + adapter)
      -> Transcript Domain                       (segment_seq allocation, persist)
      -> GET /transcript snapshot                (durable fact)
      -> GET /events (SSE)                       (live increment, named frames)
      -> Finalizer + Summary                     (freeze -> LLM(fake) -> COMPLETED)

Why a subprocess server rather than TestClient: the mock producer must be able
to talk over the network exactly as the collaborator will, and SSE needs a real
socket to prove frames actually arrive. TestClient bypasses both.

The React leg is asserted at the frame level (the exact `event:` names + payload
field names the frontend subscribes to). A real-browser CDP pass lives in
`scripts/e2e_asr_browser.py` for the DOM-level assertions.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND_DIR / "scripts" / "mock_transcript_producer.py"
VENV_PY = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PY if VENV_PY.exists() else Path(sys.executable))


# --------------------------------------------------------------------------
# scratch server
# --------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """A real uvicorn process on a scratch port + scratch SQLite file."""

    def __init__(self, tmp_path: Path) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.db = tmp_path / "e2e.db"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite:///{self.db.as_posix()}"
        env["LLM_PROVIDER"] = "fake"
        env["DEEPSEEK_API_KEY"] = ""
        env["SSE_HEARTBEAT_INTERVAL"] = "60"
        # stdout MUST go to a file, never subprocess.PIPE: uvicorn access-logs
        # every request, and an undrained pipe fills its OS buffer then blocks
        # the server mid-request (which would hang the whole module).
        self.log_path = self.db.with_suffix(".log")
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:app", "--port", str(self.port)],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"server died on startup:\n{self._read_log()}"
                )
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=1):
                    return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("server did not become healthy in time")

    def _read_log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except Exception:
            return "<no log>"

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self._log.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def server(tmp_path_factory) -> _Server:
    srv = _Server(tmp_path_factory.mktemp("asr-e2e"))
    srv.start()
    yield srv
    srv.stop()


# --------------------------------------------------------------------------
# tiny HTTP helpers
# --------------------------------------------------------------------------

def _req(method: str, url: str, body: dict | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def _new_meeting(server: _Server, title: str, *, start: bool = True) -> str:
    code, body = _req("POST", server.base + "/api/meetings", {"title": title})
    assert code == 201, body
    mid = body["id"]
    if start:
        assert _req("POST", f"{server.base}/api/meetings/{mid}/start")[0] == 200
    return mid


def _run_producer(server: _Server, mid: str, scenario: str, *extra: str):
    """Drive the REAL mock producer script as an external subprocess."""
    return subprocess.run(
        [
            PYTHON, str(SCRIPT),
            "--base-url", server.base,
            "--meeting-id", mid,
            "--scenario", scenario,
            "--delay-ms", "5",
            *extra,
        ],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _segments(server: _Server, mid: str) -> list[dict]:
    return _req("GET", f"{server.base}/api/meetings/{mid}/transcript")[1]["segments"]


# ==========================================================================
# A. the producer is a genuine external client
# ==========================================================================

class TestProducerIsExternal:
    """The producer must be a pure external HTTP client.

    These assertions read the script's CODE (docstrings/comments stripped), so
    prose explaining a constraint does not count as violating it.
    """

    @staticmethod
    def _code_only() -> str:
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                # drop the docstring
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:] or [ast.Pass()]
        return ast.unparse(tree)

    def test_producer_never_imports_backend_internals(self):
        code = self._code_only()
        for forbidden in ("app.services", "app.transcript", "live_cache", "sqlalchemy", "app.db"):
            assert forbidden not in code, f"producer must not reference {forbidden}"

    def test_producer_only_imports_the_standard_library(self):
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= {"__future__", "argparse", "json", "sys", "time",
                            "urllib", "urllib.error", "urllib.request"}, imported

    def test_base_event_builder_never_carries_ordering(self):
        """Ordering is Transcript-owned: the normal wire format must not carry it.

        `segmentSeq` legitimately appears ONCE, in the errors scenario, where a
        malformed payload is deliberately sent to prove the ingress rejects it.
        The normal builder must never produce it.

        Asserted on the KEYS of the returned dict literal, not on text — the
        builder's docstring legitimately mentions the field while documenting
        its absence.
        """
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        builder = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_base_event"
        )
        keys: set[str] = set()
        for node in ast.walk(builder):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
        assert keys, "could not locate the event dict literal"
        assert "segmentSeq" not in keys
        assert "segment_seq" not in keys
        assert "utteranceId" not in keys
        assert "eventId" not in keys
        # and the fields that ARE the collaborator contract
        assert {"type", "meetingId", "asrSessionId", "sentenceId",
                "speakerId", "startMs", "endMs", "text", "provider"} <= keys


# ==========================================================================
# B. scenario coverage: happy / reconnect / duplicate / out-of-order / errors
# ==========================================================================

class TestScenarios:
    def test_happy_path_full_chain(self, server):
        """`happy` emits the transcript only; lifecycle stays with the caller.

        The producer deliberately does NOT control the meeting lifecycle, so no
        STREAM_END is sent here (a stream-end while IN_PROGRESS is correctly 409).
        """
        mid = _new_meeting(server, "e2e-happy")
        run = _run_producer(server, mid, "happy", "--no-stream-end")
        assert run.returncode == 0, run.stdout + run.stderr

        segs = _segments(server, mid)
        assert [s["segment_seq"] for s in segs] == [1, 2, 3]
        assert [s["utterance_id"] for s in segs] == ["mock-session-001:1",
                                                     "mock-session-001:2",
                                                     "mock-session-001:3"]
        # speakerId=-1 during PARTIAL, resolved on FINAL -> never "speaker_-1"
        assert all(s["speaker_id"].startswith("speaker_") for s in segs)

    def test_reconnect_scenario_keeps_identities_distinct(self, server):
        mid = _new_meeting(server, "e2e-reconnect")
        run = _run_producer(server, mid, "reconnect", "--no-stream-end")
        assert run.returncode == 0, run.stdout + run.stderr

        segs = _segments(server, mid)
        utterances = [s["utterance_id"] for s in segs]
        # session A sentence 1, then session B sentence 1 — no collision.
        assert utterances == ["mock-session-001:1", "mock-session-002:1"]
        assert [s["segment_seq"] for s in segs] == [1, 2]
        # session-local clocks are preserved verbatim (no rebase in MVP)
        assert segs[0]["start_ms"] == 1000
        assert segs[1]["start_ms"] == 0

    def test_duplicate_scenario_is_idempotent(self, server):
        mid = _new_meeting(server, "e2e-duplicate")
        run = _run_producer(server, mid, "duplicate", "--no-stream-end")
        assert run.returncode == 0, run.stdout + run.stderr
        assert "duplicate FINAL -> 200" in run.stdout
        assert "no extra segment persisted" in run.stdout
        assert [s["segment_seq"] for s in _segments(server, mid)] == [1]

    def test_out_of_order_scenario_orders_by_arrival(self, server):
        mid = _new_meeting(server, "e2e-ooo")
        run = _run_producer(server, mid, "out-of-order", "--no-stream-end")
        assert run.returncode == 0, run.stdout + run.stderr
        assert "seqs are 1..N by arrival" in run.stdout

        segs = _segments(server, mid)
        assert [s["segment_seq"] for s in segs] == [1, 2]
        # sentence 2 was sent first, so it owns seq 1
        assert segs[0]["utterance_id"] == "mock-session-001:2"

    def test_errors_scenario_all_rejections_hold(self, server):
        mid = _new_meeting(server, "e2e-errors")
        run = _run_producer(server, mid, "errors", "--no-stream-end")
        assert run.returncode == 0, run.stdout + run.stderr
        # The producer exits non-zero on any failed expectation; assert the count
        # explicitly too. Match the summary line, not the substring "FAIL" — the
        # success line itself contains "0 FAIL".
        assert "[FAIL]" not in run.stdout, run.stdout
        assert run.stdout.count("[PASS]") == 9, run.stdout
        assert "9 PASS / 0 FAIL" in run.stdout, run.stdout


# ==========================================================================
# C. SSE leg — the exact frames the React consumer subscribes to
# ==========================================================================

class TestSseLeg:
    def test_final_frames_arrive_as_named_events(self, server):
        """Named frames must carry `event: transcript.final`.

        Regression we already paid for once: named SSE frames do NOT fire
        `onmessage`, so the frontend must addEventListener per event name. This
        asserts the wire actually has the `event:` line it subscribes to.
        """
        mid = _new_meeting(server, "e2e-sse")
        collected: list[str] = []
        stop = threading.Event()

        def drain():
            req = urllib.request.Request(
                f"{server.base}/api/meetings/{mid}/events?after_segment_seq=0"
            )
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    while not stop.is_set():
                        line = resp.readline()
                        if not line:
                            break
                        collected.append(line.decode("utf-8", "replace").rstrip("\r\n"))
            except Exception:
                pass

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        time.sleep(1.0)  # let the subscription register

        code, body = _req(
            "POST",
            f"{server.base}/api/internal/meetings/{mid}/asr-events",
            {
                "type": "transcript.final",
                "meetingId": mid,
                "asrSessionId": "sse-sess",
                "sentenceId": 1,
                "speakerId": 0,
                "startMs": 0,
                "endMs": 1000,
                "text": "SSE 帧断言",
                "provider": "tencent",
            },
        )
        assert code == 200, body

        deadline = time.time() + 15
        while time.time() < deadline and not any(
            ln.startswith("event: transcript.final") for ln in collected
        ):
            time.sleep(0.2)
        stop.set()
        t.join(timeout=5)

        assert "event: transcript.final" in collected, collected
        data_line = next(
            (ln for ln in collected if ln.startswith("data: ") and "transcript.final" in ln),
            None,
        )
        assert data_line is not None, collected
        envelope = json.loads(data_line[len("data: "):])
        # Field names the frontend reads off the envelope.
        for key in ("event_type", "meeting_id", "emitted_at", "payload"):
            assert key in envelope, envelope
        assert envelope["event_type"] == "transcript.final"
        assert envelope["payload"]["segment_seq"] == 1
        # Stable dedupe id (Step 5 Part 13)
        assert any(
            ln == "id: final:%s:1" % mid for ln in collected
        ), [ln for ln in collected if ln.startswith("id:")]

    def test_snapshot_is_the_recovery_fact_source(self, server):
        """Refresh semantics: Snapshot alone reproduces the full FINAL history."""
        mid = _new_meeting(server, "e2e-snapshot")
        assert _run_producer(server, mid, "happy", "--no-stream-end").returncode == 0

        segs = _segments(server, mid)
        assert len(segs) == 3
        # ascending, gap-free — exactly what catch-up + dedupe relies on
        assert [s["segment_seq"] for s in segs] == [1, 2, 3]


# ==========================================================================
# D. full chain to Summary / COMPLETED
# ==========================================================================

class TestFullChainToSummary:
    def test_happy_meeting_reaches_completed_with_summary(self, server):
        mid = _new_meeting(server, "e2e-summary")
        # Transcript first (no stream-end: the producer does not own lifecycle).
        assert _run_producer(server, mid, "happy", "--no-stream-end").returncode == 0
        assert [s["segment_seq"] for s in _segments(server, mid)] == [1, 2, 3]

        # The CALLER ends the meeting, then the terminal signal is accepted.
        assert _req("POST", f"{server.base}/api/meetings/{mid}/finish")[0] == 200
        code, body = _req(
            "POST",
            f"{server.base}/api/internal/meetings/{mid}/stream-end",
            {
                "event_id": "evt_e2e_end",
                "meeting_id": mid,
                "event_type": "TRANSCRIPT_STREAM_END",
                # omitted on purpose -> server derives max(segment_seq) = 3
            },
        )
        assert code == 200, body
        assert body["last_segment_seq"] == 3

        # stream-end triggers the Finalizer in-process; poll for the terminal state.
        deadline = time.time() + 60
        status = None
        while time.time() < deadline:
            status = _req("GET", f"{server.base}/api/meetings/{mid}")[1]["status"]
            if status == "COMPLETED":
                break
            time.sleep(0.4)
        assert status == "COMPLETED", f"meeting stuck at {status}"

        code, summary = _req("GET", f"{server.base}/api/meetings/{mid}/summary")
        assert code == 200, summary
        assert summary["status"] == "COMPLETED"
        assert summary["version"] == 1

    def test_empty_meeting_reaches_completed(self, server):
        """ASR-0B decision 1, end to end: no transcript still finalizes."""
        mid = _new_meeting(server, "e2e-empty")
        assert _req("POST", f"{server.base}/api/meetings/{mid}/finish")[0] == 200

        # Omit last_segment_seq entirely — the server must derive 0.
        code, body = _req(
            "POST",
            f"{server.base}/api/internal/meetings/{mid}/stream-end",
            {
                "event_id": "evt_empty_end",
                "meeting_id": mid,
                "event_type": "TRANSCRIPT_STREAM_END",
            },
        )
        assert code == 200, body
        assert body["last_segment_seq"] == 0

        deadline = time.time() + 60
        status = None
        while time.time() < deadline:
            status = _req("GET", f"{server.base}/api/meetings/{mid}")[1]["status"]
            if status == "COMPLETED":
                break
            time.sleep(0.4)
        assert status == "COMPLETED", f"empty meeting stuck at {status}"
        assert _req("GET", f"{server.base}/api/meetings/{mid}/summary")[1]["status"] == "COMPLETED"


# ==========================================================================
# E. internal ASR API has its own OpenAPI surface
# ==========================================================================

class TestInternalOpenApi:
    def test_asr_endpoints_are_documented(self, server):
        code, schema = _req("GET", server.base + "/openapi.json")
        assert code == 200
        paths = schema["paths"]
        for path in (
            "/api/internal/meetings/{meeting_id}/asr-events",
            "/api/internal/meetings/{meeting_id}/transcript-events",
            "/api/internal/meetings/{meeting_id}/stream-end",
        ):
            assert path in paths, f"missing {path} from OpenAPI"

    def test_asr_dto_schema_is_fully_documented(self, server):
        _, schema = _req("GET", server.base + "/openapi.json")
        dto = schema["components"]["schemas"]["AsrTranscriptEvent"]
        props = dto["properties"]
        assert set(props) >= {
            "type", "meetingId", "asrSessionId", "sentenceId",
            "speakerId", "startMs", "endMs", "text", "provider",
        }
        assert props["asrSessionId"]["maxLength"] == 48
        assert "description" in props["asrSessionId"]

    def test_internal_dtos_are_separate_from_business_dtos(self, server):
        """`AsrTranscriptEvent` (external) and `TranscriptEvent` (internal) coexist."""
        _, schema = _req("GET", server.base + "/openapi.json")
        schemas = schema["components"]["schemas"]
        assert "AsrTranscriptEvent" in schemas
        assert "TranscriptEvent" in schemas
        # The external one must NOT expose internal identity/ordering fields.
        assert "segment_seq" not in schemas["AsrTranscriptEvent"]["properties"]
        assert "utterance_id" not in schemas["AsrTranscriptEvent"]["properties"]


class TestFocusedInternalDocs:
    """The internal ASR API has its own Swagger, derived from the real routes."""

    def test_focused_openapi_lists_only_internal_asr_paths(self, server):
        code, schema = _req("GET", server.base + "/api/internal/openapi.json")
        assert code == 200
        paths = set(schema["paths"])
        assert paths == {
            "/api/internal/meetings/{meeting_id}/asr-events",
            "/api/internal/meetings/{meeting_id}/transcript-events",
            "/api/internal/meetings/{meeting_id}/stream-end",
        }, paths
        assert schema["info"]["title"].startswith("小水华")

    def test_focused_openapi_does_not_leak_business_endpoints(self, server):
        """The collaborator view must NOT document frontend/business routes."""
        _, schema = _req("GET", server.base + "/api/internal/openapi.json")
        paths = schema["paths"]
        for business in (
            "/api/meetings",
            "/api/meetings/{meeting_id}/summary",
            "/api/meetings/{meeting_id}/transcript",
            "/api/meetings/{meeting_id}/events",
        ):
            assert business not in paths, f"leaked business route {business}"

    def test_focused_openapi_carries_the_frozen_bounds(self, server):
        _, schema = _req("GET", server.base + "/api/internal/openapi.json")
        props = schema["components"]["schemas"]["AsrTranscriptEvent"]["properties"]
        assert props["asrSessionId"]["maxLength"] == 48

    def test_swagger_ui_served(self, server):
        import urllib.request as u

        with u.urlopen(server.base + "/api/internal/docs", timeout=15) as r:
            assert r.status == 200
            body = r.read().decode("utf-8", "replace")
        assert "swagger" in body.lower()
        assert "/api/internal/openapi.json" in body

    def test_main_openapi_still_documents_everything(self, server):
        """The focused view is ADDITIVE — the main schema is unchanged."""
        _, schema = _req("GET", server.base + "/openapi.json")
        paths = schema["paths"]
        assert "/api/meetings" in paths
        assert "/api/internal/meetings/{meeting_id}/asr-events" in paths
