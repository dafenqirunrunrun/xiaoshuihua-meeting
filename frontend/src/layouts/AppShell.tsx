import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Layout, Menu } from 'antd'
import {
  HomeOutlined,
  CalendarOutlined,
  SettingOutlined,
} from '@ant-design/icons'

const { Sider, Content } = Layout

const NAV_ITEMS = [
  { key: '/', icon: <HomeOutlined />, label: '首页' },
  { key: '/meetings', icon: <CalendarOutlined />, label: '会议' },
  { key: '/settings', icon: <SettingOutlined />, label: '设置' },
]

/**
 * AppShell —— 全局侧栏 + 内容骨架（fronted_designer §6，Visual Polish）。
 * 安静白侧栏 + 细边框 + 波形 logo mark；选中项浅蓝胶囊；
 * 底部 tagline「让每一次会议，都有回响。」以浅薄荷装饰板收尾。
 */
export function AppShell() {
  const navigate = useNavigate()
  const location = useLocation()

  // 选中项：当前路径映射到一级导航（/meetings/:id 归入「会议」）
  const selectedKey =
    location.pathname === '/'
      ? '/'
      : location.pathname.startsWith('/meetings')
        ? '/meetings'
        : location.pathname

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        width={220}
        theme="light"
        style={{ borderRight: '1px solid #EEF3F9' }}
      >
        <div className="app-brand">
          <svg
            className="app-brand__mark"
            width="26"
            height="26"
            viewBox="0 0 26 26"
            aria-hidden
          >
            <rect width="26" height="26" rx="7" fill="#3478F6" />
            <path
              d="M6 16.5c1.6-5 3.2-7.5 4.7-7.5s2 3.4 3.4 3.4 2.6-8.4 4.2-8.4c1.3 0 1.3 8 1.9 12.5"
              stroke="#fff"
              strokeWidth="1.8"
              strokeLinecap="round"
              fill="none"
            />
          </svg>
          <span>
            <span className="app-brand__name">小水华</span>
            <span className="app-brand__subtitle">会议助手</span>
          </span>
        </div>

        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          onClick={({ key }) => navigate(key)}
          items={NAV_ITEMS}
          style={{ borderInlineEnd: 'none' }}
        />

        <div className="app-side-footer">
          <span className="app-side-footer__tag">小水华</span>
          让每一次会议，都有回响。
        </div>
      </Sider>

      <Layout>
        <Content>
          <div className="app-content">
            <Outlet />
          </div>
        </Content>
      </Layout>
    </Layout>
  )
}
