import { Card, Empty, Typography } from 'antd'

const { Title } = Typography

/**
 * 设置页骨架（fronted_designer §31 保留）。
 * 明确不做：DeepSeek API Key 输入框 —— Key 属于 Backend Secret，
 * 不应由浏览器前端收集 / 保存（PRD §45，Arch §10）。
 */
export function SettingsPage() {
  return (
    <div style={{ maxWidth: 960, margin: '0 auto' }}>
      <Title level={2} style={{ marginBottom: 4 }}>
        设置
      </Title>
      <Card style={{ borderRadius: 16 }}>
        <Empty description="设置项将在后续版本提供" />
      </Card>
    </div>
  )
}
