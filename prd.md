# 小水华会议助手 MVP V0.1 产品需求文档（优化版 / React）

> 版本：MVP V0.1  
> 文档类型：Product Requirements Document / Technical Contract  
> 核心目标：跑通“实时转写展示 + Transcript 持久化 + 会议结束后 AI 会议纪要”完整闭环。  
> 前端：React + TypeScript + Vite + Zustand + Axios + Ant Design + SSE  
> 后端：Python 3.11+ + FastAPI + Pydantic + httpx  
> LLM：公司内部 DeepSeek V4.1 Flash  
> 上游：语音采集 / ASR / Speaker 识别由其他同学负责  
> MVP 原则：主链路稳定优先，不提前引入 Agent、RAG、Memory、Multi-Agent、Todo、Decision。  

---

# 0. 文档优先级与使用方式

本 PRD 是 MVP V0.1 的**业务与接口事实源**。

配套文件：

```text
小水华会议助手_MVP_V0.1_PRD_优化版_React.md
→ 定义做什么、不做什么、状态机、接口契约、数据模型、验收标准

小水华会议助手_MVP_V0.1_前端设计方案_React.md
→ 定义具体 UI、页面布局、组件拆分、交互和视觉规范
```

当两份文档发生冲突：

```text
业务范围 / 状态机 / API / 数据契约
→ 以 PRD 为准

页面布局 / Design Token / 组件结构 / 交互细节
→ 以前端设计方案为准
```

Claude Code / Codex 开发时应按以上优先级读取。

---

# 1. 产品目标

小水华会议助手 MVP V0.1 只验证两个核心价值。

## 1.1 实时会议转写

会议进行时：

```text
语音输入
↓
ASR
↓
PARTIAL / FINAL Transcript Event
↓
FastAPI
↓
SSE
↓
React
↓
实时 Transcript
```

用户无需主动刷新，即可持续看到会议内容。

---

## 1.2 AI 会议纪要

会议结束后：

```text
等待最后 FINAL
↓
收到 TRANSCRIPT_STREAM_END
↓
Transcript Freeze
↓
读取完整 FINAL Transcript
↓
DeepSeek V4.1 Flash
↓
Structured Summary
↓
保存
↓
React 展示
```

---

# 2. MVP 成功标准

MVP 完成不以“功能多”为判断标准，而以以下五点为准：

```text
1. 会议能正常开始
2. 实时字幕能持续显示
3. FINAL Transcript 不重复、不丢失、可恢复
4. 结束会议不会漏掉最后一句
5. 完整 Transcript 能生成并展示会议纪要
```

满足以上条件即视为完成 MVP V0.1 主链路。

---

# 3. MVP 范围

## 3.1 必须实现

```text
首页 / 最近会议
创建会议
开始会议
Meeting Room
实时 Transcript
PARTIAL 原地更新
FINAL 固化
FINAL Transcript 持久化
SSE 实时推送
SSE 基础重连
结束会议
FINALIZING
TRANSCRIPT_STREAM_END
Transcript 完整性检查
DeepSeek V4.1 Flash 总结
会议结果页
AI 会议纪要
完整 Transcript 查询
Summary 失败后的重新生成
Mock ASR / Mock Stream
```

---

## 3.2 MVP 不做

```text
Meeting Agent
Discussion Node
Agent Thread
Memory Snapshot
RAG
向量数据库
跨会议问答
Multi-Agent
LangGraph
结构化 Todo
结构化 Decision
自动章节
自动议题识别
声纹身份映射
真实 Speaker → 员工姓名映射
复杂设备管理
复杂权限模型
完整音频断线补传
历史会议语义搜索
复杂消息中心
知识库
AI PPT
思维导图
通用 AI 办公工作台
```

这些能力进入后续版本。

---

# 4. 团队职责边界

## 4.1 语音 / 输入同学负责

上游负责：

```text
音频输入
↓
语音采集
↓
ASR
↓
Speaker 区分（如能力支持）
↓
PARTIAL / FINAL Transcript Event
↓
TRANSCRIPT_STREAM_END
```

上游需要保证：

```text
meeting_id 正确
event_id 唯一
同一发言 utterance_id 稳定
FINAL segment_seq 在会议内唯一且有序
结束时发送 STREAM_END
```

---

## 4.2 当前负责范围

### 前端

```text
React 页面
创建会议
会议状态展示
实时 Transcript
PARTIAL / FINAL UI
SSE 连接与恢复
结束会议交互
FINALIZING
会议纪要展示
失败状态
```

### Python 后端

```text
Meeting 生命周期
Transcript Event 接收
PARTIAL 临时状态
FINAL 幂等与持久化
SSE 广播
Transcript 查询
FINALIZING
Stream End 处理
Transcript 完整性确认
LLM Provider
DeepSeek Summary
Summary 持久化
结果查询
失败恢复
```

---

# 5. 系统边界

系统结构：

```text
┌──────────────────── 上游负责 ────────────────────┐

Audio
  ↓
ASR / Speaker
  ↓
Transcript Event

└────────────────────────┬─────────────────────────┘
                         │
                         │ HTTP / Internal Contract
                         ▼

┌──────────────────── 当前系统 ────────────────────┐

                 FastAPI
            ┌──────┼──────┐
            │      │      │
      Meeting  Transcript  Finalizer
            │      │      │
            │      │      ▼
            │      │  DeepSeek V4.1 Flash
            │      │
            └──────┼──────────→ Database
                   │
                   ▼
                  SSE
                   │
                   ▼
                 React

└───────────────────────────────────────────────────┘
```

---

# 6. MVP 前端技术栈

固定使用：

```text
React
TypeScript
Vite
Zustand
Axios
Ant Design
SSE
```

禁止在 MVP 中擅自替换为：

```text
Vue
Pinia
Element Plus
Next.js
Redux
```

除非项目负责人明确重新决策。

---

# 7. 后端技术栈

统一使用：

```text
Python 3.11+
FastAPI
Pydantic
Uvicorn
httpx
SQLAlchemy / 公司统一数据平台 Client
```

后端优先采用 async I/O。

适合：

```text
SSE
HTTP API
LLM API
外部数据平台
```

---

# 8. LLM 设计原则

MVP 固定使用：

```text
DeepSeek V4.1 Flash
```

实际：

```text
model name
base url
api key
```

全部以公司内部接口真实配置为准。

业务 Service 不直接绑定具体模型 SDK。

统一抽象：

```text
LLMProvider
     ↑
DeepSeekProvider
```

---

# 9. 用户主流程

```text
打开小水华
↓
首页
↓
创建会议
↓
开始会议
↓
Meeting = IN_PROGRESS
↓
页面建立 SSE
↓
ASR 持续输出 Transcript Event
↓
PARTIAL 实时更新
↓
FINAL 固化并保存
↓
会议继续
↓
用户点击结束会议
↓
Meeting = FINALIZING
↓
等待最后 FINAL
↓
收到 TRANSCRIPT_STREAM_END
↓
检查 Transcript 完整性
↓
Freeze Transcript
↓
生成会议纪要
↓
Meeting = COMPLETED
↓
进入会议结果页
↓
查看 AI 会议纪要
↓
查看完整 Transcript
```

---

# 10. 前端页面范围

MVP 用户可见页面：

```text
1. 首页 / 最近会议
2. 创建会议 Modal
3. Meeting Room
4. Finalizing 状态
5. Meeting Result
6. 设置（MVP 可仅保留基础入口）
```

UI 具体规范以：

```text
小水华会议助手_MVP_V0.1_前端设计方案_React.md
```

为准。

---

# 11. Meeting 状态机

Meeting 只保留四个业务状态：

```text
CREATED
   ↓
IN_PROGRESS
   ↓
FINALIZING
   ↓
COMPLETED
```

---

## 11.1 CREATED

表示：

```text
会议已经创建
尚未开始
```

允许：

```text
查询
开始
```

---

## 11.2 IN_PROGRESS

表示：

```text
会议正在进行
```

允许：

```text
接收 PARTIAL
接收 FINAL
推送 SSE
持久化 FINAL
结束会议
```

---

## 11.3 FINALIZING

表示：

```text
用户已经请求结束
但 Transcript / Summary 尚未处理完成
```

此阶段：

```text
等待上游最后 FINAL
等待 STREAM_END
检查 Transcript 完整性
Freeze Transcript
调用 LLM
保存 Summary
```

前端不得把 FINALIZING 显示成：

```text
会议已完整处理完成
```

---

## 11.4 COMPLETED

表示：

```text
本场会议事实已经固化
```

正常情况下：

```text
Transcript 完整
Summary 已生成
```

如果 Summary 后续重新生成，不修改 Transcript。

---

# 12. Summary 状态

为了避免把“AI 生成失败”误表达成“会议失败”，Summary 单独维护状态：

```text
NOT_STARTED
GENERATING
COMPLETED
FAILED
```

推荐：

```text
meeting.status = COMPLETED
summary.status = FAILED
```

表达：

```text
会议事实已经完整保存
但 AI 纪要生成失败
```

前端展示：

```text
会议记录已保存
AI 会议纪要生成失败
[重新生成]
```

而不是：

```text
会议失败
```

---

# 13. 开始会议

接口：

```text
POST /api/meetings/{meeting_id}/start
```

成功后：

```text
Meeting = IN_PROGRESS
```

响应示例：

```json
{
  "id": "meeting_001",
  "status": "IN_PROGRESS",
  "started_at": "2026-09-16T10:00:00+08:00"
}
```

---

# 14. GrillMe：ASR 与 meeting_id 如何关联

这是联调前必须确认的问题。

上游发送的每一个 Transcript Event 必须携带：

```text
meeting_id
```

因此在正式联调之前，团队必须明确：

```text
ASR 模块如何获得当前 meeting_id？
```

推荐原则：

```text
创建 / 开始会议
↓
业务系统生成 meeting_id
↓
meeting_id 传给 ASR 会话
↓
后续所有 Transcript Event 带回同一个 meeting_id
```

具体的“如何通知 ASR 开始 / 停止”由上游实现决定，本 PRD 不强绑定 SDK。

但禁止：

```text
依靠当前唯一会议
或
依靠时间猜 meeting_id
```

---

# 15. Transcript Event Contract

这是 MVP 最重要的跨团队数据契约。

推荐：

```json
{
  "event_id": "evt_01JABC",
  "meeting_id": "meeting_001",
  "utterance_id": "utt_000102",
  "segment_seq": 102,
  "speaker_id": "speaker_2",
  "text": "第一版先把实时字幕和会议纪要跑通。",
  "status": "FINAL",
  "start_ms": 120300,
  "end_ms": 125600,
  "emitted_at": 1789551600123
}
```

---

# 16. event_id

含义：

```text
一次上游事件的唯一 ID
```

要求：

```text
全局或业务范围内唯一
```

作用：

```text
网络重试幂等
日志追踪
问题定位
```

---

# 17. utterance_id

同一段正在识别的发言必须稳定。

例如：

```text
utt_101 PARTIAL：我们
utt_101 PARTIAL：我们第一版
utt_101 PARTIAL：我们第一版先做实时字幕
utt_101 FINAL：我们第一版先做实时字幕。
```

前端根据：

```text
utterance_id
```

原地更新。

---

# 18. segment_seq

正式 FINAL Transcript 的会议内顺序号。

要求：

```text
同一 meeting 内唯一
FINAL 时必须存在
原则上单调递增
```

例如：

```text
1
2
3
4
...
```

PARTIAL 可没有正式 segment_seq。

---

# 19. speaker_id

MVP 不要求真实姓名。

允许：

```text
speaker_0
speaker_1
speaker_2
unknown
```

上游 Speaker 不稳定时：

```text
speaker_id = "unknown"
```

不能阻塞主链路。

---

# 20. status

只允许：

```text
PARTIAL
FINAL
```

---

## 20.1 PARTIAL

```text
临时识别结果
可以变化
用于实时 UI
原则上不写正式 Transcript 表
```

---

## 20.2 FINAL

```text
正式识别结果
不可被普通业务逻辑覆盖
必须持久化
属于会议事实
```

---

# 21. Transcript 接收接口

推荐：

```text
POST /api/internal/meetings/{meeting_id}/transcript-events
```

后端：

```text
校验 meeting
↓
校验 Meeting Status
↓
校验 event_id
↓
校验 body.meeting_id == path meeting_id
↓
PARTIAL / FINAL 分流
```

---

# 22. PARTIAL 处理

推荐：

```text
live_utterance_cache
```

Key：

```text
meeting_id + utterance_id
```

Value：

```text
最新 PARTIAL
```

收到新 PARTIAL：

```text
覆盖旧 PARTIAL
↓
SSE
↓
React 原地更新
```

MVP 单实例可先用：

```text
进程内 dict
```

多实例再升级 Redis。

---

# 23. FINAL 处理

收到 FINAL：

```text
校验 event_id
↓
校验 meeting_id + segment_seq
↓
持久化 transcript_segment
↓
删除对应 PARTIAL cache
↓
广播 transcript.final
```

唯一约束：

```text
UNIQUE(event_id)
UNIQUE(meeting_id, segment_seq)
```

---

# 24. FINAL 冲突策略

相同：

```text
meeting_id + segment_seq
```

再次提交。

如果内容完全一致：

```text
幂等成功
200
```

如果内容不同：

```text
409 Conflict
记录 ERROR
不 silent overwrite
```

---

# 25. Transcript Stream End

仅靠：

```text
用户点击结束
+
sleep 5 秒
```

不能判断 ASR 是否结束。

上游必须发送：

```text
TRANSCRIPT_STREAM_END
```

示例：

```json
{
  "event_id": "evt_end_001",
  "meeting_id": "meeting_001",
  "event_type": "TRANSCRIPT_STREAM_END",
  "last_segment_seq": 128,
  "emitted_at": 1789551700123
}
```

---

# 26. 结束会议流程

用户点击：

```text
结束会议
```

接口：

```text
POST /api/meetings/{meeting_id}/finish
```

响应：

```json
{
  "status": "FINALIZING"
}
```

这个响应只表示：

```text
已经接受结束请求
```

不表示：

```text
会议纪要已经完成
```

---

# 27. Finalizing 流程

```text
POST finish
↓
Meeting = FINALIZING
↓
停止继续产生新的业务会议内容
↓
等待上游最后 FINAL
↓
收到 TRANSCRIPT_STREAM_END
↓
检查 last_segment_seq
↓
检查数据库 FINAL Segment
↓
确认完整
↓
Transcript Freeze
↓
Summary = GENERATING
↓
DeepSeek
↓
保存 Summary
↓
Meeting = COMPLETED
↓
Summary = COMPLETED
```

---

# 28. Transcript 完整性

收到：

```text
last_segment_seq = 128
```

后端至少确认：

```text
最后已保存 segment_seq == 128
```

并检查显式序号缺口。

例如：

```text
126
127
129
```

说明：

```text
128 缺失
```

默认：

```text
存在明确缺口
→ 不直接生成最终纪要
→ 记录 warning / error
→ 等待重试或进入人工可排查状态
```

禁止悄悄跳过。

---

# 29. Transcript Freeze

Freeze 含义：

```text
本场正式 Transcript 已确认结束
```

不要求复制整份 Transcript。

Freeze 以后：

```text
普通 FINAL 不再允许 silent append
```

如果仍收到 FINAL：

```text
记录异常
进入排查
```

避免：

```text
Summary 已生成
但之后又出现新的会议事实
```

---

# 30. SSE 设计

接口：

```text
GET /api/meetings/{meeting_id}/events
```

事件：

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

# 31. SSE 与鉴权

如果生产环境 SSE 需要：

```text
Authorization: Bearer <token>
```

前端优先采用：

```text
Fetch-based SSE
```

而不是强依赖浏览器原生 EventSource。

原因：

```text
原生 EventSource 不方便自定义 Authorization Header
```

如果未来采用：

```text
同源 Cookie / Session 鉴权
```

则可以使用原生 EventSource。

最终实现按公司统一认证方案确定。

---

# 32. 页面初始化与 SSE 恢复

Meeting Room 打开时：

```text
GET Meeting
+
GET 当前 FINAL Transcript Snapshot
+
建立 SSE
```

禁止只依赖 SSE 历史消息恢复页面。

网络断开：

```text
SSE reconnect
↓
重新 GET Transcript Snapshot
↓
去重
↓
继续接收实时事件
```

---

# 33. 前端数据模型

```ts
export type MeetingStatus =
  | 'CREATED'
  | 'IN_PROGRESS'
  | 'FINALIZING'
  | 'COMPLETED'

export type SummaryStatus =
  | 'NOT_STARTED'
  | 'GENERATING'
  | 'COMPLETED'
  | 'FAILED'

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

# 34. Zustand 状态

建议：

```text
meeting
finalSegments
partialUtterances
summary
summaryStatus
streamStatus
```

必须分开：

```text
finalSegments
和
partialUtterances
```

禁止把 PARTIAL / FINAL 混在一个无类型数组。

---

# 35. 前端实时更新规则

收到：

```text
transcript.partial
```

执行：

```text
upsertPartial(utterance_id)
```

收到：

```text
transcript.final
```

执行：

```text
removePartial(utterance_id)
↓
upsertFinal(segment_seq)
↓
sort by segment_seq
```

---

# 36. 前端自动滚动

默认：

```text
用户位于底部
→ 自动跟随
```

用户主动向上：

```text
暂停自动跟随
↓
显示“回到最新”
```

不得因新 PARTIAL 强制抢用户滚动位置。

---

# 37. 创建会议

接口：

```text
POST /api/meetings
```

MVP 请求：

```json
{
  "title": "小水华 MVP 方案讨论"
}
```

建议字段只保留：

```text
title
description（可选）
```

---

# 38. GrillMe：创建会议不显示虚假麦克风设置

如果浏览器不是实际音频采集源，则创建会议页面禁止展示：

```text
浏览器麦克风选择
麦克风权限
音量测试
输入设备
```

否则会误导用户认为：

```text
网页麦克风
=
ASR 实际输入
```

只有未来真正使用浏览器采音时才增加。

---

# 39. Meeting Result

会议结果页展示：

```text
会议标题
开始时间
结束时间
会议时长
AI 会议纪要
完整 FINAL Transcript
```

建议两个 Tab：

```text
会议纪要
会议内容
```

默认：

```text
会议纪要
```

---

# 40. Summary 输入

只允许读取：

```text
数据库中已 Freeze 的全部 FINAL Transcript
```

禁止：

```text
读取 PARTIAL
读取前端临时状态
基于未完成 Transcript 总结
```

---

# 41. LLM 输入格式

推荐：

```text
[Meeting]
Title: 小水华 MVP 讨论会
Start: 2026-09-16 10:00
End: 2026-09-16 11:30

[Transcript]
[00:00:03 - 00:00:08] Speaker 1: 今天先讨论 MVP。
[00:00:10 - 00:00:17] Speaker 2: 第一版先把实时字幕做出来。
[00:00:18 - 00:00:28] Speaker 1: 结束以后生成会议纪要。
```

保留：

```text
Speaker
时间
Segment 顺序
文本
```

---

# 42. Summary Prompt 约束

System Prompt 必须要求：

```text
只基于 Transcript
不补充 Transcript 没有出现的事实
不猜测 Speaker 真实身份
不把“建议”写成“决定”
不确定内容放到 open_questions
```

---

# 43. Summary Schema

MVP：

```json
{
  "overview": "本次会议主要讨论……",
  "key_topics": [
    "实时会议转写",
    "AI 会议纪要"
  ],
  "conclusions": [
    "第一版优先实现实时字幕和会议纪要。"
  ],
  "open_questions": []
}
```

Pydantic：

```python
class MeetingSummarySchema(BaseModel):
    overview: str
    key_topics: list[str]
    conclusions: list[str]
    open_questions: list[str]
```

---

# 44. Summary Parse 失败

```text
模型返回
↓
Schema Parse
```

第一次失败：

```text
格式修复 Prompt
↓
最多重试 1 次
```

仍失败：

```text
summary.status = FAILED
保存 raw_model_output
保留 Transcript
允许重新生成
```

禁止无限重试。

---

# 45. 超长 Transcript

MVP 优先：

```text
Full Transcript
→ 一次 Summary
```

必须有输入长度保护。

超过安全阈值：

```text
Chunk Transcript
↓
Partial Summary
↓
Final Summary
```

这仍属于：

```text
Summary Pipeline
```

不是 Agent。

MVP 可以先预留接口，后续再完善。

---

# 46. 数据模型

MVP 核心三张表。

---

## 46.1 meeting

```text
id
title
description
status
started_at
ended_at
completed_at
created_at
updated_at
```

---

## 46.2 transcript_segment

```text
id
meeting_id
segment_seq
utterance_id
event_id
speaker_id
start_ms
end_ms
text
created_at
```

约束：

```text
UNIQUE(event_id)
UNIQUE(meeting_id, segment_seq)
```

正式 FINAL：

```text
Append Only
```

---

## 46.3 meeting_summary

```text
id
meeting_id
version
overview
key_topics_json
conclusions_json
open_questions_json
raw_model_output
model_name
status
created_at
```

保留：

```text
version
```

用于未来重新生成。

---

# 47. Transcript 是唯一事实源

系统必须始终遵守：

```text
Transcript = 会议事实
Summary = AI 派生结果
```

因此：

```text
Summary 错误
→ 可以重新生成

Transcript 丢失 / 错乱
→ 会议事实受损
```

工程优先级：

```text
Transcript 完整性
>
Transcript 顺序
>
幂等
>
可恢复
>
Summary 质量优化
```

---

# 48. API 清单

MVP 对外业务 API：

```text
POST /api/meetings

POST /api/meetings/{meeting_id}/start

POST /api/meetings/{meeting_id}/finish

GET /api/meetings/{meeting_id}

GET /api/meetings/{meeting_id}/transcript

GET /api/meetings/{meeting_id}/events

GET /api/meetings/{meeting_id}/summary

POST /api/meetings/{meeting_id}/summary/regenerate
```

内部接口：

```text
POST /api/internal/meetings/{meeting_id}/transcript-events

POST /api/internal/meetings/{meeting_id}/stream-end
```

具体是否将 Stream End 和 Transcript Event 合并为同一 Endpoint，可由后端实现决定，但事件语义必须固定。

---

# 49. 错误处理

## 49.1 Event Schema 错误

```text
400
```

记录：

```text
meeting_id
event_id
validation_error
```

---

## 49.2 FINAL 冲突

相同数据：

```text
幂等成功
```

不同数据：

```text
409
+
ERROR 日志
```

---

## 49.3 SSE 断线

前端：

```text
轻量提示
自动重连
重新拉 Snapshot
```

不要弹阻塞式错误 Modal。

---

## 49.4 DeepSeek 超时

```text
明确 timeout
有限重试
最终 FAILED
```

Transcript 不受影响。

---

# 50. 日志

关键日志至少：

```text
meeting_id
request_id / event_id
operation
status
latency_ms
```

LLM：

```text
meeting_id
model_name
input_size
latency_ms
parse_success
retry_count
```

禁止记录：

```text
API Key
Token
内部密码
敏感凭证
```

---

# 51. 配置

环境变量：

```text
DEEPSEEK_BASE_URL
DEEPSEEK_API_KEY
DEEPSEEK_MODEL
DATABASE_URL / DATA_PLATFORM_URL
```

禁止硬编码。

---

# 52. Mock 能力

前后端开发不能被真实 ASR 联调阻塞。

必须提供：

```text
Mock Transcript Producer
```

支持模拟：

```text
PARTIAL
FINAL
STREAM_END
```

前端还可以提供：

```text
Mock SSE
```

建议环境变量：

```text
VITE_USE_MOCK_STREAM=true
```

---

# 53. MVP 验收

## Case 1：PARTIAL 原地更新

输入：

```text
utt_1 我们
utt_1 我们今天
utt_1 我们今天讨论 MVP
```

页面最终只有：

```text
一条正在识别内容
```

---

## Case 2：FINAL 固化

收到：

```text
utt_1 FINAL
```

结果：

```text
PARTIAL 删除
FINAL 固化
下一 utterance 独立显示
```

---

## Case 3：重复 event_id

同一个 Event 两次：

```text
数据库不得产生两份正式事实
```

---

## Case 4：FINAL 重试

同一个：

```text
meeting_id + segment_seq
```

相同内容：

```text
幂等成功
```

不同内容：

```text
Conflict
```

---

## Case 5：页面刷新

IN_PROGRESS 刷新：

```text
历史 FINAL 恢复
SSE 重连
新 Transcript 继续出现
```

---

## Case 6：结束不提前总结

用户点击结束：

```text
Stream End 未到
→ 不调用最终 Summary
```

---

## Case 7：最后一句不丢

结束前最后一句：

```text
“第一版最终使用 SSE。”
```

其 FINAL 即使在 finish 之后到达，也必须进入：

```text
Transcript
+
Summary
```

---

## Case 8：完整 Transcript Summary

DeepSeek 输入必须来自：

```text
Freeze 后数据库全部 FINAL
```

---

## Case 9：LLM 失败

```text
Transcript 完整
Meeting 事实可查看
Summary = FAILED
允许重新生成
```

---

## Case 10：SSE 断线恢复

```text
断线
↓
重连
↓
GET Snapshot
↓
去重
↓
继续实时更新
```

---

# 54. 基础性能目标

建议：

```text
ASR Event → 页面显示
P95 < 1 秒
（不含 ASR 自身耗时）

FINAL 入库
P95 < 500 ms

Transcript
至少约 1000 Segment 不明显卡顿

SSE
支持自动恢复
```

Summary 生成时间受公司模型服务影响。

前端必须：

```text
有 loading
有 timeout 状态
有失败状态
有重试入口
```

---

# 55. 基础安全要求

MVP 至少：

```text
内部 Transcript Endpoint 不裸奔公网
LLM API Key 不进入前端
浏览器只访问业务后端
后端统一访问 DeepSeek
日志不输出密钥
```

---

# 56. 前端开发约束

Claude Code / Codex 必须遵守：

```text
React + TypeScript
Vite
Zustand
Axios
Ant Design
SSE

PARTIAL 与 FINAL 分离
meeting.status 驱动页面
SSE 封装为 Hook / Service
API 封装在 api 层
刷新能恢复 FINAL
Summary 失败不能显示“会议失败”
```

禁止擅自增加：

```text
RAG
Agent
Multi-Agent
LangGraph
Todo
Decision
知识库
AI Chat
```

---

# 57. 后端开发约束

必须：

```text
FastAPI
async I/O
Pydantic Schema
LLM Provider
Service / Repository 分层
FINAL 幂等
Transcript Append Only
Stream End
Freeze
Summary Schema Validation
有限重试
```

---

# 58. 推荐开发顺序

```text
Step 1
FastAPI 基础骨架

Step 2
Meeting 状态机

Step 3
Transcript Contract

Step 4
Mock ASR Producer

Step 5
FINAL 持久化

Step 6
SSE

Step 7
React App Shell

Step 8
Meeting Room

Step 9
PARTIAL / FINAL 实时 UI

Step 10
SSE 重连 + Snapshot 恢复

Step 11
Finish + Finalizing

Step 12
DeepSeek Provider

Step 13
Summary Pipeline

Step 14
Meeting Result

Step 15
错误恢复

Step 16
真实 ASR 联调
```

---

# 59. 里程碑

## Milestone 1：Fake Real-time Demo

```text
Mock ASR
↓
FastAPI
↓
SSE
↓
React
```

实时字幕正常。

---

## Milestone 2：Transcript Closed Loop

```text
FINAL
↓
Database
↓
Refresh
↓
Snapshot Recover
```

---

## Milestone 3：Summary

```text
Freeze Transcript
↓
DeepSeek V4.1 Flash
↓
Meeting Summary
↓
React
```

---

## Milestone 4：Real ASR Integration

```text
Mock Producer
↓
替换为真实 ASR Event
```

如果 Contract 正确，不应大改业务系统。

---

# 60. 后续版本

## V0.2

```text
Todo
Decision
Summary Version
更完整的导出
```

## V0.3

```text
Discussion Node
Meeting Agent
会议中问答
```

## V0.4

```text
Rolling Memory
长会议 Context Builder
```

## V1+

```text
RAG
跨会议检索
声纹映射
自动议题
Multi-Agent
```

---

# 61. MVP 最终原则

1. **先跑通主链路，而不是堆 AI 功能。**
2. **Transcript 是唯一会议事实源。**
3. **PARTIAL 是实时 UI 状态，FINAL 才是正式事实。**
4. **结束会议必须经过 FINALIZING。**
5. **必须依赖明确 Stream End，而不是固定 sleep。**
6. **Summary 与 Transcript 解耦。**
7. **Summary 失败不等于会议失败。**
8. **LLM 必须经过 Provider 层。**
9. **前端固定 React 技术栈。**
10. **实时链路使用 SSE，生产鉴权方式需与公司认证方式匹配。**
11. **上游与业务系统必须用 meeting_id 明确关联。**
12. **Mock 链路必须先于真实 ASR 联调完成。**

---

# 62. MVP 完成后的目标形态

```text
打开小水华
↓
创建会议
↓
开始会议
↓
Meeting = IN_PROGRESS
↓
ASR PARTIAL
↓
React 实时更新
↓
ASR FINAL
↓
FINAL Transcript 保存
↓
会议继续
↓
用户结束会议
↓
Meeting = FINALIZING
↓
最后 FINAL
↓
STREAM_END
↓
Transcript Freeze
↓
DeepSeek V4.1 Flash
↓
Structured Summary
↓
Meeting = COMPLETED
↓
结果页
↓
AI 会议纪要
+
完整 Transcript
```

达到以上状态，即代表：

> **小水华会议助手 MVP V0.1 已经完成从语音识别结果到实时会议体验，再到 AI 会后纪要的第一个真实业务闭环。**
