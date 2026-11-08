# harbor-purchase-agent

Harbor 采购 Agent —— 一个可跑的电商购物 Agent（GlobeX 复刻的垂直切片）。

买家说一句「预算300元，买个背包寄到中国」，Agent 走 ReAct 循环检索商品，
返回带价格/币种的 SKU 推荐；工具重复调用时能自己收敛。往后是一整条工程链路：
语义检索 + 硬约束过滤、多 Agent 调度、下单交易、护栏、上下文治理、偏好记忆、
评测、可观测性、AG-UI 事件流，以及 S15 的队列与多进程部署。

技术栈：AgentScope 2.0.11 + FastAPI + Qdrant + SQLite + Redis Streams + React/Vite/TS。

## 当前进度

| 阶段 | 能力 | 状态 |
|---|---|---|
| S3 | 检索闭环：Qdrant 语义召回 + 硬约束 filter，失败降级 keyword_2gram | ✅ |
| S4 | 采购 Agent + `product_search` 工具 + 结构化商品卡 + 交互式 REPL | ✅ |
| S5 | HTTP 会话入口 `POST /commerce/intents` | ✅ |
| S6 | 会话持久化与恢复（SQLite） | ✅ |
| S7 | 上下文治理：触发比例、归档、保护最近轮次 | ✅ |
| S8 | 偏好记忆（跨会话） | ✅ |
| S9 | 多 Agent 调度（并发上限、批次预算） | ✅ |
| S10 | 下单交易：草稿、确认、取消、证据留存 | ✅ |
| S11 | 护栏与可靠性：超时/重试/熔断/预算/循环/结构/内容/输出 | ✅ |
| S12 | Golden set 评测：build / verify / run / report / compare | ✅ |
| S13 | 可观测性：trace、event、metric、SSE 状态流 | ✅ |
| S14 | AG-UI runs：事件先编号落库再广播，断线 ≠ 取消，刷新可重放 | ✅ |
| S15 | 队列与部署：API 不再执行 turn，worker 消费 Redis 唤醒 | ✅ |

后端 **794** 条测试、前端 **28** 条测试全绿。

## 快速开始（单进程）

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

打开 <http://localhost:5173> 使用聊天界面。首次运行 `make setup` 会从
`backend/.env.example` 复制出 `backend/.env`；模型 key 默认留空，健康检查显示
「LLM 未配置」即可。默认（`HARBOR_QUEUE_ENABLED=false`）是 **inline 模式**：
turn 由 API 进程自己执行，不需要 Redis。

## 队列模式（S15）

打开后 API 只做三件事：**登记 run、入队、读 journal**；执行全部发生在 worker。
Redis Streams 只是一声唤醒，SQLite 的 `run_events` 才是唯一真相。

```bash
# 终端 1：API 只登记，不执行
HARBOR_QUEUE_ENABLED=true make dev-api

# 终端 2：执行进程
HARBOR_QUEUE_ENABLED=true make worker
```

需要本地 Redis（默认 `redis://localhost:6379/0`）。注意 **Qdrant local-path
模式是单进程独占的**：API 与 worker 同时启动时，要给两者都设
`HARBOR_QDRANT_URL` 指向一个 Qdrant server，或者直接用下面的容器编排。

### 一套容器起全栈

```bash
make docker-up     # redis + qdrant + api + worker
make docker-logs   # 跟踪 api 与 worker 日志
make docker-down
```

API 在 <http://127.0.0.1:8010>。首次启动要建检索索引，健康检查通过前
worker 不会开始领任务。worker 固定 `HARBOR_INDEX_BUILD_ENABLED=false`：
只读共享索引，也因此跳过启动时的 embedding 探测（额度只花一次，由 API 花）。

### S15 的几条不变量

- **先落库，再喊人。** 行写进 SQLite 并追加 `RUN_STARTED` 之后才 XADD；
  入队失败只丢一次唤醒，sweeper 会补喊，run 不会丢。
- **一次投递 ≠ 一次执行。** worker 用 `UPDATE ... WHERE status='queued'`（CAS）
  认领；影响 1 行的人才执行，影响 0 行的直接 ack。重复消息天然无害。
- **已经开始的 run 永不重跑。** 它的旁白已经在别人的屏幕上；心跳停止由
  sweeper 改判 `interrupted(worker_lost)`，滚动发布时由进程自己写
  `interrupted(server_restarted)`。
- **取消分两种。** queued 的 run 由 API 一条 UPDATE 直接结束；running 的 run
  只放标记，由持有写序的 worker 收尾。

完整设计见 [S15 spec](docs/superpowers/specs/2026-11-08-s15-queue-deployment-design.md)，
图解见 [S15 教学页](docs/_pipeline_html/s15-queue-deployment-pipeline.html)。

## 常用命令

| 命令 | 说明 |
|---|---|
| `make setup` | 安装依赖并生成本地 `.env` |
| `make dev` | 并行启动前后端（Ctrl-C 统一退出） |
| `make dev-api` | 仅启动后端，开发模式下自动重载 |
| `make dev-web` | 仅启动前端 |
| `make worker` | S15：单独启动执行进程（需先开队列模式的 API） |
| `make docker-up` | 构建并启动 redis / qdrant / api / worker |
| `make docker-logs` | 跟踪 api 与 worker 日志 |
| `make docker-down` | 停止整套容器 |
| `make cli` | 交互式采购助手 REPL（`python -m harbor.agents`） |
| `make test` | 运行后端 pytest |
| `make lint` | ruff 检查与格式检查 |
| `make format` | ruff 自动修复与格式化 |
| `make typecheck` | mypy + TypeScript 类型检查 |
| `make gen-api` | 导出 OpenAPI 并生成前端 TS 类型 |
| `make check` | lint + typecheck + test |
| `make clean` | 清理缓存与前端构建产物 |
| `make distclean` | 连虚拟环境和 node_modules 一起删除 |

评测（S12）：

```bash
uv run --directory backend python -m harbor.eval build      # 冻结 golden set
uv run --directory backend python -m harbor.eval run --out result.json
uv run --directory backend python -m harbor.eval compare --baseline a.json --candidate b.json
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/commerce/intents` | 单请求执行一次 turn |
| `GET` | `/commerce/sessions?buyer_id=` | 列出买家的会话 |
| `POST` | `/agui/runs` | 登记 run，返回 `202` |
| `GET` | `/agui/runs/{id}/events` | SSE：先重放持久日志，再跟随；`after` 或 `Last-Event-ID` 续传 |
| `POST` | `/agui/runs/{id}/cancel` | 取消 queued 或 running 的 run |
| `GET` | `/agui/runs/{id}` | 查询 run 快照 |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/guardrails/status` | 护栏状态 |
| `GET` | `/api/observability/{status,traces,metrics,events}` | S13 可观测性 |

REST 契约由 FastAPI OpenAPI 自动生成前端类型；`frontend/openapi.json` 与
`frontend/src/lib/api/schema.d.ts` 是生成物，改后端 schema 后运行
`make gen-api`，不要手改。

## 配置

后端配置全部来自环境变量或 `backend/.env`（模板见
[backend/.env.example](backend/.env.example)），前缀 `HARBOR_`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HARBOR_APP_ENV` | `development` | `development` / `test` / `production` |
| `HARBOR_HOST` | `127.0.0.1` | 监听地址 |
| `HARBOR_PORT` | `8010` | 监听端口 |
| `HARBOR_LOG_LEVEL` | `INFO` | 日志级别 |
| `HARBOR_CORS_ORIGINS` | `["http://localhost:5173","http://127.0.0.1:5173"]` | JSON 数组 |
| `HARBOR_MODEL_PROVIDER` | `deepseek` | `deepseek` / `dashscope`（千问） |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点；千问用 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `OPENAI_API_KEY` | 空 | 模型 key |
| `OPENAI_MODEL` | `deepseek-chat` | 模型名；千问用 `qwen-plus` |
| `OPENAI_EMBEDDING_MODEL` | `qwen3.7-text-embedding-flash` | 向量模型（需端点支持 `/embeddings`） |
| `OPENAI_EMBEDDING_BATCH_SIZE` | `20` | 单次 embedding 请求的文本条数 |
| `HARBOR_TURN_TIMEOUT_SECONDS` | `120` | 单次 turn 上限 |
| `HARBOR_QUEUE_ENABLED` | `false` | 打开后 API 不再执行 turn |
| `HARBOR_REDIS_URL` | `redis://localhost:6379/0` | 也接受 `REDIS_URL` |
| `HARBOR_QUEUE_STREAM` | `harbor:runs` | Redis Stream 名 |
| `HARBOR_QUEUE_GROUP` | `harbor-workers` | 消费者组名 |
| `HARBOR_QUEUE_CLAIM_IDLE_MS` | `60000` | 未 ack 消息的重投等待 |
| `HARBOR_WORKER_CONCURRENCY` | `2` | 每个 worker 进程的领取并发 |
| `HARBOR_RUN_LEASE_SECONDS` | `90` | 心跳过期即判死 |
| `HARBOR_RUN_REQUEUE_AFTER_SECONDS` | `15` | queued 超过多久由 sweeper 补喊 |
| `HARBOR_RUN_SWEEP_SECONDS` | `5` | sweeper 周期 |
| `HARBOR_RUN_FOLLOW_POLL_SECONDS` | `0.1` | API 跟随 journal 的轮询间隔 |
| `HARBOR_QDRANT_URL` | 空 | 设置后走 Qdrant server；多进程部署必填 |
| `HARBOR_INDEX_BUILD_ENABLED` | `true` | worker 设 `false`：只读索引、跳过探测 |

切换千问（阿里云百炼）：`HARBOR_MODEL_PROVIDER=dashscope`、
`OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`、
`OPENAI_MODEL=qwen-plus`，三处一起改。DashScope 的 compatible-mode 同时提供
`/embeddings`，语义召回可直接跑通；DeepSeek 端点没有 `/embeddings`，检索会降级为
`keyword_2gram`。

`OPENAI_*` 也兼容 `HARBOR_OPENAI_*` 写法。key 使用 `SecretStr` 保存，
健康检查只暴露 `llm_configured`，不会返回 key。`backend/.env` 已被 git 忽略。

开发期前端通过 Vite 代理访问后端，默认目标是 `http://127.0.0.1:8010`。
如果修改了 `HARBOR_PORT`，请同步设置 `VITE_API_PROXY_TARGET`（例如写在
`frontend/.env.local`），否则页面会请求错误的端口。

## 目录

```
.
├── Makefile                 开发命令入口
├── docker-compose.yml       S15：redis / qdrant / api / worker
├── scripts/dev.sh           并行启动前后端
├── docs/
│   ├── architecture.md      分层规则
│   ├── api-contract.md      REST 与 SSE 契约
│   ├── _pipeline_html/      各阶段流程图（浏览器直接打开）
│   ├── superpowers/specs/   设计文档（S9–S15）
│   └── adr/                 架构决策记录
├── backend/                 FastAPI（uv 管理，自包含）
│   ├── Dockerfile           API 与 worker 共用镜像
│   ├── src/harbor/
│   │   ├── main.py          create_app() 组装点
│   │   ├── runtime.py       共用 turn 装配（API / worker）
│   │   ├── worker.py        S15 worker 进程入口
│   │   ├── core/            config / logging / errors
│   │   ├── features/        health、catalog、chat、runs、orders、guardrails、memory
│   │   ├── agents/          AgentScope 装配、工具、提示词、CLI
│   │   ├── guardrails/      S11 超时/重试/熔断/预算/循环/结构/内容
│   │   ├── eval/            S12 golden set 评测
│   │   └── observability/   S13 trace / metric / event
│   ├── scripts/             OpenAPI 导出
│   └── tests/               794 条后端测试
└── frontend/                Vite + React + TS + Tailwind
    ├── openapi.json         由后端导出（生成物）
    └── src/
        ├── lib/api/         openapi-fetch 客户端与生成类型
        └── features/        chat（AG-UI 客户端与 reducer）、health
```

## 架构

- 后端按**功能纵切**组织，`main.py` 是唯一组装点；`features/` 不反向依赖
  `agents/`。详见 [docs/architecture.md](docs/architecture.md)。
- **run 是生命周期对象（S14）**：事件先落库拿到序号，再广播；断线只是少一个
  订阅者；刷新从持久日志重放。浏览器端用 `seq` 做幂等键，商品卡只来自
  `STATE_SNAPSHOT`。
- **执行与请求解耦（S15）**：API 只登记、入队、读 journal；worker 通过
  CAS 认领后执行。Redis 故障、消息重复、进程崩溃都只影响「多快」，不影响
  「会不会」被处理。
- 每个阶段的设计取舍都有一篇 spec 和一张教学图，见 `docs/superpowers/specs/`
  与 `docs/_pipeline_html/`。
