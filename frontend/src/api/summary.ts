import { http } from './http'
import type { MeetingSummary, RegenerationAccepted } from '@/types/summary'

/**
 * GET /api/meetings/:id/summary —— 最新版本 Summary 快照（Step 7B §15）。
 * durable 恢复事实源：SSE summary.* 非 durable 不回放，重连后靠本接口恢复。
 */
export async function getSummary(
  meetingId: string,
): Promise<MeetingSummary> {
  const { data } = await http.get<MeetingSummary>(
    `/api/meetings/${meetingId}/summary`,
  )
  return data
}

/**
 * POST /api/meetings/:id/summary/regenerate —— 重新生成纪要（Step 7B §22）。
 * 返回 202 { version, status: GENERATING }；LLM 在后台完成，不阻塞。
 */
export async function regenerateSummary(
  meetingId: string,
): Promise<RegenerationAccepted> {
  const { data } = await http.post<RegenerationAccepted>(
    `/api/meetings/${meetingId}/summary/regenerate`,
  )
  return data
}
