import { Typography } from 'antd'
import { CalendarOutlined } from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'

const { Title } = Typography

/**
 * 会议列表页（Visual Polish）。
 * 后端当前无 GET /meetings 列表接口 —— 不为页面擅自新增后端 API。
 * 用轻量信息区块替代巨型 Empty Card，明确 MVP 占位。
 */
export function MeetingsPage() {
  return (
    <div className="page">
      <div className="section-header" style={{ marginBottom: 16 }}>
        <Title level={3} className="h-m0">
          会议
        </Title>
      </div>

      <div className="panel">
        <EmptyPlaceholder
          icon={<CalendarOutlined />}
          title="会议列表即将上线"
          description="后续版本会在这里列出你的全部会议记录，支持继续查看实时记录与 AI 纪要。"
          compact
        />
      </div>
    </div>
  )
}
