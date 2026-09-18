import { useState } from 'react'
import { Button, Typography } from 'antd'
import { PlusOutlined, CalendarOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { CreateMeetingModal } from '@/components/CreateMeetingModal'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'
import { HeroIllustration } from '@/components/HeroIllustration'

const { Title, Paragraph } = Typography

/**
 * 首页（fronted_designer §8-§10，Visual Polish）。
 * Hero：左文右插画——「小水华，让每一次会议更清晰」+ 唯一 Primary CTA「创建会议」
 * + 次级「查看会议」。插画纯装饰（pointer-events:none），不阻塞文字。
 * 最近会议保持真实空态（无 fake 列表），CTA 复用创建弹窗。
 * 创建 ≠ 开始：点击只创建（status=CREATED），然后进入会前准备页。
 */
export function HomePage() {
  const [modalOpen, setModalOpen] = useState(false)
  const navigate = useNavigate()

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

      {/* 最近会议：真实空态（无 CTA —— 主 CTA 已在 Hero，避免同屏重复） */}
      <section>
        <div className="section-header">
          <div>
            <div className="section-title">最近的会议</div>
            <div className="section-subtitle">实时转写与 AI 纪要将在这里沉淀。</div>
          </div>
        </div>

        <div className="panel">
          <EmptyPlaceholder
            icon={<CalendarOutlined />}
            title="暂无会议记录"
            description="创建你的第一场会议，实时转写与 AI 纪要将在这里沉淀。"
            compact
          />
        </div>
      </section>

      <CreateMeetingModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  )
}
