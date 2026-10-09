# 小水华会议助手（XiaoShuiHua Meeting Assistant）

企业内部 AI 会议记录工具。把**实时语音**变成**可追溯的会议事实**与**可核对的会议纪要**，
并在讨论出现事实性分歧时，让一个旁路的「水华队长」AI 裁判查证外部资料后给出带证据的判读。

> 本仓库同时包含后端、前端与 ESP32-S3 音频采集固件。

---

## 目录

- [它做什么](#它做什么)
- [架构总览](#架构总览)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [测试](#测试)
- [水华队长（Captain）：AI 裁判侧车](#水华队长captainai-裁判侧车)
- [配置与密钥](#配置与密钥)
- [项目结构](#项目结构)
- [文档索引](#文档索引)
- [开源许可](#开源许可)

---

## 它做什么

两条主链路，互不干扰：

1. **实时会议转写**
   `ESP32-S3 麦克风 → ASR（腾讯云实时语音识别）→ Backend Transcript → SSE → React 实时字幕`
2. **会后 AI 会议纪要**
   `会议 FINALIZING → Transcript Freeze → LLM 摘要 → 结果落盘`

在此之上，**水华队长（Captain）** 是一个按需触发的旁路 Agent：

- 用唤醒词（默认「水华队长」）或按钮触发一次裁判；
- 先给 `Quick` 第一段判断（1–2 秒），再给 `Deep` 完整推理；
- 若讨论中出现**可核对的事实分歧**，会按规则策略去外部检索资料（Web Search），
  把外部证据与会议转写证据**严格分层**后交给模型；
- 外部检索失败或超时**永远不会**导致报告失败——它只是「增强」，不是前置依赖。

---

## 架构总览

```text
┌──────────── 上游（语音系统）────────────┐
│ Audio → ASR → Transcript Event          │
└──────────────────┬───────────────────────┘
                   │ HTTP / Internal Contract
                   ▼
┌──────────── 当前系统（FastAPI）──────────┐
│  Transcript Ingress                      │
│   ├─ live_utterance_cache  (PARTIAL,内存)│
│   └─ transcript_segment    (FINAL,持久化)│
│  Meeting State Machine (4 态)            │
│  SSE Broadcast                           │
│  Summary Pipeline   (会后纪要)           │
│  Captain Sidecar    (旁路 AI 裁判)       │
└──────────────────┬───────────────────────┘
                   │ Snapshot + SSE
                   ▼
            React Frontend（纯 Consumer）
```

核心原则：

- **Backend 是 Transcript 与 Meeting State 的唯一事实源**；Frontend 只是消费者。
  前端断网 / 刷新 / 关浏览器 / SSE 断开，都不影响后端继续接收、持久化与生成纪要。
- **Transcript 是唯一会议事实源**，Summary 是 AI 派生结果，可重新生成，不反向改动 Transcript。
- **Meeting 状态机固定 4 态**：`CREATED → IN_PROGRESS → FINALIZING → COMPLETED`，禁止扩展。
- **所有模型调用必须经过统一的 LLMProvider 抽象**；禁止前端直接调用模型，禁止泄露密钥。

---

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | React 18 · TypeScript · Vite · Zustand · Ant Design · Axios · SSE |
| 后端 | Python 3.11+ · FastAPI · SQLAlchemy · Pydantic · Uvicorn |
| 数据库 | SQLite（MVP 单进程） |
| LLM | Provider 统一抽象（离线 `fake` / 真实 DeepSeek 兼容网关） |
| 外部检索 | 可插拔 Web Search Provider（`fake` / Brave） |
| 固件 | ESP32-S3 · ESP-IDF |

---

## 快速开始

前置：Python 3.11+、Node.js 18+。默认使用**离线 FakeProvider**，无需任何真实密钥即可跑通全链路。

### 1. 后端

```bash
cd backend

# 创建虚拟环境
python3 -m venv .venv                 # macOS / Linux
# python -m venv .venv                # Windows

source .venv/bin/activate             # macOS / Linux
# .venv\Scripts\activate              # Windows (PowerShell)

pip install -r requirements.txt

# 复制配置模板（默认 LLM_PROVIDER=fake，无需真实 Key）
cp .env.example .env

# 启动
python -m uvicorn app.main:app --reload --port 8000
```

启动后：

- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/health>

### 2. 前端

```bash
cd frontend
npm install
npm run dev                           # :5173，proxy /api -> :8000
```

打开 <http://127.0.0.1:5173>。开发环境通过 Vite proxy 把 `/api` 转发到
`http://127.0.0.1:8000`，无需额外配置。

### 3. 固件（可选）

固件包含在本仓库中（`firmware/esp32-s3/`），用于 ESP32-S3 的音频采集与上传。
没有硬件时，可用后端的「模拟参会人发言」入口（`app/demo/`）或
`backend/scripts/mock_transcript_producer.py` 驱动整条链路。

固件构建需要 ESP-IDF 环境，见 [`firmware/README.md`](firmware/README.md)。

---

## 测试

### 后端

```bash
cd backend
pytest tests/ -q
```

离线、确定性：测试套件通过 `tests/conftest.py` 固定 `LLM_PROVIDER=fake` 与
`CAPTAIN_TWO_STAGE_ENABLED=false`，不会访问真实模型或搜索引擎。

需要真实模型的回放脚本（**会发起付费调用，CI 绝不运行**）位于 `backend/scripts/`：

```bash
python scripts/replay_real_referee.py        # 真实裁判端到端回放
python scripts/smoke_two_stage_research.py   # 两阶段 + 外部检索 5 例冒烟
```

### 前端

```bash
cd frontend
npm run typecheck      # tsc --noEmit
npm run build          # 类型检查 + 生产构建
npm run test:hooks     # 钩子单元测试（node:test）
```

---

## 水华队长（Captain）：AI 裁判侧车

Captain 是一个**按需触发的旁路 Agent**，与主链路只共享同一份只读的 FINAL 转写事实源，
状态独立、互不派生。设计详见 [`docs/decisions/`](docs/decisions/) 下的 ADR-002 / ADR-003。

- **两阶段**：`Quick`（1–2 秒的第一段判断，字段由 schema 钉死）
  → `Deep`（完整推理）。Quick 失败会**降级**为「没有第一段」，绝不拖垮 Deep。
- **外部证据检索排在 Quick 之后**：检索是一个延迟调用的 thunk，在 Quick 交付客户端
  *之后*才执行。Quick 的 1–2 秒目标因此**按构造**不含网络请求。
- **检索策略是纯规则，不是第二次 LLM 调用**：判定「该不该查、问什么」，并在问题
  答案存在于公司内部时**拒绝检索**。
- **两份证据严格分层、结构上不可混淆**：转写证据带 `segment_seq` 且逐字校验；
  外部证据没有 `segment_seq`，由流水线从 provider 结果构造，模型**无法伪造**来源编号。
- **降级总是披露，从不静默**：检索超时 / 失败会折进报告的 `uncertainty`。

> 关键不变量：**任何检索失败（超时 / provider 错误 / 畸形响应）都不会让报告失败**，
> 只会让它缺少外部证据。

---

## 配置与密钥

**永远不要提交真实密钥。** 仓库通过 `.gitignore` 忽略所有 `.env`：

```gitignore
.env
.env.*
!**/.env.example
```

模板：

- [`backend/.env.example`](backend/.env.example) —— LLM / ASR / 检索配置
- [`frontend/.env.example`](frontend/.env.example) —— 前端 API 地址

常用开关（完整说明见模板内注释）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `fake` | `fake` 离线确定性 / `deepseek` 真实模型 |
| `CAPTAIN_TWO_STAGE_ENABLED` | `false` | 打开两阶段裁判（Quick + Deep） |
| `CAPTAIN_EXTERNAL_SEARCH_ENABLED` | `true` | 外部检索总开关 |
| `CAPTAIN_SEARCH_PROVIDER` | `fake` | 检索提供方（未知值 fail-closed） |
| `BRAVE_SEARCH_API_KEY` | 空 | Brave Search 密钥；空则每次检索如实降级 |

真实模型 / ASR / 检索的密钥只放在**本机** `backend/.env`，不进版本库。

---

## 项目结构

```text
.
├── backend/            FastAPI 后端
│   ├── app/
│   │   ├── captain/        水华队长：AI 裁判侧车（含 search/ 外部检索）
│   │   ├── transcript/     转写事实源
│   │   ├── meeting/        会议与状态机
│   │   ├── meeting_state/  会中状态理解
│   │   ├── agent/          LLM Provider 抽象与摘要管线
│   │   ├── realtime/       SSE 广播
│   │   ├── speech/         ASR 接入（腾讯云）
│   │   └── demo/           模拟参会人发言（无硬件演示用）
│   ├── tests/          pytest 测试套件
│   └── scripts/        回放 / 冒烟 / 运维脚本
├── frontend/           React + Vite 前端
│   └── src/
│       ├── components/     面板与可视化组件
│       ├── hooks/          数据钩子
│       ├── api/            API 客户端
│       └── types/          类型定义
├── firmware/           ESP32-S3 音频采集固件
├── docs/               架构、API 契约、ADR、进度
├── protocols/          跨模块通信契约事实源
└── prd.md              产品需求文档
```

---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`CLAUDE.md`](CLAUDE.md) | 项目入口、核心不变式、开发规范 |
| [`prd.md`](prd.md) | 产品需求、状态机、验收标准 |
| [`docs/architecture.md`](docs/architecture.md) | 已定架构 |
| [`docs/api-contract.md`](docs/api-contract.md) | API / SSE / Transcript 契约 |
| [`docs/decisions/`](docs/decisions/) | ADR 决策记录 |
| [`protocols/`](protocols/) | 跨模块通信契约 |

冲突时的优先级：`prd.md` > Accepted ADR > `docs/architecture.md` >
`protocols/*.md` > `docs/api-contract.md` > 现有实现。

---

## 开源许可

本项目基于 [MIT License](LICENSE) 开源。
