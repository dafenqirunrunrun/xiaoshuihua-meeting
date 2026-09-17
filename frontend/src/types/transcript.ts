/** FINAL TranscriptSegment（TranscriptSegmentOut，PRD §33） */
export interface TranscriptSegment {
  segment_seq: number
  utterance_id: string
  speaker_id: string
  text: string
  start_ms: number | null
  end_ms: number | null
}

/** GET /api/meetings/:id/transcript —— 仅 FINAL，按 segment_seq 升序 */
export interface TranscriptSnapshot {
  meeting_id: string
  segments: TranscriptSegment[]
}

/** 实时 PARTIAL 出声（SSE transcript.partial 的 payload） */
export interface PartialUtterance {
  utterance_id: string
  speaker_id: string
  text: string
}
