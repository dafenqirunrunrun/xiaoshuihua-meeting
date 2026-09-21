# Project Handoff — Develop

> 面向**新拉取 `develop` 的协作者**。目标：在尽量少问人的情况下跑起项目、理解当前
> 系统边界，并继续接 ASR。
>
> 状态：`develop`（含 ASR-0A / ASR-0B / AGENT-1 / AGENT-2 / LLM-CONFIG-1）。
> 本文只描述**当前真实实现**；未冻结的部分显式标为 ⏳ PENDING。

---

## 1. 当前系统做到哪里

```text
Mock / Future Real ASR
        ↓
ASR Adapter
        ↓
Transcript FINAL
        ↓
AGENT-1
        ↓
Meeting State
        ↓
AGENT-2
        ↓
Final Summary
```

### 已完成

| 能力 | 说明 |
| --- | --- |
| ASR Adapter contract | 外部 camelCase DTO → 内部 domain，`segment_seq` 由 Transcript 侧分配 |
| Transcript FINAL | append-only 唯一事实源，`segment_seq` 排序，先落库后广播 |
| SSE | 具名帧增量流 + Snapshot 恢复（`after_segment_seq` 游标） |
| Meeting lifecycle | 四态机 `CREATED → IN_PROGRESS → FINALIZING → COMPLETED` |
| Mock / Replay | Mock Producer（外部 HTTP 客户端）+ 真实 provider replay 脚本 |
| Meeting State Agent | 近实时工作记忆（AGENT-1），单请求 + pending high-watermark + Memory Snapshot |
| Final Summary V2 | 冻结后的完整 FINAL 转写为事实源，latest-good State 为可选上下文（AGENT-2） |
| Evidence trace | `source_segment_seqs` 优先级选择 + ±1 邻域扩展 |
| legacy fallback | 仅由 `FINAL_SUMMARY_V2_ENABLED=false` 显式回退旧输出契约 |
| FakeProvider | 离线确定性，支持 Meeting State 与 Final Summary 两种 shape |
| DeepSeek real-provider | 已在真实 DeepSeek 上端到端验证（见 §8） |
| pytest / regression / frontend build / browser E2E | 全部可离线运行 |

### 尚未完成 / 暂缓

* 真实 ASR Provider 最终接入（V0.2）
* ESP32 / PCM / 音频采集
* `TRANSCRIPT_STREAM_END` 最终跨团队契约（见 §10，⏳ PENDING）
* Meeting-global ASR timeline（当前是 session-local）
* multi-worker DB 并发控制
* Meeting State 前端智能面板
* Evidence 点击溯源 UI

---

## 2. 协作者最需要理解的系统边界

### ASR 负责

```text
产生标准化识别结果
```

### Transcript 负责

```text
canonical FINAL
segment_seq
持久化
排序
```

### Agent 负责

```text
只消费 FINAL Transcript
```

Agent **不依赖**：

* Tencent 特有字段
* DeepSeek ASR
* provider 原始 payload
* ESP32 具体协议

> **未来真实 ASR 接入后，只要继续产出当前 Transcript contract，
> 下游 AGENT-1 / AGENT-2 不需要修改。**

契约细节见 [`protocols/transcript-format.md`](../protocols/transcript-format.md)。

---

## 3. 当前关键业务链

```text
Transcript
        ↓
Meeting State
```

Meeting State 当前维护：

* `current_topic`
* `topics`
* `decisions`
* `action_items`
* `open_questions`
* `risks`
* `source_segment_seqs`
* `last_processed_segment_seq`
* `version`

真实 FINAL 在录音过程中通过合并调度持续更新 Meeting State：默认停顿 2 秒刷新，
连续输入最迟 6 秒发起刷新。该窗口会强制处理未达到普通批次阈值的尾部，因此短会议
不再等到点击结束才首次产生会议理解。

Final Summary 使用：

```text
Meeting State
+
完整 FINAL Transcript
```

默认使用整场 FINAL Transcript 保证纪要完整性；组合 Prompt 超过容量上限时，才退化为
优先选择的 Transcript Evidence。

---

## 4. Provider 模式

### A. 没有真实 API 的协作者（推荐起点）

本地 `.env`：

```env
LLM_PROVIDER=fake
MEETING_AGENT_ENABLED=true
FINAL_SUMMARY_V2_ENABLED=true
```

此模式：

```text
Transcript
→ FakeProvider
→ Meeting State
→ FakeProvider
→ Final Summary
```

适合：日常开发 / ASR 联调 / 前端开发 / CI / 不需要真实模型效果的功能调试。

### B. 有真实 DeepSeek Provider 的开发者

```env
LLM_PROVIDER=deepseek
DEEPSEEK_BASE_URL=<YOUR_BASE_URL>
DEEPSEEK_API_KEY=<YOUR_API_KEY>
DEEPSEEK_MODEL=<YOUR_MODEL>

MEETING_AGENT_ENABLED=true
FINAL_SUMMARY_V2_ENABLED=true
```

> ⚠️ 本文档**不含**任何真实 API Key / token / credential / 内部地址。
> 各自的凭据由各自在本机 `.env` 填写，**永不要提交**。

**fail-closed**：`LLM_PROVIDER` 写成未知值（拼错）会直接抛
`LLMConfigurationError`，**绝不会静默回落到 FakeProvider**。

---

## 5. `.env` 说明

```text
backend/.env
```

是**本机配置，不进入 Git**（`.gitignore` 第 21 行 `.env`）。

模板见 [`backend/.env.example`](../backend/.env.example)：

```env
# Default local/offline development
LLM_PROVIDER=fake

MEETING_AGENT_ENABLED=true
FINAL_SUMMARY_V2_ENABLED=true

# Optional real provider
DEEPSEEK_BASE_URL=
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4.1-flash

# Optional summary model override
FINAL_SUMMARY_MODEL=
```

复制一份改成本机的即可：`cp backend/.env.example backend/.env`。

> 本仓库另有根目录 `ENVIRONMENT.md` / `protocolphoto.png` /
> `requirements-collab.txt` 三个文件按约定**不纳管**（未 tracked 也未 ignored），
> 请勿擅自 `git add`。

---

## 6. 如何启动

### Backend

```bash
cd backend

# 1) 虚拟环境（仓库不含 .venv）
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

# 2) 本地配置（默认离线 FakeProvider 即可启动）
cp .env.example .env

# 3) 启动
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

* 健康检查：`GET http://127.0.0.1:8000/api/health` → `{"status":"ok"}`
* API 文档：`http://127.0.0.1:8000/docs`
* 内部 ASR 契约文档：`http://127.0.0.1:8000/api/internal/docs`

### Frontend

```bash
cd frontend
npm install
npm run dev        # Vite dev server :5173，proxy /api -> http://127.0.0.1:8000
```

（`npm run dev` / `build` / `typecheck` 均为 `package.json` 中真实存在的脚本。）

---

## 7. 如何验证项目

### 后端：pytest（推荐）

```bash
cd backend
.venv/Scripts/python -m pytest
```

### 后端：legacy 回归脚本（早前 Step 的权威记录，与 pytest 并存）

```bash
cd backend
.venv/Scripts/python scripts/test_sse_step5.py          # Step 5  SSE
.venv/Scripts/python scripts/test_finalize_step6.py     # Step 6  Freeze
.venv/Scripts/python scripts/test_summary_step7a.py     # Step 7A Summary
.venv/Scripts/python scripts/test_summary_step7b.py     # Step 7B Finalizer
.venv/Scripts/python scripts/test_list_meetings.py      # 会议列表
.venv/Scripts/python scripts/test_asr_adapter.py        # ASR-0A Adapter
```

> 这些脚本**自带离线保证**（内部把 `LLM_PROVIDER` 钉为 `fake` 并清空
> `DEEPSEEK_*`），因此**本机就算配了真实 provider 也不会误发真实 API 调用**。

### 前端

```bash
cd frontend
npm run typecheck
npm run build
```

### Mock / Replay 联调（**没有真实 ASR 也能跑通全链路**）

启动 backend 后，另开一个终端：

```bash
cd backend
# 自动建会议并跑 happy 场景（PARTIAL → FINAL → STREAM_END）
.venv/Scripts/python scripts/mock_transcript_producer.py

# 对指定会议跑，可选场景：
#   happy / reconnect / duplicate / out-of-order / errors / all
.venv/Scripts/python scripts/mock_transcript_producer.py --meeting-id meeting_xxx --scenario happy
```

Mock Producer 是**纯外部 HTTP 客户端**（仅标准库），模拟协作者格式输出，
不 import Backend 内部。

> ✅ 没有真实 ASR，也可以用 Mock / Replay 完成
> `Transcript → Agent → Summary` 的联调。

真实浏览器 E2E（需本机真实 Chrome + 运行中的前后端，**不进 CI**）：

```bash
cd backend && .venv/Scripts/python scripts/e2e_asr_browser.py   # 需 Chrome 调试端口 9222
```

---

## 8. 真实 LLM 验收结论

已在真实 DeepSeek 上完成端到端验证：

```text
Transcript FINAL × 6
→ AGENT-1
→ real DeepSeek
→ Meeting State ACTIVE
→ cursor = final seq (6)
→ AGENT-2
→ real DeepSeek
→ Final Summary COMPLETED
→ summary_source=meeting_state
```

验收结论：

* **模型输出形状稳定**：多次运行的 Final Summary 均**首次解析成功**（0 次 repair）
* **没有** repair
* **没有** retry（成功路径）
* **没有** transcript_fallback
* source trace 存在（`source_segment_seqs`）

> Realtime 与 Final Summary 使用不同 SLA：Realtime 默认 25s、provider retry=0；
> Final Summary 默认 120s，完整 timeout 不自动重试。Realtime 临时失败时游标不推进，
> 有 last-good 内容则进入 `STALE` 并继续展示，后续 high-watermark 触发会合并重放。
> Final Summary 始终直接读取冻结 Transcript，不会等待 Realtime 恢复。

复现脚本（**会发真实付费请求，故意不放在 `tests/`，CI 绝不跑**）：

```bash
cd backend
.venv/Scripts/python scripts/replay_real_provider.py             # 12 项验收
.venv/Scripts/python scripts/replay_real_provider_stability.py 5 # 连跑 5 次看稳定性
```

> 该脚本**不打印任何 API Key**。真实 provider 验收**不是** develop 合并的强制条件
> —— 没有 Key 的协作者用 FakeProvider 即可继续开发。

---

## 9. FakeProvider 的意义

FakeProvider **不是生产模型**。它的作用是：

```text
offline development
CI
无 API Key 的协作者联调
```

当前它已能按 prompt 契约返回两种确定性 shape：

* Meeting State shape（AGENT-1）
* Final Summary shape（AGENT-2）
* legacy summary shape（降级路径）

所以**协作者没有 DeepSeek Key，也不影响继续开发 ASR 和前端**。

它不代表生产模型效果 —— 真实效果只由 `LLM_PROVIDER=deepseek` 验证。

---

## 10. STREAM_END 当前状态

```text
⏳ PENDING
```

目前：

* Mock 会发送 `STREAM_END`
* **真正由谁发送、什么时候发送，尚未和真实 ASR / Audio 协作者最终冻结**
* **不要擅自把当前 Mock 行为当成最终生产协议**

已冻结的部分：`last_segment_seq` 归属（Transcript-owned，可选，省略时后端派生
`max(seq) or 0`）与时间轴语义（session-local，见 §11）。

未冻结的部分：**发送者身份与时机**。这部分未来需要协作者共同确认。

---

## 11. 当前重要 Known Limitations

1. **multi-worker 并发暂未做 DB 行锁**。游标推进依赖进程内 `meeting_lock`，
   仅单进程 MVP 成立。升级路径：DB 行锁或 `UPDATE ... WHERE version = :expected`。
2. **ASR session 时间戳是 session-local，不是 meeting-global**。跨 session
   **不可比较**；排序**永远只认 `segment_seq`**；MVP **不做 rebase**。
3. **Meeting State / Summary evidence 前端暂未产品化**。V2 明细后端已具备，
   前端目前仍只消费扁平化后的旧四字段。
4. **`summary_source` 当前主要用于日志，不持久化独立列**。
5. **V2 detail 当前兼容存放于现有 `raw_model_output` 列**（不新增 DB 列）。
6. **真实 ASR 未接入**。
7. **STREAM_END 最终 ownership 未冻结**（见 §10）。
8. Meeting State merge 区分**字段缺省**与**显式空值**：缺省保留旧值，空数组或
   `null` 清除旧值；提示词要求模型在纠正、撤回或问题解决后输出完整的新状态。
9. `source_segment_seqs` 只保证**字段存在**，不保证引用正确。
10. **真实 provider 仍可能超时**。Realtime 以 25s 无重试快速释放 worker，并保留
    pending watermark；Final Summary 以 120s 独立运行。两条链路互不等待。

---

## 12. 协作者接 ASR 时最重要的一句话

> ## 不要修改 AGENT-1 / AGENT-2 来适配具体 ASR Provider。

正确方式：

```text
Real ASR
↓
Adapter
↓
现有 Transcript Contract
↓
后面全部复用
```

provider-specific 格式应当**止步于 Adapter**（`app/transcript/asr_adapter.py`），
内部 domain 保持 snake_case，不要因为某个 provider 的字段名而改动下游。

---

## 附：待协作决策清单（⏳ 均**未**冻结）

| 事项 | 状态 | 需要谁确认 |
| --- | --- | --- |
| `STREAM_END` 发送者 / 时机 / ownership | ⏳ PENDING | ASR / Audio 协作者 + 后端 |
| 真实 ASR Provider 接入（Tencent 等） | ⏳ V0.2 | ASR 协作者 + 后端 |
| Meeting-global ASR timeline | ⏳ V0.2 tech debt | ASR 协作者 + 后端 |
| ESP32 / PCM 音频上行 | ⏸ NOT IN SCOPE | 见 [`protocols/esp32-audio.md`](../protocols/esp32-audio.md) |

契约事实源： [`protocols/`](../protocols/README.md)
（`prd.md` > Accepted ADR > `docs/architecture.md` > `protocols/*.md` > `docs/api-contract.md` > 现有实现）
