import { createBrowserRouter } from 'react-router-dom'
import { AppShell } from '@/layouts/AppShell'
import { HomePage } from '@/pages/Home/HomePage'
import { MeetingsPage } from '@/pages/Meetings/MeetingsPage'
import { MeetingRoomPage } from '@/pages/MeetingRoom/MeetingRoomPage'
import { MeetingResultPage } from '@/pages/MeetingResult/MeetingResultPage'
import { SettingsPage } from '@/pages/Settings/SettingsPage'
import { NotFoundPage } from '@/pages/NotFound/NotFoundPage'

/**
 * 路由（fronted_designer §7）：
 *   /                            首页
 *   /meetings                    会议列表
 *   /meetings/:meetingId         会议进行中（Meeting Room）
 *   /meetings/:meetingId/result  会议结果
 *   /settings                    设置
 *   *                            NotFound fallback
 * 创建会议使用 Modal，不单独建路由。
 */
export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <HomePage /> },
      { path: 'meetings', element: <MeetingsPage /> },
      { path: 'meetings/:meetingId', element: <MeetingRoomPage /> },
      { path: 'meetings/:meetingId/result', element: <MeetingResultPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
