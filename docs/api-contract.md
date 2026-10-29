# API 契约

## REST

### `GET /api/health`

响应 `200 application/json`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `"ok"` | 固定值 |
| `service` | string | 应用名，默认 `harbor-purchase-agent` |
| `version` | string | 应用版本 |
| `environment` | string | `development` / `test` / `production` |
| `llm_configured` | boolean | 是否配置了模型 API key；**不返回 key 本身** |
| `time` | string (ISO 8601) | 服务端 UTC 时间 |

示例：

```json
{
  "status": "ok",
  "service": "harbor-purchase-agent",
  "version": "0.1.0",
  "environment": "development",
  "llm_configured": false,
  "time": "2026-10-29T12:00:00.000000Z"
}
```

### 错误信封

所有 HTTP 错误使用统一结构：

```json
{
  "error": {
    "code": "not_found",
    "message": "Not Found"
  }
}
```

`code` 规则：404 为 `not_found`，其余为 `http_<status>`；未捕获异常为
`internal_error`，响应体不包含堆栈。

### 元数据端点

- `GET /openapi.json`：OpenAPI 3.1 schema。
- `GET /docs`：Swagger UI。
- ReDoc 已关闭。

## 契约生成链路

```bash
make gen-api
```

执行两步：

1. `backend/scripts/export_openapi.py`：用 `create_app(Settings(_env_file=None,
   app_env="test"))` 生成 schema，写入 `frontend/openapi.json`（键排序，稳定输出）。
2. `openapi-typescript openapi.json -o src/lib/api/schema.d.ts`：生成 TS 类型。

`frontend/openapi.json` 与 `frontend/src/lib/api/schema.d.ts` 都是**生成物**，
不要手改。修改后端路由/schema 后重新执行 `make gen-api`。

## SSE 契约（Phase 2 实现，Phase 1 仅文档化）

### 请求

```
POST /api/chat/stream
Content-Type: application/json

{"session_id": "demo", "message": "预算300元，买个背包寄到中国"}
```

响应 `Content-Type: text/event-stream`。

### 帧格式

- 每个事件一帧：`data: <json>\n\n`。
- 心跳：`: ping\n\n`，用于保持连接。
- 不使用 SSE 的 `event:` 字段；统一用 JSON 内的 `type` 判别。
- 因为请求是 POST，浏览器不能用 `EventSource`；前端用 `fetch` +
  `ReadableStream` 解析。

### 事件联合

公共字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 事件判别字段 |
| `seq` | integer | 从 1 单调递增 |
| `data` | object | 事件负载 |

事件类型：

| `type` | `data` | 说明 |
|---|---|---|
| `reply_start` | `{}` | 一次回复开始 |
| `text_delta` | `{"text": string}` | 增量文本，前端追加 |
| `tool_call_start` | `{"id": string, "name": string, "arguments": object}` | 工具调用开始 |
| `tool_result` | `{"id": string, "name": string, "ok": boolean, "content": string}` | 工具结果 |
| `reply_end` | `{"stop_reason": "completed" \| "max_iters" \| "error"}` | 回复结束 |
| `error` | `{"code": string, "message": string}` | 流内错误 |

示例：

```
data: {"type":"reply_start","seq":1,"data":{}}

data: {"type":"text_delta","seq":2,"data":{"text":"我先检索"}}

data: {"type":"tool_call_start","seq":3,"data":{"id":"call_1","name":"product_search","arguments":{"budget":300}}}

data: {"type":"tool_result","seq":4,"data":{"id":"call_1","name":"product_search","ok":true,"content":"..."}}

data: {"type":"reply_end","seq":5,"data":{"stop_reason":"completed"}}
```

### AgentScope 事件映射

适配器放在 `backend/src/harbor/features/chat/service.py`：

| AgentScope `EventType` | 契约事件 |
|---|---|
| `TEXT_BLOCK_DELTA` | `text_delta` |
| `TOOL_CALL_START` | `tool_call_start` |
| `TOOL_RESULT_*` | `tool_result` |
| `REPLY_END` | `reply_end` |

`exceed_max_iters` 映射为 `stop_reason: "max_iters"`，异常路径映射为
`stop_reason: "error"` 并补一帧 `error`。
