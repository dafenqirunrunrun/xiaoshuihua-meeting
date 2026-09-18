import { Typography } from 'antd'
import { SettingOutlined, LockOutlined } from '@ant-design/icons'
import { EmptyPlaceholder } from '@/components/EmptyPlaceholder'

const { Title } = Typography

/**
 * 设置页骨架（Visual Polish，fronted_designer §31 保留）。
 * 明确不做：API Key / 麦克风 等不真实能力（PRD §45 设备拒绝、arch §10）。
 */
export function SettingsPage() {
  return (
    <div className="page">
      <div className="section-header" style={{ marginBottom: 16 }}>
        <Title level={3} className="h-m0">
          设置
        </Title>
      </div>

      <div className="panel">
        <EmptyPlaceholder
          icon={<SettingOutlined />}
          title="设置项将在后续版本提供"
          description="模型与凭据由后端统一管理，不会在本机浏览器中暴露或保存。"
          action={
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                fontSize: 12,
                color: '#98A2B3',
              }}
            >
              <LockOutlined /> DeepSeek Key 由 Backend Env 管理
            </span>
          }
          compact
        />
      </div>
    </div>
  )
}
