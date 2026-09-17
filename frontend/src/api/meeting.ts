import { http } from './http'
import type { Meeting, MeetingCreate } from '@/types/meeting'

/** POST /api/meetings —— 创建会议（PRD §37） */
export async function createMeeting(payload: MeetingCreate): Promise<Meeting> {
  const { data } = await http.post<Meeting>('/api/meetings', payload)
  return data
}

/** POST /api/meetings/:id/start —— 开始会议（CREATED -> IN_PROGRESS） */
export async function startMeeting(meetingId: string): Promise<Meeting> {
  const { data } = await http.post<Meeting>(
    `/api/meetings/${meetingId}/start`,
  )
  return data
}

/** POST /api/meetings/:id/finish —— 结束会议（IN_PROGRESS -> FINALIZING） */
export async function finishMeeting(meetingId: string): Promise<Meeting> {
  const { data } = await http.post<Meeting>(
    `/api/meetings/${meetingId}/finish`,
  )
  return data
}

/** GET /api/meetings/:id —— 查询单个会议 */
export async function getMeeting(meetingId: string): Promise<Meeting> {
  const { data } = await http.get<Meeting>(`/api/meetings/${meetingId}`)
  return data
}
