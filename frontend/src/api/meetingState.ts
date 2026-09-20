import { http } from './http'
import type { MeetingState } from '@/types/meetingState'

/**
 * GET /api/meetings/:id/state —— Meeting State（会议工作记忆）的**事实源**。
 *
 * Step UI-INTEL-1：这是产品侧唯一的 Meeting State 读取入口。
 *  - 首次进入 / 刷新 / SSE 重连 → 都以本次 GET 为准（不依赖 SSE 回放）；
 *  - `meeting.state.updated` SSE 只是失效通知（invalidation），收到后重新调用本函数；
 *  - 后端 internal 调试接口 `/api/internal/meetings/:id/state` 前端**不使用**。
 *
 * 会议不存在 → 404；会议存在但尚未处理任何 FINAL → 200 + version 0 + EMPTY
 * （这是最常见的早期状态，不是错误）。
 */
export async function getMeetingState(
  meetingId: string,
): Promise<MeetingState> {
  const { data } = await http.get<MeetingState>(
    `/api/meetings/${meetingId}/state`,
  )
  return data
}
