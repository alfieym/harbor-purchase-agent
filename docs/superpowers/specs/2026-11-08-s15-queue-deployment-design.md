# S15 队列与部署 — 设计

**目标：** 把 turn 的执行从 API 进程里拿出去。API 只做三件事——登记 run、入队、读 journal；
Redis 只负责唤醒；SQLite 的 `run_events` 仍然是唯一真相。

**S14 的现状：** `RunManager.start_run` 在同一个进程里 `create_task(self._execute)`，
订阅者靠内存队列扇出，`startup()` 把库里所有 `running` 标成 interrupted。
一旦 API 拆成两个进程，后两条会直接出错：内存队列看不到别的进程写的事件，
而 API 一重启就会把 worker 正在跑的 run 判成中断。

---

## 核心不变量

> **事件先落库，再唤醒；订阅者只认 journal。**

排行榜第二的不变量是：**认领用 CAS，重复消息无害。**
Redis 是 at-least-once 的，同一条消息可能被投两次；`UPDATE runs SET status='running'
WHERE run_id=? AND status='queued'` 的 rowcount 是 0 就是「别人已经拿走了」，
duplicate 在这一步被吃掉，而不是靠去重表。

---

## 架构

```
        POST /agui/runs                    GET /agui/runs/{id}/events
              │                                       ▲
   ①建行(queued) ②写 RUN_STARTED ③XADD               │ 轮询 journal（100ms）
              ▼                                       │
        ┌───────────┐                          ┌──────┴──────┐
        │   Redis   │  Streams + group         │     API     │
        │  (唤醒)   │ ───────────────────────► │  (不执行)   │
        └───────────┘   XREADGROUP / XAUTOCLAIM└─────────────┘
              │
              ▼  claim
        ┌───────────┐   写 run_events（唯一写入者）
        │  Worker   │ ─────────────────────────► SQLite journal
        └───────────┘   append → ACK
```

API 与 worker 之间只有两条通道：Redis Stream（唤醒）和 SQLite（状态与事件）。
没有任何进程内的共享对象，所以 worker 可以随时加副本。

---

## 决策

| 决策点 | 结论 | 为什么 |
|---|---|---|
| 队列 | Redis Streams + consumer group | `BRPOP` 弹出即丢，worker 崩了消息就没了；Streams 有 PEL 和 `XAUTOCLAIM`，崩溃重投是内建的 |
| Redis 的角色 | 只负责「有事发生了」 | 状态全在 SQLite。丢一条消息 = 少一次唤醒，sweeper 会补 |
| 投递语义 | at-least-once + CAS 认领 | 重复消息被 status 挡掉，不需要去重表 |
| 执行中的 run 崩了 | 心跳过期 → `interrupted`，**不重跑** | 已经流给用户的文本不能重放；重跑会重复扣预算 |
| 还没开始的 run（queued） | 重新入队，可以安全执行 | 日志里只有 RUN_STARTED |
| API 的实时跟随 | 轮询 journal，默认 100ms | 零新失败模式；Redis 挂了流照样能读 |
| 取消 | DB 标志，worker 在每次 append 时检查 | 进程内 `task.cancel()` 跨不了进程 |
| 同一 session 的并发 | SQLite 会话租约 | `SessionStore` 的 `asyncio.Lock` 只在单进程内有效 |
| `/commerce/intents` | 保持同步执行，不改 | 保住 S5 契约；S15 只动 run 这条链路 |
| 兼容开关 | `queue_enabled`，默认 `false` | 没有 Redis 也能跑，现有 730 个测试不受影响 |

---

## 状态机

```
queued ──claim(CAS)──► running ──┬──► finished
  │                              ├──► failed        (RUN_ERROR)
  │                              ├──► cancelled     (取消标志)
  │                              └──► interrupted   (心跳过期 / worker_lost)
  └──cancel(CAS)──► cancelled
```

只有 worker 能写 `queued → running`；只有 API 能写 `queued → cancelled`（两者都是 CAS，
所以谁先谁赢，不会同时成立）。

---

## 组件

### 1. `RunQueue` 协议（`features/runs/queue.py`）

```python
@dataclass(frozen=True)
class Claimed:
    message_id: str
    run_id: str

class RunQueue(Protocol):
    async def enqueue(self, run_id: str) -> None: ...
    async def claim(self, consumer: str, *, block_ms: int) -> Claimed | None: ...
    async def ack(self, message_id: str) -> None: ...
    async def reclaim(self, consumer: str, *, min_idle_ms: int, count: int) -> list[Claimed]: ...
    async def close(self) -> None: ...
```

两个实现：

- `RedisRunQueue(redis_url, stream, group, consumer)` —— `XADD` / `XREADGROUP` / `XACK` /
  `XAUTOCLAIM`；`XADD` 后 `XTRIM MAXLEN ~ 10000`（journal 才是真相，裁剪安全）。
- `MemoryRunQueue` —— 测试用，用 `asyncio.Queue` + 一张 pending 表模拟 PEL，
  能复现「未 ACK 的消息会被重投」。

### 2. Store 变更（`features/runs/store.py`）

新增列（旧库用 `PRAGMA table_info` 探测后 `ALTER TABLE` 补，不需要重建）：

| 列 | 用途 |
|---|---|
| `owner` | 正在执行这个 run 的 worker 标识 |
| `heartbeat_at` | 最后一次心跳；过期即可判定 worker 已死 |
| `cancel_requested` | API 写的取消标志，worker 读 |
| `enqueued_at` | 上次入队时间，sweeper 用 |

新增表：

```sql
CREATE TABLE IF NOT EXISTS session_leases (
    session_id TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    owner      TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
```

新增方法：

- `try_claim(run_id, owner, lease_seconds) -> bool` —— CAS `queued → running`
- `heartbeat(run_id, owner) -> bool` —— 顺带返回 `cancel_requested`
- `request_cancel(run_id) -> bool` —— CAS `queued → cancelled`
- `interrupt_stale(lease_seconds) -> list[str]` —— `running` 且心跳过期
- `stale_queued(seconds) -> list[str]` —— 供 sweeper 重投
- `acquire_session(session_id, run_id, owner, ttl) -> bool` / `release_session(...)`

### 3. Worker（`harbor/worker.py` + `features/runs/worker.py`）

```
loop:
  claim = queue.claim(...)          # 阻塞 5s
  if claim is None: sweep(); continue
  handle(claim):                     # 见下
```

`handle` 的顺序就是全部语义：

1. `get_run` —— 不存在或已终态 → ACK，丢弃
2. `try_claim` —— rowcount 0 → ACK（别人在跑，或已取消）
3. `acquire_session` —— 拿不到 → ACK，留在 queued 等 sweeper 重投
4. 执行 turn（复用 S14 的 `_execute` 逻辑，抽成 `RunExecutor`）
5. `ack`

`sweep()` 每 `sweep_seconds` 跑一次，两件事：

- 心跳过期的 `running` → 标 `interrupted`，写 `RUN_ERROR(worker_lost)`，释放会话租约
- 停留超过 `requeue_after_seconds` 的 `queued` → 重新 `XADD`

worker 启动时**不**调用 `interrupt_running()`。那条路径在 S14 里是对的（单进程），
在多进程下就是错的——它会把队友正在跑的 run 判成中断。取而代之的是心跳租约。

### 4. API（`features/runs/service.py`）

`RunManager` 退化成一个**登记 + 跟随**的组件：

- `start_run`：建行（`queued`）→ 写 `RUN_STARTED` → `enqueue`。入队失败只记日志，
  不把请求变成 500：行已经在库里，sweeper 会补。
- `events`：`JournalFollower`，每 `follow_poll_seconds` 读一次
  `list_events(after_seq)`，读到终态事件或终态 status 就结束。订阅者不再需要内存队列。
- `cancel`：终态 → 原样返回；`queued` → CAS 成 cancelled 并补终态事件；
  `running` → 只写 `cancel_requested`，由 worker 收尾。
- `startup` / `shutdown`：队列模式下什么都不做（不再 `interrupt_running`）。
- `queue_enabled=false` 时保留 S14 的进程内执行路径。

### 5. 前端

不需要改代码：`queued` 不是终态，`terminalRunStatus` 返回 `null`，界面按「还在跑」处理。
补一条测试锁住这个映射。

### 6. 部署

- `backend/Dockerfile`（uv + python:3.12-slim），api 与 worker 共用一个镜像，只是命令不同
- `docker-compose.yml`：`redis` / `api` / `worker`，`./data` 挂载为共享卷
- `make docker-up` / `make docker-logs`
- `scripts/dev.sh` 增加可选的本地 worker

---

## 失败矩阵

| 事故 | 系统做什么 | 用户看到 |
|---|---|---|
| 入队时 Redis 不可用 | 行已落库，只记日志 | 202，稍后才开始跑（sweeper 补投） |
| worker 执行中崩了 | 心跳停，别的 worker 的 sweep 标 interrupted | `RUN_ERROR(worker_lost)` |
| worker 在拿到消息后、执行前崩了 | 消息留在 PEL，`XAUTOCLAIM` 后由别人执行 | 正常完成，慢一些 |
| 同一条消息投递两次 | 第二次 `try_claim` 失败 → ACK | 无感 |
| API 重启 | worker 不受影响 | 前端断流重连，从 `lastSeq` 接着读 |
| API 与 worker 同时重启 | 重启后 sweep 收拾 | interrupted |
| 同一 session 两条 run | 后到者拿不到会话租约，留在 queued | 稍慢，不会串话 |

---

## 配置

| 名称 | 默认 | 说明 |
|---|---|---|
| `queue_enabled` | `false` | 打开后 API 不再执行 run |
| `redis_url` | `redis://localhost:6379/0` | |
| `queue_stream` | `harbor:runs` | |
| `queue_group` | `harbor-workers` | |
| `queue_claim_idle_ms` | `60000` | 多久没 ACK 就算别的 worker 死了 |
| `run_lease_seconds` | `90` | 心跳过期阈值 |
| `run_requeue_after_seconds` | `15` | queued 停留多久后重投 |
| `run_sweep_seconds` | `5` | sweeper 间隔 |
| `run_follow_poll_seconds` | `0.1` | API 轮询 journal 的间隔 |
| `worker_concurrency` | `2` | 单 worker 进程内并行执行的 turn 数 |

---

## 测试策略

全部离线：`MemoryRunQueue` + 临时 SQLite 文件，**不连真 Redis、不调远程 embedding**。

- `test_run_queue.py` —— Memory 与 Redis 实现的行为一致（Redis 用假 client 断言命令形状）
- `test_runs_store_s15.py` —— CAS、心跳、取消标志、租约、迁移旧库
- `test_worker.py` —— 认领顺序、重复消息、会话互斥、崩溃后不重跑
- `test_s15_acceptance.py` —— 端到端：API 登记 → worker 执行 → API 跟随完整事件流

---

## 任务拆分

1. store：迁移 + 认领/心跳/取消/租约/sweep 查询
2. queue：协议 + Memory 实现 + Redis Streams 实现
3. worker：执行器抽取 + 认领循环 + sweeper
4. API：登记入队 + journal 跟随 + 取消重写
5. 部署：Dockerfile + compose + make 目标
6. 验收：端到端套件 + 文档页
