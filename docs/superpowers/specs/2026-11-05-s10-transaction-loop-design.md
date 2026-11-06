# S10 交易闭环 — 设计文档

- 日期：2026-11-05
- 状态：待评审
- 前置：S4 工具面、S6 会话持久化、S8 两步式偏好、S9 派发回执
- 范围：执行链（写链、决议链、取消链）+ 契约（交易工具）

## 1. 意图与为什么

现在 Agent 只能推荐商品；买家看完卡片，交易就断了。S10 把「推荐 → 下单 → 取消」接成
一条可审计的闭环，并且把最关键的一步——**订单的最终确认**——交还给买家本人。

整条链的安全边界只有一句：**模型只能提议，应用层才能提交。**

- 模型可以创建「订单草稿」和查询订单，**不能**创建订单、确认取消或改变任何已成立订单。
- 买家通过应用层的明确动作确认订单；「嗯」「行吧」这类对话文本不构成确认。
- 取消订单同样分两步：应用层发起取消请求，买家再做一次应用层确认。

这样，「确认后才创建」不是 prompt 里的纪律，而是表结构和服务边界保证的事实。

## 2. 目标 / 非目标 / 反目标

### 目标

1. 新增模型工具 `create_order_draft`：把本会话检索到的 SKU 落成订单草稿，服务端计算
   单价、总额、币种，模型不能自带价格。
2. 新增只读工具 `get_order`：按订单号查状态和取消请求状态。
3. 应用层确认草稿时，在**一个事务**里创建订单、迁移草稿状态、写操作日志；重复确认幂等。
4. 同一会话只保留一份待确认草稿；建新草稿自动顶掉旧草稿（同一事务）。
5. 取消链全部由应用层发起：取消请求 → 应用层确认 → 订单取消；支持撤回请求；重复确认幂等。
6. 订单、草稿、取消请求、操作日志全部落 SQLite（WAL + `BEGIN IMMEDIATE`）。
7. `/commerce/intents` 响应新增 `pending_draft` 字段，由服务端从数据库重建，不信模型复述。
8. CLI 提供确认 / 取消命令，便于手工验收整条链。
9. 每次成功检索的命中落一张证据表；草稿在上下文被压缩后仍能按 SKU 找回价格与币种。
   证据按会话隔离，并按保留期清理。
10. 全部单测离线运行，不调用远程 embedding。

### 非目标

- 支付、库存扣减、退款、物流、收货地址——仓库没有这些数据源，本期不建模。
- 草稿到确认之间的**价格变动校验**（买家已明确：本期不做；草稿价格是快照）。
- 多级审批 / 审批人——确认人就是买家本人。
- 草稿原地编辑（改数量/换商品）——重新建草稿，旧草稿自动作废。
- 草稿过期回收——新草稿会顶掉旧的，不会堆积。
- 模型发起取消——取消请求只由应用层创建。
- 订单列表、报表、搜索。
- 鉴权体系——沿用现有 `buyer_id` 多租户约定，不引入登录态。
- 只凭 S9 派发回执下单——草稿行必须有一条带价格的 `product_search` 命中作证据。
- 修改 S9 的 `task_dispatch` 回执结构。

### 反目标（技术上能跑也算失败）

- 模型工具路径写入了 `orders` 表。
- 对话里说「确认」就创建了订单。
- 草稿不属于该买家，却能被确认。
- `superseded` / `cancelled` 的草稿还能被确认。
- 订单已创建但没有对应的操作日志。
- 网络重试产生第二张订单。
- 取消请求挂起时订单状态提前变成 `cancelled`。
- 交易工具的参数校验失败导致整轮 502（应让模型看到失败原因并继续对话）。

## 3. 三条链与状态机

### 3.1 写链（模型 → 草稿）

```
用户表达购买意图
  → 模型调用 create_order_draft(items, currency)
  → 工具按 (buyer, session, sku) 从证据表取标题/单价/币种
     （表里没有才回退上下文命中或压缩摘要附录）
  → 同一事务：旧 pending 草稿置 superseded，插入新草稿 + 行项目 + 事件
  → 返回草稿回执（这不是订单）
```

- 草稿状态：`pending → confirmed | cancelled | superseded`。
- 终态：`confirmed` / `cancelled` / `superseded`；任何终态都不能再确认。
- 同一 `(buyer_id, shopping_session_id)` 下最多一份 `pending` 草稿（部分唯一索引保证）。
- 模型一次回复里多次调用：以最后一次成功回执为准，前面的草稿已被顶掉。

### 3.2 决议链（人 → 订单）

```
买家在应用层点「确认」
  → POST /commerce/orders/drafts/{draft_id}/confirm
  → 校验：买家匹配、草稿仍是 pending
  → 同一事务：插入订单，草稿置 confirmed，写 draft_confirmed + order_created 事件
  → 返回订单
```

- 订单状态：`created → cancelled`，没有别的入口。
- 重复确认同一草稿：返回第一次创建的订单（`idempotent=true`），不新建。
- 草稿被顶掉 / 已作废 / 已确认后再次确认：`409`，响应带当前状态。

### 3.3 取消链（应用层发起 → 人确认）

```
应用层发起取消请求
  → POST /commerce/orders/{order_id}/cancellation
  → 旧 pending 请求置 superseded，插入新请求（pending），写 cancellation_requested 事件
  → 订单状态不变，仍是 created

买家在应用层确认取消
  → POST /commerce/orders/{order_id}/cancellation/confirm
  → 同一事务：请求置 confirmed，订单置 cancelled，
     写 cancellation_confirmed + order_cancelled 事件
```

- 取消请求状态：`pending → confirmed | withdrawn | superseded`。
- 同一订单最多一份 `pending` 请求（部分唯一索引保证）。
- 撤回请求：请求置 `withdrawn`，订单不变。
- 重复确认：已 `confirmed` 且订单已 `cancelled` 时返回同一结果（`idempotent=true`）。
- 订单已 `cancelled` 时再发起取消请求：`409`。

**草稿作废不需要二次确认**：那时还没有订单，没有副作用，应用层直接 `pending → cancelled`。

## 4. 数据模型（SQLite）

数据库路径：`data/sessions/orders.sqlite3`；沿用 S8 的 WAL、`busy_timeout`、
`BEGIN IMMEDIATE` 和进程内 `asyncio.Lock`。

### 4.1 `order_drafts`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | `drf_` + 12 hex |
| `buyer_id` | TEXT | 买家 |
| `shopping_session_id` | TEXT | 会话 |
| `status` | TEXT | `pending/confirmed/cancelled/superseded` |
| `currency` | TEXT | 展示币种 |
| `total_amount` | TEXT | 十进制字符串，`Decimal` 序列化 |
| `created_at` / `updated_at` | TEXT | UTC ISO-8601 |

约束与索引：

- `CREATE UNIQUE INDEX ux_drafts_one_pending ON order_drafts(buyer_id, shopping_session_id) WHERE status='pending'`

### 4.2 `order_draft_items`

| 列 | 类型 | 说明 |
|---|---|---|
| `draft_id` | TEXT | 外键 |
| `line_no` | INTEGER | 行号 |
| `product_id` | TEXT | 商品 |
| `sku_id` | TEXT | 下单 SKU |
| `title` | TEXT | 下单时的标题快照 |
| `quantity` | INTEGER | 1–99 |
| `unit_price` | TEXT | 展示币种单价快照（已量化） |
| `currency` | TEXT | 展示币种 |
| `evidence_unit_price` | TEXT | 检索命中里的单价，换算前的值 |
| `evidence_currency` | TEXT | 检索命中里的币种 |

主键 `(draft_id, line_no)`。

### 4.3 `orders`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | `ord_` + 12 hex |
| `draft_id` | TEXT UNIQUE | 一张草稿最多一张订单 |
| `buyer_id` / `shopping_session_id` | TEXT | 归属 |
| `status` | TEXT | `created/cancelled` |
| `currency` / `total_amount` | TEXT | 从草稿复制 |
| `created_at` / `updated_at` | TEXT | |

索引：`(buyer_id, created_at DESC)`。

### 4.4 `order_cancellation_requests`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | `can_` + 12 hex |
| `order_id` | TEXT | 外键 |
| `buyer_id` | TEXT | 归属 |
| `status` | TEXT | `pending/confirmed/withdrawn/superseded` |
| `reason` | TEXT NULL | 可选 |
| `idempotency_key` | TEXT | 应用层提供 |
| `created_at` / `decided_at` | TEXT | |

约束与索引：

- `UNIQUE(buyer_id, idempotency_key)`
- `CREATE UNIQUE INDEX ux_cancel_one_pending ON order_cancellation_requests(order_id) WHERE status='pending'`

### 4.5 `product_evidence`（下单证据）

`product_search` 每次成功返回后，按 `(buyer_id, shopping_session_id, sku_id)` upsert 一条
命中快照。同一 SKU 只保留最近一次观测，因此行数天然不超过目录规模。

| 列 | 类型 | 说明 |
|---|---|---|
| `buyer_id` / `shopping_session_id` | TEXT | 归属；证据不跨会话 |
| `sku_id` | TEXT | 主键组成部分 |
| `product_id` | TEXT | 商品 |
| `title` | TEXT | 命中标题快照 |
| `unit_price` | TEXT | 命中里的单价（检索目标币种） |
| `currency` | TEXT | 命中里的币种 |
| `observed_at` | TEXT | 最近一次观测时间 |

主键 `(buyer_id, shopping_session_id, sku_id)`。

**保留 / 清理策略**：默认保留 30 天（`evidence_retention_days`）。服务启动时清理一次；
之后每天第一次写入时惰性清理一次——删除 `observed_at` 早于保留期的行。会话隔离意味着
删掉一个会话的证据不会影响别的会话；同一 SKU 只留最新一行，行数有界。

### 4.6 `order_events`（只追加）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | `evt_` + 12 hex |
| `subject_type` | TEXT | `draft/order/cancellation` |
| `subject_id` | TEXT | 对应记录 id |
| `event` | TEXT | 见下 |
| `from_status` / `to_status` | TEXT NULL | 迁移前后 |
| `actor` | TEXT | `model/buyer/system` |
| `source` | TEXT | `tool/app` |
| `reason` | TEXT NULL | |
| `metadata` | TEXT | JSON，可空 |
| `created_at` | TEXT | |

事件名：`draft_created`、`draft_superseded`、`draft_confirmed`、`draft_cancelled`、
`order_created`、`cancellation_requested`、`cancellation_superseded`、
`cancellation_confirmed`、`cancellation_withdrawn`、`order_cancelled`。

索引：`(subject_type, subject_id, created_at)`。

## 5. 契约

### 5.1 模型工具 `create_order_draft`

| 项 | 契约 |
|---|---|
| 输入 | `items: [{product_id, sku_id, quantity}]`，`currency`（默认 CNY）。**没有幂等键** |
| 身份 | 从注入的 `AgentState` 取 `buyer_id` + `shopping_session_id`；缺失即 `ok=false` |
| 前置 | 1–20 行；数量 1–99；SKU 不重复；每行都能在 `product_evidence` 里按 `(buyer, session, sku_id)` 找到快照；表里没有时回退到本会话上下文里的成功 `product_search` 命中或压缩摘要附录。`selected_sku_id` 与 `product_id` 必须对得上 |
| 价格 | 一律取证据里的单价，用 `fx.convert` 换算到草稿币种，再按币种小数位量化（见 §8.8）；证据价与证据币种另存为 `evidence_*`。模型给的价格一概不采信 |
| 后置 | 同一会话最多一份 `pending` 草稿；旧 pending 自动 `superseded`；写 `draft_created`（+ 旧草稿的 `draft_superseded`）事件 |
| 幂等 | 若当前 `pending` 草稿的行项目与币种和本次调用完全一致，直接返回它，`replayed=true`，不再顶掉；否则正常替换 |
| 失败 | **参数/证据校验失败返回 SUCCESS 工具块 + `ok=false` + 机器可读 `error`**，让模型继续对话；只有数据库/程序异常才是 ERROR |
| 副作用 | 只写 `order_drafts` / `order_draft_items` / `order_events`；**永不写 `orders`** |

回执形状：

```json
{"ok": true, "kind": "order_draft", "replayed": false,
 "draft": {"draft_id": "drf_...", "status": "pending", "currency": "CNY",
           "total": "178.00", "items": [{"sku_id": "...", "quantity": 2,
           "unit_price": "89.00"}]}}
```

### 5.2 模型工具 `get_order`

| 项 | 契约 |
|---|---|
| 输入 | `order_id` |
| 前置 | 订单属于当前 `buyer_id` |
| 输出 | 订单状态、行项目、总额，以及最新一条取消请求的状态 |
| 副作用 | 无 |
| 失败 | 不存在或不属于该买家：`ok=false` + `error=not_found`（不区分，避免泄露） |

### 5.3 应用层动作

| 动作 | 前置 | 迁移（同一事务） | 幂等 | 错误 |
|---|---|---|---|---|
| `confirm_draft` | 买家匹配；草稿 `pending` | 草稿→`confirmed`；插订单；事件 | 已 `confirmed` 返回已有订单 | 404 不存在/非本人；409 非 pending |
| `cancel_draft` | 买家匹配；草稿 `pending` | 草稿→`cancelled`；事件 | 已 `cancelled` 返回成功 | 404；409 非 pending |
| `request_cancellation` | 买家匹配 | 旧 pending→`superseded`；插新 pending；事件 | 先查 `(buyer,idempotency_key)`：命中就直接返回，不看订单状态；未命中才要求订单 `created` | 404；409 订单已取消 |
| `confirm_cancellation` | 买家匹配；看**最新一条**请求 | 请求→`confirmed`；订单→`cancelled`；两事件 | 最新请求已 `confirmed` 或订单已 `cancelled`：返回同一结果 | 404；409 最新请求是 `withdrawn`/`superseded`，或没有请求 |
| `withdraw_cancellation` | 买家匹配；看**最新一条**请求 | 请求→`withdrawn`；事件 | 最新请求已 `withdrawn`：返回成功 | 404；409 最新请求是 `confirmed`/`superseded`，或没有请求 |

统一的 HTTP 映射：`404` 找不到（含非本人）、`409` 状态不允许、`503` 订单服务未配置、
`500` 存储失败。所有动作的响应都只包含服务端数据库里的事实。

## 6. 接口

### 6.1 对话响应

`CommerceIntentResponse` 新增可选字段：

```json
{
  "final_text": "...",
  "cards": [...],
  "pending_draft": {
    "draft_id": "drf_...",
    "status": "pending",
    "currency": "CNY",
    "total": "178.00",
    "items": [{"product_id": "...", "sku_id": "...", "title": "...",
               "quantity": 2, "unit_price": "89.00", "currency": "CNY"}],
    "created_at": "2026-11-05T..."
  }
}
```

服务端在 `run_turn` 结束后，按 `(buyer_id, shopping_session_id)` 从数据库查询当前
`pending` 草稿；没有就返回 `null`。它不依赖模型是否在本轮提到草稿，也**不采信**
模型在 `summary` 里写的草稿号、金额——即使在另一轮产生了草稿，本轮响应照样能把它带给
前端。页面刷新后由 `GET /orders/drafts` 重新取回。

### 6.2 订单接口（挂在 `/commerce`）

- `GET /orders/drafts?buyer_id=...&shopping_session_id=...` — 当前 pending 草稿或 `null`
- `POST /orders/drafts/{draft_id}/confirm` — body `{buyer_id}`
- `POST /orders/drafts/{draft_id}/cancel` — body `{buyer_id}`
- `GET /orders/{order_id}?buyer_id=...`
- `POST /orders/{order_id}/cancellation` — body `{buyer_id, reason?, idempotency_key}`
- `POST /orders/{order_id}/cancellation/confirm` — body `{buyer_id}`
- `POST /orders/{order_id}/cancellation/withdraw` — body `{buyer_id}`

### 6.3 CLI

新增命令，全部走同一个 `OrderService`（应用层动作，不经过模型）：

- `/draft` 查看当前会话的 pending 草稿
- `/confirm <draft_id>` 确认草稿
- `/cancel-draft <draft_id>` 作废草稿
- `/order <order_id>` 查订单
- `/cancel-order <order_id> [reason]` 发起取消请求
- `/confirm-cancel <order_id>` 确认取消
- `/withdraw-cancel <order_id>` 撤回取消请求

每次对话后如果产生了 pending 草稿，REPL 直接打印草稿摘要和确认命令。

## 7. Agent 侧

### 7.1 身份

`SessionStore.get_or_create` 往 `state.middle_context[IDENTITY_STATE_KEY]` 里补
`shopping_session_id`，与 `buyer_id` 并列。CLI 使用
`{"buyer_id": "cli", "shopping_session_id": "cli"}`。两个交易工具都从注入的
`AgentState` 读取身份，不接受模型传入的买家/会话参数。

### 7.2 Prompt

新增 `ORDER_GUIDANCE`，仅在交易能力开启时拼接：

- 只有用户明确表达购买意图（「下单」「买这个」「来两个」）才建草稿；浏览、比价、
  收藏都不建。
- 草稿行只能来自本轮或本会话 `product_search` 返回过的 SKU；数量默认 1，用户说了才改。
- 草稿不是订单；**你没有确认能力**，必须告诉用户到应用界面确认，禁止说「已下单」
  「订单已创建」。
- 工具返回 `ok=false` 时，把原因用用户能懂的话说明，必要时重新检索，不要假装成功。
- 取消订单不归你管；用户要取消时，引导他去应用界面发起取消。
- 仍然遵守「summary 只描述本轮」。

### 7.3 装配

- `Settings` 新增 `transaction_enabled: bool = True`、
  `orders_db_path: Path = data/sessions/orders.sqlite3`、
  `evidence_retention_days: int = 30`。
- `main.py` 的 lifespan 无论模型是否配置都创建 `OrderService`，放到
  `app.state.order_service`；`SessionStore` 的工厂把 service 传给 `build_agent`。
- `build_agent(..., order_service=None)`：非空且 `transaction_enabled` 时注册两个交易工具、
  追加 `ORDER_GUIDANCE`、放行工具权限；同时把证据存储传给主 Agent 的
  `make_product_search_tool`。S9 子 Agent 的 `product_search` 不传证据存储（没有买家身份）。
  为空时行为与 S9 完全一致（现有测试不受影响）。
- `run_turn(..., order_service=None)`：每轮结束后按 `(buyer, session)` 查询当前 pending
  草稿，填进 `pending_draft`。
- CLI 自己创建 `OrderService`，并传给 `build_agent`。

## 8. 关键决策（实现者可直接照做）

1. **草稿与订单分表**。模型只能写草稿表；`orders` 只由应用层确认事务写入。这是可测试的
   结构安全边界，也是「确认后才创建」的字面实现。
2. **一会话一 pending 草稿**。新草稿在同一事务里把旧的置 `superseded`；部分唯一索引兜底。
3. **证据落表，不只靠上下文**。`product_search` 把每条命中快照 upsert 到
   `product_evidence`（按会话 + SKU 唯一，最近一次观测覆盖旧的）；草稿优先读证据表，
   表里没有再回退上下文或压缩摘要附录。这样 S7 压缩后仍能「就买刚才那个」；本期不校验
   价格是否变化，最近一次观测价即快照。
4. **仅接受 `product_search` 命中作为草稿证据**。S9 派发回执缺少币种字段，不足以定价；
   需要下单时先对具体 SKU 做一次 `product_search`。压缩摘要里的受保护商品事实
   （SKU / 标题 / 价格 / 币种，由 `facts_from_appendix` 解析）视为同等证据。
5. **工具校验收到的失败用 `ok=false`**，不用 ERROR。现有 `run_turn` 会把本回合任何
   ERROR 变成整轮 502，交易参数错误不该有这种后果。因此交易工具在 schema 层把所有参数
   都设成可省略（`items` 默认空列表），真正的校验在函数体里做——框架层的参数校验错误
   同样会变成 ERROR 工具块，必须绕开。
6. **取消是应用层两步**：发起请求和确认请求都是应用接口，模型没有取消工具。
7. **草稿作废无二次确认**，因为它没有副作用。
8. **币种与量化**：工具接受一个展示币种（默认 CNY），逐行用 `fx.convert` 换算；换算后
   按 ISO-4217 小数位（CNY/USD/EUR/SGD=2，JPY=0，未知=2）用 `ROUND_HALF_UP` 量化单价，
   行小计 = 量化单价 × 数量，订单总额 = 各行小计之和。命中里的价格/币种另存
   `evidence_unit_price` / `evidence_currency`，含义是「换算前的那份证据」。
9. **数量**：1–99；未说明按 1。
10. **幂等**：草稿不给模型幂等键；同一会话里行项目和币种完全相同的 pending 草稿直接复用
    （`replayed=true`），否则替换。订单由 `draft_id` 唯一性保证；取消请求
    `(buyer_id, idempotency_key)` 唯一且每单最多一条 pending。
11. **待确认草稿可从数据库重新取得**：`/commerce/intents` 每轮都返回当前 pending 草稿，
   另有 `GET /orders/drafts` 供页面刷新后取回；不依赖某一轮的模型回执。
12. **证据保留期默认 30 天**（`evidence_retention_days`）：启动时清理一次，之后每天第一次
   写入时惰性清理；同一 SKU 只留最新一行，行数有界。子 Agent 没有买家身份，不写证据表。

## 9. 验收清单

1. 模型建草稿后，`/commerce/intents` 返回 `pending_draft`，且 `orders` 表仍为空。
2. 确认草稿只产生一张订单，事件齐全；重复确认返回同一张订单。
3. 建新草稿会顶掉旧草稿；确认旧草稿返回 409。
4. 用别的 `buyer_id` 确认草稿返回 404。
5. 作废草稿不产生订单。
6. 发起取消请求后订单仍为 `created`；确认取消后订单与请求在同一事务里迁移。
7. 撤回取消请求后订单仍为 `created`。
8. 未检索过的 SKU、数量 0、空 items 都返回 `ok=false`，整轮不 502。
9. prompt 明确禁止模型声称「已下单」，并引导到应用层确认。
10. 相同行项目与币种的重复建草稿返回同一份（`replayed=true`），不会顶掉自己。
11. 页面刷新后 `GET /orders/drafts` 能取回 pending 草稿；未建草稿的轮次
    `/commerce/intents` 也返回当前 pending 草稿。
12. 行小计之和严格等于订单总额；JPY 金额没有小数位。
13. 上下文里已经没有该 SKU 的检索明细时，只要证据表有快照，草稿仍能创建。
14. 保留期外的证据会被清理，保留期内的不受影响。
15. `pytest` 全绿、`ruff`/`mypy` 干净，测试不调用远程 embedding。

## 10. 测试策略

- **存储/服务层**：`tmp_path` 下建库，覆盖状态机、部分唯一索引、事务原子性、幂等、
  跨买家隔离、事件完整性。不需要模型和 embedding。
- **证据层**：同一 SKU 的新观测覆盖旧价；证据按会话隔离；保留期外的行被清理，期内保留
  （注入假时钟，避免等待）。
- **工具层**：构造带 `product_search` 回执的假 `AgentState`，覆盖证据匹配、价格换算、
  重复 SKU、数量边界、`ok=false` 形状、绝不写 orders；再单独覆盖「上下文为空但证据表
  有快照」仍能建草稿，以及「子 Agent 无身份不写证据」。
- **路由层**：假 service 覆盖 404/409/503 映射与响应形状。
- **对话层**：假 Agent 产出草稿回执，断言 `pending_draft` 来自数据库而不是模型文本。
- **Prompt**：断言交易指引只在开启时出现，且包含「不能确认 / 禁止已下单」。

## 11. 留给实现者的选择

- `features/orders/` 内部的模块拆分与类名（本文只固定了表、状态、契约和接口）。
- SQL 语句细节、错误类命名、事件 `metadata` 的具体 JSON 字段。
- CLI 输出排版。
- 测试文件划分与命名。
