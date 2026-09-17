import { Card, Empty, Tabs, Typography } from 'antd'

const { Title, Text } = Typography

/**
 * Meeting Result —— Step 8A 仅 UI 骨架（fronted_designer §29-§35）。
 * 提供「会议内容 / 会议纪要」两个 Tab；内容先用 Empty 占位，不伪造数据。
 * 真实 Transcript / Summary 渲染在 Step 8C 接入。
 */
export function MeetingResultPage() {
  return (
    <div style={{ maxWidth: 960, margin: '0 auto' }}>
      <Title level={2} style={{ marginBottom: 4 }}>
        会议结果
      </Title>
      <Text type="secondary">2026-09-17 · 会议时长待计算</Text>

      <Card style={{ marginTop: 16, borderRadius: 16 }}>
        <Tabs
          defaultActiveKey="summary"
          items={[
            {
              key: 'summary',
              label: '会议纪要',
              children: <Empty description={<Text type="secondary">暂无可展示的会议纪要</Text>} />,
            },
            {
              key: 'transcript',
              label: '会议内容',
              children: <Empty description={<Text type="secondary">暂无可展示的会议内容</Text>} />,
            },
          ]}
        />
      </Card>
    </div>
  )
}
