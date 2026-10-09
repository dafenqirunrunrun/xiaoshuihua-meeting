# 小水华会议助手

企业内部 AI 会议记录工具。

核心链路：

1. 实时转写
ESP32/Audio
→ ASR
→ Backend Transcript
→ SSE
→ React

2. 会后纪要
Meeting FINALIZING
→ Transcript Freeze
→ LLM Summary
→ Result


# 开发前必读

按顺序：

1. prd.md
   产品需求、状态机、验收标准

2. docs/architecture.md
   系统架构与设计原则

3. docs/api-contract.md
   API / SSE / Transcript协议

4. protocols/
   ASR与实时事件协议

5. docs/decisions/
   ADR决策记录

6. docs/handoff.md
   当前系统边界与未完成事项

7. docs/progress.md
   开发进度


# 优先级

冲突时：

PRD
>
ADR / architecture
>
API contract / protocols
>
frontend design
>
implementation


# 技术栈

Frontend:
- React
- TypeScript
- Vite
- Zustand
- Ant Design
- Axios
- SSE


Backend:
- Python 3.11
- FastAPI
- SQLAlchemy
- Pydantic
- Uvicorn


LLM:
- Provider统一抽象
- 禁止前端调用模型
- 禁止泄露API Key


Database:
- MVP SQLite
- Repository隔离DB实现


# 核心不可破坏规则

## Transcript

1. Transcript 是唯一会议事实源。
2. Summary 是AI派生结果。
3. PARTIAL只用于实时展示，不落正式表。
4. FINAL才能持久化。
5. SSE不是事实源。


## Meeting State

状态机固定：

CREATED
→ IN_PROGRESS
→ FINALIZING
→ COMPLETED


禁止新增状态。

结束会议必须：

最后FINAL
+
STREAM_END
+
完整性确认

之后才能Freeze并调用LLM。


## Frontend

Frontend只是Consumer。

恢复流程：

GET Meeting
↓
GET Transcript Snapshot
↓
Subscribe SSE


禁止前端自行推导会议状态。


## Transcript Seq

segment_seq由Backend生成。

禁止使用ASR sentenceId。


## LLM

所有模型调用必须经过：

LLMProvider

禁止：
- 前端调用
- Fake fallback
- 日志输出密钥/完整模型结果


# SSE规范

新增SSE事件必须同步：

Backend:
SSE_EVENT_TYPES

Frontend:
types/sse.ts

Frontend:
utils/sse.ts


统一使用：

subscribeSseEnvelopes()


# 工程规范

- commit-before-publish
- 所有重试路径必须幂等
- 可选字段失败不能导致500
- MVP单进程，不支持多worker


# 测试

Backend:

pytest tests/


真实Provider测试：

scripts/replay_real_provider*.py


禁止：

CI调用真实付费模型。


# 常用命令

Backend:

cd backend

.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000


Test:

.venv/Scripts/python -m pytest tests/ -q


Frontend:

cd frontend

npm run dev

npm run typecheck

npm run build


# 当前开发原则

新增能力：

优先新增独立模块。

不要破坏已有：

ASR
→ Transcript
→ SSE
→ Summary

新增Agent默认采用旁路设计。

详细架构：
docs/architecture.md


# 文档入口

prd.md
          产品事实源

docs/architecture.md
          系统架构

docs/api-contract.md
          API协议

protocols/
          外部协议

docs/handoff.md
          当前状态

docs/progress.md
          开发记录