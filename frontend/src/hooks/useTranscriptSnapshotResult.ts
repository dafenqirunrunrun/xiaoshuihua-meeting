import { useCallback, useEffect, useState } from 'react'
import { getTranscriptSnapshot, normalizeApiError } from '@/api'
import type { TranscriptSegment } from '@/types/transcript'

export interface TranscriptSnapshotResult {
  segments: TranscriptSegment[]
  loading: boolean
  /** '' 无错误；'network' 网络/5xx；其它为具体错误消息 */
  error: string
  /** 只重新 GET Transcript Snapshot */
  retry: () => void
}

/**
 * Result 页专用 Transcript Snapshot 加载钩子（Step 8C-1）。
 *
 * 与 MeetingRoom 的 useTranscriptSnapshot 的差异：
 *  - **不读 / 不写 meetingStore** —— Result 是「归档阅读态」，持久化 FINAL
 *    全部来自 backend GET，不依赖上一页面 Zustand 残留（PRD §54，refresh 恢复）。
 *  - 不参与 SSE（本 Step 明确不消费 transcript SSE / summary SSE）。
 *  - 独立错误域：Transcript 失败不影响 Summary / Meeting 展示。
 *
 * Snapshot 契约（PRD §32/§48）：仅 FINAL，按 segment_seq ASC。
 */
export function useTranscriptSnapshotResult(
  meetingId: string | undefined,
): TranscriptSnapshotResult {
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    if (!meetingId) {
      setSegments([])
      setError('')
      return
    }

    let cancelled = false
    setLoading(true)
    setError('')

    getTranscriptSnapshot(meetingId)
      .then((snapshot) => {
        if (cancelled) return
        // 防御性确认 ASC（后端已保证，前端不重写业务排序逻辑）
        const sorted = [...snapshot.segments].sort(
          (a, b) => a.segment_seq - b.segment_seq,
        )
        setSegments(sorted)
      })
      .catch((raw: unknown) => {
        if (cancelled) return
        const normalized = normalizeApiError(raw)
        if (
          normalized.kind === 'network' ||
          (normalized.kind === 'http' && (normalized.status ?? 0) >= 500)
        ) {
          setError('network')
        } else {
          setError(normalized.message || '会议内容加载失败')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [meetingId, reloadKey])

  const retry = useCallback(() => setReloadKey((k) => k + 1), [])

  return { segments, loading, error, retry }
}
