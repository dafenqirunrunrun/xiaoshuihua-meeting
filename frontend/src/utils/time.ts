/**
 * 时间格式化轻量工具。
 * 只用 Intl.DateTimeFormat / Date —— 不额外引入 moment / dayjs。
 */

/**
 * Parse an API datetime as an absolute instant.
 *
 * New responses carry `Z`; legacy SQLite responses may be timezone-naive even
 * though they contain UTC. Treat only those legacy values as UTC. Explicit
 * offsets remain authoritative and presentation still follows the device zone.
 */
export function parseApiDate(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const value = iso.trim()
  if (!value) return null
  const hasExplicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
  const parsed = new Date(hasExplicitZone ? value : `${value}Z`)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/** ISO string（后端 UTC datetime）→ 当前设备时区的日期时间。 */
export function formatDateTime(
  iso: string | null | undefined,
  withSeconds = false,
): string {
  const d = parseApiDate(iso)
  if (!d) return '--'
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
  const d = parseApiDate(iso)
  if (!d) return '--'
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
  const from = parseApiDate(fromIso)
  if (!from) return 0
  return Math.max(0, now.getTime() - from.getTime())
}

/** Two API datetimes → non-negative elapsed milliseconds. */
export function durationBetweenMs(
  fromIso: string | null | undefined,
  toIso: string | null | undefined,
): number {
  const from = parseApiDate(fromIso)
  const to = parseApiDate(toIso)
  if (!from || !to) return 0
  return Math.max(0, to.getTime() - from.getTime())
}
