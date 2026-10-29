# harbor-purchase-agent

Harbor 采购 Agent —— 最小可跑的电商购物 Agent（GlobeX 复刻的 v0 垂直切片）。

买家说一句「预算300元，买个背包寄到中国」，Agent 走 ReAct 循环检索商品，
返回带价格/币种的 SKU 推荐；工具重复调用时能自己收敛。

技术栈：AgentScope 2.x + Python FastAPI + React。

## 当前进度

| 阶段 / 里程碑 | 状态 |
|---|---|
| Phase 1 · 仓库骨架（配置、健康检查、契约生成、前端占位页） | ✅ 本轮完成 |
| M1 检索闭环（ReAct 循环 + product_search + 本地 catalog） | ⏳ 待做 |
| M2 护栏（loop detector + schema 断言） | ⏳ 待做 |
| M3 落盘 + golden case 评测 | ⏳ 待做 |
| M4 LLM planner（AgentScope Function Calling） | ⏳ 待做 |

Phase 1 **只包含骨架**：可运行、可测试、前后端连通、配置与契约就位。
**不包含** AgentScope 依赖、Agent 实现、`/api/chat/stream`、SSE、商品检索。
这些从 Phase 2 开始，接入方案见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

前置：Python 3.11+（由 uv 管理）、Node.js 22.12+、`uv`、`npm`、`make`。

```bash
make setup     # 安装后端/前端依赖，生成 backend/.env
make dev       # 同时启动 API (8010) 与 Web (5173)
```

也可以分两个终端启动：

```bash
make dev-api   # http://127.0.0.1:8010/docs
make dev-web   # http://localhost:5173
```

打开 <http://localhost:5173>，页面会显示后端健康状态。首次运行
`make setup` 会从 `backend/.env.example` 复制出 `backend/.env`；
模型 key 默认留空，健康检查显示「LLM 未配置」即可。

## 常用命令

| 命令 | 说明 |
|---|---|
| `make setup` | 安装依赖并生成本地 `.env` |
| `make dev` | 并行启动前后端（Ctrl-C 统一退出） |
| `make dev-api` | 仅启动后端，开发模式下自动重载 |
| `make dev-web` | 仅启动前端 |
| `make test` | 运行后端 pytest |
| `make lint` | ruff 检查与格式检查 |
| `make format` | ruff 自动修复与格式化 |
| `make typecheck` | mypy + TypeScript 类型检查 |
| `make gen-api` | 导出 OpenAPI 并生成前端 TS 类型 |
| `make check` | lint + typecheck + test |
| `make clean` | 清理缓存与前端构建产物 |
| `make distclean` | 连虚拟环境和 node_modules 一起删除 |

## 配置

后端配置全部来自环境变量或 `backend/.env`（模板见
[backend/.env.example](backend/.env.example)）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HARBOR_APP_ENV` | `development` | `development` / `test` / `production` |
| `HARBOR_HOST` | `127.0.0.1` | 监听地址 |
| `HARBOR_PORT` | `8010` | 监听端口 |
| `HARBOR_LOG_LEVEL` | `INFO` | 日志级别 |
| `HARBOR_CORS_ORIGINS` | `["http://localhost:5173","http://127.0.0.1:5173"]` | JSON 数组 |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `OPENAI_API_KEY` | 空 | 模型 key，Phase 1 不调用 |
| `OPENAI_MODEL` | `deepseek-chat` | 模型名，Phase 1 不调用 |

`OPENAI_*` 也兼容 `HARBOR_OPENAI_*` 写法。key 使用 `SecretStr` 保存，
健康检查只暴露 `llm_configured`，不会返回 key。`backend/.env` 已被 git 忽略。

开发期前端通过 Vite 代理访问 `/api`，默认目标是 `http://127.0.0.1:8010`。
如果修改了 `HARBOR_PORT`，请同步设置 `VITE_API_PROXY_TARGET`（例如写在
`frontend/.env.local`），否则页面会请求错误的端口。

## 目录

```
.
├── Makefile                 开发命令入口
├── scripts/dev.sh           并行启动前后端
├── docs/
│   ├── architecture.md      分层规则与路线图
│   ├── api-contract.md      REST 与未来 SSE 契约
│   └── adr/                 架构决策记录
├── backend/                 FastAPI（uv 管理，自包含）
│   ├── src/harbor/
│   │   ├── main.py          create_app() 组装点
│   │   ├── core/            config / logging / errors
│   │   ├── features/        health、catalog（模型 + persistence）、chat（占位）
│   │   └── agents/          Phase 2 Agent 层（占位）
│   ├── scripts/             OpenAPI 导出
│   └── tests/               配置、健康检查、catalog 模型与持久化测试
└── frontend/                Vite + React + TS + Tailwind
    ├── openapi.json         由后端导出（生成物）
    └── src/
        ├── lib/api/         openapi-fetch 客户端与生成类型
        └── features/        health（已实现）、chat（占位）
```

## 架构与契约

- 后端按**功能纵切**组织，`main.py` 是唯一组装点。详见
  [docs/architecture.md](docs/architecture.md) 与
  [ADR-0001](docs/adr/0001-vertical-slice-deferred-agentscope.md)。
- REST 契约由 FastAPI OpenAPI 自动生成前端类型；SSE 事件联合类型已先定义。
  详见 [docs/api-contract.md](docs/api-contract.md)。
- `frontend/openapi.json` 与 `frontend/src/lib/api/schema.d.ts` 是生成物，
  修改后端 schema 后运行 `make gen-api`，不要手改。

## 下一阶段

Phase 2 才会引入 `agentscope==2.0.11`，装配采购 Agent，实现
`POST /api/chat/stream`、会话存储、`product_search` 工具与护栏。
现有目录边界与配置字段已为接入预留。
