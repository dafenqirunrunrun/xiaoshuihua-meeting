/**
 * SSE 事件类型：严格对应对应后端 SSE_EVENT_TYPES（schemas/sse.py）。
 * PRD §30：transcript.partial / transcript.final / meeting.status / summary.* / heartbeat。
 * Step UI-INTEL-1 新增 meeting.state.updated（Meeting State 失效通知，payload 仅
 * {version,status}，真正的状态走 GET /api/meetings/:id/state）。
 * TRANSCRIPT_STREAM_END 不广播（契约不可扩展），无需类型。
 */
export type SseEventType =
  | 'transcript.partial'
  | 'transcript.final'
  | 'meeting.status'
  | 'summary.started'
  | 'summary.completed'
  | 'summary.failed'
  | 'meeting.state.updated'
  | 'heartbeat'

/** SSE data: 行的完整信封（SSEEventEnvelope.to_sse_data，不含 sse_id） */
export interface SseEnvelope {
  event_type: SseEventType
  meeting_id: string
  emitted_at: string
  payload: Record<string, unknown>
}

/** SSE 连接生命周期（Step 8B 使用）：idle / connecting / connected / reconnecting / error */
export type StreamStatus =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'error'
