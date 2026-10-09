"""Phase C.5 freeze smoke: REAL two-stage run (Quick -> Research -> Deep).

This is NOT a regression test and is deliberately NOT under `tests/`: it makes
live, paid, NON-DETERMINISTIC calls to whatever `LLM_PROVIDER` points at, and it
resolves the configured SEARCH provider too (so with `CAPTAIN_SEARCH_PROVIDER=
brave` and a key set, it makes real Brave calls). CI must never run it — pytest
pins `fake` precisely so it cannot.

What it answers that the offline suite cannot
---------------------------------------------
The offline tests inject scripted providers, so they prove the ORDER of the
stages and the SHAPE of the payload but never that a real model reads the
external block and a real search API returns usable sources. This script runs
the real chain in-process (the captain worker thread runs inside the same
process, so we can observe the emitted events directly) on five windows:

  should-search:     技术事实争议 / 最新产品能力 / 行业标准
  should-not-search: 内部负责人是谁 / 会议是否达成共识

and prints, per case, the policy verdict, whether `captain.quick_assessment`
preceded research and `captain.completed` the Deep pass, the research status,
and the external-evidence count.

Run it from `backend/` with a real provider configured:

    .venv/Scripts/python scripts/smoke_two_stage_research.py

It NEVER prints the API key or a search key. Read-only w.r.t. the app: it uses a
throwaway temp SQLite DB (unless DATABASE_URL is already pinned).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

if not os.environ.get("DATABASE_URL"):
    _TMP = tempfile.mkdtemp(prefix="captain_smoke_")
    os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'smoke.db')}"

# NOTE: LLM_PROVIDER and CAPTAIN_SEARCH_PROVIDER are deliberately NOT pinned —
# resolving the REAL configured providers is the point.

# Two-stage must be ON for this script to mean anything; assert rather than
# silently run the single-pass path.
os.environ.setdefault("CAPTAIN_TWO_STAGE_ENABLED", "true")


CASES: list[tuple[str, bool, list[str]]] = [
    ("技术事实争议", True, [
        "Redis 的默认端口是 6379 对吧?",
        "不对，我印象里默认端口早就不是 6379 了。",
    ]),
    ("最新产品能力", True, [
        "现在这个框架还支持 HTTP/2 吗?",
        "我记得最新版已经不支持 HTTP/2 了。",
    ]),
    ("行业标准", True, [
        "这个时间格式应该符合 ISO 8601 标准吧?",
        "不是吧，我记得对应的标准是 RFC 3339。",
    ]),
    ("内部负责人是谁", False, [
        "这个模块到底是谁负责?",
        "我觉得应该是老王负责吧。",
    ]),
    ("会议是否达成共识", False, [
        "我们这次会议是否已经达成共识了?",
        "我觉得还没有达成共识。",
    ]),
]


def _events() -> list:
    """Record every captain.* envelope from the REAL broker."""
    from app.realtime.event_broker import event_broker

    events: list = []
    original = event_broker.publish

    def spy(meeting_id, event):
        if event.event_type.startswith("captain."):
            events.append(event)
        return original(meeting_id, event)

    event_broker.publish = spy  # type: ignore[assignment]
    return events


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):  # pragma: no cover
        pass

    from app.config import settings

    print("=== 配置（不打印任何 key）===")
    print(f"  LLM_PROVIDER              = {settings.llm_provider}")
    print(f"  captain_two_stage_enabled = {settings.captain_two_stage_enabled}")
    print(f"  captain_search_provider   = {settings.captain_search_provider}")
    print(f"  brave key configured      = {bool(settings.brave_search_api_key)}")
    print(f"  search timeout ms         = {settings.captain_search_timeout_ms}")

    if not settings.captain_two_stage_enabled:
        print("\n两阶段未开启：CAPTAIN_TWO_STAGE_ENABLED 必须为 true。中止。")
        return 2

    from fastapi.testclient import TestClient

    from app.captain.service import reset_throttle
    from app.captain.worker import captain_worker
    from app.main import app

    results: list[tuple[str, bool, bool, str, int, bool]] = []

    with TestClient(app) as client:
        for name, want_search, lines in CASES:
            events = _events()
            reset_throttle()

            created = client.post("/api/meetings", json={"title": f"smoke-{name}"})
            mid = created.json()["id"]
            client.post(f"/api/meetings/{mid}/start")
            for i, text in enumerate(lines, start=1):
                client.post(
                    f"/api/internal/meetings/{mid}/transcript-events",
                    json={
                        "event_id": f"evt-{mid}-{i}",
                        "meeting_id": mid,
                        "utterance_id": f"utt-{i}",
                        "segment_seq": i,
                        "speaker_id": "unknown",
                        "text": text,
                        "status": "FINAL",
                        "start_ms": (i - 1) * 10_000,
                        "end_ms": (i - 1) * 10_000 + 5_000,
                    },
                )

            body = client.post(f"/api/meetings/{mid}/captain", json={}).json()
            if "report_id" not in body:
                print(f"\n--- {name} ---")
                print(f"  trigger 未返回 report_id: {body}")
                # Release the device binding so the NEXT case can start.
                client.post(f"/api/meetings/{mid}/finish")
                continue
            rid = body["report_id"]
            drained = captain_worker.drain(timeout=240.0)
            report = client.get(f"/api/meetings/{mid}/captain/{rid}").json()
            # Release the device binding: it admits only ONE active meeting, so
            # a leftover IN_PROGRESS meeting makes the next case's /start fail.
            client.post(f"/api/meetings/{mid}/finish")

            types = [e.event_type for e in events]
            quick_pub = "captain.quick_assessment" in types
            completed_pub = "captain.completed" in types
            if quick_pub and completed_pub:
                ordered = types.index("captain.quick_assessment") < types.index(
                    "captain.completed"
                )
            else:
                ordered = False

            rep = report.get("report") or {}
            status = report.get("status")
            rstatus = rep.get("research_status", "<missing>")
            evidence = rep.get("external_evidence") or []
            results.append(
                (name, want_search, ordered, status, len(evidence), quick_pub,
                 rstatus, drained)
            )

            print(f"\n--- {name} ---")
            print(f"  期望搜索      : {want_search}")
            print(f"  drain 完成    : {drained}")
            print(f"  report.status : {status}")
            print(f"  quick 事件    : {quick_pub}   (应在 research/completed 之前)")
            print(f"  quick<completed: {ordered}")
            print(f"  research_status: {rstatus}")
            print(f"  外部证据条数   : {len(evidence)}")
            for e in evidence[:3]:
                print(f"    [{e.get('source_id')}] {e.get('domain')} · "
                      f"{e.get('source_quality')}/{e.get('source_type')} · "
                      f"relation={e.get('relation')}")
                print(f"        {str(e.get('title'))[:60]}  {e.get('url')}")

    print("\n=========== 判定 ===========")
    ok = True
    for row in results:
        name, want_search, ordered, status, n_ev, quick_pub, rstatus, drained = row
        searched = n_ev > 0
        good = (
            status == "COMPLETED"
            and drained
            and quick_pub
            and ordered
            and (searched == want_search)
        )
        ok = ok and good
        print(
            f"  [{'PASS' if good else 'FAIL'}] {name:14s} "
            f"status={status} quick={quick_pub} ordered={ordered} "
            f"搜索={searched}(期望 {want_search}) research_status={rstatus}"
        )

    print("\n全部通过。" if ok else "\n存在失败项，见上。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
