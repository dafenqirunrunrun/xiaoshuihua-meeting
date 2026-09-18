import { useState } from 'react'
import { Button, Typography } from 'antd'
import { PlusOutlined, CalendarOutlined } from '@ant-design/icons'
import { CreateMeetingModal } from '@/components/CreateMeetingModal'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'

const { Title, Paragraph } = Typography

/**
 * 首页（fronted_designer §8-§10，Visual Polish）。
 * Hero 文案层级 + 唯一 Primary CTA「开始新会议」+ 紧凑最近会议空态。
 * 保持产品感（会议工具），不做后台仪表盘。
 */
export function HomePage() {
  const [modalOpen, setModalOpen] = useState(false)

  return (
    <div className="page">
      {/* Hero：标题层级，CTA 唯一主按钮 */}
      <div className="home-hero">
        <div>
          <Title level={1} className="home-hero__title h-m0">
            让每一次会议都有回响
          </Title>
          <Paragraph
            type="secondary"
            className="home-hero__subtitle"
          >
            小水华为你实时记录发言，并在会议结束后自动生成 AI 纪要。
          </Paragraph>
        </div>
      </div>

      {/* 最近会议 */}
      <section>
        <div className="section-header">
          <span className="section-title">最近的会议</span>
        </div>

        <div className="panel">
          <EmptyPlaceholder
            icon={<CalendarOutlined />}
            title="还没有会议记录"
            description="开始你的第一场会议，实时转写与 AI 纪要将自动接续。"
            action={
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => setModalOpen(true)}
              >
                开始新会议
              </Button>
            }
            compact
          />
        </div>
      </section>

      <CreateMeetingModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  )
}
