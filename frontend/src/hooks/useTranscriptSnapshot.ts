import { useEffect, useState } from 'react'
import { getTranscriptSnapshot, normalizeApiError } from '@/api'
import { useMeetingStore } from '@/stores/meetingStore'

/**
 * Transcript Snapshot 加载钩子（Step 8B-3）。
 *
 * 恢复模型第二步（PRD §53/§54）：GET Meeting 成功 → GET Transcript Snapshot
 * → setFinalSegments(...)。Snapshot 是「已持久化 FINAL」的历史事实源；PARTIAL
 * / SSE 不做，留给 8B-4。
 *
 * 独立错误域：Transcript 失败绝不影响 meeting 状态 —— 页面仍展示 meeting，
 * 只让 Transcript 区显示「转写记录加载失败」+ 轻量「重试」（只重新 GET
 * Snapshot）。meetingId 变化时 store 已被 useMeetingLoad reset，finalSegments
 * 清空，不会跨会议串数据。
 *
 * 触发时机：meetingId 变化 / status 变化（IN_PROGRESS→FINALIZING→COMPLETED
 * 重新拉一次，取到手边界附近落库的 FINAL）/ 主动重试。这不是 SSE 实时性——
 * 仍是「主动重新加载 Snapshot」语义。
 */
export function useTranscriptSnapshot(
  meetingId: string | undefined,
  enabled: boolean,
  status?: string,
) {
  const setFinalSegments = useMeetingStore((s) => s.setFinalSegments)
  const [transcriptError, setTranscriptError] = useState('')
  const [transcriptLoading, setTranscriptLoading] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    // meetingId 变化 → 清空上一次会议的加载状态（finalSegments 由 store reset 清空）
    if (!meetingId || !enabled) {
      setTranscriptError('')
      return
    }

    let cancelled = false
    setTranscriptLoading(true)
    setTranscriptError('')

    getTranscriptSnapshot(meetingId)
      .then((snapshot) => {
        if (cancelled) return
        // backend 已按 segment_seq ASC 排序（repository.list_sorted）
        setFinalSegments(snapshot.segments)
      })
      .catch((raw: unknown) => {
        if (cancelled) return
        setFinalSegments([])
        setTranscriptError(normalizeApiError(raw).message || '转写记录加载失败')
      })
      .finally(() => {
        if (!cancelled) setTranscriptLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [meetingId, enabled, status, reloadKey, setFinalSegments])

  return {
    transcriptError,
    transcriptLoading,
    retryTranscript: () => setReloadKey((k) => k + 1),
  }
}
