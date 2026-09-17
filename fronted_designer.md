# 小水华会议助手 MVP V0.1 前端设计方案（React）

> 文档类型：Frontend Design Spec / UI UX / 工程实现约束  
> 版本：MVP V0.1  
> 前端技术栈：React + TypeScript + Vite + Zustand + Axios + Ant Design + SSE  
> 设计依据：`小水华会议助手 MVP V0.1 PRD` + 当前产品原型图  
> 核心目标：优先跑通“实时转写展示 + 会议结束后自动生成会议纪要”两条主链路  
> 实现对象：前端开发、产品、后端、Claude Code / Codex  
> 设计原则：先完成真实闭环，不为了“AI 感”堆叠非 MVP 功能

---

# 1. MVP 前端目标

小水华会议助手 MVP 第一阶段只验证两个核心产品价值：

```text
价值 1：
会议进行过程中，
用户能稳定、连续、低延迟地看到实时 Transcript。

价值 2：
会议结束后，
用户能看到完整 Transcript，
并获得一份 AI 自动生成的会议纪要。
```

完整前端主链路：

```text
首页 / 会议列表
↓
创建会议
↓
开始会议
↓
会议进行中
↓
实时 Transcript
↓
结束会议
↓
FINALIZING
↓
会议完成
↓
AI 会议纪要 + 完整 Transcript
```

MVP 第一版不引入：

```text
Meeting Agent
Discussion Node
RAG
知识库
Multi-Agent
Todo
Decision
跨会议问答
自动议题识别
自动章节
声纹身份映射
复杂消息中心
复杂权限中心
```

---

# 2. 产品定位

产品定位：

> **企业内部的 AI 会议记录工具。**

它首先应该让用户感觉：

```text
“这是一个稳定的会议工具”
```

其次才是：

```text
“这是一个 AI 产品”
```

因此 MVP 的设计优先级：

```text
可用性
>
状态清晰
>
信息可读性
>
交互效率
>
视觉装饰
```

---

# 3. 原型图评审结论

当前原型的总体方向正确：

```text
左侧导航
+
大面积主内容区
+
轻量企业级视觉
+
蓝色品牌色
+
会议进行页左右分栏
+
结果页 Transcript / 会议纪要切换
```

建议保留。

但 MVP 需要进一步收敛。

---

## 3.1 保留

```text
首页
我的会议
设置

开始新会议
最近会议

Meeting Room
实时 Transcript
会议信息
结束会议

Finalizing
处理进度

Meeting Result
会议内容
AI 会议纪要
```

---

## 3.2 删除 / 暂缓

原型中若出现以下能力，MVP 暂不实现：

```text
复杂通知中心
知识库
AI 对话
联网智搜
思维导图
AI PPT
工作台
复杂参与人管理
复杂麦克风设置
高级导出中心
```

理由：

> 它们会让“小水华”从会议产品变成通用 AI 办公平台，导致第一版产品边界失焦。

---

# 4. 前端技术栈

MVP 固定：

```text
React
TypeScript
Vite
Zustand
Axios
Ant Design
SSE
```

建议版本：

```text
React 19+
TypeScript 5+
Vite 7+
Ant Design 5+
Zustand 5+
```

不强制锁死小版本，以项目实际可安装版本为准。

---

# 5. 技术选型职责

## React

负责：

```text
页面
组件
视图状态
交互
```

---

## TypeScript

负责：

```text
Meeting
Transcript
Summary
SSE Event
API Response
```

的类型约束。

---

## Zustand

只保存真正的跨组件业务状态：

```text
meeting
finalSegments
partialUtterances
summary
streamStatus
```

不要把所有 Modal、Hover、Loading 都塞进全局 Store。

---

## Axios

负责普通 HTTP：

```text
创建会议
开始会议
结束会议
查询 Meeting
查询 Transcript
查询 Summary
重新生成 Summary
```

---

## SSE

负责服务端实时推送：

```text
transcript.partial
transcript.final
meeting.status
summary.started
summary.completed
summary.failed
heartbeat
```

---

## Ant Design

优先使用：

```text
Button
Modal
Input
Card
Tag
Tabs
Spin
Progress
Result
Alert
Avatar
Tooltip
Dropdown
Skeleton
Empty
```

避免重复造基础 UI 组件。

---

# 6. 信息架构

MVP 一级导航只保留：

```text
首页
我的会议
设置
```

左侧导航：

```text
┌─────────────────┐
│ 小水华           │
│ 会议助手         │
├─────────────────┤
│ 首页             │
│ 我的会议         │
│ 设置             │
│                 │
│                 │
├─────────────────┤
│ 用户头像 张三    │
└─────────────────┘
```

---

# 7. 路由设计

推荐：

```text
/
首页

/meetings
我的会议

/meetings/:meetingId/live
会议进行中

/meetings/:meetingId/result
会议结果

/settings
设置
```

创建会议使用 Modal，不单独创建路由。

---

# 8. 页面一：首页 / 会议列表

## 8.1 页面目标

首页只做三件事：

```text
开始新会议
加入会议（如业务需要）
查看最近会议
```

---

## 8.2 推荐布局

```text
┌───────────────┬──────────────────────────────────┐
│               │ 早上好，张三 👋                 │
│ 小水华        │ 让每一次会议都有回响             │
│ 会议助手      │                                  │
│               │ [ 开始新会议 ] [ 加入会议 ]      │
│ 首页          │                                  │
│ 我的会议      │ 最近的会议                       │
│ 设置          │                                  │
│               │ ┌──────────────────────────────┐ │
│               │ │ 小水华 MVP 方案讨论          │ │
│               │ │ 今天 10:00 - 11:30           │ │
│               │ │ 已完成                       │ │
│               │ └──────────────────────────────┘ │
│               │                                  │
│ 张三          │ ┌──────────────────────────────┐ │
└───────────────┴──────────────────────────────────┘
```

---

# 9. 首页组件设计

```text
<AppShell>
  <Sidebar />
  <PageContent>
    <HomeHeader />
    <QuickActions />
    <RecentMeetingList />
  </PageContent>
</AppShell>
```

---

## 9.1 Quick Action

开始新会议：

```text
Primary Card
图标：Camera / Audio Wave
标题：开始新会议
说明：立即开始实时转写
```

加入会议：

```text
Secondary Card
标题：加入会议
说明：输入会议 ID 加入
```

如果 MVP 暂时不做多人加入：

> 直接删除“加入会议”，不要保留一个无法工作的入口。

---

# 10. 最近会议卡片

展示字段：

```text
会议名称
开始时间
结束时间 / 时长
状态
```

状态映射：

| 后端状态 | 前端文案 | UI |
|---|---|---|
| CREATED | 待开始 | 默认 |
| IN_PROGRESS | 进行中 | 蓝色 / 红点 |
| FINALIZING | 处理中 | Loading |
| COMPLETED | 已完成 | 绿色 |
| FINALIZING_FAILED | 纪要生成失败 | 警告色 |

不要展示：

```text
模型名称
Transcript 摘要
Token
内部 ID
```

---

# 11. 创建会议 Modal

## 11.1 MVP 字段

只需要：

```text
会议标题 *
会议描述（可选）
```

推荐：

```text
┌────────────────────────────┐
│ 创建新会议              × │
│                            │
│ 会议标题 *                 │
│ [小水华 MVP 方案讨论____]  │
│                            │
│ 会议描述                   │
│ [_______________________]  │
│ [_______________________]  │
│                            │
│       [取消] [创建并开始]   │
└────────────────────────────┘
```

---

## 11.2 GrillMe：不要假装控制不存在的音频设备

如果真实音频采集 / ASR 输入由其他同学负责，而浏览器本身并不采集声音，则不要在前端设计：

```text
麦克风选择
输入设备
音量测试
浏览器录音权限
```

否则用户会误以为：

```text
网页麦克风 = ASR 实际输入
```

这属于产品语义错误。

只有实际接入浏览器采集时，才增加音频设备设置。

---

# 12. 创建会议交互

点击：

```text
创建并开始
```

流程：

```text
POST /api/meetings
↓
POST /api/meetings/{id}/start
↓
navigate(/meetings/{id}/live)
```

按钮状态：

```text
默认
↓
loading
↓
成功进入 Meeting Room
```

失败：

```text
Modal 保留
+
显示明确错误
+
允许重试
```

---

# 13. 页面二：Meeting Room

Route：

```text
/meetings/:meetingId/live
```

这是 MVP 第一核心页面。

---

# 14. Meeting Room 总体布局

推荐：

```text
Top Header
+
Transcript Main
+
Meeting Side Panel
```

Desktop：

```text
┌──────────────────────────────────────────────────────────────┐
│ 小水华 | 小水华 MVP 方案讨论 | ● 会议进行中 | 00:24:18     │
│                                             [结束会议]       │
├────────────────────────────────────────┬─────────────────────┤
│                                        │                     │
│ Transcript                             │ 会议信息            │
│                                        │                     │
│ 张三                     10:00:12      │ 小水华 MVP 方案讨论 │
│ 我们今天主要讨论……                    │ 10:00 开始          │
│                                        │ 00:24:18            │
│ 李四                     10:00:28      │                     │
│ 第一版首先实现实时……                  ├─────────────────────┤
│                                        │ 会议控制            │
│ 王五                     10:00:45      │                     │
│ Agent 等留到后续版本。                 │ [结束会议]          │
│                                        │                     │
│ 李四 · 正在说话                       │                     │
│ ▂ ▃ ▅ ▆ ▃ 正在识别……                  │                     │
└────────────────────────────────────────┴─────────────────────┘
```

---

# 15. Meeting Header

Header 固定。

展示：

```text
Logo / 产品名
会议名称
状态
会议计时
结束会议
```

状态：

```text
● 会议进行中
```

不要在 MVP 顶部增加：

```text
通知
收藏
摄像头
分享
复杂更多菜单
```

除非真实业务已经支持。

---

# 16. Transcript 主区域

主区域占可用宽度：

```text
Desktop：约 72%
右侧信息栏：约 28%
```

最小主区域宽度建议：

```text
720px
```

---

# 17. Transcript Item

推荐结构：

```tsx
<TranscriptItem
  speaker="Speaker 1"
  time="10:00:12"
  text="我们今天主要讨论……"
  status="FINAL"
/>
```

视觉：

```text
Avatar
Speaker
Timestamp
Text
```

---

## 17.1 FINAL

FINAL：

```text
正常白底
固定文本
正常不透明度
```

建议不要每句话都使用厚重 Card。

更合适：

```text
轻列表 / 轻分组
```

从而避免长会议页面出现“卡片墙”。

---

## 17.2 PARTIAL

PARTIAL：

```text
浅蓝背景
“正在识别”
Wave / Loading Indicator
文本持续变化
```

例如：

```text
Speaker 2 · 正在说话
▂ ▃ ▅ ▆ ▃

我觉得这个版本先把实时字幕……
```

---

# 18. PARTIAL 更新规则

前端必须根据：

```text
utteranceId
```

更新。

例如收到：

```text
utt_101: 我们
utt_101: 我们第一版
utt_101: 我们第一版先做实时字幕
```

前端必须：

```text
同一个 Item 更新 text
```

禁止：

```text
追加 3 条 Transcript
```

Store：

```ts
partialUtterances[utteranceId] = latestPartial
```

---

# 19. FINAL 到达规则

收到：

```text
transcript.final
```

前端：

```text
1. 删除对应 partialUtterances[utteranceId]
2. finalSegments append / upsert
3. 按 segmentSeq 排序
4. 渲染正式 Transcript
```

---

# 20. Transcript 自动滚动

默认：

```text
用户位于底部
→ 自动跟随最新 Transcript
```

当：

```text
用户主动向上滚动
```

则：

```text
停止自动滚动
```

显示：

```text
↓ 回到最新
```

点击：

```text
scrollToBottom()
恢复 autoFollow
```

---

## GrillMe：禁止强制抢滚动

错误体验：

```text
用户正在查看 10 分钟前内容
↓
新 PARTIAL 到来
↓
页面强制跳回底部
```

这会严重影响会议中的实际使用。

---

# 21. Transcript 性能

PRD 目标：

```text
至少约 1000 Segment 不明显卡顿
```

MVP 第一版：

```text
普通列表即可
```

但组件边界必须允许后续换成：

```text
react-virtual / react-window
```

不要在每个 Transcript Item 内引入复杂全局状态订阅。

---

# 22. Meeting Side Panel

MVP 右侧只保留两个区域。

---

## 22.1 会议信息

展示：

```text
会议名称
开始时间
会议时长
会议状态
```

如果业务已有参会人：

```text
参会人数
Avatar Group
```

否则暂不显示虚假的参会人信息。

---

## 22.2 会议控制

只保留：

```text
结束会议
```

如果后端支持暂停 ASR：

```text
暂停转写
```

否则不要放一个“暂停”按钮。

---

# 23. 结束会议确认

点击：

```text
结束会议
```

必须二次确认：

```text
确认结束本次会议？

结束后将停止接收新的会议内容，
并进入会议纪要生成流程。

[取消] [确认结束]
```

避免误触。

---

# 24. Finalizing 状态

用户确认结束以后：

```text
IN_PROGRESS
↓
FINALIZING
```

页面不得立即跳转“已完成”。

---

# 25. Finalizing 设计

可以直接在 Meeting Room 中覆盖主内容区域。

推荐：

```text
┌────────────────────────────────────┐
│                                    │
│             ◌                      │
│        正在结束会议                │
│                                    │
│  正在等待最后一批语音识别结果……   │
│                                    │
│  ✓ 停止接收新的会议输入            │
│  ● 等待最后的识别结果              │
│  ○ 确认 Transcript 完整性          │
│  ○ 生成会议纪要                    │
│  ○ 保存会议结果                    │
│                                    │
│  通常需要数秒，请稍候              │
│                                    │
└────────────────────────────────────┘
```

---

# 26. Finalizing 阶段映射

前端不需要自己推理流程。

后端通过 SSE 发送状态。

建议前端状态：

```ts
type FinalizingStage =
  | 'WAITING_ASR'
  | 'VERIFYING_TRANSCRIPT'
  | 'GENERATING_SUMMARY'
  | 'SAVING'
  | 'DONE'
```

如果后端目前只有：

```text
FINALIZING
```

也可以 MVP 先显示统一 Loading。

不要为了前端进度条伪造百分比。

---

# 27. GrillMe：不要显示假的进度

禁止：

```text
17%
48%
83%
```

除非后端真的有可计算进度。

推荐：

```text
阶段式进度
```

因为：

```text
LLM 生成耗时不可线性估算
```

---

# 28. Finalizing 失败

如果：

```text
Meeting = FINALIZING_FAILED
```

显示：

```text
会议记录已保存

AI 会议纪要生成失败，
完整 Transcript 不受影响。

[重新生成会议纪要]
[查看会议记录]
```

这是重要的信任设计。

不要显示：

```text
会议失败
```

因为会议事实已经保存。

---

# 29. 页面三：Meeting Result

Route：

```text
/meetings/:meetingId/result
```

---

# 30. Result 页面顶部

展示：

```text
会议名称
日期
开始时间
结束时间
时长
状态：已完成
```

示例：

```text
小水华 MVP 方案讨论
2026-09-16 10:00 - 11:30 · 1小时30分钟
                                      已完成
```

---

# 31. Result 页面 Tabs

MVP 使用两个 Tab：

```text
会议内容
会议纪要
```

默认建议：

```text
会议纪要
```

理由：

> 用户会议结束后最关心的是“总结结果”，而不是重新从头读 Transcript。

但如果从 Meeting Room 自动跳转：

```text
优先打开会议纪要
```

---

# 32. 会议纪要 Tab

建议布局：

```text
┌─────────────────────────────────────────────┐
│ AI 会议纪要                                │
│ DeepSeek V4.1 Flash 生成 · 11:30            │
│                                             │
│ 一、会议概述                                │
│ 本次会议主要讨论……                         │
│                                             │
│ 二、核心议题                                │
│ 1. 实时字幕                                │
│ 2. 会议纪要                                │
│                                             │
│ 三、主要结论                                │
│ 1. MVP 优先……                              │
│                                             │
│ 四、未决问题                                │
│ 1. ……                                      │
└─────────────────────────────────────────────┘
```

---

# 33. Summary 数据映射

后端：

```json
{
  "overview": "...",
  "key_topics": [],
  "conclusions": [],
  "open_questions": []
}
```

前端对应：

```text
会议概述
核心议题
主要结论
未决问题
```

禁止前端自己 NLP 重组。

---

# 34. 空数组处理

如果：

```json
"open_questions": []
```

前端建议：

```text
暂无明确未决问题
```

不要整个页面出现空白标题。

---

# 35. 会议内容 Tab

展示：

```text
完整 FINAL Transcript
```

规则：

```text
按 segmentSeq 排序
只展示 FINAL
不展示 PARTIAL
```

支持：

```text
滚动
复制
```

MVP 不要求：

```text
全文搜索
Speaker 筛选
时间跳转
音频回放
```

---

# 36. 页面状态设计

前端必须由：

```text
meeting.status
```

驱动，而不是一堆布尔状态。

---

## 36.1 状态表

| Meeting Status | 页面表现 |
|---|---|
| CREATED | 显示开始会议 |
| IN_PROGRESS | 实时 Transcript + 结束会议 |
| FINALIZING | 处理中 |
| COMPLETED | Result 页面 |
| FINALIZING_FAILED | Transcript 已保存 + 重新生成 |

---

# 37. Zustand Store

建议：

```ts
interface MeetingStore {
  meeting: Meeting | null

  finalSegments: TranscriptSegment[]

  partialUtterances: Record<
    string,
    PartialUtterance
  >

  summary: MeetingSummary | null

  streamStatus:
    | 'idle'
    | 'connecting'
    | 'connected'
    | 'reconnecting'
    | 'error'

  autoFollow: boolean

  setMeeting: (meeting: Meeting) => void

  upsertPartial: (
    item: PartialUtterance
  ) => void

  commitFinal: (
    item: TranscriptSegment
  ) => void

  setSummary: (
    summary: MeetingSummary
  ) => void
}
```

---

# 38. 类型设计

```ts
export type MeetingStatus =
  | 'CREATED'
  | 'IN_PROGRESS'
  | 'FINALIZING'
  | 'COMPLETED'
  | 'FINALIZING_FAILED'

export interface Meeting {
  id: string
  title: string
  status: MeetingStatus
  startedAt?: string
  endedAt?: string
  completedAt?: string
}

export interface TranscriptSegment {
  segmentSeq: number
  utteranceId: string
  speakerId: string
  text: string
  startMs: number
  endMs: number
}

export interface PartialUtterance {
  utteranceId: string
  speakerId: string
  text: string
}

export interface MeetingSummary {
  overview: string
  keyTopics: string[]
  conclusions: string[]
  openQuestions: string[]
}
```

---

# 39. SSE Hook

建议封装：

```text
useMeetingStream(meetingId)
```

职责：

```text
连接 SSE
解析事件
更新 Store
处理断线
处理重连
清理连接
```

不要在 `MeetingRoom.tsx` 内直接写几十行 SSE 逻辑。

---

# 40. SSE Event 映射

```text
transcript.partial
→ upsertPartial()

transcript.final
→ commitFinal()

meeting.status
→ setMeetingStatus()

summary.started
→ summary loading

summary.completed
→ setSummary()

summary.failed
→ show error
```

---

# 41. 页面初始化

Meeting Room 打开：

```text
GET Meeting
+
GET Transcript Snapshot
+
Connect SSE
```

不要只依赖 SSE。

原因：

```text
页面刷新
网络断线
浏览器重新打开
```

都必须能恢复正式 Transcript。

---

# 42. SSE 重连 UX

连接状态：

```text
CONNECTED
RECONNECTING
ERROR
```

正常情况不要一直展示。

断线后显示轻量提示：

```text
正在重新连接实时转写……
```

恢复：

```text
已恢复连接
```

短暂 Toast 后消失。

---

# 43. GrillMe：实时状态不是错误弹窗

SSE 短暂断线：

不要：

```text
Modal：
系统错误！
```

应该：

```text
Inline Alert / Message
```

因为长连接短暂重连是正常网络事件。

---

# 44. API Client

建议：

```text
src/api/http.ts
src/api/meeting.ts
```

`http.ts`：

```text
baseURL
timeout
interceptors
统一错误转换
```

`meeting.ts`：

```text
createMeeting()
startMeeting()
finishMeeting()
getMeeting()
getTranscript()
getSummary()
regenerateSummary()
```

---

# 45. 推荐前端目录

```text
frontend/
├── src/
│   ├── app/
│   │   ├── App.tsx
│   │   └── router.tsx
│   │
│   ├── layouts/
│   │   └── AppLayout.tsx
│   │
│   ├── pages/
│   │   ├── HomePage/
│   │   ├── MeetingsPage/
│   │   ├── MeetingRoomPage/
│   │   ├── MeetingResultPage/
│   │   └── SettingsPage/
│   │
│   ├── components/
│   │   ├── Sidebar/
│   │   ├── CreateMeetingModal/
│   │   ├── MeetingHeader/
│   │   ├── MeetingInfoPanel/
│   │   ├── TranscriptList/
│   │   ├── TranscriptItem/
│   │   ├── PartialTranscript/
│   │   ├── FinalizingPanel/
│   │   └── SummaryPanel/
│   │
│   ├── hooks/
│   │   ├── useMeetingStream.ts
│   │   └── useAutoScroll.ts
│   │
│   ├── stores/
│   │   └── meetingStore.ts
│   │
│   ├── api/
│   │   ├── http.ts
│   │   └── meeting.ts
│   │
│   ├── types/
│   │   ├── meeting.ts
│   │   └── stream.ts
│   │
│   ├── utils/
│   │   ├── time.ts
│   │   └── transcript.ts
│   │
│   └── styles/
│       ├── tokens.css
│       └── global.css
│
└── package.json
```

---

# 46. 组件职责边界

## `MeetingRoomPage`

只负责：

```text
组织页面
读取 route param
初始化页面数据
```

不要把：

```text
SSE
Transcript merge
auto scroll
Summary
```

全写在 Page 内。

---

## `TranscriptList`

负责：

```text
FINAL list
PARTIAL display
scroll container
回到最新
```

---

## `TranscriptItem`

负责：

```text
Avatar
Speaker
Timestamp
Text
```

不直接调用 API。

---

## `FinalizingPanel`

只接受：

```text
status
stage
error
```

不自己调用 LLM API。

---

# 47. Design Token

建议建立统一 Token，而不是组件里写随机值。

```css
:root {
  --color-primary: #2F6BFF;
  --color-primary-hover: #4C7DFF;
  --color-primary-light: #EEF4FF;

  --color-success: #22B573;
  --color-warning: #F5A623;
  --color-danger: #F04438;

  --text-primary: #1F2937;
  --text-secondary: #667085;
  --text-tertiary: #98A2B3;

  --bg-page: #F7F9FC;
  --bg-card: #FFFFFF;

  --border-color: #E4E7EC;

  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
}
```

---

# 48. Typography

建议：

```text
中文：
PingFang SC
Microsoft YaHei
system-ui

英文 / 数字：
Inter
system-ui
```

不要额外引入复杂商业字体。

---

## 48.1 字号

```text
页面标题：24px / 32px
区块标题：18px / 26px
正文：14px / 22px
辅助信息：12px / 20px
会议计时：14px Medium
```

---

# 49. 间距体系

统一使用：

```text
4
8
12
16
24
32
40
48
```

避免：

```text
13px
19px
27px
```

这类无规则间距。

---

# 50. 页面宽度

桌面优先：

```text
min-width: 1180px
```

推荐核心适配：

```text
1366 × 768
1440 × 900
1920 × 1080
```

MVP 暂不以移动端作为核心验收对象。

---

# 51. 响应式

## >= 1280px

```text
Sidebar
+
Transcript
+
Right Panel
```

---

## 960px ~ 1279px

右侧 Panel 可以收窄。

---

## < 960px

MVP 可以：

```text
Right Panel 折叠
```

不需要为手机重新设计完整会议交互。

---

# 52. Loading 状态

必须有：

```text
页面初始化 Skeleton
创建会议 Button Loading
结束会议 Loading
Finalizing
Summary Loading
SSE reconnect
```

禁止：

```text
点击按钮后没有反馈
```

---

# 53. Empty State

最近会议为空：

```text
还没有会议记录

开始你的第一场会议吧

[开始新会议]
```

Transcript 暂无内容：

```text
会议已开始

等待语音输入……
```

---

# 54. 错误状态

建议统一：

```text
业务错误
→ Inline / Message

关键失败
→ Result / Alert
```

例如：

```text
创建会议失败
→ Message

SSE 断线
→ Inline Alert

Summary 失败
→ Result Panel
```

---

# 55. Accessibility

MVP 也需要基础可访问性：

```text
Button 使用真实 button
Modal 有 focus trap
Icon Button 有 aria-label
状态不能只靠颜色表示
正文颜色满足基本对比度
```

---

# 56. 关键交互动画

只保留：

```text
Modal 进入
Button Loading
PARTIAL fade
Finalizing Spinner
Tab 切换
```

时长：

```text
150ms ~ 220ms
```

不要加入：

```text
复杂粒子
AI 光晕
大面积动态渐变
```

---

# 57. GrillMe 产品评审

以下是 MVP 开发前必须回答清楚的问题。

---

## 57.1 “开始会议”究竟控制什么？

必须明确：

```text
前端开始按钮
↓
后端 Meeting = IN_PROGRESS
↓
ASR 是否同步开始？
```

不能出现：

```text
前端显示“会议进行中”
但 ASR 实际尚未启动
```

---

## 57.2 “结束会议”何时算完成？

不是：

```text
用户点击结束
```

而是：

```text
用户点击结束
↓
FINALIZING
↓
最后 FINAL
↓
STREAM_END
↓
Transcript Freeze
↓
Summary
↓
COMPLETED
```

因此前端必须存在 FINALIZING UI。

---

## 57.3 最后一句话会不会丢？

前端不能：

```text
点击结束
↓
立即跳 Result
```

必须等待后端状态。

---

## 57.4 PARTIAL 是否需要保留历史？

不需要。

```text
PARTIAL = UI 临时状态
FINAL = 正式事实
```

刷新后：

```text
只恢复 FINAL
```

是合理行为。

---

## 57.5 Speaker 识别错误怎么办？

MVP：

```text
显示 ASR 返回的 Speaker ID
```

不做人工编辑，不做真实姓名强绑定。

---

## 57.6 Summary 错了怎么办？

MVP 至少支持：

```text
重新生成
```

但：

```text
重新生成 Summary
≠
修改 Transcript
```

---

## 57.7 用户刷新 Meeting Room 怎么办？

必须：

```text
GET Transcript Snapshot
+
Reconnect SSE
```

恢复。

---

## 57.8 用户同时打开两个标签页怎么办？

MVP 可以允许只读同步。

“结束会议”接口必须由后端保证幂等。

前端不要假设自己是唯一客户端。

---

## 57.9 长 Transcript 会不会卡？

第一版优先完成闭环。

但：

```text
TranscriptList
```

必须独立封装，未来可替换为虚拟列表。

---

## 57.10 UI 能否脱离真实 ASR 开发？

必须可以。

前端开发阶段通过：

```text
Mock SSE
```

模拟：

```text
PARTIAL
FINAL
STATUS
SUMMARY
```

不依赖语音同学每天联调。

---

# 58. Mock 模式

建议：

```text
VITE_USE_MOCK_STREAM=true
```

开发环境可以使用：

```text
mockMeetingStream.ts
```

按时间模拟：

```text
PARTIAL
PARTIAL
FINAL
PARTIAL
FINAL
```

用于前端独立开发。

---

# 59. MVP 前端验收标准

## Case 1：创建会议

```text
点击开始新会议
↓
填写标题
↓
创建并开始
↓
进入 Meeting Room
```

---

## Case 2：PARTIAL 原地更新

输入：

```text
我们
我们今天
我们今天讨论 MVP
```

页面：

```text
始终只有一个 PARTIAL Item
```

---

## Case 3：FINAL 固化

收到 FINAL：

```text
PARTIAL 消失
FINAL 固定
下一句独立出现
```

---

## Case 4：自动滚动

用户在底部：

```text
自动跟随
```

用户向上浏览：

```text
暂停跟随
出现“回到最新”
```

---

## Case 5：刷新恢复

会议进行中刷新：

```text
历史 FINAL 恢复
SSE 重新连接
新 Transcript 继续出现
```

---

## Case 6：结束会议

点击结束：

```text
Modal 二次确认
↓
FINALIZING
```

禁止立即显示“已完成”。

---

## Case 7：Summary 成功

```text
COMPLETED
↓
自动进入 Result
↓
会议纪要完整展示
```

---

## Case 8：Summary 失败

```text
Transcript 可查看
+
明确显示纪要失败
+
支持重新生成
```

---

# 60. MVP 页面最终形态

```text
01 首页
↓
02 创建会议 Modal
↓
03 Meeting Room
   实时 Transcript
↓
04 Finalizing
↓
05 Result
   ├── AI 会议纪要
   └── 完整 Transcript
```

---

# 61. Claude Code / Codex 执行约束

开发 Agent 必须遵守：

```text
1. 严格按 React + TypeScript 实现。
2. 不擅自换成 Vue。
3. 不引入 Next.js，除非明确要求。
4. 不引入 Redux，MVP 使用 Zustand。
5. 不引入 WebSocket 替代 SSE，除非后端契约改变。
6. 不加入 Agent / RAG / 知识库。
7. 不自己发明 Todo / Decision。
8. PARTIAL 和 FINAL 必须分离。
9. UI 必须由 meeting.status 驱动。
10. 所有 API 调用集中在 api 层。
11. SSE 必须封装为 Hook / Service。
12. 页面刷新必须能恢复 FINAL Transcript。
13. 不把 Summary 失败表达为“会议失败”。
14. 不硬编码真实后端返回数据。
15. 先 Mock 跑通，再接真实 API。
```

---

# 62. 推荐开发顺序

```text
Step 1
Vite + React + TypeScript 初始化

Step 2
Ant Design + Design Token

Step 3
AppLayout + Sidebar

Step 4
首页 + 创建会议 Modal

Step 5
Meeting Room 静态布局

Step 6
Zustand Meeting Store

Step 7
Mock Transcript Stream

Step 8
PARTIAL / FINAL UI

Step 9
Auto Scroll

Step 10
真实 HTTP API

Step 11
真实 SSE

Step 12
Finalizing UI

Step 13
Meeting Result

Step 14
Summary Failed / Regenerate

Step 15
联调真实 ASR
```

---

# 63. MVP 前端一句话定义

> **小水华 MVP 前端不是一个通用 AI 工作台，而是一个围绕“开会 → 实时转写 → 结束 → AI 纪要”构建的企业级会议交互界面。**

只要：

```text
实时字幕稳定
+
状态清晰
+
最后一句不丢
+
会议纪要可查看
+
失败可以恢复
```

第一版前端就达到了 MVP 的产品目标。

---

# 64. 后续版本预留

MVP 完成后再考虑：

```text
V0.2
Todo
Decision
Summary Version

V0.3
Discussion Node
Meeting Agent

V0.4
长会议 Memory

V1+
RAG
跨会议搜索
声纹映射
Multi-Agent
```

这些能力不应反向污染当前 MVP 的前端结构。
