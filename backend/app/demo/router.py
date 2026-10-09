"""演示输入 HTTP surface（产品化会议室输入框的后端入口）。

::

    POST /api/meetings/{meeting_id}/demo/utterances   -> 200（服务端投影）

刻意与两个已有 ingress 区分开（见 ``app/demo/schemas.py`` 的模块文档）：

  ``/internal/.../transcript-events``  producer-owned ``segment_seq``，前端不可用
  ``/internal/.../asr-events``         协作方（ASR）语义，identity 不匹配产品 UI
  ``/meetings/{id}/demo/utterances``   产品级：``speaker`` + ``text``，序号服务端分配

返回值不是 204 而是 200 + 投影，因为服务端才是 ``segment_seq`` / 时间轴的**唯一
事实源**：前端渲染必须用后端给的值，绝不能自己算（那会让前端成为 ordering 的
第二个所有者）。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.demo import service as demo_service
from app.demo.schemas import DemoUtteranceAccepted, DemoUtteranceIn

router = APIRouter(tags=["demo"])


@router.post(
    "/meetings/{meeting_id}/demo/utterances",
    response_model=DemoUtteranceAccepted,
    status_code=status.HTTP_200_OK,
)
def ingest_demo_utterance(
    meeting_id: str,
    payload: DemoUtteranceIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> DemoUtteranceAccepted:
    """Append one simulated participant utterance as a real FINAL.

    Thin by design: validation and delegation only. Ordering allocation,
    idempotency, SSE broadcast and the 水华队长 wake-word decision all happen in
    the existing Transcript Domain / captain package — this route adds no
    business logic, so it cannot drift from the real ASR path.
    """
    if not settings.demo_ingress_enabled:
        # 关闭时路由**不存在**（404），而不是 403：入口被禁用不是「你无权」，
        # 而是这套能力根本不在这台部署上。与 captain_enabled 的 503 语义刻意不同
        # —— 水华队长是可用的产品能力被临时关掉，演示输入是整个功能的移除。
        raise HTTPException(status_code=404, detail="not found")

    return demo_service.ingest_demo_utterance(
        db, meeting_id, payload, background_tasks=background_tasks
    )


__all__ = ["router"]
