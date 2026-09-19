"""Independent OpenAPI / Swagger for the INTERNAL ASR API (ASR-0B).

The internal ASR surface is a **cross-team contract** with the upstream Audio /
ASR collaborators, so it deserves its own documentation page rather than being
buried among the business endpoints a frontend developer reads.

Design — the schema is DERIVED, never hand-written:

  A second ``FastAPI`` instance includes the very same routers the real app
  serves, with the same ``/api`` prefix, and we expose that instance's generated
  OpenAPI document. Because it is built from the live route objects, it cannot
  silently drift from the implementation the way a hand-maintained spec would.

Exposed (added to the main app, alongside the business endpoints):

    GET /api/internal/openapi.json   the internal-ASR-only OpenAPI document
    GET /api/internal/docs           Swagger UI for that document

The regular ``/docs`` continues to describe the whole application; this is an
additional, focused view for the collaborator. No route ownership moves: the
ingress endpoints still live in ``internal_router`` and are served by the main
app exactly as before.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from app.transcript import internal_router

_INTERNAL_TITLE = "小水华 · Internal ASR Ingress API"
_INTERNAL_DESCRIPTION = """
上游 Audio / ASR 流水线对接的**内部接口契约**（跨团队）。

- `POST /api/internal/meetings/{meeting_id}/asr-events` —— 外部协作者格式
  （camelCase，**不含** `segmentSeq`；排序由 Transcript Domain 分配）。
- `POST /api/internal/meetings/{meeting_id}/transcript-events` —— 内部契约
  （snake_case，`segment_seq` 由 Producer 提供），保留兼容。
- `POST /api/internal/meetings/{meeting_id}/stream-end` —— 终止信号
  （`last_segment_seq` 可选，省略时由后端派生）。

契约事实源见仓库 `protocols/transcript-format.md` 与 `protocols/realtime-events.md`。
两者与本页不一致时，以 `protocols/` 为准并视为缺陷。
"""

router = APIRouter(tags=["internal-docs"])


def _build_schema() -> dict:
    """Generate the internal-ASR-only OpenAPI document from the real routes."""
    from fastapi import FastAPI

    app = FastAPI(
        title=_INTERNAL_TITLE,
        version="0.1.0",
        description=_INTERNAL_DESCRIPTION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Same prefix the real app uses -> identical paths, no drift.
    app.include_router(internal_router.router, prefix="/api")
    return app.openapi()


@router.get("/internal/openapi.json", include_in_schema=False)
def internal_openapi() -> JSONResponse:
    """OpenAPI document covering ONLY the internal ASR ingress contract."""
    return JSONResponse(_build_schema())


@router.get("/internal/docs", include_in_schema=False)
def internal_swagger() -> HTMLResponse:
    """Swagger UI for the internal ASR ingress contract."""
    return get_swagger_ui_html(
        openapi_url="/api/internal/openapi.json",
        title=f"{_INTERNAL_TITLE} — Swagger UI",
    )
