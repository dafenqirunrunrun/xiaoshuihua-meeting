import { create } from 'zustand'
import type { Meeting } from '@/types/meeting'
import type { PartialUtterance, TranscriptSegment } from '@/types/transcript'
import type { MeetingSummary } from '@/types/summary'
import type { StreamStatus } from '@/types/sse'

/**
 * 会议级 Zustand store（PRD §34/§35，fronted_designer §37）。
 *
 * 硬性分离：finalSegments（FINAL 正式事实）与 partialUtterances（PARTIAL 实时 UI）
 * 是两个独立结构，禁止混入同一数组。8A 只定义 shape + reset，
 * 8B 再填充 SSE reducer（upsertPartial / commitFinal 等），此处不实现。
 */
interface MeetingState {
  /** 当前会议（由 GET /meetings/:id 填充） */
  meeting: Meeting | null
  /** 仅 FINAL 正式事实，按 segment_seq 升序维护 */
  finalSegments: TranscriptSegment[]
  /** PARTIAL 实时出声，key = utterance_id，同一 key 原地更新 */
  partialUtterances: Record<string, PartialUtterance>
  /** 最新版本 Summary 快照（GET /summary 恢复；SSE summary.* 只是增量通知） */
  summary: MeetingSummary | null
  /** SSE 连接生命周期 */
  streamStatus: StreamStatus
  /** 页面初始加载中 */
  loading: boolean
  /** 加载失败状态（'not_found' | 'network' | 其它错误消息）；null = 无错误 */
  error: string | null

  setMeeting: (meeting: Meeting | null) => void
  setFinalSegments: (segments: TranscriptSegment[]) => void
  setLoading: (loading: boolean) => void
  setError: (error: string | null) => void
  /** 重置会议级状态（退出页面 / 切换会议时清理，避免跨会议串数据） */
  resetMeetingState: () => void
}

export const useMeetingStore = create<MeetingState>((set) => ({
  meeting: null,
  finalSegments: [],
  partialUtterances: {},
  summary: null,
  streamStatus: 'idle',
  loading: false,
  error: null,

  setMeeting: (meeting) => set({ meeting }),
  setFinalSegments: (segments) => set({ finalSegments: segments }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),

  resetMeetingState: () =>
    set({
      meeting: null,
      finalSegments: [],
      partialUtterances: {},
      summary: null,
      streamStatus: 'idle',
      loading: false,
      error: null,
    }),
}))
