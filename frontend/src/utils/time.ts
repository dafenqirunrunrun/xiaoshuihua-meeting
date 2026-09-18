/**
 * 时间格式化轻量工具。
 * 只用 Intl.DateTimeFormat / Date —— 不额外引入 moment / dayjs。
 */

/** ISO string（后端 datetime, 如 "2026-09-17T10:21:11"）→ "2026-09-17 10:21" */
export function formatDateTime(
  iso: string | null | undefined,
  withSeconds = false,
): string {
  if (!iso) return '--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--'
  const fmt = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' as const } : {}),
    hour12: false,
  })
  return fmt.format(d)
}

/** ISO string → 仅日期 "2026-09-17" */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--'
  const fmt = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
  return fmt.format(d)
}

/**
 * 毫秒 → "HH:MM:SS"（会议时长）。
 * 例：0 → "00:00:00"，3723 → "00:00:03"，723000 → "00:12:03"。
 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) ms = 0
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(h)}:${pad(m)}:${pad(s)}`
}

/** 计算事件 time 到 now 的时间差（毫秒） */
export function elapsedMs(fromIso: string | null | undefined, now: Date = new Date()): number {
  if (!fromIso) return 0
  const d = new Date(fromIso).getTime()
  if (Number.isNaN(d)) return 0
  return Math.max(0, now.getTime() - d)
}
