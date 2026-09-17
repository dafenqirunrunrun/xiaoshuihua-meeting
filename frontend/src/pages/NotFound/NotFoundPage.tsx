import { Button, Result } from 'antd'
import { useNavigate } from 'react-router-dom'

export function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="你访问的页面不存在或已被移动。"
      extra={
        <Button type="primary" onClick={() => navigate('/')}>
          返回首页
        </Button>
      }
    />
  )
}
