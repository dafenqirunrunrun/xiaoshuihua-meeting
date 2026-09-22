import { useCallback, useEffect, useRef, useState } from 'react'
import { listMeetings, normalizeApiError } from '@/api'
import type { Meeting, MeetingListQuery } from '@/types/meeting'

/** 列表页分页：每页 20，后端默认亦为 20（LIST_DEFAULT_LIMIT）。 */
const DEFAULT_LIMIT = 20

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
export function useMeetingList(
  query: MeetingListQuery = {},
  reloadKey = 0,
) {
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const inFlight = useRef(0)

  // 参数收敛为稳定标量：query 往往是渲染时新建的对象字面量，直接依赖其
  // 引用会导致「请求 → setState → 重渲染 → 新引用 → 再次请求」的无限
  // 循环，表现为首页持续抖动（每次响应都闪现 loading 骨架）。
  const status = query.status ?? null
  const search = query.q?.trim() ?? ''
  const createdFrom = query.created_from ?? ''
  const createdTo = query.created_to ?? ''
  const sort = query.sort ?? 'updated_desc'
  const limit = query.limit ?? DEFAULT_LIMIT
  const offset = query.offset ?? 0

  const load = useCallback(() => {
    const reqId = ++inFlight.current
    setLoading(true)
    setError(null)
    listMeetings({
      status: status ?? undefined,
      q: search || undefined,
      created_from: createdFrom || undefined,
      created_to: createdTo || undefined,
      sort,
      limit,
      offset,
    })
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
  }, [status, search, createdFrom, createdTo, sort, limit, offset])

  const reload = useCallback(() => setRetry((k) => k + 1), [])

  useEffect(() => {
    load()
  }, [load, reloadKey, retry])

  return { meetings, total, loading, error, reload }
}
