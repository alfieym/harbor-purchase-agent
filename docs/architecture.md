# 架构

## 现状（Phase 1）

仓库只实现 M0 等价骨架：FastAPI 应用工厂、配置、健康检查、统一错误信封、
前端占位页与 OpenAPI 类型生成。**未安装 AgentScope，未实现聊天/SSE/商品检索。**

## 目录与依赖规则

后端按**功能纵切（vertical slice）**组织：

```
backend/src/harbor/
├── main.py                 唯一组装点：创建 app、挂中间件、include_router
├── core/                   共享基础设施
│   ├── config.py           Settings（pydantic-settings）
│   ├── logging.py          日志初始化
│   └── errors.py           统一错误信封
├── features/               每个功能自包含
│   ├── health/             router.py + schemas.py（当前唯一样例）
│   ├── chat/               Phase 2：/api/chat/stream + SSE
│   └── catalog/            商品模型、仓储端口、persistence/ 适配器
└── agents/                 Phase 2：prompts / tools / guardrails
```

规则：

1. `features/<name>/` 自带 `router.py`、`schemas.py`，需要时再加 `service.py`、
   `repo.py`、`models/`（领域模型较多时按类拆成独立文件，用 `__init__.py`
   统一 re-export）。功能之间不直接 import 对方的内部模块。
2. `core/` 只放配置、日志、错误等基础设施，**不得 import `features/`**。
3. `agents/` 是横切层，可以调用 feature service；**feature 不得反向依赖
   `agents/`**。
4. `main.py` 是唯一组装点：新增 feature 时只加一行 `include_router`。
5. 前端同样纵切：`src/features/<name>/`，共享组件放 `components/`，
   生成物放 `lib/api/`。
6. 需要持久化适配器时，在 feature 下建 `persistence/` 子包；领域端口（如
   `catalog/repo.py`）留在 feature 根，`persistence/` 只放 seed、内存或文件
   实现。

选择纵切而非 README 原先的 DDD/六边形分层，理由见
[ADR-0001](adr/0001-vertical-slice-deferred-agentscope.md)。

## 配置

所有配置经 `harbor.core.config.Settings` 读取：

- 通用配置使用 `HARBOR_` 前缀（`HARBOR_PORT`、`HARBOR_CORS_ORIGINS` 等）。
- 模型配置接受标准 `OPENAI_*`，也兼容 `HARBOR_OPENAI_*`。
- `OPENAI_API_KEY` 用 `SecretStr` 保存，健康检查只返回 `llm_configured` 布尔值。
- `.env` 只存在于 `backend/`，被 git 忽略；模板是 `backend/.env.example`。

## 前后端契约

- REST：FastAPI 生成 `/openapi.json`，`make gen-api` 导出到
  `frontend/openapi.json` 并生成 `frontend/src/lib/api/schema.d.ts`。
  `openapi-fetch` 客户端据此获得路径与响应类型。
- SSE：Phase 1 只在 [api-contract.md](api-contract.md) 定义事件联合类型，
  不实现服务端。

## 路线图

| 阶段 | 内容 |
|---|---|
| P1（当前） | M0 骨架：配置、健康检查、错误信封、前端占位页、契约生成链路 |
| P2 | 安装 `agentscope==2.0.11`；装配采购 Agent；实现 `/api/chat/stream`；会话状态；prompts / tools / guardrails；M1 / M2 / M4 |
| P3 | 检索质量、M3 落盘、golden case 评测 |

README 原先的「规则版 planner」在 AgentScope 2.x 下不再成立：M1 起就是 LLM
Agent。离线测试与 CI 需要 fake model 或录制回放，不能依赖真实 API。

## AgentScope 2.0.11 事实清单

以下能力已核对 2.0.11 文档，Phase 1 **未安装、未使用**：

- `agentscope.agent.Agent`；`agent.reply(msg)` 与 `agent.reply_stream(msg)`。
- `reply_stream` 产出 `AgentEvent`，事件类型包括 `TEXT_BLOCK_DELTA`、
  `TOOL_CALL_START`、`TOOL_RESULT_*`、`REPLY_START` / `REPLY_END`。
- `Toolkit`、`FunctionTool`、`ToolBase` 注册工具。
- `MiddlewareBase` / `ToolMiddlewareBase.on_tool_call` 做重复工具调用检测。
- `ReActConfig(max_iters=...)` 作为推理-行动循环硬上限。
- `AgentState` + `RedisStorage` 支持会话持久化；`structured_schema` 约束输出。
- 模型类以 credential + model 构造；OpenAI 兼容端点是否支持 `base_url` 需在
  接入时确认 `OpenAICredential`，本阶段的 `OPENAI_BASE_URL` 配置保持不变。
