# ESP32 音频协议

> 状态：**⏸ NOT IN SCOPE —— V0.2**
> 更新：ASR-0B（2026-09-19）

---

## 结论

本协议**当前不定义任何内容**，且 **MVP V0.1 明确不做**：

- 不做 ESP32-S3 固件
- 不做音频采集 / 上传链路
- 不做真实麦克风音频

依据：`CLAUDE.md`「MVP 范围」——不做「真实麦克风音频 / 语音采集 / 声纹身份映射」。
ASR-0B 的限制同样明确：**不接腾讯 ASR、不做音频采集、不做 ESP32**。

---

## 为什么先占位而不删除

ESP32 是**已规划的跨团队接口**，将来音频上行会与本文档所在目录的其它协议并列。
保留占位是为了让目录索引完整，并在有人开始实现前给出明确信号：**这里还没定，别猜**。

---

## V0.2 开工前必须先回答

以下问题必须由双方确认，写进本文件并标记 ✅ FROZEN 之后，才允许动实现：

1. **传输**：WebSocket 还是 HTTP 分片上传？采样率 / 编码（PCM / Opus）？
2. **分片**：分片时长、序号语义、断线续传与去重。
3. **身份**：设备与 `meetingId` 如何绑定？单设备单会议？
4. **时间轴**：设备端时钟如何对齐 ASR 会话时间轴
   （与 [transcript-format.md §5](./transcript-format.md) 的 session-local offsets 关系）。
5. **与 ASR 的关系**：音频直达 ASR 厂商，还是经后端中转？
   若上游直连 ASR，本协议与 `asr-events` 契约的衔接点在哪一层？
6. **STREAM_END 归属**：真实终止信号由谁发出（见
   [realtime-events.md §6.5](./realtime-events.md) 的 PENDING）。

---

## 与现有契约的关系

ESP32 位于 [transcript-format.md](./transcript-format.md) 边界图的最上游：

```text
ESP32 音频上行（本协议，未定）
  ↓
Audio / ASR 归一化 PARTIAL / FINAL（transcript-format.md，已冻结）
  ↓
后端 Transcript Domain → SSE → 前端
```

**约定**：ESP32 链路的实现不得要求下游 `asr-events` 契约做出破坏性变更。
若确实需要变更，按 [README.md](./README.md) 的变更流程先改契约、再改实现。
