import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Layout, Menu, Typography } from 'antd'
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
 * AppShell —— 全局侧栏骨架（fronted_designer §6）。
 * MVP 一级导航：首页 / 会议 / 设置。不做复杂视觉动画。
 */
export function AppShell() {
  const navigate = useNavigate()
  const location = useLocation()

  // 选中项：当前路径映射到一级导航（/meetings/:id 归入「会议」）
  const selectedKey =
    location.pathname === '/' ? '/' : location.pathname.startsWith('/meetings') ? '/meetings' : location.pathname

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider width={220} theme="light" style={{ borderRight: '1px solid #E4E7EC' }}>
        <div style={{ padding: '20px 20px 12px' }}>
          <Typography.Title level={4} style={{ margin: 0, color: '#1F2937' }}>
            小水华
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            会议助手
          </Typography.Text>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          onClick={({ key }) => navigate(key)}
          items={NAV_ITEMS}
          style={{ borderInlineEnd: 'none' }}
        />
      </Sider>
      <Layout>
        <Content style={{ padding: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
