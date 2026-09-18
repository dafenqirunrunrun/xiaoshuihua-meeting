import { useCallback, useEffect, useState } from 'react'
import { getSummary, normalizeApiError } from '@/api'
import type { MeetingSummary } from '@/types/summary'

export interface SummaryLoadResult {
  /** 最新版本 Summary（latest version，Step 7B §15） */
  summary: MeetingSummary | null
  loading: boolean
  /**
   * 错误归一化：
   *  - '' 无错误
   *  - 'not_found' —— 会议存在但 Summary 尚未生成（后端 404 detail=summary not found）
   *  - 'network'   —— 网络 / 5xx（后端不可达）
   *  - 其它        —— 具体错误消息
   */
  error: string
  /** 轻量重试：只重新 GET Summary，不影响 Meeting / Transcript */
  retry: () => void
}

/**
 * Summary Snapshot 加载钩子（Step 8C-1）。
 *
 * Result 页数据域之一：GET /api/meetings/:id/summary 获取**持久化 latest
 * version**。独立错误域 —— Summary 失败绝不影响 Meeting / Transcript 展示。
 *
 * 404 语义区分（依赖后端 actual contract）：
 *  - meeting 不存在 → 后端在 GET /summary 内先调 get_meeting，抛
 *    「Meeting not found」（由 useMeetingLoad 处理 meeting 的 404）；
 *  - meeting 存在但 summary 未生成 → 404 detail=「summary not found」→ 这里归为
 *    not_found（UI 显示「纪要尚未生成」）。
 *
 * 触发时机：meetingId 变化 / 主动 retry。不做自动轮询（实时更新留 8C-2）。
 */
export function useMeetingSummary(
  meetingId: string | undefined,
): SummaryLoadResult {
  const [summary, setSummary] = useState<MeetingSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    // meetingId 变化 / 未就绪 → 归零，等待下次触发
    if (!meetingId) {
      setSummary(null)
      setError('')
      return
    }

    let cancelled = false
    setLoading(true)
    setError('')

    getSummary(meetingId)
      .then((data) => {
        if (cancelled) return
        setSummary(data)
      })
      .catch((raw: unknown) => {
        if (cancelled) return
        const normalized = normalizeApiError(raw)
        if (normalized.kind === 'http' && normalized.status === 404) {
          // meeting 存在但 summary 未生成
          setError('not_found')
        } else if (
          normalized.kind === 'network' ||
          (normalized.kind === 'http' && (normalized.status ?? 0) >= 500)
        ) {
          setError('network')
        } else {
          setError(normalized.message || '纪要加载失败')
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

  return { summary, loading, error, retry }
}
