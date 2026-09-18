import { http } from './http'
import type {
  Meeting,
  MeetingCreate,
  MeetingList,
  MeetingListQuery,
} from '@/types/meeting'

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

/**
 * GET /api/meetings —— 会议列表（Step 8B-6，Meeting Discovery）。
 * 复用后端 MeetingListOut：{items, total, limit, offset}；status/limit/offset 可选。
 */
export async function listMeetings(
  params: MeetingListQuery = {},
): Promise<MeetingList> {
  const { data } = await http.get<MeetingList>('/api/meetings', { params })
  return data
}
