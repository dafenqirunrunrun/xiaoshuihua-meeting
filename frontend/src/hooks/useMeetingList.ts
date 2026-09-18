import { useCallback, useEffect, useRef, useState } from 'react'
import { listMeetings, normalizeApiError } from '@/api'
import type { Meeting, MeetingListQuery } from '@/types/meeting'

/**
 * Meeting 列表加载钩子（Step 8B-6，Meeting Discovery）。
 *
 * 职责：
 *  - 只在依赖变化时 GET /api/meetings（query / reloadKey 变化触发重新拉取）；
 *  - 错误归一化（network / http / unknown），暴露轻量重试 reload()；
 *  - 不缓存到 Zustand —— 列表每次刷新/重进都重新请求（RES-15：
 *    刷新后不依赖前端缓存，Backend 是唯一事实源）。
 *
 * 组件不得直接调 axios —— 一律走 API 层 listMeetings。
 */
export function useMeetingList(query: MeetingListQuery = {}, reloadKey = 0) {
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const inFlight = useRef(0)

  const load = useCallback(() => {
    const reqId = ++inFlight.current
    setLoading(true)
    setError(null)
    listMeetings(query)
      .then((res) => {
        if (inFlight.current !== reqId) return
        setMeetings(res.items)
        setTotal(res.total)
      })
      .catch((raw: unknown) => {
        if (inFlight.current !== reqId) return
        setMeetings([])
        setError(normalizeApiError(raw).message || '会议列表加载失败')
      })
      .finally(() => {
        if (inFlight.current === reqId) setLoading(false)
      })
  }, [query])

  const reload = useCallback(() => setRetry((k) => k + 1), [])

  useEffect(() => {
    load()
  }, [load, reloadKey, retry])

  return { meetings, total, loading, error, reload }
}
