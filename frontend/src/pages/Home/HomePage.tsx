import { useState } from 'react'
import { Button, Skeleton, Typography } from 'antd'
import { PlusOutlined, CalendarOutlined, ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { CreateMeetingModal } from '@/components/CreateMeetingModal'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { HeroIllustration } from '@/components/HeroIllustration'
import {
  ActiveMeetingCard,
  MeetingListItem,
} from '@/components/MeetingListItem'
import { useMeetingList } from '@/hooks/useMeetingList'

const { Title, Paragraph } = Typography

/** 首页一次拉取上限（Active/Recent 分区，不一次返回无限历史） */
const HOME_LIST_LIMIT = 50

/**
 * 首页（fronted_designer §8-§10，Visual Polish + Step 8B-6 Meeting Discovery）。
 * Hero：左文右插画——「小水华，让每一次会议更清晰」+ 唯一 Primary CTA「创建会议」
 * + 次级「查看会议」。
 * 下方按真实 Meeting List 分区：
 *   - 正在进行（Active）：IN_PROGRESS / FINALIZING —— 用户离开 Meeting Room 后
 *     仍能重新发现并 Resume；最多展示 3 张，附「查看全部会议」。
 *   - 最近会议（Recent）：CREATED（待开始）/ COMPLETED（已完成）—— 真实空态保留。
 * 创建 ≠ 开始：点击只创建（status=CREATED），然后进入会前准备页。
 * 不新增 fake 会议 / fake 时长：所有时间文本来自后端真实字段。
 */
export function HomePage() {
  const [modalOpen, setModalOpen] = useState(false)
  const navigate = useNavigate()
  const { meetings, loading, error, reload } = useMeetingList({
    limit: HOME_LIST_LIMIT,
    offset: 0,
  })

  // 前端分组（不伪造“唯一当前会议”规则；多个一并展示，最多 3 个 active）
  const active = meetings.filter(
    (m) => m.status === 'IN_PROGRESS' || m.status === 'FINALIZING',
  )
  const recent = meetings.filter(
    (m) => m.status === 'CREATED' || m.status === 'COMPLETED',
  )
  const hasAny = meetings.length > 0

  const listErrorBlock = error ? (
    <div className="panel" style={{ padding: 24 }}>
      <EmptyPlaceholder
        icon={<ReloadOutlined />}
        title="会议列表加载失败"
        description={error}
        action={
          <Button size="small" icon={<ReloadOutlined />} onClick={reload}>
            重试
          </Button>
        }
        compact
      />
    </div>
  ) : null

  return (
    <div className="page">
      {/* Hero：左文右画，标题层级 + 唯一主 CTA */}
      <div className="home-hero">
        <div className="home-hero__copy">
          <Title level={1} className="home-hero__title h-m0">
            小水华，让每一次会议更清晰
          </Title>
          <Paragraph type="secondary" className="home-hero__subtitle">
            实时转写每句话，会后自动生成 AI 纪要——专注会议本身，剩下的交给小水华。
          </Paragraph>
          <div className="home-hero__actions">
            <Button
              type="primary"
              size="large"
              icon={<PlusOutlined />}
              onClick={() => setModalOpen(true)}
            >
              创建会议
            </Button>
            <Button size="large" icon={<CalendarOutlined />} onClick={() => navigate('/meetings')}>
              查看会议
            </Button>
          </div>
        </div>
        <div className="home-hero__visual">
          <HeroIllustration />
        </div>
      </div>

      {/* Loading：轻量 Skeleton */}
      {loading ? (
        <div className="panel" style={{ padding: 24 }}>
          <Skeleton active paragraph={{ rows: 3 }} title={false} />
        </div>
      ) : null}

      {/* Error：独立重试，不拖垮整页 */}
      {error ? listErrorBlock : null}

      {/* 正在进行：离开 Meeting Room 后仍能重新发现 / Resume */}
      {!loading && !error && active.length > 0 ? (
        <section style={{ marginBottom: 32 }}>
          <div className="section-header">
            <div>
              <div className="section-title">正在进行</div>
              <div className="section-subtitle">
                这些会议仍在进行中，回到会议即可继续记录。
              </div>
            </div>
            <Button type="link" size="small" onClick={() => navigate('/meetings')}>
              查看全部会议
            </Button>
          </div>
          <div className="panel" style={{ padding: '8px 20px' }}>
            {active.slice(0, 3).map((m) => (
              <ActiveMeetingCard key={m.id} meeting={m} />
            ))}
          </div>
        </section>
      ) : null}

      {/* 最近会议：CREATED（继续准备）+ COMPLETED（查看结果） */}
      <section>
        <div className="section-header">
          <div>
            <div className="section-title">最近的会议</div>
            <div className="section-subtitle">实时转写与 AI 纪要将在这里沉淀。</div>
          </div>
        </div>

        {!loading && !error && !hasAny ? (
          <div className="panel">
            <EmptyPlaceholder
              icon={<CalendarOutlined />}
              title="暂无会议记录"
              description="创建你的第一场会议，实时转写与 AI 纪要将在这里沉淀。"
              compact
            />
          </div>
        ) : null}

        {!loading && !error && hasAny && recent.length === 0 ? (
          <div className="panel">
            <EmptyPlaceholder
              icon={<CalendarOutlined />}
              title="暂无待开始或已完成的会议"
              description="进行中的会议已在「正在进行」区域展示。"
              compact
            />
          </div>
        ) : null}

        {!loading && !error && recent.length > 0 ? (
          <div className="panel" style={{ padding: '8px 20px' }}>
            {recent.slice(0, 5).map((m) => (
              <MeetingListItem key={m.id} meeting={m} />
            ))}
          </div>
        ) : null}
      </section>

      <CreateMeetingModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  )
}
