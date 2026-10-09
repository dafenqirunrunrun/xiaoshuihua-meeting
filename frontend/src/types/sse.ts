/**
 * SSE 事件类型：严格对应对应后端 SSE_EVENT_TYPES（schemas/sse.py）。
 * PRD §30：transcript.partial / transcript.final / meeting.status / summary.* / heartbeat。
 * Step UI-INTEL-1 新增 meeting.state.updated（Meeting State 失效通知，payload 仅
 * {version,status}，真正的状态走 GET /api/meetings/:id/state）。
 * ADR-002 Phase 2-A 新增 captain.completed / captain.failed：水华队长报告就绪/失败。
 * captain.completed **例外地**在 payload 中直接携带报告内容（positions /
 * dispute_points / uncertainty），与 meeting.state.updated 的"纯失效通知"约定不同；
 * 但 report_id 始终在 payload 中，断线重连仍以 GET
 * /api/meetings/:id/captain/{report_id} 为事实源（SSE 不可回放）。
 * Task 6（两阶段裁判）新增 captain.quick_assessment：快速判断已就绪、深入核对仍在
 * 进行中。它必须与 captain.completed **分开**：收到 completed 的客户端可以停止
 * loading，收到 quick_assessment 的客户端必须继续等待。因此 Quick 的内容放在这个
 * 事件里，而**不是** captain.completed 的某个子字段 —— 否则每个客户端都要检查子字段
 * 才能判断该不该停止 loading。
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
  | 'captain.completed'
  | 'captain.failed'
  | 'captain.quick_assessment'
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
