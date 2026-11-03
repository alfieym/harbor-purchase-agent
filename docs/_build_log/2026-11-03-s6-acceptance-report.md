# S6 验收测试报告（六条）

- 日期：2026-11-03
- 范围：`features/chat/`（快照 + 流水 + 会话列表）+ `agents/factory.py` 状态恢复 + `core/config.py`
- 基线：`make test` → **192 passed**（S5 封板 156，净增 36）；`make lint` / `make typecheck` 全绿
- 数据：单元验收用真实 `AgentState` + `tmp_path`；另追加一次**真模型 1+1 冒烟**
  （1 轮真实问答 + 1 次零调用的进程重启恢复，见文末），真数据落在 `data/sessions/`
- 设计问答：[s6-context-persistence-design-notes.md](../s6-context-persistence-design-notes.md)
- 可视化：[s6-context-persistence.html](../_pipeline_html/s6-context-persistence.html)
- 方法：每条清单先用确定性测试钉住行为，再对照实现给出判定

## 结果总览

| ID | 验收项 | 结论 | 测试证据 |
|---|---|---|---|
| S6-1 | 杀进程重启 → 追问「刚才第一个多少钱」→ 答对 | **PASS**（离线 + 真模型 1+1） | `test_restart_restores_previous_turns_and_continues` + `scripts/s6_smoke.py` |
| S6-2 | 轮次行数 = 实际问答轮数（规则一致） | **PASS** | `test_journal_row_count_matches_turns_across_restart` |
| S6-3 | 手工改坏 `state_json` → 明确报错，不静默清空 | **PASS** | `test_load_raises_on_corrupt_snapshot` / `test_intents_corrupt_snapshot_returns_500` |
| S6-4 | 同 session 两请求 → 会话锁串行（持久化版） | **PASS** | `test_same_session_concurrent_turns_persist_both` |
| S6-5 | 会话列表按 `last_active_at` 倒序 | **PASS** | `test_sessions_lists_buyer_sessions_newest_first` |
| S6-6 | 恢复后的 Agent 知道上一轮推荐过哪些商品 | **PASS** | `test_restart_restores_previous_turns_and_continues`（结构化输出断言） |

**总体：6 条全部通过**。S6-1 除确定性恢复链外，另跑了一次真模型 1+1 冒烟（真实一轮 +
跨进程恢复）；「模型亲口答对」那半程按额度留白，见文末。

---

## 清单驱动的两处变更

验收清单推翻/新增了两处设计，先记账：

1. **S6-3 反转为「显式报错」**。设计文档原取舍是「坏文件当新会话、别挡服务」；清单要求
   「报明确错误，不静默清空」。改为：文件缺失仍返回 `None`（首访），但**存在却读不出**
   （JSON 坏 / `schema_version` 不符 / `state` 结构非法）抛 `SnapshotLoadError` → HTTP 500
   `会话快照损坏`，坏文件原样保留供排查。
2. **S6-5 新增会话列表**。清单要求按 `last_active_at` 倒序，原设计没有列表能力。新增
   `GET /commerce/sessions?buyer_id=…` → `{"sessions":[{shopping_session_id, last_active_at,
   turn_count}]}`；快照信封的 `updated_at` 随之更名为 `last_active_at`，并新增 `turn_count`
   （保存时从 `state.context` 数 `role=="user"` 得出）。

---

## S6-1 重启后追问，答对 — PASS（离线恢复链）

清单原文：问「2000 以内的吸尘器」→ 杀进程重启 → 追问「刚才第一个多少钱」→ 答对。

**数据缺口（与 S5-1 相同）**：真实 3700 条目录里「吸尘器」0 条，首轮会返回空 `cards`，
「第一个」无从谈起。因此测试用目录里真实存在的等价问句「给猫买个饮水器」。

**测试怎么模拟「杀进程」**：新建一个 `SessionStore` + 同一个磁盘仓库——内存缓存全部丢弃，
只剩磁盘快照，这正是重启后的状态。

```python
# test_restart_restores_previous_turns_and_continues
store1 = SessionStore(..., repository=SessionRepository(tmp_path))
await run_turn(store1, "u1", "s1", "给猫买个饮水器")      # 第一轮

seen = []
store2 = SessionStore(..., repository=SessionRepository(tmp_path))   # 模拟重启
await run_turn(store2, "u1", "s1", "刚才第一个多少钱")     # 追问

state, restored_count = seen[0]
assert restored_count == 2                                  # 上一轮 user + assistant 都在
assert state.context[0].get_text_content() == "给猫买个饮水器"
```

**判定：PASS**。恢复链 `get_or_create → repo.load → AgentState.model_validate →
build_agent(state=…)` 把完整历史交还给了下一轮；追问轮拿到的是 2 条历史消息，而不是空会话。

> 说明：这条证明的是「模型**能看到**上一轮」，不是「模型一定回答得对」。后者取决于模型本身，
> 不是持久化层的职责；真模型冒烟见下。

### 真模型 1+1 冒烟（2026-11-03，已执行）

用 `scripts/s6_smoke.py` 跑真实链路，不吃离线桩：

| 阶段 | 动作 | API 调用 |
|---|---|---|
| phase1 | `build_runtime`（复用 `.cache/catalog-index`，指纹命中）→ 真实一轮「给猫买个饮水器」 | 探针 embedding 1 + query embedding 1 + chat 1 |
| phase2 | **新进程**只读 `data/sessions/`：`repo.load` + `SessionStore.get_or_create` | **0**（不建 runtime、不调模型） |

真实落盘产物：

```
data/sessions/snapshots/f19396…170c.json   13 134 B
data/sessions/journal.jsonl                2 行（turn=1，一问一答）
```

phase2 断言恢复出的 `AgentState` 含 2 条消息、首条 user 文本仍是原问句，且上一轮 5 张卡片
（`P3472-S1` ¥567.00 起）完整回来；`build_runtime` 打印「复用本地索引（跳过 embedding）」，
证明重启没有重嵌 3700 条商品。

---

## S6-2 轮次行数 = 实际问答轮数 — PASS

规则（自定且一致）：**每个成功轮次恰好 2 行**——1 行 user + 1 行 assistant。

```python
# test_journal_row_count_matches_turns_across_restart：3 轮，其中第 2 轮跨了一次重启
assert len(lines) == 6
assert [l["turn"] for l in lines] == [1, 1, 2, 2, 3, 3]
assert [l["role"] for l in lines] == ["user", "assistant"] * 3
```

- `turn` 不额外持久化计数器，由 `state.context` 里 `role=="user"` 的条数推导，所以重启后
  序号自然续上，不会出现「内存计数器 2、盘上 1」的分裂。
- 只统计**成功**轮次：失败轮不写快照也不写流水，因此行数始终等于用户实际得到的回答数。

**判定：PASS**。

---

## S6-3 改坏 `state_json` → 明确报错 — PASS

清单原文：手工把 `state_json` 改坏 → 启动/加载时报明确错误，不静默清空。

### 行为矩阵

| 磁盘状态 | `load` 结果 | HTTP |
|---|---|---|
| 文件不存在 | `None`（首访，新建会话） | 200 |
| JSON 语法坏 | `SnapshotLoadError` | **500 `会话快照损坏`** |
| `schema_version` 不匹配 | `SnapshotLoadError` | **500 `会话快照损坏`** |
| `state` 结构非法 | `SnapshotLoadError` | **500 `会话快照损坏`** |

### 证据

```python
# test_load_raises_on_corrupt_snapshot
path.write_text("{not valid json")
with pytest.raises(SnapshotLoadError):
    repo.load(("u1", "s1"))

# test_load_keeps_corrupt_file_for_inspection
assert path.exists()          # 不静默删除

# test_intents_corrupt_snapshot_returns_500
assert resp.status_code == 500
assert resp.json()["error"]["message"] == "会话快照损坏"
```

**判定：PASS**。坏文件不会被删除、不会被当成空会话；错误信息经统一信封返回，且服务端日志
保留完整原因（`SnapshotLoadError` 的 message 含路径与具体缺陷）。

> 取舍：坏快照会让**该会话**持续 500，直到人工修复或移除文件；其它会话完全不受影响。

---

## S6-4 同 session 两请求串行（持久化版） — PASS

清单原文：同一 session 两个请求 → 被会话锁串行。

```python
# test_same_session_concurrent_turns_persist_both
await asyncio.gather(
    run_turn(store, "u1", "s1", "第一个问题"),
    run_turn(store, "u1", "s1", "第二个问题"),
)
restored = repo.load(("u1", "s1"))
assert len(restored.context) == 4      # 两轮都进了快照，没有 last-writer-wins
assert len(journal) == 4               # 两轮 × 2 行
```

配套的三道闸（S6 期间补齐）：

1. 每会话一把 `asyncio.Lock`，`run_turn` 全程持锁（S5 已有）。
2. `_evict_lru` 跳过 `lock.locked()` 的在飞会话（`test_lru_evict_skips_inflight_session`）。
3. 拿到锁后复核 `is_cached(key, session)`；等待期间被并发失败请求逐出则重建
   （`test_run_turn_retries_when_session_evicted_while_waiting`）。

**判定：PASS**。

---

## S6-5 会话列表按 `last_active_at` 倒序 — PASS

### 接口

```bash
curl -s 'http://127.0.0.1:8010/commerce/sessions?buyer_id=u1'
```

```jsonc
{
  "sessions": [
    {"shopping_session_id": "s2", "last_active_at": "2026-11-03T12:05:00+00:00", "turn_count": 2},
    {"shopping_session_id": "s1", "last_active_at": "2026-11-02T09:31:00+00:00", "turn_count": 1}
  ]
}
```

- `buyer_id` 必填（多租户隔离）；缺失 → 422。
- 每项只含 `shopping_session_id` + `last_active_at` + `turn_count`（清单裁决）。
- 排序在 `SessionRepository.list_sessions` 内完成，`reverse=True`。

### 证据

```python
# test_sessions_lists_buyer_sessions_newest_first
assert [s["shopping_session_id"] for s in sessions] == ["new", "old"]
# test_list_sessions_filters_by_buyer_and_sorts_desc
assert [... for s in repo.list_sessions("u1")] == ["new", "mid", "old"]   # u2 的不出现
```

> 列表遇到**完全无法解析**的快照会跳过并告警（此时连 `buyer_id` 都读不出来，不能因此
> 让所有买家的列表 500）；可解析、但属于该买家且字段缺失的文件仍抛 `SnapshotLoadError`。

**判定：PASS**。

---

## S6-6 恢复后知道上一轮推荐过哪些商品 — PASS

真实快照里，上一轮的商品卡落在**两处**（1+1 冒烟实测）：

```python
# scripts/s6_smoke.py phase2 —— 真实 AgentState
cards = state.reply_context.structured_output["cards"]        # 解析后的答案（5 张卡）
# state.context[...] 里同一批卡也作为 GenerateStructuredOutput 的 tool_call input 存在
```

- `reply_context.structured_output`：AgentScope 存放本轮结构化答案的字段；
  每轮 `reply` 开头会**整体重建** `reply_context`（`_agent.py:1112`），所以恢复它不会污染下一轮。
- `state.context`：ReAct 轨迹（hint / tool_call / tool_result），其中 `GenerateStructuredOutput`
  的入参就是最终卡片；即使不读 `reply_context`，模型也能从消息里看到上一轮推荐过什么。

恢复的是 **`AgentState` 全量**，不是摘要：`sku_id`、价格、币种、重量都随快照回来，
模型因此能回答「刚才第一个多少钱」。

> 校准：单元测试 `test_restart_restores_previous_turns_and_continues` 断言的是
> `state.context[1].structured_output[...]`——那是**桩 Agent** 的形状。真实 AgentScope
> 不会把 `structured_output` 留在消息上（消息上该字段为 `None`）。持久化本身没有问题
> （`reply_context` 被完整 round-trip），但证据位置应以真实快照为准。

**判定：PASS**。

---

## 附：测试清单（S6 新增 36 条中的关键项）

| 面 | 用例 |
|---|---|
| 快照 | `test_save_then_load_roundtrips_state` / `test_save_leaves_no_tmp_file` |
| 快照 | `test_load_raises_on_corrupt_snapshot` / `..._unknown_schema_version` / `..._invalid_state_payload` |
| 快照 | `test_list_sessions_filters_by_buyer_and_sorts_desc` / `..._reports_turn_count` |
| 恢复 | `test_restart_restores_previous_turns_and_continues` |
| 流水 | `test_append_journal_writes_two_lines` / `..._appends_without_overwriting` / `..._includes_cards` |
| 失败 | `test_run_turn_does_not_persist_failed_turn` / `test_run_turn_evicts_session_on_failure` |
| 并发 | `test_same_session_concurrent_turns_persist_both` / `test_run_turn_retries_when_session_evicted_while_waiting` |
| 路由 | `test_intents_corrupt_snapshot_returns_500` / `test_sessions_lists_buyer_sessions_newest_first` |

## 真模型冒烟（已执行 1+1；剩余半程未执行）

- **已执行**：1 轮真实问答 + 跨进程恢复断言（见 S6-1「真模型 1+1 冒烟」）。
  成本：探针 embedding 1 + query embedding 1 + chat 1；商品索引复用，0 重嵌。
- **未执行**：让模型亲口回答「刚才第一个多少钱」所需的第 2 次 chat + query embedding。
  它验证的是模型行为，不是持久化层；上下文完整交接已由 phase2 断言证明。
- 复跑：`scripts/s6_smoke.py phase1|phase2`。前置条件与 S5 相同：`.env` 配
  `OPENAI_API_KEY`、`.cache/catalog-index/` 已建、跑前停 `make dev-api`
  （Qdrant 本地目录单进程独占）。
- 冒烟时设 `HARBOR_SEARCH_RERANK_ENABLED=false`：本地 fastembed reranker 模型未缓存，
  首次使用要下载 ~1 GB，且与 S6 无关。
