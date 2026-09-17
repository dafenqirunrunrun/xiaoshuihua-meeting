import { useParams } from 'react-router-dom'
import { Card, Empty, Typography, Tag } from 'antd'

const { Title, Text } = Typography

/**
 * Meeting Room —— Step 8A 仅 UI 骨架（fronted_designer §14-§24）。
 * 包含：顶部 Header（会议名 + 状态 + 计时占位 + 结束按钮占位）、
 * Transcript 主区空态、右侧会议信息 / 控制空态。
 * SSE / Snapshot 恢复在 Step 8B 实现，本步骤不做实时数据。
 */
export function MeetingRoomPage() {
  const { meetingId } = useParams<{ meetingId: string }>()
  void meetingId // Step 8A 骨架页不依赖 route param；9B 用其拉取 Snapshot/SSE

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, height: '100%' }}>
      {/* Header 骨架 */}
      <Card style={{ borderRadius: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Title level={4} style={{ margin: 0 }}>
              会议进行中
            </Title>
            <Tag color="blue">会议进行中</Tag>
          </div>
          <Text type="secondary">--:--:--</Text>
        </div>
      </Card>

      {/* 主区 + 侧栏骨架 */}
      <div style={{ display: 'flex', gap: 16, flex: 1, minHeight: 0 }}>
        <Card title="Transcript" style={{ flex: 1, borderRadius: 16 }}>
          <Empty description={<Text type="secondary">等待语音输入……</Text>} />
        </Card>
        <Card title="会议信息" style={{ width: 280, borderRadius: 16 }}>
          <Empty description={<Text type="secondary">暂无内容</Text>} />
        </Card>
      </div>
    </div>
  )
}
