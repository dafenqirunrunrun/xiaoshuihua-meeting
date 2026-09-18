# 小水华会议助手：字幕链路协作说明

## 1. 目的

本文用于约定 **音频/ASR 链路** 与 **会议业务/前端链路** 之间的边界。

核心原则：

> 音频链路负责把声音变成“标准字幕事件”；会议业务和前端只消费标准字幕事件，不关心 ESP32、PCM、腾讯云 ASR 的具体实现。

## 2. 两边职责

### 我负责：Audio / Speech → Transcript 输入

负责链路：

```text
IM73D122
→ ESP32-S3
→ PCM
→ FastAPI Audio
→ 腾讯云实时 ASR
→ ASR 结果标准化
→ Transcript Service
```

主要负责：ESP32 音频上传、FastAPI 接收 PCM、原始音频保存、腾讯云实时 ASR、解析 PARTIAL / FINAL、Speaker ID 与时间戳解析、把腾讯云返回结果转换成统一 Transcript Event，并交给 `transcript` 模块。

### 协作者负责：Transcript → 页面 / Agent / 会议业务

负责链路：

```text
Transcript
→ Realtime SSE
→ React 实时字幕
→ Discussion Node
→ Meeting Agent
→ Summary / Todo / Decision
```

主要负责 React 实时字幕页面、会议页面、会议状态、Discussion Node、Agent、会后 Summary / Todo / Decision、历史 Transcript 查询。

协作者不需要关心 PDM、PCM、ESP32、腾讯云 WebSocket、腾讯云签名和腾讯云原始 JSON。

## 3. 双方唯一核心交界面

双方以 **Transcript Event** 作为主要接口。

```text
腾讯云 ASR
   ↓
speech/
   ↓
统一 Transcript Event
   ↓
transcript/
   ├── 实时推送
   └── FINAL 落库
```

协作者只需要认识我们自己定义的 Transcript Event。

## 4. PARTIAL 字幕格式

PARTIAL 表示腾讯云当前正在识别、后续仍可能发生变化的临时字幕。

```json
{
  "type": "transcript.partial",
  "meetingId": "M202609180001",
  "asrSessionId": "0139ec51-b32b-11f1-99ee-7008108bf18b",
  "sentenceId": 2,
  "speakerId": -1,
  "startMs": 950,
  "endMs": 6480,
  "text": "我们现在正在测试小水华实时语音识别",
  "provider": "tencent"
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `type` | 固定为 `transcript.partial` |
| `meetingId` | 当前会议 ID |
| `asrSessionId` | 一次腾讯 ASR WebSocket 会话 ID |
| `sentenceId` | 腾讯当前句子编号 |
| `speakerId` | 说话人编号，PARTIAL 阶段可能为 `-1` |
| `startMs` | 相对会议/ASR 会话的起始时间 |
| `endMs` | 当前识别到的结束时间 |
| `text` | 当前临时文本 |
| `provider` | 当前为 `tencent` |

PARTIAL 的处理规则：

```text
PARTIAL
→ 不正式落数据库
→ 写入 Live Cache
→ 立即通过 SSE 推送前端
→ 同 sentenceId 的新 PARTIAL 覆盖旧 PARTIAL
```

因此前端不应该把同一句话的 PARTIAL 一条条追加，而应始终展示最新版本。

## 5. FINAL 字幕格式

FINAL 表示一句话已经由 ASR 确认。进入 `transcript` 模块后，由后端生成正式的 `segmentId` 和 `segmentSeq`，然后再推送给前端。

```json
{
  "type": "transcript.final",
  "meetingId": "M202609180001",
  "segmentId": "8f8a7d1c-...",
  "segmentSeq": 25,
  "asrSessionId": "0139ec51-b32b-11f1-99ee-7008108bf18b",
  "sentenceId": 2,
  "speakerId": 0,
  "startMs": 950,
  "endMs": 7900,
  "text": "我们现在正在测试小水华实时语音识别功能。",
  "provider": "tencent"
}
```

FINAL 的处理规则：

```text
FINAL
→ Transcript Service
→ 幂等检查
→ 正式落库
→ 清除对应 PARTIAL Cache
→ 通过 SSE 推送前端
```

前端收到 FINAL 后，用它替换当前对应的 PARTIAL，并追加为正式字幕段。

## 6. 谁负责落库

> **由 FastAPI 后端 `transcript` 模块统一落库。前端绝不直接落库。**

职责边界：

```text
speech/
负责：腾讯云结果 → 标准 ASR/Transcript Event

transcript/
负责：PARTIAL 缓存、FINAL 幂等、FINAL 落库、segmentSeq、实时事件发布、Transcript 查询
```

最终链路：

```text
Tencent ASR
    ↓
speech/tencent_client.py
    ↓
标准事件
    ↓
transcript/service.py
    │
    ├── PARTIAL → live_cache → realtime/SSE
    │
    └── FINAL   → repository → DB
                         ↓
                     realtime/SSE
```

Transcript 数据库是会议文字的唯一事实源。

## 7. 为什么 PARTIAL 不落库

腾讯实时识别过程中，同一句话可能连续产生很多版本：

```text
我们
我们现在
我们现在正在
我们现在正在讨论
我们现在正在讨论会议助手
```

如果全部 INSERT，会产生大量无效记录、重复字幕，并可能让后续 Agent 读取错误中间结果。因此规则固定为：

```text
PARTIAL = 临时状态
FINAL   = 正式事实
```

只有 FINAL 进入正式 `transcript_segment`。

## 8. FINAL 建议落库字段

`transcript_segment` 至少包含：

```text
id
meeting_id
segment_seq
speaker_id
start_ms
end_ms
text
asr_provider
asr_session_id
asr_sentence_id
created_at
```

建议幂等依据：

```text
meeting_id + asr_session_id + asr_sentence_id
```

同一个 FINAL 即使因网络重试到达两次，也不能插入两条。同时保留：

```text
UNIQUE(meeting_id, segment_seq)
```

保证会议字幕顺序唯一。

## 9. 前端如何实时接收

当前项目继续使用已有 SSE，例如：

```text
GET /meetings/{meeting_id}/events
```

FastAPI 持续推送：

```text
transcript.partial
transcript.final
meeting.status
summary.status
...
```

React 不直接连接腾讯云。正确架构：

```text
Tencent
   ↓
FastAPI
   ↓
SSE
   ↓
React
```

## 10. 前端实时字幕状态建议

React 页面可以维护：

```text
finalSegments[]
currentPartial
```

收到新的 PARTIAL：覆盖 `currentPartial`。

收到 FINAL：清空/替换当前 `currentPartial`，并将正式 Segment 追加到 `finalSegments`。

这样页面不会把几十条中间结果全部堆出来。

## 11. Speaker 约定

V1 暂时没有声纹身份识别，因此：

```text
speakerId = 0 → Speaker 0
speakerId = 1 → Speaker 1
speakerId = 2 → Speaker 2
```

PARTIAL 阶段腾讯可能返回 `speakerId = -1`，这是正常情况，不是错误。前端可显示“正在识别...”或暂不显示 Speaker，等 FINAL 到达后再显示正式 Speaker。

## 12. 时间戳约定

统一使用：

```text
startMs
endMs
```

单位统一为毫秒。不要一部分代码使用秒、一部分使用毫秒。

## 13. 协作者当前如何模拟我的输出

在真实 ESP32 / 腾讯 ASR 完成之前，协作者不需要等我，可以使用 Mock Event。

PARTIAL：

```json
{
  "type": "transcript.partial",
  "meetingId": "M001",
  "sentenceId": 1,
  "speakerId": -1,
  "startMs": 1000,
  "endMs": 2500,
  "text": "我们今天先讨论..."
}
```

随后模拟 FINAL：

```json
{
  "type": "transcript.final",
  "meetingId": "M001",
  "segmentId": "mock-seg-001",
  "segmentSeq": 1,
  "sentenceId": 1,
  "speakerId": 0,
  "startMs": 1000,
  "endMs": 3200,
  "text": "我们今天先讨论会议助手的整体方案。"
}
```

这样 React 字幕页面可以完全独立开发。等真实链路完成后，只需要把 Mock Producer 替换成 Tencent ASR，前端协议不变。

## 14. 双方模块边界

我主要修改：

```text
firmware/esp32-s3/
backend/app/device/
backend/app/audio/
backend/app/speech/
backend/app/transcript/   # ASR 输入、标准化、落库入口
protocols/
```

协作者主要修改：

```text
backend/app/meeting/
backend/app/agent/
backend/app/auth/
backend/app/weiguang/
frontend/
```

共同修改前必须沟通：

```text
backend/app/transcript/
backend/app/realtime/
protocols/
```

尤其以下字段不能某一方自行修改：

```text
type
meetingId
segmentId
segmentSeq
sentenceId
speakerId
startMs
endMs
text
```

如果协议需要变化，先修改：

```text
protocols/transcript-format.md
protocols/realtime-events.md
```

双方确认后再改代码。

## 15. 一句话总结

```text
我负责：声音 → 标准字幕

Transcript Service 负责：FINAL 落库 + 所有字幕实时事件发布

协作者负责：消费字幕 → 实时页面 + Agent + 会后业务
```

其中：

```text
PARTIAL：只实时显示，不正式落库
FINAL：正式落库，并实时显示
```

这就是 V1 字幕链路的协作边界。
