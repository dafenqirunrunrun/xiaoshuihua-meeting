import { useState } from 'react'
import { Button, Card, Typography, Empty } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { CreateMeetingModal } from '@/components/CreateMeetingModal'

const { Title, Paragraph, Text } = Typography

/**
 * 首页（fronted_designer §8-§10）。
 * 产品名 + 一句话说明 + 「开始新会议」+ 最近会议（MVP 先空态）。
 * 不做加入会议 —— 后端无多人能力，不做无法工作的入口。
 */
export function HomePage() {
  const [modalOpen, setModalOpen] = useState(false)

  return (
    <div style={{ maxWidth: 960, margin: '0 auto' }}>
      <div style={{ marginBottom: 32 }}>
        <Title level={2} style={{ marginBottom: 4 }}>
          早上好 👋
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 24, fontSize: 16 }}>
          让每一次会议都有回响
        </Paragraph>
        <Button type="primary" size="large" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          开始新会议
        </Button>
      </div>

      <Card title="最近的会议" style={{ borderRadius: 16 }}>
        <Empty
          description={
            <span>
              还没有会议记录
              <br />
              <Text type="secondary">开始你的第一场会议吧</Text>
            </span>
          }
        >
          <Button type="primary" onClick={() => setModalOpen(true)}>
            开始新会议
          </Button>
        </Empty>
      </Card>

      <CreateMeetingModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  )
}
