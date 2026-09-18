import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Skeleton, Tag } from 'antd'
import {
  ClockCircleOutlined,
  AudioOutlined,
  ReloadOutlined,
  ArrowLeftOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { InfoRow } from '@/components/InfoRow'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import { useMeetingStore } from '@/stores/meetingStore'
import { useMeetingLoad } from '@/hooks/useMeetingLoad'
import { formatDateTime, formatDuration, elapsedMs } from '@/utils/time'
import type { Meeting } from '@/types/meeting'

/** 实时秒数刷新 hook：仅 active 时以 1s 步进更新，unmount 或 active 变 false 即清理。 */
function useNowTick(active: boolean, startIso: string | null | undefined) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active || !startIso) return
    setNow(Date.now())
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [active, startIso])
  return now
}

/** Header strip：真实后端字段，title 缺失/为空时才 fallback「未命名会议」。 */
function Header({ meeting, action }: { meeting: Meeting; action?: React.ReactNode }) {
  const title = meeting.title?.trim() || '未命名会议'
  const startedAtText = meeting.started_at
    ? `${formatDateTime(meeting.started_at, true)} 开始`
    : '等待开始'
  return (
    <header className="room__header">
      <div style={{ minWidth: 0 }}>
        <h1 className="room__title h-m0" title={meeting.title || undefined}>
          {title}
        </h1>
        <div className="room__meta" style={{ marginTop: 6 }}>
          {meeting.id ? <span className="room__id">#{meeting.id}</span> : null}
          {meeting.id ? <span className="meta-sep" /> : null}
          <span>{startedAtText}</span>
          <span className="meta-sep" />
          <MeetingStatusTag status={meeting.status} />
        </div>
      </div>
      {action ? <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>{action}</div> : null}
    </header>
  )
}

/** 主区占位文案（四态）：8B-3/8B-4 才挂真正的实时转写。 */
function transcriptPlaceholder(status: Meeting['status']) {
  switch (status) {
    case 'IN_PROGRESS':
      return {
        title: '实时记录将在 Step 8B-3/8B-4 接入',
        description: '会议进行中。实时转写与 AI 纪要将在后续步骤接入。',
      }
    case 'FINALIZING':
      return {
        title: '正在收尾，会议结束中',
        description: '正在等待最后的转写内容与会议纪要生成。',
      }
    case 'COMPLETED':
      return {
        title: '会议已结束',
        description: '会议已结束，纪要将在后续步骤接入。',
      }
    default:
      return { title: '', description: '' }
  }
}

/** AI 纪要标签（仅展示，不请求 summary 接口） */
function summaryTag(status: Meeting['status']) {
  if (status === 'COMPLETED') {
    return <Tag color="green">已完成</Tag>
  }
  if (status === 'FINALIZING') {
    return <Tag color="processing">正在生成</Tag>
  }
  return <Tag>等待会议结束</Tag>
}

/** 转写标签（仅展示） */
function transcriptTag(status: Meeting['status']) {
  if (status === 'IN_PROGRESS') return <Tag color="blue">进行中</Tag>
  if (status === 'FINALIZING' || status === 'COMPLETED') return <Tag>已关闭</Tag>
  return <Tag color="default">等待会议开始</Tag>
}

/**
 * Meeting Room —— Step 8B-1 真实 GET Meeting 驱动 + CREATED 会前准备状态。
 * 创建 ≠ 开始：CREATED 只展示「准备开始会议」信息，不自动记录 / 不显示记录态。
 * 8B-3/8B-4 才接入 Snapshot + SSE 实时转写；此处不调用 transcript / summary 接口。
 */
export function MeetingRoomPage() {
  const { meetingId } = useParams<{ meetingId: string }>()
  const navigate = useNavigate()

  const meeting = useMeetingStore((s) => s.meeting)
  const loading = useMeetingStore((s) => s.loading)
  const error = useMeetingStore((s) => s.error)
  const setError = useMeetingStore((s) => s.setError)

  // 重试：bump reloadKey → hook 重新 GET 同一 meeting
  const [reloadKey, setReloadKey] = useState(0)
  useMeetingLoad(meetingId, reloadKey)

  const now = useNowTick(
    meeting?.status === 'IN_PROGRESS',
    meeting?.started_at ?? null,
  )
  const durationText =
    meeting?.status === 'IN_PROGRESS' && meeting.started_at
      ? formatDuration(elapsedMs(meeting.started_at, new Date(now)))
      : '--'

  // 首次加载（且尚未拿到 meeting 或错误）→ 轻量 Skeleton，不闪现 demo 值
  if (loading && !meeting && !error) {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <Skeleton active paragraph={{ rows: 1 }} title={{ width: 240 }} />
        <div style={{ marginTop: 20 }}>
          <Skeleton active paragraph={{ rows: 6 }} />
        </div>
      </div>
    )
  }

  // 404 → 明确「不存在」而非 fake
  if (error === 'not_found') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <div className="panel" style={{ padding: 32 }}>
          <EmptyPlaceholder
            icon={<AudioOutlined />}
            title="会议不存在或已被删除"
            description="请确认链接是否正确，或返回首页创建新会议。"
            action={
              <Button type="primary" icon={<ArrowLeftOutlined />} onClick={() => navigate('/')}>
                返回首页
              </Button>
            }
          />
        </div>
      </div>
    )
  }

  // network / 5xx / 其它 → 明确「加载失败」+ 重试（只重新 GET meeting）
  if (error && error !== 'not_found') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <div className="panel" style={{ padding: 32 }}>
          <EmptyPlaceholder
            icon={<ReloadOutlined />}
            title="会议加载失败"
            description={
              error === 'network'
                ? '无法连接后端服务，请确认服务已启动后重试。'
                : '加载会议时出现问题，请稍后重试。'
            }
            action={
              <Button
                type="primary"
                icon={<ReloadOutlined />}
                onClick={() => {
                  setError('')
                  setReloadKey((k) => k + 1)
                }}
              >
                重试
              </Button>
            }
          />
        </div>
      </div>
    )
  }

  if (!meeting) return null

  // ---- CREATED：会前准备（8B-1 不做 start，按钮 disabled 注明后续接入） ----
  if (meeting.status === 'CREATED') {
    return (
      <div className="page page--wide" style={{ minHeight: 0 }}>
        <Header
          meeting={meeting}
          action={
            <Button type="primary" disabled title="开始记录将在下一步接入">
              开始会议
            </Button>
          }
        />

        <div style={{ marginTop: 24 }}>
          <EmptyPlaceholder
            icon={<ClockCircleOutlined />}
            title="准备开始会议"
            description="开始会议后，小水华将实时记录会议内容，并在会议结束后自动生成纪要。"
          />
        </div>

        <aside className="room__rail" style={{ marginTop: 24 }}>
          <div className="rail-section">
            <div className="rail-title">会议状态</div>
            <InfoRow label="会议状态" value={<MeetingStatusTag status="CREATED" />} />
            <InfoRow label="开始时间" value="--" />
            <InfoRow label="持续时长" value="--" />
          </div>
          <div className="rail-section">
            <div className="rail-title">转写与纪要</div>
            <InfoRow label="转写状态" value={<Tag>等待会议开始</Tag>} />
            <InfoRow label="AI 纪要" value={<Tag>等待会议结束</Tag>} />
          </div>
          <div className="rail-section">
            <div className="rail-title">会议信息</div>
            <InfoRow label="Meeting ID" value={<span className="room__id">{meeting.id}</span>} />
          </div>
        </aside>
      </div>
    )
  }

  // ---- 其它状态：IN_PROGRESS / FINALIZING / COMPLETED ----
  const placeholder = transcriptPlaceholder(meeting.status)
  return (
    <div className="page page--wide" style={{ minHeight: 0 }}>
      {/* 8B-1：结束/开始动作在 8B-2 接入，此处不留误导性按钮 */}
      <Header meeting={meeting} />

      <div className="room__body">
        <section className="room__transcript">
          <div className="room__section-head">
            <span className="live-dot" />
            <span>实时记录</span>
          </div>
          <div className="transcript-surface">
            <EmptyPlaceholder
              icon={<AudioOutlined />}
              title={placeholder.title}
              description={placeholder.description}
              compact
            />
          </div>
        </section>

        <aside className="room__rail">
          <div className="rail-section">
            <div className="rail-title">状态</div>
            <InfoRow label="会议状态" value={<MeetingStatusTag status={meeting.status} />} />
            <InfoRow
              label="开始时间"
              value={meeting.started_at ? formatDateTime(meeting.started_at, true) : '--'}
            />
            <InfoRow label="持续时长" value={durationText} />
          </div>

          <div className="rail-section">
            <div className="rail-title">AI 纪要</div>
            <InfoRow label="纪要状态" value={summaryTag(meeting.status)} />
            <InfoRow
              label="纪要说明"
              value={
                <span style={{ fontSize: 12, color: '#98A2B3' }}>
                  结束会议后自动生成
                </span>
              }
            />
          </div>

          <div className="rail-section">
            <div className="rail-title">会议信息</div>
            <InfoRow label="Meeting ID" value={<span className="room__id">{meeting.id}</span>} />
            <InfoRow label="转写" value={transcriptTag(meeting.status)} />
          </div>
        </aside>
      </div>
    </div>
  )
}
