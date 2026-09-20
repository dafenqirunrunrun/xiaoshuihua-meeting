# 小水华会议助手 MVP V0.1

企业内部 AI 会议记录工具。

> **协作者首次拉取 `develop`：见 [`docs/handoff.md`](docs/handoff.md)。**
> 里面包含启动命令、Provider 两种模式（离线 FakeProvider / 真实 DeepSeek）、
> 验证脚本、系统边界与当前待协作决策。

---

## 两条主链路

1. 实时会议转写（ASR → Backend → SSE → React 实时字幕）
2. 会后 AI 会议纪要（FINALIZING → Transcript Freeze → LLM → Result）

## 快速开始

```bash
# 后端
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env                      # 默认 LLM_PROVIDER=fake，无需真实 Key
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev                               # :5173，proxy /api -> :8000
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`docs/handoff.md`](docs/handoff.md) | **交接 / onboarding（先读这个）** |
| [`CLAUDE.md`](CLAUDE.md) | 项目入口、核心不变式、Step 进度 |
| [`prd.md`](prd.md) | 产品 / 技术契约 / 状态机 / 验收 |
| [`docs/architecture.md`](docs/architecture.md) | 已定架构 |
| [`docs/api-contract.md`](docs/api-contract.md) | API / SSE / Transcript 契约 |
| [`protocols/`](protocols/README.md) | 跨模块通信契约事实源（ASR / SSE / 会议 API） |

优先级：`prd.md` > Accepted ADR > `docs/architecture.md` > `protocols/*.md` >
`docs/api-contract.md` > 现有实现。
