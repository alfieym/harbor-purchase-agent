# S14 · AG-UI 传输 + 前端

**目标**：把一次 run 从「HTTP 请求的副产品」变成有自己生命周期的对象。

## 决策

| 决策点 | 结论 |
|---|---|
| 协议 | 严格对齐 AG-UI（CopilotKit）事件形状：`RUN_STARTED` / `STEP_*` / `TEXT_MESSAGE_*` / `TOOL_CALL_*` / `STATE_SNAPSHOT` / `STATE_DELTA` / `RUN_FINISHED` / `RUN_ERROR` / `CUSTOM`。JSON 一律 camelCase。 |
| 传输 | SSE（`text/event-stream`）。`POST /agui/runs` 建 run；`GET /agui/runs/{id}/events` 读流。每帧带 `id: <seq>`，支持 `Last-Event-ID` / `after` 续传。 |
| 持久化 | SQLite（`data/sessions/runs.sqlite3`），两张表：`runs`、`run_events(run_id, seq)`。**先落库拿到 seq，再广播**。 |
| 生命周期 | 进程内 `asyncio.Task` 持有 run。HTTP 断开只移除一个订阅者，不取消任务。 |
| 取消 | 显式 `POST /agui/runs/{id}/cancel`。取消后落 `RUN_FINISHED`（outcome interrupt，status=cancelled）。 |
| 刷新重建 | 前端保存 `runId`，重连时从 `after=0` 回放全部持久事件，reducer 重建消息、工具调用与商品卡。 |
| 商品卡 | 只从 `STATE_SNAPSHOT`（`CommerceIntentResponse.cards` 的结构化 dump）渲染，前端不解析文本。 |
| 进程重启 | 启动时把遗留 `running` 标记为 `interrupted` 并补一条 `RUN_ERROR`，回放有终态。 |

## 后端结构

```
features/runs/
├── agui.py      # AG-UI 事件构造函数（camelCase dict）
├── schemas.py   # StartRequest / Snapshot（pydantic）
├── store.py     # SqliteRunStore：runs + run_events，seq 分配
├── service.py   # RunManager：start / cancel / events / startup / shutdown
└── router.py    # /agui/runs ...
```

`RunManager` 通过构造参数注入 `turn_runner`，因此 `features/runs` 不 import
`features/chat`；组装在 `main.py` 完成。`run_turn` 新增可选
`on_tool_results` 回调，把本轮 tool result 交给 run 层，后者翻译成
`TOOL_CALL_*` 事件。

## 事件顺序（一次成功 run）

```
RUN_STARTED
STEP_STARTED(agent)
TOOL_CALL_START / ARGS / END / RESULT   (每个工具结果)
TEXT_MESSAGE_START / CONTENT / END
STATE_SNAPSHOT {cards, pendingDraft, status}
STEP_FINISHED
RUN_FINISHED
```

失败：`RUN_ERROR`（稳定 code，不带内部异常文本）。取消：`RUN_FINISHED`
+ `result.status = "cancelled"`。

## 前端结构

```
src/features/chat/
├── types.ts        # AG-UI 事件联合类型、ChatState
├── reducer.ts      # 纯函数 reduceEvent(state, event)
├── sse.ts          # SSE 帧解析（可单测）
├── api.ts          # startRun / cancelRun / streamRunEvents
├── useChatRun.ts   # 刷新回放、断线重连、取消
├── ChatPanel.tsx
├── ProductCard.tsx
└── Composer.tsx
```

## 非目标

- 不做 token 级流式（AgentScope 的 `reply_stream` 与结构化输出/grounding
  的整合留到后续）；文本以单个 `TEXT_MESSAGE_CONTENT` 发送。
- 不做 run 列表分页/长期保留策略。
- 不改现有 `/commerce/intents` 同步接口。
