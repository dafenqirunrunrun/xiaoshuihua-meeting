import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Popconfirm, Tooltip } from 'antd'
import { DeleteOutlined, EditOutlined, PlayCircleOutlined } from '@ant-design/icons'
import type { Meeting } from '@/types/meeting'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { durationBetweenMs, formatDateTime, formatDuration, elapsedMs } from '@/utils/time'

/**
 * 状态驱动的列表项 CTA（Step 8B-6 §13）。语义：
 *   CREATED    -> 继续准备（/meetings/:id 会前准备页）
 *   IN_PROGRESS-> 继续会议（Resume = GET Meeting -> Snapshot -> SSE）
 *   FINALIZING -> 查看收尾
 *   COMPLETED  -> 查看结果（/meetings/:id/result）
 * 绝不出现「重新开始 / 继续录音」——这些语义目前不存在。
 */
export function meetingCta(meeting: Meeting): {
  label: string
  to: string
} {
  switch (meeting.status) {
    case 'CREATED':
      return { label: '继续准备', to: `/meetings/${meeting.id}` }
    case 'IN_PROGRESS':
      return { label: '继续会议', to: `/meetings/${meeting.id}` }
    case 'FINALIZING':
      return { label: '查看收尾', to: `/meetings/${meeting.id}` }
    case 'COMPLETED':
      return { label: '查看结果', to: `/meetings/${meeting.id}/result` }
    default:
      return { label: '查看', to: `/meetings/${meeting.id}` }
  }
}

/** 真实可计算时长：只展示 started_at -> 现在（进行中）或 ended_at（已结束）。 */
export function meetingDurationText(meeting: Meeting): string {
  if (meeting.status === 'IN_PROGRESS' && meeting.started_at) {
    return formatDuration(elapsedMs(meeting.started_at, new Date()))
  }
  if (
    (meeting.status === 'FINALIZING' || meeting.status === 'COMPLETED') &&
    meeting.started_at &&
    meeting.ended_at
  ) {
    return formatDuration(durationBetweenMs(meeting.started_at, meeting.ended_at))
  }
  return ''
}

/** 列表项次要时间文案（只使用真实字段）。 */
export function meetingMetaText(meeting: Meeting): string {
  switch (meeting.status) {
    case 'CREATED':
      return meeting.created_at ? `创建于 ${formatDateTime(meeting.created_at)}` : ''
    case 'IN_PROGRESS':
      return meeting.started_at ? `开始于 ${formatDateTime(meeting.started_at)}` : ''
    case 'FINALIZING':
      return meeting.ended_at ? `结束于 ${formatDateTime(meeting.ended_at)}` : ''
    case 'COMPLETED':
      return meeting.completed_at
        ? `完成于 ${formatDateTime(meeting.completed_at)}`
        : meeting.created_at
          ? `创建于 ${formatDateTime(meeting.created_at)}`
          : ''
    default:
      return ''
  }
}

interface MeetingListItemProps {
  meeting: Meeting
  /** 覆盖列表项标题前的状态 Tag（默认显示 MeetingStatusTag） */
  statusTag?: ReactNode
  /** 自定义右侧 CTA 区（默认按状态渲染） */
  onNavigate?: (meeting: Meeting) => void
  /** 仅 CREATED / COMPLETED 可删除；未提供时不显示删除入口。 */
  onDelete?: (meeting: Meeting) => Promise<void>
  deleteLoading?: boolean
  onRename?: (meeting: Meeting) => void
}

/**
 * 会议列表项 —— Meeting Discovery 的通用展示单元（Home / Meetings 复用）。
 * 纯展示 + 导航；不做任何业务写操作。状态 CTA 语义见 meetingCta。
 */
export function MeetingListItem({
  meeting,
  statusTag,
  onNavigate,
  onDelete,
  deleteLoading = false,
  onRename,
}: MeetingListItemProps) {
  const navigate = useNavigate()
  const { label, to } = meetingCta(meeting)

  return (
    <div className="meeting-item">
      <div className="meeting-item__main">
        <div className="meeting-item__title-row">
          <span className="meeting-item__title" title={meeting.title || undefined}>
            {meeting.title?.trim() || '未命名会议'}
          </span>
          {onRename ? (
            <Tooltip title="修改会议名称">
              <Button
                type="text"
                size="small"
                className="meeting-item__edit"
                icon={<EditOutlined />}
                onClick={() => onRename(meeting)}
                aria-label={`修改会议名称 ${meeting.title?.trim() || '未命名会议'}`}
              />
            </Tooltip>
          ) : null}
          {statusTag ?? <MeetingStatusTag status={meeting.status} />}
        </div>
        <div className="meeting-item__meta">
          <span className="meeting-item__id">
            会议号 {meeting.meeting_code}
          </span>
          <span className="meta-sep" aria-hidden />
          <span>{meetingMetaText(meeting)}</span>
          {meetingDurationText(meeting) ? (
            <>
              <span className="meta-sep" aria-hidden />
              <span className="u-tabular">{meetingDurationText(meeting)}</span>
            </>
          ) : null}
        </div>
      </div>
      <div className="meeting-item__actions">
        <Button
          type={meeting.status === 'IN_PROGRESS' ? 'primary' : 'default'}
          size="small"
          className="meeting-item__cta"
          onClick={() => (onNavigate ? onNavigate(meeting) : navigate(to))}
        >
          {label}
        </Button>
        {onDelete && (meeting.status === 'CREATED' || meeting.status === 'COMPLETED') ? (
          <Popconfirm
            title={`删除“${meeting.title?.trim() || '未命名会议'}”？`}
            description="转写、AI 会议理解和纪要将一并删除，且无法恢复。"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            placement="topRight"
            onConfirm={() => onDelete(meeting)}
          >
            <Tooltip title="删除会议">
              <Button
                type="text"
                size="small"
                className="meeting-item__delete"
                icon={<DeleteOutlined />}
                loading={deleteLoading}
                aria-label={`删除会议 ${meeting.title?.trim() || '未命名会议'}`}
              />
            </Tooltip>
          </Popconfirm>
        ) : null}
      </div>
    </div>
  )
}

/**
 * 轻量「正在进行的会议」横幅/条目（Home Active 区）。
 * 只表达 IN_PROGRESS / FINALIZING；无 fake 数据。
 */
export function ActiveMeetingCard({ meeting }: { meeting: Meeting }) {
  const navigate = useNavigate()
  const isLive = meeting.status === 'IN_PROGRESS'
  const startText = meeting.started_at
    ? `已开始 ${formatDateTime(meeting.started_at, true)}`
    : ''
  return (
    <div className={`active-meeting${isLive ? ' active-meeting--live' : ''}`}>
      <span className="active-meeting__dot" aria-hidden />
      <div className="active-meeting__body">
        <div className="active-meeting__title">
          {meeting.title?.trim() || '未命名会议'}
        </div>
        <div className="active-meeting__meta">
          {isLive ? (
            <>
              <span style={{ color: 'var(--color-mint)' }}>● 进行中</span>
              {startText ? <span>{startText}</span> : null}
            </>
          ) : (
            <>
              <span style={{ color: 'var(--color-warning)' }}>● 正在收尾</span>
              {startText ? <span>{startText}</span> : null}
            </>
          )}
        </div>
      </div>
      <Button
        type="primary"
        size="small"
        icon={isLive ? <PlayCircleOutlined /> : undefined}
        onClick={() => navigate(`/meetings/${meeting.id}`)}
      >
        {meetingCta(meeting).label}
      </Button>
    </div>
  )
}
