/** Summary 状态：与后端 SummaryOut.status 一致（PRD §12） */
export type SummaryStatus =
  | 'NOT_STARTED'
  | 'GENERATING'
  | 'COMPLETED'
  | 'FAILED'

/**
 * GET /api/meetings/:id/summary 返回（SummaryOut，Step 7B §15）。
 * Durable 恢复事实源：SSE summary.* 事件非 durable、不回放，刷新后靠本接口恢复。
 */
export interface MeetingSummary {
  id: number
  meeting_id: string
  version: number
  status: SummaryStatus
  overview: string | null
  key_topics: string[]
  conclusions: string[]
  open_questions: string[]
  model_name: string | null
  raw_model_output: string | null
  created_at: string | null
}

/** POST /api/meetings/:id/summary/regenerate 的 202 响应（RegenerationAccepted） */
export interface RegenerationAccepted {
  meeting_id: string
  version: number
  status: 'GENERATING'
}

/** Pydantic 校验通过的结构化Summary（渲染层直接消费 overview/topics/conclusions/questions） */
export interface MeetingSummaryContent {
  overview: string
  key_topics: string[]
  conclusions: string[]
  open_questions: string[]
}
