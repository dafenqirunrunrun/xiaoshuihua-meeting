import { Card, Empty, Typography } from 'antd'

const { Title, Text } = Typography

/**
 * 会议列表页（fronted_designer §13）。
 * 后端目前没有 GET /meetings 列表接口 —— 不为页面擅自新增后端 API。
 * 明确展示 MVP 占位，不伪造会议历史。
 */
export function MeetingsPage() {
  return (
    <div style={{ maxWidth: 960, margin: '0 auto' }}>
      <Title level={2} style={{ marginBottom: 4 }}>
        会议
      </Title>
      <Card style={{ borderRadius: 16 }}>
        <Empty
          description={
            <span>
              <Text type="secondary">会议列表将在后续版本提供</Text>
            </span>
          }
        />
      </Card>
    </div>
  )
}
