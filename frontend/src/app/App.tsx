import { RouterProvider } from 'react-router-dom'
import { ConfigProvider, App as AntdApp } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { router } from './router'
import { antdTheme } from '@/styles/designTokens'
import '@/styles/global.css'

export default function App() {
  return (
    <ConfigProvider theme={antdTheme} locale={zhCN}>
      <AntdApp>
        <RouterProvider router={router} />
      </AntdApp>
    </ConfigProvider>
  )
}
