# 会议接口协议（Meeting API）

> 状态：**✅ FROZEN**（ASR-0B，2026-09-19）
> 适用：前端 ↔ 后端
> 对应实现：`backend/app/meeting/`、`frontend/src/api/meeting.ts`

---

## 1. 状态机 ✅ FROZEN

**只有四态**，不得新增：

```text
CREATED → IN_PROGRESS → FINALIZING → COMPLETED
```

- **禁止** `FINALIZING_FAILED` / `FAILED` / `PAUSED`。
- Summary 失败用 `summary.status = FAILED` 表达，**Meeting 仍为 COMPLETED**。
- **没有 Pause/Resume**：离开页面 = 纯导航，Meeting 从不暂停。
  `POST /resume` **不存在**（Resume 的唯一入口是 `GET /meetings` + 进入房间）。

---

## 2. 生命周期接口 ✅ FROZEN

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `POST` | `/api/meetings` | 创建 → `CREATED`。仅 `title` + 可选 `description` |
| `GET` | `/api/meetings` | 列表（discovery / resume 唯一入口），支持 `status?/limit?/offset?` |
| `GET` | `/api/meetings/{id}` | 单个会议 |
| `POST` | `/api/meetings/{id}/start` | `CREATED → IN_PROGRESS`，记 `started_at` |
| `POST` | `/api/meetings/{id}/finish` | `IN_PROGRESS → FINALIZING`，记 `ended_at` |
| `GET` | `/api/meetings/{id}/transcript` | Transcript Snapshot（**仅 FINAL**，ASC） |
| `GET` | `/api/meetings/{id}/events` | SSE 增量流（见 realtime-events.md） |
| `GET` | `/api/meetings/{id}/summary` | 最新 Summary 快照（durable 恢复事实源） |
| `POST` | `/api/meetings/{id}/summary/regenerate` | `202`，分配 vN+1 |

### 幂等/冲突 ✅ FROZEN

- `start`：已 `IN_PROGRESS` → 返回当前，**不**改 `started_at`；
  `FINALIZING`/`COMPLETED` → **409**。
- `finish`：已 `FINALIZING` → 幂等返回；已 `COMPLETED` → 幂等 no-op；`CREATED` → **409**。
- 不存在 → **404**；非法 `status`/`limit`/`offset` → **422**。

---

## 3. 关键不变式 ✅ FROZEN

1. **Transcript 是会议唯一事实源**；Summary 是 AI 派生结果。
2. **PARTIAL 只做实时展示**，绝不写 `transcript_segment` 表。
3. **FINAL 先持久化成功，才广播 SSE**；SSE 失败不回滚已提交事务。
4. **结束会议必须进入 FINALIZING**；必须等最后 FINAL + `TRANSCRIPT_STREAM_END` +
   完整性确认后才 Freeze，Freeze 后才允许调 LLM。**禁止固定 `sleep` 等尾包**。
5. **Meeting 完成只有一条路径**：Summary 终态（COMPLETED / FAILED）之后由
   Finalizer 驱动 `FINALIZING → COMPLETED`。路由层永不直接写 `status=COMPLETED`。
6. **Regenerate 永不把 Meeting 移出 COMPLETED**；版本 append-only（v1→v2→v3），
   前端绝不自行 `version++`、不猜版本号。

---

## 4. `GET /meetings` 列表 ✅ FROZEN

- Query：`status`（四态之一，非法 → **422**）、`limit`（1..100，默认 20）、
  `offset`（`>= 0`，默认 0）。
- 排序：`updated_at DESC, created_at DESC`。
- 响应含 `items` / `total` / `limit` / `offset`；`total` 为满足过滤条件的总数。
- 路由顺序：`GET /meetings` 必须先于 `GET /meetings/{id}` 注册。
- **无 user isolation**（MVP）。

---

## 5. Summary 契约 ✅ FROZEN

`GET /summary` 响应字段：`id / meeting_id / version / status / overview / key_topics /
conclusions / open_questions / model_name / raw_model_output / created_at`。

- meeting 不存在 → **404 "meeting not found"**；无 summary → **404 "summary not found"**。
- `status` ∈ `GENERATING | COMPLETED | FAILED`（前端另有 `NOT_STARTED` 展示态）。
- 是前端重连的 **durable 恢复事实源**（Summary SSE 非 durable、不回放）。

---

## 6. 展示注意（前端）

- **浏览器不是录音源时，禁止做假的麦克风选择器**（MVP 不接真实音频）。
- 前端恢复模型固定：`GET Meeting → GET Transcript Snapshot → 订阅 SSE`。
- 绝不把 SSE payload 直接当事实渲染（尤其 `summary.*` 只有 `{version,status}`）。
