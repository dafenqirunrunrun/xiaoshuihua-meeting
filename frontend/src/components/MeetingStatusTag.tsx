import type { MeetingStatus } from '@/types/meeting'
import { Tag } from 'antd'

/**
 * Meeting 状态 Tag。严格四态映射（PRD §11 / §12 精神）：
 *   待开始 / 进行中 / 处理中 / 已完成。
 * 不存在 FINALIZING_FAILED 的会议状态 ——「纪要失败」由 Summary 状态表达，
 * 不在此冒充 Meeting 状态。
 */
const STATUS_MAP: Record<MeetingStatus, { label: string; color: string }> = {
  CREATED: { label: '待开始', color: 'default' },
  IN_PROGRESS: { label: '进行中', color: 'blue' },
  FINALIZING: { label: '处理中', color: 'processing' },
  COMPLETED: { label: '已完成', color: 'green' },
}

export function MeetingStatusTag({
  status,
}: {
  status: MeetingStatus
}) {
  const meta = STATUS_MAP[status] ?? { label: status, color: 'default' }
  return <Tag color={meta.color}>{meta.label}</Tag>
}

export const MEETING_STATUS_OPTIONS = Object.keys(STATUS_MAP) as MeetingStatus[]
