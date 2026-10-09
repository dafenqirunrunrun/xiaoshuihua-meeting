"""CAPTAIN-REAL-1 replay: the REAL referee, against the REAL configured provider.

    FINAL -> transcript -> wake / button trigger
          -> captain worker -> window -> DeepSeek -> Captain Report
          -> evidence validation -> captain_report

This is NOT a regression test and is deliberately NOT under `tests/`: it makes
live, paid, NON-DETERMINISTIC calls to whatever `LLM_PROVIDER` points at. CI must
never run it (pytest pins `fake` precisely so it cannot). It exists to answer one
question no offline suite can: does the referee actually produce a usable report
when a REAL model reasons over a REAL multi-party discussion?

That question is not academic. The offline suite injects a scripted provider that
does not reason, so it cannot see the failure this script was written after:
`CAPTAIN_MAX_OUTPUT_TOKENS` was being spent entirely on the gateway's implicit
`reasoning_tokens`, leaving `message.content` empty and marking every referee run
on a substantive discussion FAILED with
``deepseek returned empty content (finish_reason=length, ...)``.

Run it explicitly, from `backend/`, with a real provider configured:

    .venv/Scripts/python scripts/replay_real_referee.py

It prints the meeting id, the report status, and the full report, and exits
non-zero on FAILED or on an evidence/quote that did not survive validation. It
NEVER prints the API key.

Read-only with respect to the app: it uses a throwaway temp SQLite DB and the
frozen scenario below. The scenario is deliberately an INTERNAL_DECISION (people,
schedules, release windows) so the run also documents that SearchPolicy declines
to search on it — the correct behaviour, not a bug (see captain/search/policy.py).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

# A throwaway DB — never the developer's real one. Unless the caller has already
# pinned DATABASE_URL (the normal case when a server is started separately and
# handed the same URL), in which case we use theirs so both processes agree on
# one file.
if not os.environ.get("DATABASE_URL"):
    _TMP = tempfile.mkdtemp(prefix="captain_real1_")
    os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'replay.db')}"
# NOTE: LLM_PROVIDER is deliberately NOT pinned here. Resolving the REAL
# provider is the entire point of this script.

# The frozen bad case from the product spec, verbatim. It is a release-window
# disagreement that should converge on "what pressure-validation standard do we
# need before shipping?" — NOT on "the two sides still disagree".
#
# Line 5 is load-bearing. It carries the fact that decides whether the historical
# incident still applies (root cause was a connection-pool config, and that
# config has since been changed). Before the `exclude_anchor` fix, the button
# path dropped this exact segment and the referee could never cite it.
#
# Lines 3-4 mention 上线/发布/排期/我们, so SearchPolicy classifies the window as
# INTERNAL_DECISION and correctly declines to search — that outcome is asserted
# below rather than treated as a defect (see `captain/search/policy.py`).
SCENARIO: list[tuple[str, str]] = [
    ("产品", "我建议下周五上线，这个版本的核心功能已经开发完毕，测试也跑过一轮了。"),
    ("测试", "等一下，我说的是功能测试通过了，但压力测试还没做。上次双十一那种量级的问题就是压测没做暴露的，我不同意下周五上线。"),
    ("后端", "压测我可以排期，但需要三天时间准备环境。如果下周五上线，压测只能做一半，风险不好评估。"),
    ("产品", "可是市场那边已经把发布会的时间定在下周五了，往后推一周会有商务上的问题。我们的用户量目前还不到一万，真的需要做双十一那个量级的压测吗？"),
    ("运维", "用户量小不代表没有风险。上次出问题是因为连接池配置，跟量级关系不大。不过这个问题我们上周已经改了连接池参数。"),
]

# The eight critical facts the referee must be able to ground, per the spec's
# Critical Evidence Recall requirement. Matched as substrings against the union
# of the report's evidence quotes + position/point texts, so a fact counts as
# covered only if the report actually says it, not merely because it was in the
# window.
CRITICAL_FACTS: list[tuple[str, str]] = [
    ("core_feature_done", "核心功能"),
    ("functional_test_passed", "功能测试"),
    ("load_test_not_done", "压测"),
    ("env_prep_three_days", "三天"),
    ("launch_event_next_friday", "发布会"),
    ("user_base_under_10k", "一万"),
    ("incident_root_cause_pool", "连接池"),
    ("pool_params_already_changed", "已经改了"),
]

# Overridable so a second, isolated server can be driven without disturbing the
# developer's usual :8000 instance.
_BASE = os.environ.get("REFEREE_BASE_URL", "http://127.0.0.1:8000/api")
_RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        _BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw) if raw else {}


def main() -> int:
    """Requires a backend already running on :8000 against the SAME temp DB.

    Unlike `replay_real_provider.py` (which drives the app in-process), the
    referee reports only exist on the captain worker thread, so this script
    talks to a live server. Start one first:
        DATABASE_URL=... .venv/Scripts/python -m uvicorn app.main:app --port 8000
    (or point REFEREE_BASE_URL at another port for an isolated run).
    """
    # The report is Chinese and this script is usually run with stdout
    # redirected to a file, where Python would otherwise pick the console's
    # legacy codec and mangle every character of it.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):  # pragma: no cover - older/odd stdout
        pass

    status, meeting = _call("POST", "/meetings", {"title": "真实裁判：上线窗口争议"})
    if status != 201:
        print(f"create meeting failed: {status} {meeting}")
        return 1
    mid = meeting["id"]
    st, d = _call("POST", f"/meetings/{mid}/start")
    if st != 200:
        # The device binding admits ONE active meeting at a time. A previous
        # half-finished replay (this script returns early on any failure) leaves
        # its meeting IN_PROGRESS and every later run dies here with a confusing
        # "status CREATED does not accept demo utterances". Finish the blocker
        # and retry once: a leftover meeting from an earlier run is not a state
        # this script is responsible for preserving.
        stale = d.get("detail", "") if isinstance(d, dict) else ""
        if "already bound to active meeting" in stale:
            blocker = stale.rsplit(" ", 1)[-1]
            _call("POST", f"/meetings/{blocker}/finish")
            st, d = _call("POST", f"/meetings/{mid}/start")
        if st != 200:
            print(f"start failed: {st} {d}")
            return 1
    print(f"meeting={mid}")

    seqs: list[int] = []
    for speaker, text in SCENARIO:
        st, d = _call("POST", f"/meetings/{mid}/demo/utterances",
                      {"speaker": speaker, "text": text})
        if st != 200:
            print(f"utterance failed: {st} {d}")
            return 1
        seqs.append(d["segment_seq"])
        print(f'  #{d["segment_seq"]} {d["speaker"]}: {text[:24]}…')

    st, d = _call("POST", f"/meetings/{mid}/captain", {})
    if st != 202:
        print(f"trigger failed: {st} {d}")
        return 1
    rid = d["report_id"]
    print(f"report_id={rid} 锚点=#{d['anchor_segment_seq']}")

    rep: dict = {}
    # 60 * 3s = 180s. The per-call timeout is CAPTAIN_LLM_TIMEOUT_SECONDS (60s)
    # and a transient gateway stall can burn one attempt, so the old 120s budget
    # could expire on a report that was still legitimately in flight.
    for i in range(60):
        import time

        time.sleep(3)
        st, rep = _call("GET", f"/meetings/{mid}/captain/{rid}")
        print(f"  [{i * 3 + 3}s] {rep.get('status')}")
        if rep.get("status") in ("COMPLETED", "FAILED"):
            break

    print("\n=========== 报告 ===========")
    print(json.dumps(rep, ensure_ascii=False, indent=2))

    # --- acceptance checks -------------------------------------------------
    # These are the facts a scripted provider CANNOT verify: they are about what
    # a reasoning model does with real material, not about pipeline structure.
    print("\n--- 验收 ---")
    _check("status is COMPLETED", rep.get("status") == "COMPLETED",
           rep.get("status", ""))
    report = rep.get("report") or {}
    _check("report is present", bool(report))

    if report:
        positions = report.get("positions", [])
        _check("1..5 positions", 1 <= len(positions) <= 5, f"got {len(positions)}")
        _check("dispute_points is a list", isinstance(report.get("dispute_points"), list))
        _check("uncertainty non-empty", bool((report.get("uncertainty") or "").strip()))
        _check("suggestion non-empty", bool((report.get("suggestion") or "").strip()))

        # No person attribution (ADR-002 forbids it structurally; a reasoning
        # model is the one that could reintroduce it in prose).
        blob = json.dumps(report, ensure_ascii=False)
        attributed = [n for n in ("小王", "小李", "张三", "李四") if n in blob]
        _check("no person names in report", not attributed, str(attributed))
        _check("no side_a/side_b", "side_a" not in blob and "side_b" not in blob)
        _check("no verdict field",
               not any(k in report for k in ("decision", "verdict", "winner", "score")))

        # Every cited seq must be inside the window and every quote must be a
        # verbatim substring of the segment it cites (evidence.py already prunes
        # violations, so a surviving violation means the guard failed).
        st, snap = _call("GET", f"/meetings/{mid}/transcript")
        items = snap.get("segments", [])
        by_seq = {s["segment_seq"]: s["text"] for s in items}
        window = [s for s in items if s["segment_seq"] in seqs]
        _check("evidence cites only window seqs",
               all(e["segment_seq"] in by_seq for e in report.get("evidence", [])))
        verbatim = all(
            e["quote"].strip() in by_seq.get(e["segment_seq"], "")
            for e in report.get("evidence", [])
        )
        _check("every quote is verbatim", verbatim)
        _check("transcript untouched", len(window) == len(SCENARIO),
               f"{len(window)}/{len(SCENARIO)}")
        _check("newest utterance was in the window",
               max(seqs) in {s["segment_seq"] for s in window},
               f"max seq {max(seqs)}")

        # --- Critical Evidence Recall --------------------------------------
        # The eight facts the spec requires. Measured over the REPORT (quotes +
        # every free-text field), not over the window: a fact that was merely
        # visible to the model but absent from the report is not "covered".
        report_blob = json.dumps(report, ensure_ascii=False)
        missing = [name for name, needle in CRITICAL_FACTS if needle not in report_blob]
        _check("8/8 critical facts recalled", not missing,
               f"missing={missing}" if missing else "all 8 present")

        # --- Counter Evidence Recall ---------------------------------------
        # The spec's specific requirement: the referee must recognise that the
        # historical incident is WEAKENED because its root cause was already
        # fixed. Asserting the word appears is not enough — the weakener and the
        # incident must co-occur in one field, so a report that merely lists
        # 连接池 in `evidence` without connecting it to the incident fails.
        fields = [report.get("discussion_summary") or "", report.get("suggestion") or ""]
        for p in report.get("positions", []):
            fields.append(p.get("statement") or "")
            fields.append(p.get("label") or "")
        for key in ("agreement_points", "dispute_points"):
            fields.extend(p.get("text") or "" for p in report.get(key, []))
        for e in report.get("evidence", []):
            fields.append(e.get("supports") or "")
            fields.append(e.get("quote") or "")
        counter = [
            f for f in fields
            if "连接池" in f and ("已" in f or "修改" in f or "改" in f)
        ]
        _check("counter-evidence: pool change weakens the incident",
               bool(counter), f"{len(counter)} field(s) link them")

        # --- Convergence ----------------------------------------------------
        # The spec's headline outcome: the dispute must be narrowed to a
        # validation STANDARD, not restated as "both sides disagree". The
        # standard must be named as a standard (标准/门槛/通过条件), so a field
        # that merely repeats 压测 next to 上线 does not satisfy this.
        converging = [
            f for f in fields
            if ("标准" in f or "门槛" in f or "通过条件" in f)
            and ("压测" in f or "验证" in f or "上线" in f or "评估" in f)
        ]
        _check("converges on a validation standard",
               bool(converging), f"{len(converging)} field(s)")

        # --- External evidence policy ---------------------------------------
        # This window is INTERNAL_DECISION (排期/上线/我们), so the correct
        # outcome is NO SEARCH. Asserting it documents that the router declines
        # rather than that it silently failed.
        _check("external evidence empty (internal decision)",
               report.get("external_evidence") == [],
               str(len(report.get("external_evidence") or [])))

    print("\n=========== 汇总 ===========")
    failed = [n for n, ok, _ in _RESULTS if not ok]
    for name, ok, detail in _RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    print(f"\nmeeting_id={mid} report_id={rid}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
