# 2026-10-29 · Phase 1 仓库骨架实现记录

- 项目：`harbor-purchase-agent`
- 阶段：Phase 1（仓库骨架与配置）
- 状态：已完成，待验收；本轮**未提交 git**
- 技术栈：AgentScope 2.x（Phase 2 接入）+ Python FastAPI + React

## 1. 本轮目标

把「预算 300 元，买个背包寄到中国」的采购 Agent 落到一个可运行、可测试、
前后端连通的工程骨架上，先固定四件事：

1. 目录边界（功能纵切）。
2. 配置来源（环境变量 + pydantic-settings）。
3. 前后端契约（OpenAPI 自动生成 TypeScript 类型；SSE 契约先文档化）。
4. 开发命令（Makefile + 一键脚本）。

## 2. 范围

| 包含 | 不包含 |
|---|---|
| FastAPI 应用工厂、健康检查、统一错误信封 | AgentScope 依赖与 Agent 实例化 |
| 配置加载、密钥保护、CORS | `/api/chat/stream` 与 SSE 服务端实现 |
| React + Vite + TypeScript + Tailwind 占位页 | 商品检索、catalog 数据与 `product_search` 工具 |
| 健康状态卡片（成功 / 失败 / 重试） | ReAct 循环、护栏、评测执行器 |
| OpenAPI → 前端类型生成链路 | Docker、CI、pre-commit、鉴权、数据库、Redis |
| Makefile、`scripts/dev.sh`、测试与质量门禁 | AgentRuntime / Agent 工厂等过早抽象 |

## 3. 关键决策

| 决策点 | 结论 |
|---|---|
| 阶段范围 | 纯骨架，不接 Agent，不安装 AgentScope |
| 后端目录 | 功能纵切：`features/{health,chat,catalog}` + `core/` + `agents/` |
| AgentScope 集成 | 整体推迟到 Phase 2 再定（进程内 Agent  vs Agent Service） |
| 模型配置 | OpenAI 兼容通用配置：`base_url + api_key + model`，默认 DeepSeek，仅占位不调用 |
| 前端 | Vite + React + TypeScript + Tailwind v4，极简占位页 + 健康状态卡片 |
| API 契约 | REST 走 OpenAPI 生成 TS 类型；SSE 事件联合类型先写入文档 |
| 开发基建 | Makefile + `scripts/dev.sh`；不做 Docker / CI / pre-commit |
| 包管理器 | 后端 uv；前端 npm（本机 pnpm corepack 缓存损坏，不做全局修复） |

## 4. 环境事实与端口偏差

已验证的环境：

- 系统 `python3` 为 3.9.6；uv 管理 Python 3.11.16，后端声明 `>=3.11,<3.14` 并用
  `.python-version` 固定 3.11。
- 直连 `pypi.org` 不通，`mirrors.aliyun.com/pypi` 可用；Makefile 默认注入
  `UV_DEFAULT_INDEX`，可覆盖。
- npm 使用 `registry.npmmirror.com`，可用。
- `openapi-typescript@7.13` 的 peer 要求 TypeScript `^5.x`，因此 TS 固定为
  `~5.9.3`，不跟随 latest（7.x）。

**端口偏差**：本机 `127.0.0.1:8000` 已被另一个 `ccielts` FastAPI 服务占用，
按原计划的 8000 会导致 `make dev` 报 `address already in use`。因此：

- 后端默认端口改为 **8010**（`HARBOR_PORT`）。
- Vite 代理默认指向 `http://127.0.0.1:8010`。
- 如修改 `HARBOR_PORT`，需同步设置 `VITE_API_PROXY_TARGET`。
- 前端开发地址仍为 `http://localhost:5173`。

## 5. 最终目录结构

```text
harbor-purchase-agent/
├── 2026-10-29-phase1-skeleton.md   # 本记录
├── .gitignore
├── Makefile
├── README.md
├── scripts/
│   └── dev.sh
├── docs/
│   ├── architecture.md
│   ├── api-contract.md
│   └── adr/0001-vertical-slice-deferred-agentscope.md
├── backend/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── .python-version
│   ├── .env.example
│   ├── src/harbor/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── main.py
│   │   ├── core/
│   │   │   ├── __init__.py
│   │   │   ├── config.py
│   │   │   ├── logging.py
│   │   │   └── errors.py
│   │   ├── features/
│   │   │   ├── __init__.py
│   │   │   ├── health/{__init__.py,router.py,schemas.py}
│   │   │   ├── chat/README.md
│   │   │   └── catalog/README.md
│   │   └── agents/README.md
│   ├── scripts/export_openapi.py
│   └── tests/{conftest.py,test_health.py,test_config.py}
└── frontend/
    ├── package.json
    ├── package-lock.json
    ├── index.html
    ├── vite.config.ts
    ├── tsconfig.json
    ├── tsconfig.app.json
    ├── tsconfig.node.json
    ├── openapi.json
    ├── public/favicon.svg
    └── src/
        ├── main.tsx
        ├── vite-env.d.ts
        ├── App.tsx
        ├── styles.css
        ├── components/ui/Card.tsx
        ├── features/health/HealthCard.tsx
        ├── features/chat/README.md
        └── lib/api/{client.ts,schema.d.ts}
```

## 6. 文件清单

本轮新增/改写 **47 个纳入版本管理的文件**（不含本记录本身）。其中 7 个是工具
生成的锁文件或契约产物，其余为手写或基于脚手架改写。

### 6.1 工程入口层

| 层次 | 文件 | 作用 | 来源 |
|---|---|---|---|
| 工程入口 | `.gitignore` | 忽略 `.env`、虚拟环境、`node_modules`、缓存和 `dist`；保留锁文件与契约生成物 | 手写 |
| 工程入口 | `Makefile` | 统一开发命令；默认 PyPI 镜像；GNU Make 3.81 兼容 | 手写 |
| 工程入口 | `scripts/dev.sh` | 并行启动 API 与 Web，`trap` 统一清理子进程 | 手写 |
| 项目说明 | `README.md` | 项目定位、Phase 1 范围、真实进度、快速开始、配置与命令 | 改写已有 |

### 6.2 文档与决策层

| 层次 | 文件 | 作用 | 来源 |
|---|---|---|---|
| 架构文档 | `docs/architecture.md` | 纵切规则、依赖方向、配置、契约、P1–P3 路线图、AgentScope 事实清单 | 手写 |
| 契约文档 | `docs/api-contract.md` | Health 字段、错误信封、OpenAPI 生成链路、未来 SSE 帧格式与事件联合 | 手写 |
| 架构决策 | `docs/adr/0001-vertical-slice-deferred-agentscope.md` | 记录纵切选型与 Phase 1 推迟 AgentScope 的背景、决策、后果、替代方案 | 手写 |

### 6.3 后端层

| 层次 | 文件 | 作用 | 来源 |
|---|---|---|---|
| 构建 | `backend/pyproject.toml` | 依赖、hatchling src 布局、pytest/ruff/mypy 配置 | 手写 |
| 构建 | `backend/.python-version` | 固定 Python 3.11 | 手写 |
| 配置 | `backend/.env.example` | 本地 `.env` 模板：`HARBOR_*` 与 `OPENAI_*` | 手写 |
| 依赖 | `backend/uv.lock` | uv 锁定依赖版本 | 工具生成 |
| 组装 | `backend/src/harbor/main.py` | `create_app()`：日志、CORS、异常信封、health 路由；lifespan 预留 P2 资源 | 手写 |
| 入口 | `backend/src/harbor/__main__.py` | `python -m harbor`，uvicorn factory 模式，开发期热重载 | 手写 |
| 包标识 | `backend/src/harbor/__init__.py` | `__version__ = "0.1.0"` | 手写 |
| 核心 | `backend/src/harbor/core/__init__.py` | 共享基础设施包标记 | 手写 |
| 核心 | `backend/src/harbor/core/config.py` | `Settings`：`HARBOR_*` / `OPENAI_*` 别名、`SecretStr`、`llm_configured` | 手写 |
| 核心 | `backend/src/harbor/core/logging.py` | 统一日志格式与级别，同步 uvicorn | 手写 |
| 核心 | `backend/src/harbor/core/errors.py` | 统一错误信封；404 / 500；不泄露堆栈 | 手写 |
| 功能包 | `backend/src/harbor/features/__init__.py` | 功能纵切包标记 | 手写 |
| health | `backend/src/harbor/features/health/__init__.py` | health 功能包标记 | 手写 |
| health | `backend/src/harbor/features/health/router.py` | `GET /health`，读取 `app.state.settings` | 手写 |
| health | `backend/src/harbor/features/health/schemas.py` | `HealthResponse` 模型，OpenAPI 契约来源 | 手写 |
| chat 预留 | `backend/src/harbor/features/chat/README.md` | P2：`/api/chat/stream`、SSE、事件适配、会话状态 | 手写 |
| catalog 预留 | `backend/src/harbor/features/catalog/README.md` | P2+：商品模型、JSONL 仓储、检索、`product_search` | 手写 |
| Agent 预留 | `backend/src/harbor/agents/README.md` | P2：prompts / tools / guardrails 与依赖规则；当前无代码 | 手写 |
| 契约导出 | `backend/scripts/export_openapi.py` | 不启动服务生成 OpenAPI，排序稳定输出到前端 | 手写 |
| 测试 | `backend/tests/conftest.py` | 隔离 `Settings` 与 `TestClient` | 手写 |
| 测试 | `backend/tests/test_health.py` | 健康检查字段、时间、404 错误信封 | 手写 |
| 测试 | `backend/tests/test_config.py` | 默认值、端口覆盖、OpenAI 别名、密钥保护、CORS JSON | 手写 |

### 6.4 前端层

| 层次 | 文件 | 作用 | 来源 |
|---|---|---|---|
| 构建 | `frontend/package.json` | `dev/build/typecheck/gen:api/preview` 脚本与依赖声明 | 脚手架 + 改写 |
| 依赖 | `frontend/package-lock.json` | npm 锁定依赖版本 | 工具生成 |
| 入口 | `frontend/index.html` | 中文页面、标题、favicon、Vite 入口 | 脚手架 + 改写 |
| 构建 | `frontend/vite.config.ts` | React + Tailwind 插件、5173 固定端口、`/api` 代理到 8010 | 脚手架 + 改写 |
| 构建 | `frontend/tsconfig.json` | 引用 app / node 子配置 | 脚手架 |
| 构建 | `frontend/tsconfig.app.json` | 浏览器端 TS 配置 | 脚手架 |
| 构建 | `frontend/tsconfig.node.json` | `vite.config.ts` 的 Node 端 TS 配置 | 脚手架 |
| 契约 | `frontend/openapi.json` | 后端导出的 OpenAPI 3.1 快照 | 工具生成 |
| 展示 | `frontend/public/favicon.svg` | 手写站点图标，消除 `/favicon.ico` 404 | 手写 |
| 入口 | `frontend/src/main.tsx` | React `createRoot` + StrictMode + 全局样式 | 脚手架 + 改写 |
| 类型 | `frontend/src/vite-env.d.ts` | `vite/client` 类型引用 | 手写 |
| 页面 | `frontend/src/App.tsx` | Phase 1 标题、架构链路、健康卡片、P2 说明 | 重写 |
| 样式 | `frontend/src/styles.css` | Tailwind v4 入口，仅 `@import "tailwindcss";` | 手写 |
| 基础组件 | `frontend/src/components/ui/Card.tsx` | 复用卡片容器 | 手写 |
| health | `frontend/src/features/health/HealthCard.tsx` | 请求 `/api/health`，loading/ok/error、AbortController、重试 | 手写 |
| chat 预留 | `frontend/src/features/chat/README.md` | P2：流式对话 UI 与 `fetch + ReadableStream` 说明 | 手写 |
| API 客户端 | `frontend/src/lib/api/client.ts` | `openapi-fetch` 类型安全客户端 | 手写 |
| API 类型 | `frontend/src/lib/api/schema.d.ts` | 由 `make gen-api` 生成的 TS 类型 | 工具生成 |

### 6.5 本地生成、不纳入版本管理

| 文件/目录 | 作用 |
|---|---|
| `backend/.env` | `make setup` 从模板复制，已被忽略 |
| `backend/.venv/` | uv 虚拟环境 |
| `backend/**/__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`.mypy_cache/` | Python 与工具缓存 |
| `frontend/node_modules/` | npm 依赖 |
| `frontend/dist/` | Vite 构建产物 |
| `frontend/.env.local` | 可选的前端本地覆盖配置，已被忽略；当前未创建 |

清理掉的脚手架文件：`src/App.css`、`src/index.css`、`src/assets/`、模板
`README.md`、`public/icons.svg`、`.oxlintrc.json`。

## 7. 核心实现说明

### 7.1 后端启动链

```text
python -m harbor
  → __main__.py 读取 Settings
  → uvicorn.run("harbor.main:create_app", factory=True, ...)
  → create_app()：setup_logging → FastAPI → CORS → 异常处理 → include_router(health)
```

- `create_app(settings=None)` 支持注入配置，测试和 OpenAPI 导出都走这条路径。
- 没有模块级 `app`，避免导入时就读取 `.env`。
- 开发环境 `reload=True`，由 `HARBOR_APP_ENV` 控制。
- lifespan 目前只记录启停，Phase 2 在这里挂 Agent / 会话存储。

### 7.2 配置与密钥

- 通用字段使用 `HARBOR_` 前缀。
- `openai_base_url / openai_api_key / openai_model` 使用 `AliasChoices`，同时接受
  `OPENAI_*` 与 `HARBOR_OPENAI_*`。
- key 使用 `SecretStr`；健康检查只返回 `llm_configured` 布尔值。
- `.env` 只存在于 `backend/`，被 git 忽略。

### 7.3 健康检查与错误信封

成功响应示例：

```json
{
  "status": "ok",
  "service": "harbor-purchase-agent",
  "version": "0.1.0",
  "environment": "development",
  "llm_configured": false,
  "time": "2026-10-29T13:21:41.485366Z"
}
```

错误响应：

```json
{"error": {"code": "not_found", "message": "Not Found"}}
```

### 7.4 契约生成

```text
FastAPI schema
  → backend/scripts/export_openapi.py
  → frontend/openapi.json
  → openapi-typescript
  → frontend/src/lib/api/schema.d.ts
```

`openapi.json` 与 `schema.d.ts` 是生成物；修改后端 schema 后执行 `make gen-api`。

### 7.5 前端健康卡片

- `useState` 维护 `loading | ok | error`。
- `useEffect` 中用 `AbortController` 取消过期请求。
- `openapi-fetch` 返回 `{ data, error, response }`；网络失败在 `catch` 中处理。
- 成功显示服务名、版本、环境、LLM 配置状态。
- 失败显示原因、`make dev-api` 提示与「刷新」按钮。
- 不引入 React Query / SWR。

## 8. 验证记录

### 8.1 安装与质量门禁

```bash
make setup
# uv sync 成功；npm install 成功；backend/.env 已生成
make check
# ruff check: All checks passed
# ruff format --check: 18 files already formatted
# mypy: Success: no issues found in 11 source files
# tsc -b: 无错误
# pytest: 6 passed
```

测试通过时有一条非阻塞警告：
`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated`。

### 8.2 HTTP 契约

```bash
curl http://127.0.0.1:8010/api/health
# {"status":"ok","service":"harbor-purchase-agent","version":"0.1.0",
#  "environment":"development","llm_configured":false,"time":"..."}

curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/docs
# 200

curl http://127.0.0.1:8010/openapi.json
# title: harbor-purchase-agent
# paths: ["/api/health"]

curl http://127.0.0.1:8010/api/nope
# 404 {"error":{"code":"not_found","message":"Not Found"}}
```

### 8.3 契约可复现

```bash
cp frontend/openapi.json /tmp/openapi.before
make gen-api
diff -q /tmp/openapi.before frontend/openapi.json
# 无输出，说明生成结果稳定
```

### 8.4 一键开发与前端

```bash
make dev
# 后端 8010 + 前端 5173 同时启动
curl http://127.0.0.1:8010/api/health          # 200
curl http://localhost:5173/                    # 200
curl http://localhost:5173/api/health          # 200，Vite 代理正常
```

浏览器实测（Chrome DevTools）：

- 页面正常渲染标题、架构链路和健康卡片。
- 健康卡片显示「后端正常 · harbor-purchase-agent · 0.1.0 · development · LLM 未配置」。
- 停止后端后重新加载，显示「连接失败」和 `make dev-api` 提示，页面不崩溃。
- 重启后端后点击「刷新」，恢复「后端正常」。
- 补齐 favicon 后控制台无错误。
- 注意：本机 Vite 监听 `localhost`，验证使用 `http://localhost:5173`。

构建：

```bash
npm --prefix frontend run build
# tsc -b 通过；vite build 成功；dist/ 生成
```

## 9. 版本基线

后端（`uv.lock`）：

| 包 | 版本 |
|---|---|
| fastapi | 0.162.0 |
| uvicorn | 0.54.0 |
| pydantic-settings | 2.16.2 |
| pytest | 9.2.0 |
| httpx | 0.28.1 |
| ruff | 0.17.3 |
| mypy | 2.4.1 |

前端（`package-lock.json`）：

| 包 | 版本 |
|---|---|
| react / react-dom | 19.3.1 |
| vite | 8.3.5 |
| @vitejs/plugin-react | 6.1.2 |
| typescript | 5.9.3 |
| tailwindcss / @tailwindcss/vite | 4.3.3 |
| openapi-typescript | 7.13.0 |
| openapi-fetch | 0.17.0 |

## 10. 已知限制与注意事项

1. **没有 AgentScope**：`agents/` 和 `features/chat/` 只有 README；不安装依赖、
   不写 Agent、不实现 SSE。
2. **没有商品数据**：`features/catalog/` 只有 README；无 catalog JSONL、无检索。
3. **端口需联动**：修改 `HARBOR_PORT` 时同步设置 `VITE_API_PROXY_TARGET`。
4. **Vite 监听 localhost**：本机通过 `http://localhost:5173` 访问；
   `127.0.0.1:5173` 可能因绑定方式不同而失败。
5. **测试警告**：Starlette TestClient 对 httpx 的弃用警告不影响当前测试；
   后续 Starlette 升级时可评估切换到 `httpx2`。
6. **未提交 git**：本轮所有文件仍未提交，首次提交需用户确认。
7. **前端无路由 / 状态库**：占位页单页，不引入 React Query、SWR、Router；
   Phase 2 处理聊天流式状态时再按需引入。

## 11. Phase 2 交接

现有骨架无需重构，按以下顺序接入：

1. `backend` 增加 `agentscope==2.0.11`，重新 `uv sync`。
2. `agents/purchase_agent.py`：用 `Agent(...)` + `OpenAIChatModel` +
   `Toolkit` + `ReActConfig(max_iters=...)` 装配。
3. `features/chat/`：实现 `POST /api/chat/stream`，把 `agent.reply_stream()` 的
   `AgentEvent` 映射为 `docs/api-contract.md` 中的事件联合。
4. `features/catalog/`：JSONL 仓储 + `product_search` 工具。
5. `agents/guardrails.py`：重复工具调用检测、迭代上限、结构化输出断言。
6. 离线测试：fake ChatModel 或录制回放；`evals/` 放 golden cases。
7. `main.py` 只加 `include_router(chat.router)`；配置字段、错误信封、日志和
   lifespan 都是现成接入点。

## 12. 本轮不提交说明

按计划本轮不创建 git 提交。验收后如需要首次提交，建议提交信息：

```text
feat: scaffold Phase 1 backend, frontend and API contract

Co-Authored-By: Claude Code <noreply@anthropic.com>
```
