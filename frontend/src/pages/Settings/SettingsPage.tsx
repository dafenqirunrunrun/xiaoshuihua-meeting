import { Typography } from 'antd'
import {
  SettingOutlined,
  DatabaseOutlined,
  InfoCircleOutlined,
  RightOutlined,
} from '@ant-design/icons'

const { Title } = Typography

const SETTINGS_ENTRIES = [
  {
    icon: <SettingOutlined />,
    name: '模型设置',
    desc: '提供程序与参数配置将在后续版本开放，由后端统一管理。',
  },
  {
    icon: <DatabaseOutlined />,
    name: '数据管理',
    desc: '会议记录与转写数据的导出、清理将在后续版本开放。',
  },
  {
    icon: <InfoCircleOutlined />,
    name: '关于我们',
    desc: '小水华是一款企业 AI 会议记录工具，专注实时转写与 AI 纪要。',
  },
]

/**
 * 设置页（fronted_designer §31 保留，8B-5.1 紧凑化）。
 * 三个分组条目（模型设置 / 数据管理 / 关于我们）+ 轻量 chevron，形如真实设置列表。
 * 明确不做：API Key / 麦克风 等不真实能力（PRD §45 设备拒绝、arch §10）。
 */
export function SettingsPage() {
  return (
    <div className="page">
      <div className="section-header" style={{ marginBottom: 20 }}>
        <div>
          <Title level={3} className="h-m0">
            设置
          </Title>
          <div className="section-subtitle">
            模型与凭据由后端统一管理，不会在本机浏览器中暴露或保存。
          </div>
        </div>
      </div>

      <div className="panel">
        {SETTINGS_ENTRIES.map((entry) => (
          <div className="settings-entry" key={entry.name}>
            <div className="settings-entry__icon">{entry.icon}</div>
            <div className="settings-entry__body">
              <div className="settings-entry__name">{entry.name}</div>
              <div className="settings-entry__desc">{entry.desc}</div>
            </div>
            <RightOutlined className="settings-entry__arrow" aria-hidden />
          </div>
        ))}
      </div>
    </div>
  )
}
