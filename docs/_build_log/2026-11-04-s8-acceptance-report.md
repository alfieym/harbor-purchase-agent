# S8 长期记忆偏好验收报告

- 日期：2026-11-04
- 范围：三作用域偏好记忆（turn / session / long_term）+ 五工具（add / confirm /
  cancel / update / delete）+ 结构化 dislike + 版本冲突 + 200 条上限
- 基线：`make check` → **342 passed**（lint / format / mypy / pytest 全绿）
- 真额度：DashScope `qwen-plus`（真实模型）+ 确定性假 embedder（**零 embedding 调用**）
- 脚本：`backend/scripts/accept_s8_real.py`
- 设计：[s8-preference-memory-design-notes.md](../s8-preference-memory-design-notes.md)

## 设计要点

1. **作用域只决定存在哪，不决定谁优先**。turn 在进程内、session 在
   `state.middle_context`（随 S6 快照恢复）、long_term 在 SQLite。三段同时注入，
   冲突由模型按「作用域越窄越贴合当下」自己权衡——不做确定性仲裁。
2. **事实与向量同一行、同一事务**。`memories` 表的 text 和 float32 BLOB vector 在同一条
   INSERT/UPDATE 里写；embedding 在事务开启前完成，失败则一行都不落。
3. **两段式写入**。`session` / `long_term` 的 `add_preference` 只把操作放进
   `state.middle_context` 的待确认队列；用户确认后 `confirm_preference` 才落库。
   待确认队列跨轮、跨快照保留，下一轮注入「还有待确认的记忆操作」。
   `turn` 立即生效（只活一轮）。这是对 v1「写穿」的取代。
4. **失败不拖垮购物轮**：所有记忆失败都返回 `{"ok": false, ...}` 的 SUCCESS 结果，
   而不是 ERROR（S5 的 ERROR 会映射成 502）。
5. **身份不改工厂签名**：`SessionStore` 把 `buyer_id` 写进
   `state.middle_context["harbor.identity"]`，`build_agent` 从那里读；25 处
   `agent_factory(state)` 测试零改动。
6. **S7 不回退**：未接 `PreferenceMemory`（或三段全空且没有待确认操作）时，注入回退到
   S7 的启发式 + 附录；显式 session 记录与启发式按 `_preference_key` 去重合并，
   显式记录优先（它带 id 和 version）。

## 验收清单落点

| ID | 清单 | 实现 | 结果 |
|---|---|---|---|
| S8-1 | 「以后只看官方店」→ 确认后写入；新会话生效 | stage → 注入待确认 → `confirm_preference` → SQLite | ✓ |
| S8-2 | 「以后不用避开塑料了」→ 撤回过期偏好 | `delete_preference` 删 dislike，黑名单立即消失 | ✓ |
| S8-3 | 同一句话写两次只留一条 | `(buyer_id, kind, text_key)` 唯一索引 + 代词归一 | ✓ |
| S8-4 | 旧 version 更新返回 409，不覆盖新值 | `version` 列 + 写锁内比对 + 409 body | ✓ |
| S8-5 | 批量写 201 条：第 201 条被拒，dislike 豁免 | 事务内 `max_count=200` | ✓ |
| S8-6 | 未确认不落库；下一轮提示待确认 | pending 队列 + 注入块 | ✓ |
| S8-7 | 「这次预算 2000」不进长期记忆 | turn 只写进程内 `_turn`，不碰 SQLite | ✓ |
| S8-8 | 材质黑名单由代码硬过滤 | `product_search` 合并 `blacklisted_material_tags` | ✓ |

## 真额度结果（v2）

```
✓ S8-6 未确认时只暂存，不落库 — pending=['以后只看官方店']；rows=[]
✓ S8-6 下一轮提示待确认操作 — <system-info>还有待确认的记忆操作（尚未写入）：
✓ S8-1 确认后写入长期记忆 — rows=['以后只看官方店']；待确认=0
✓ S8-1 新开会话仍然生效 — [长期] pref_... 以后只看官方店（v1）
✓ S8-2 撤回后不再过滤塑料 — before=('塑料',) after=()
✓ S8-3 同一句话两次只留一条 — duplicate=True rows=['以后只看官方店']
✓ S8-4 旧 version 返回 409 且不覆盖 — stale=version_conflict/409 current=…官方旗舰店
✓ S8-5 第 201 条被拒，dislike 不受限 — rejected=limit_reached count=200
✓ S8-7「这次」不进入长期记忆 — turn=['预算 2000 元以内'] 长期2000=[]
✓ S8-8 材质黑名单由代码合并进检索 — specs=[['塑料']]
✓ embedding 额度未消耗 — 本地 embed 207 次（无远程调用）
```

真额度还暴露了一个单测没覆盖的语义问题：模型会把「我以后只看官方店」重述成
「以后只看官方店」，两者语义相同但文本不同，去重失败。修法是把开头的
「我 / 我们 / 咱们」也当作噪声前缀（`_preference_key`），并补了一条单测
（RED → GREEN）。

## 离线测试覆盖

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_memory_vector.py` | 8 | pack/unpack 字节序、cosine 边界 |
| `tests/test_memory_store.py` | 25 | 单行同写、buyer 隔离、并发写、跨会话去重、旧库迁移（text_key / version / material_tags）、故障回滚、模型过滤、版本冲突、上限、kind 过滤 |
| `tests/test_preferences.py` | 26 | turn/session 生命周期、去重、代词归一、快照往返、long_term 缓存、会话合并 |
| `tests/test_preference_tools.py` | 17 | 五工具状态码、暂存/确认/取消、重复、409、embedding 失败、相似提醒 |
| `tests/test_context_governance.py` | 49 | S7 全量 + S8 三段注入、待确认提示、dislike 标签、版本、空段省略、S7 回退、合并 |
| `tests/test_s8_acceptance.py` | 10 | 清单 S8-1..S8-8 逐条（工具面） |
| `tests/test_factory.py` | +3 | governed 注册五件套、legacy 不注册、无身份关闭长期 |
| `tests/test_chat_service.py` | +1 | SessionStore 注入 buyer 身份 |

## 复审修复（2026-11-04）

复审指出：长期间接去重只比对 `PreferenceMemory` 的内存缓存，而缓存只在 `begin_turn`
加载。同一 buyer 的两个会话（或同一会话在下次 `begin_turn` 之前）先后 add 同一条偏好时，
两次都通过缓存检查、都 embed、都 INSERT，留下两条同义记录。

修复（RED→GREEN）：

- `memories` 表增加可空的 `text_key` 列与 `(buyer_id, kind, text_key)` 唯一索引；
  旧库在 `_init_schema` 里 `ALTER TABLE` 补齐。
- `SqliteMemoryStore.add_or_get` 在**同一个 `BEGIN IMMEDIATE` 事务**里先查后写，
  返回 `(record, created)`；`add` 保持原签名，委托给它。
- `PreferenceMemory._add_long_term` 先查库（跨会话的陈旧缓存），未命中才 embed；
  `add_or_get` 再兜住「查与写之间」的并发窗口。命中时连 embedding 都不花。

## 已知取舍

- **长期只注入最近 24 条**：语义检索注入留给后续；`search_similar` 目前只用于 add 时的
  「相似偏好提醒」。
- **上限拒绝发生在 confirm 时**：`add_preference` 只暂存，所以第 201 条要等到确认才
  知道被拒；这与两段式一致，用户不会「先被写入再被回滚」。
- **写入失败的待确认操作留在队列里**：用户腾出空间后还能确认；代价是注入会继续提示。
- **无身份调用**（部分单测 / 未传 state）：turn 与 session 照常，long_term 在暂存前
  返回 `retryable: false` 的「长期记忆未启用」。
- **CLI** 显式传 `buyer_id="cli"`，因此命令行也能用长期记忆。
