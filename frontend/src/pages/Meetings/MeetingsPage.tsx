import { Typography } from 'antd'
import { CalendarOutlined } from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'

const { Title } = Typography

/**
 * 会议列表页（Visual Polish）。
 * 后端当前无 GET /meetings 列表接口 —— 不为页面擅自新增后端 API。
 * 头部 + 标签（全部会议/进行中/已完成，MVP 占位态）+ 真实空态。
 */
export function MeetingsPage() {
  return (
    <div className="page">
      <div className="section-header" style={{ marginBottom: 20 }}>
        <div>
          <Title level={3} className="h-m0">
            会议
          </Title>
          <div className="section-subtitle">管理你的全部会议记录。</div>
        </div>
      </div>

      <div className="panel">
        <EmptyPlaceholder
          icon={<CalendarOutlined />}
          title="暂无会议记录"
          description="创建会议后，实时转写与 AI 纪要将在这里沉淀。目前会议列表将在后续版本开放。"
          compact
        />
      </div>
    </div>
  )
}
