import { useParams } from 'react-router-dom'
import { Button, Tag, Tooltip } from 'antd'
import {
  ClockCircleOutlined,
  FileTextOutlined,
  AudioOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { InfoRow } from '@/components/InfoRow'
import { MeetingStatusTag } from '@/components/MeetingStatusTag'
import type { MeetingStatus } from '@/types/meeting'

/**
 * Meeting Room —— Step 8A.1 视觉落地（真实工作区为后续 8B 的实时转写铺路）。
 *
 * 结构（Visual Polish）：
 *  - Header strip（非巨型 Card）：会议名称 / ID / 状态 / 开始时间 / 结束 Action
 *  - 主区「实时记录」：8B 在此挂 PARTIAL（原地更新）/ FINAL（固化列表）
 *  - 右栏「会议信息」：状态 · 时长 · 转写状态 · AI 纪要状态 · Meeting ID
 *
 * 8A.1 仍为骨架：meeting 数据在 8B 用 GET Meeting + Snapshot + SSE 填充。
 */
export function MeetingRoomPage() {
  const { meetingId } = useParams<{ meetingId: string }>()
  const demoStatus: MeetingStatus = 'IN_PROGRESS'
  const demoStarted = '刚刚'

  return (
    <div className="page page--wide room" style={{ minHeight: 0 }}>
      {/* Header strip */}
      <header className="room__header">
        <div>
          <Tooltip title={meetingId}>
            <h1 className="room__title h-m0">未命名会议</h1>
          </Tooltip>
          <div className="room__meta" style={{ marginTop: 6 }}>
            <span className="room__id">#{meetingId}</span>
            <span className="meta-sep" />
            <span>{demoStarted}开始</span>
            <span className="meta-sep" />
            <MeetingStatusTag status={demoStatus} />
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span className="room__meta" style={{ marginRight: 4 }}>
            <ClockCircleOutlined /> 00:00
          </span>
          <Button
            type="primary"
            danger
            icon={<StopOutlined />}
          >
            结束会议
          </Button>
        </div>
      </header>

      {/* 主体：实时记录 workspace + 会议信息 rail */}
      <div className="room__body">
        {/* 实时记录主区 */}
        <section className="room__transcript">
          <div className="room__section-head">
            <span className="live-dot" />
            <span>实时记录</span>
            <span className="room__section-hint">
              FINAL 已保存 · PARTIAL 实时更新
            </span>
          </div>

          <div className="transcript-surface">
            <EmptyPlaceholder
              icon={<AudioOutlined />}
              title="等待语音输入……"
              description="发言开始后将在此实时显示。PARTIAL 临时结果与 FINAL 固化记录会分层呈现。"
              compact
            />
          </div>
        </section>

        {/* 会议信息 rail */}
        <aside className="room__rail">
          <div className="rail-section">
            <div className="rail-title">状态</div>
            <InfoRow label="会议状态" value={<MeetingStatusTag status={demoStatus} />} />
            <InfoRow label="开始时间" value="刚刚" />
            <InfoRow label="持续时长" value="00:00" />
          </div>

          <div className="rail-section">
            <div className="rail-title">AI 纪要</div>
            <InfoRow
              label="纪要状态"
              value={<Tag>未开始</Tag>}
            />
            <InfoRow
              label="纪要说明"
              value={<span style={{ fontSize: 12, color: '#98A2B3' }}>结束会议后自动生成</span>}
            />
          </div>

          <div className="rail-section">
            <div className="rail-title">会议信息</div>
            <InfoRow label="Meeting ID" value={<span className="room__id">{meetingId}</span>} />
            <InfoRow label="转写" value={<Tag color="blue">进行中</Tag>} />
            <InfoRow label="当前输入" value={<FileTextOutlined style={{ color: '#98A2B3' }} />} />
          </div>
        </aside>
      </div>
    </div>
  )
}
