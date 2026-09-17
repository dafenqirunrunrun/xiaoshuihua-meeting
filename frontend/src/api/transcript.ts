import { http } from './http'
import type { TranscriptSnapshot } from '@/types/transcript'

/** GET /api/meetings/:id/transcript —— 仅 FINAL，按 segment_seq 升序（PRD §32/§48） */
export async function getTranscriptSnapshot(
  meetingId: string,
): Promise<TranscriptSnapshot> {
  const { data } = await http.get<TranscriptSnapshot>(
    `/api/meetings/${meetingId}/transcript`,
  )
  return data
}
