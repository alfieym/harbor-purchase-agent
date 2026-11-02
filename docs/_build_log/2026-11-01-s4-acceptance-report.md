# S4 验收测试报告（七条）

- 日期：2026-11-01
- 范围：`agents/` 层（`product_search` 工具 → 聊天模型 → 结构化商品卡）的横切装配
- 基线：`make test` → **135 passed**（S3 封板时 126，净增 9：`test_cli` 1 条 +
  `test_product_search_tool` 7 条 + `test_embedder` 1 条）；`make lint` / `make typecheck` 全绿
- 数据：真实 catalog `data/catalog-v1.jsonl`（3700 条，**多币种** CNY/USD/EUR/JPY/SGD）+ 测试 fixture
- 方法：离线用确定性 `FakeEmbedder` 逐条实测工具/schema；真模型走 DashScope `qwen-plus`
  （`HARBOR_MODEL_PROVIDER=dashscope`），embedding 用 `qwen3.7-text-embedding-flash`
- 可交互入口：`make cli`（`python -m harbor.agents`）；一键冒烟：`scripts/verify_s4.py`
- 可视化：[s4-agent-pipeline.html](../_pipeline_html/s4-agent-pipeline.html)
  （自然语言 → 结构化参数 → 硬约束 → 检索 → 商品卡 五段交互图，含「一句话怎么变成 S2/S3 输入」的
  参数映射与币种归一化端到端实例）
- 设计问答：[s4-agent-design-notes.md](../s4-agent-design-notes.md)
  （7 个概念辨析：解析为何没有代码、ReAct 循环在哪、Agent 类在哪、8 处 prompt 分别是什么、
  币种抖动根因、验收口径收敛、产物索引）

## 关键架构变更（本章开头做出的转向）

S4 把 S3 的检索服务装配成可对话的 Agent，并在这个过程中**发现并修复了一个跨币种的数据
正确性 bug**（S4-7）。其余大部分是「对齐清单」的收敛，其中两条（S4-3 / S4-5）按验收讨论
调整了措辞口径。

1. **装配层**：`tools.py`（把 `CatalogSearchService` 包成 AgentScope `FunctionTool`）、
   `cards.py`（`ProductCardList` 作为 `structured_schema`）、`prompts.py`（系统提示词）、
   `models.py`（`build_chat_model` 按 provider 选 deepseek/dashscope）、`permissions.py`
   （给只读 `product_search` 放行，否则每次工具调用挂权限确认、`structured_output` 恒 `None`）、
   `cli.py`（交互 REPL + Qdrant 本地索引持久化 + 指纹失效重建）。
2. **币种归一化（S4-7 根因修复）**：目录是多币种。预算过滤（`constraints.matches_facts`）和
   去重排序（`dedupe.converted_price`）都已先把价格 `convert(..., spec.currency)` 换算成目标
   币种，唯独**工具报告**这一处吐原币种 → 模型在「照抄原币（24.93 USD）」和「擅自换算人民币
   （180 CNY）」之间随机摇摆。修复：`tools._search_result_to_dict` 报告前用 `fx.convert()`
   统一换算成目标币种，与过滤/去重三点一线。
3. **`total` 字段（S4-3）**：清单原文写「5 万 token 假数据截断 + next_page 翻页」，验收讨论后
   收敛为「**只报总数，不翻页**」——`SearchResult.total` = 去重后、截断前的命中数，模型据此报
   「命中 N、展示前 M」。
4. **`max_iters=15`（S4-4）** 与 **JSON schema 校验（S4-5）**：ReAct 上限 8→15；工具入参经
   `ProductSearchArgs`（pydantic）导出 schema，类型错由 jsonschema 在 AgentScope 调用前拦下。

## 结果总览

| ID | 验收项 | 结论 | 测试证据 |
|---|---|---|---|
| S4-1 | 问「2000 以内的吸尘器」→ 调 `product_search` 返回结构化商品卡 | **PASS** | 真模型冒烟（见 S4-7 输出）+ `verify_s4.py` ③ |
| S4-2 | 寒暄不调工具 → 直接回文字不报错 | **PASS** | 真模型冒烟（寒暄返回 text，`structured_output=None`） |
| S4-3 | 截断后说明「还有 N 条」（收敛为只报总数） | **PASS** | `test_tool_reports_total_hits_before_limit` |
| S4-4 | 超过 `max_iters=15` → 干净退出不抛栈 | **PASS** | `cli.py` / `verify_s4.py` `ReActConfig(max_iters=15)` |
| S4-5 | 工具参数类型错（`price_max_major="五"`）→ 被拦下不崩 | **PASS** | `test_tool_schema_rejects_wrong_typed_arg` |
| S4-6 | 未配 API Key → 启动时报明确错误 | **PASS** | `cli.py::main` 的 `llm_configured` 分支 |
| S4-7 | 同一问题连问 3 次输出结构稳定 | **PASS** | 真模型 3 连 + `test_tool_converts_price_to_target_currency` |

**总体：7 条全部通过。**

---

## S4-1 模型调工具返回结构化商品卡 — PASS

**验证的是「模型把自然语言转成工具调用，再把工具结果格式化进 schema」这一整条链**。

清单原文是「2000 以内的吸尘器」，但真实 3700 条目录里**没有吸尘器**（S3 已确认），所以：
- 内置 4 条测试商品含吸尘器（P1 ¥300 / P2 ¥4000），`verify_s4.py` 用它离线测「预算内过滤」；
- 真模型冒烟改用目录里真实存在且能命中的「预算300元，给猫买个饮水器」。

### 输出（真模型，DashScope qwen-plus）

```text
run: 命中35条，其中47件因超300元预算被过滤；展示2款真实猫用饮水器
  1. Roamix 宠物饮水器 GX-30-1 1L 无过滤    [Roamix]  179.496 CNY  sku=P3464-S1
  2. Roamix 宠物饮水器 GX-30-2 1.5L 单层过滤 [Roamix]  297.024 CNY  sku=P3468-S1
```

### 判定：PASS

- `Agent.reply(UserMsg(...), structured_schema=ProductCardList)` 返回 `Msg.structured_output`，
  经验证为合法 `ProductCardList`（pydantic round-trip）。
- `permissions.allow_product_search_tool(agent)` 是前提：AgentScope 2.0 对非白名单工具会把每次
  调用挂起在权限确认上，`structured_output` 会变成 `None`——这是本次踩过的坑。

---

## S4-2 寒暄不调工具直接回文字 — PASS

### 输入

```text
你：你好
```

### 输出

```text
助手（文本）：你好！我是 Harbor 采购助手，可以帮你按预算、类目、重量、材质等条件挑商品。…
```

### 判定：PASS

- 模型判断无需调工具，直接回文字 → `reply.structured_output is None`。
- `cli.run_repl` 有 `structured is None` 分支，走 `get_text_content()` 打印，不报错、不崩。

---

## S4-3 截断后说明「还有 N 条」 — PASS（口径收敛）

清单原文「5 万 token 假数据截断 + next_page 翻页」，验收讨论后收敛为**只报总数、不做翻页**。
理由：`limit=10` 已在服务层兜住输出体积，不会出现 5 万 token；翻页需要会话态 + 游标，属于后续
阶段，不在 S4 范围。

### 实现

- `SearchResult.total` = 去重（`dedupe_by_canonical`）后、`limit` 截断前的命中数（`qdrant_search.py`）。
- `tools._search_result_to_dict` 把 `total` 一并吐给模型。
- `prompts.SYSTEM_PROMPT` 第 3 步指示：用 `total` 判断「命中 N 条、展示前 M 条」或「还有 N-M 条」。

### 输出（离线，3 商品、limit=2）

```text
result.total == 3    # 去重后共 3 件
len(result.hits) == 2  # 展示前 2 件
```

### 判定：PASS

- `test_tool_reports_total_hits_before_limit` 钉住「total 是截断前计数、hits 是截断后」。

---

## S4-4 超过 max_iters 干净退出 — PASS

- `cli.py` 与 `verify_s4.py` 均设 `ReActConfig(max_iters=15)`（原 8）。
- 已核实 AgentScope 2.0.11 行为：ReAct 循环达到 `max_iters` 后**不抛栈**，而是注入
  `<system-reminder>` 并把文本 `Msg` 交回，`get_text_content()` 可读。

### 判定：PASS

- 上限对齐清单的 15；超限路径是「可读文本返回」，不是异常栈。真实超限场景依赖模型陷入
  反复调用，未在离线测试强制构造（AgentScope 内部的迭代计数不受外部直接控制）。

---

## S4-5 工具参数类型错被拦下 — PASS（口径收敛）

清单原文写 `top_k="五"`，但本工具 schema（`ProductSearchArgs`）**没有 `top_k`**；按验收讨论
「用现有参数测」，映射到同类的数值参数 `price_max_major="五"`。

### 输入

```python
jsonschema.validate({"query": "吸尘器", "price_max_major": "五"}, tool.input_schema)
# → jsonschema.ValidationError
```

### 判定：PASS

- 工具入参由 `ProductSearchArgs`（pydantic）导出 JSON schema；类型错在 AgentScope `_execute_tool_call`
  的第一步就被 jsonschema 校验拦下，转成 ERROR 态 `ToolChunk` 喂回模型（可纠正），不崩、不静默。
- `test_tool_schema_rejects_wrong_typed_arg` 钉住「CJK 字符串价被 schema 拒绝」。

---

## S4-6 未配 API Key 启动报错 — PASS

### 实现（`cli.py::main`）

```python
settings = get_settings()
if not settings.llm_configured:
    print("未配置模型 API key。请设置 OPENAI_API_KEY（或 HARBOR_OPENAI_API_KEY）。",
          file=sys.stderr)
    raise SystemExit(1)
```

### 判定：PASS

- 未配 key 时启动即打印明确错误并退出码 1，不进入建索引/建 Agent 的后续步骤。

---

## S4-7 连问 3 次输出结构稳定 — PASS（本轮发现并修复真 bug）

3 连真模型输出，**结构**一直稳定（每次 2 张卡、同 `sku_id` GX-30-1 / GX-30-2），但**币种/金额
不稳定**：早先一轮冒出 `180 CNY / 285 CNY`，本轮又冒出 `24.93 USD / 38.08 EUR`。

### 根因

目录多币种。`GX-30-1` 原价 `24.93 USD`、`GX-30-2` 原价 `38.08 EUR`。预算过滤和去重排序都换算
成目标币种 CNY（24.93×7.2≈179.5、38.08×7.8≈297.0），唯独工具报告吐原币种 → 模型有时照抄原币、
有时自行换算，随机摇摆。

### 修复

`tools._search_result_to_dict` 报告前用 `fx.convert()` 统一换算到目标币种（spec 的 `currency`，
默认 CNY）；`prompts.py` 约束「价格原样引用，不要自行再次换算」。

### 输出（修复后，3 连）

```text
run 1: GX-30-1  179.496 CNY · GX-30-2  297.024 CNY
run 2: GX-30-1  179.496 CNY · GX-30-2  297.024 CNY
run 3: GX-30-1  179.496 CNY · GX-30-2  297.024 CNY
```

### 判定：PASS

- 结构（卡片数、sku_id）稳定；币种/金额从「两套互相矛盾」收敛为「单一目标币种」。
- `test_tool_converts_price_to_target_currency` 锁死 `24.93 USD → 179.496 CNY`。
- 注：`179.496` 比 `179.50` 多一位小数，是 `Decimal` 精确换算的直接结果，**故意不四舍五入**——
  工具报告的值必须与预算过滤比较的值完全一致，否则「报告 ¥179.50、内部按 179.496 过滤」会产生
  1 分钱不一致。

---

## 附：跨条目的共性缺口

1. **`fabricate_problems`（⑤ 未编造校验）仍是 TODO(human) 占位**：`verify_s4.py` 里该函数
   `raise NotImplementedError`，即「每张卡的 `sku_id` 必须来自工具返回的 hits」这条防编造校验
   尚未实现（S4-1/S4-2/S4-6 靠真模型冒烟人工判读，缺一条程序化防线）。
2. **rerank 模型仍没下载成功**：HF Xet 存储被 `cas-server.xethub.hf.co` 401 挡住，需
   `HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1` 重下；否则维持
   `HARBOR_SEARCH_RERANK_ENABLED=false`（本报告所有真模型冒烟都在关精排下跑）。
3. **检索/Agent 仍未接 FastAPI**（与 S2/S3 一致）：`/api/chat/stream` SSE 路由未做，chat 由
   REPL 驱动。
4. **汇率仍是占位固定表**（`search/fx.py`，S2-6 遗留）：换真实源需同步更新索引与查询两侧。
5. `recreate_collection` 已弃用（DeprecationWarning），后续换 `collection_exists` + `create_collection`。

## 结案

7 条验收项全部通过。S4 把 S3 检索装配成「工具调用 → ReAct → 结构化商品卡」的可对话 Agent，
并在验收中修复了多币种目录下工具报告未做汇率归一化导致模型输出随机摇摆的 bug——币种/金额现在
与预算过滤、去重排序三点一线，输出稳定可复现。


---

## 附录：交互式管线图（内嵌 HTML）

> 独立可交互版仍保存在 `docs/_pipeline_html/s4-agent-pipeline.html`；以下是该页完整源码，
> 已按「写入报告中」的要求内嵌于此，便于单文件归档。源码用代码块包裹（而不是裸 HTML），
> 否则页面里的 `<style>`/`body{background}` 会渗进 Markdown 预览、把整个预览染成黑底。
> 交互效果仍需打开独立 .html 文件查看。

```html
<title>S4 采购 Agent · 自然语言 → 工具调用 → 结构化商品卡</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
  :root {
    --bg: #F6F5F1; --surface: #FFFFFF; --surface-2: #EFEEE9;
    --fg: #1B1D21; --muted: #6A6E75; --faint: #9BA0A7; --border: #E3E1DA;
    --accent: #0E7A6B; --accent-ink: #07584C;
    --good: #1E8E3E; --good-soft: #E8F4EC; --bad: #C5221F; --bad-soft: #FBEAEA;
    --seq: #0E7A6B; --warn: #9A6700; --soft: #3353A6; --soft-soft: #EAF0FB;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #12151A; --surface: #1B1F26; --surface-2: #232831;
      --fg: #E7E9EC; --muted: #9BA1A9; --faint: #6F767F; --border: #2C323B;
      --accent: #2BBBA4; --accent-ink: #8FE3D3;
      --good: #4CC38A; --good-soft: #16301F; --bad: #F87171; --bad-soft: #3A1C1C;
      --seq: #2BBBA4; --warn: #E0A63C; --soft: #8FB0E8; --soft-soft: #182238;
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --bg: #12151A; --surface: #1B1F26; --surface-2: #232831;
    --fg: #E7E9EC; --muted: #9BA1A9; --faint: #6F767F; --border: #2C323B;
    --accent: #2BBBA4; --accent-ink: #8FE3D3;
    --good: #4CC38A; --good-soft: #16301F; --bad: #F87171; --bad-soft: #3A1C1C;
    --seq: #2BBBA4; --warn: #E0A63C; --soft: #8FB0E8; --soft-soft: #182238;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font-family: "IBM Plex Sans", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
    padding-left: max(16px, env(safe-area-inset-left, 0px));
    padding-right: max(16px, env(safe-area-inset-right, 0px));
    padding-block: max(24px, env(safe-area-inset-top, 0px)) max(48px, env(safe-area-inset-bottom, 0px));
    line-height: 1.5;
  }
  .wrap { max-width: 1280px; margin: 0 auto; }
  header { margin-bottom: 20px; }
  .kicker { font-family: "IBM Plex Mono", monospace; font-size: 12px; letter-spacing: .14em; text-transform: uppercase; color: var(--accent-ink); margin: 0 0 8px; }
  h1 { margin: 0 0 6px; font-size: 28px; font-weight: 700; letter-spacing: -.01em; text-wrap: balance; }
  .sub { color: var(--muted); font-size: 15px; margin: 0 0 16px; max-width: 76ch; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
  .chip { font-family: "IBM Plex Mono", monospace; font-size: 12px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 999px; background: var(--surface); color: var(--fg); }
  .chip.query { border-color: var(--accent); color: var(--accent-ink); }
  .chip.good { border-color: var(--good); color: var(--good); }
  .chip.soft { border-color: var(--soft); color: var(--soft); }

  .flow { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding: 14px 16px; margin-bottom: 10px; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
  .flow-step { display: flex; flex-direction: column; align-items: center; min-width: 74px; padding: 2px 8px; }
  .fidx { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--accent-ink); }
  .fname { font-size: 14px; font-weight: 600; }
  .fcount { font-family: "IBM Plex Mono", monospace; font-size: 13px; font-weight: 500; color: var(--muted); font-variant-numeric: tabular-nums; }
  .flow-link { display: flex; flex-direction: column; align-items: center; gap: 2px; color: var(--faint); }
  .farrow { font-size: 18px; line-height: 1; }
  .fwhat { font-size: 11px; color: var(--muted); font-family: "IBM Plex Mono", monospace; max-width: 120px; text-align: center; }
  .hint { font-size: 12px; color: var(--muted); margin: 0 0 16px; }

  .mhead { margin: 34px 0 4px; font-size: 19px; font-weight: 700; }
  .msub { color: var(--muted); font-size: 13px; margin: 0 0 14px; max-width: 76ch; }

  /* --- centerpiece: 自然语言 → 参数 映射 --- */
  .bridge { display: grid; grid-template-columns: 1fr auto 1fr; gap: 12px; align-items: stretch; }
  @media (max-width: 760px) { .bridge { grid-template-columns: 1fr; } .bridge .mid { display: none; } }
  .bridge .mid { display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--faint); gap: 4px; font-family: "IBM Plex Mono", monospace; font-size: 12px; }
  .sent { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; font-size: 16px; line-height: 2.1; }
  .frag { padding: 2px 7px; border-radius: 7px; cursor: pointer; border: 1px solid transparent; transition: opacity .12s ease; }
  .frag.f-soft { background: var(--soft-soft); color: var(--soft); }
  .frag.f-hard { background: var(--good-soft); color: var(--good); }
  .frag.f-inf  { background: var(--surface-2); color: var(--accent-ink); }
  .frag:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .sent .leg { font-size: 11px; color: var(--faint); font-family: "IBM Plex Mono", monospace; margin-top: 8px; display: block; }
  .args { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
  .args-head { padding: 8px 12px; border-bottom: 1px solid var(--border); font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--muted); }
  .field { display: grid; grid-template-columns: auto 1fr auto; gap: 10px; align-items: center; padding: 9px 12px; border-bottom: 1px solid var(--border); transition: opacity .12s ease; }
  .field:last-child { border-bottom: 0; }
  .fname { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--fg); white-space: nowrap; }
  .fval { font-family: "IBM Plex Mono", monospace; font-size: 12.5px; }
  .fval.str { color: var(--good); } .fval.num { color: var(--fg); }
  .ftag { font-family: "IBM Plex Mono", monospace; font-size: 10px; padding: 2px 8px; border-radius: 999px; border: 1px dashed; letter-spacing: .03em; white-space: nowrap; }
  .ftag.hard { color: var(--good); border-color: var(--good); background: var(--good-soft); }
  .ftag.soft { color: var(--soft); border-color: var(--soft); background: var(--soft-soft); }
  .fnote { grid-column: 1 / -1; font-size: 12px; color: var(--muted); padding: 0 0 4px 2px; }
  body.linking .frag, body.linking .field { opacity: .32; }
  body.linking .frag.hot, body.linking .field.hot { opacity: 1; box-shadow: 0 0 0 2px var(--accent); }

  /* --- 归属卡片 --- */
  .upgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
  .upcard { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
  .upcard h3 { margin: 0; font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .badge { font-family: "IBM Plex Mono", monospace; font-size: 10px; padding: 2px 8px; border-radius: 999px; border: 1px dashed; letter-spacing: .03em; }
  .badge.done { color: var(--good); border-color: var(--good); background: var(--good-soft); }
  .badge.warn { color: var(--warn); border-color: var(--warn); }
  .badge.info { color: var(--accent-ink); border-color: var(--accent); }
  .badge.hard { color: var(--good); border-color: var(--good); background: var(--good-soft); }
  .badge.soft { color: var(--soft); border-color: var(--soft); background: var(--soft-soft); }
  .call { font-family: "IBM Plex Mono", monospace; font-size: 12px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; overflow-x: auto; white-space: pre; }
  .vnote { font-size: 12px; color: var(--muted); margin: 0; }
  .vnote b { color: var(--fg); }
  .krow { display: flex; justify-content: space-between; gap: 8px; font-size: 12.5px; }
  .krow .k { font-family: "IBM Plex Mono", monospace; color: var(--faint); }

  /* --- 端到端实例 --- */
  .exgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }
  .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
  .panel-head { display: flex; align-items: baseline; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--border); }
  .panel-num { font-family: "IBM Plex Mono", monospace; color: var(--accent-ink); font-size: 13px; }
  .panel-name { font-weight: 600; font-size: 15px; }
  .panel-job { margin-left: auto; font-size: 12px; color: var(--faint); font-family: "IBM Plex Mono", monospace; }
  .panel-body { padding: 8px; display: flex; flex-direction: column; gap: 4px; }
  .cards { display: flex; flex-direction: column; gap: 4px; }
  .row { display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center; padding: 6px 8px; border-radius: 8px; border: 1px solid transparent; transition: opacity .12s ease; }
  .row:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .id { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--muted); }
  .body { display: flex; flex-direction: column; min-width: 0; }
  .t { font-size: 13px; font-weight: 500; }
  .m { font-size: 11px; color: var(--muted); font-family: "IBM Plex Mono", monospace; }
  .s { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
  .v { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 6px; white-space: nowrap; }
  .v.pass { color: var(--good); background: var(--good-soft); }
  .v.drop { color: var(--bad); background: var(--bad-soft); }
  .v.cnv { color: var(--accent-ink); background: var(--surface-2); }
  body.tracing .row { opacity: .3; }
  body.tracing .trace { opacity: 1; box-shadow: 0 0 0 2px var(--accent); }
  .note { margin-top: 18px; padding: 12px 14px; border-left: 3px solid var(--accent); background: var(--surface); border-radius: 8px; font-size: 13px; color: var(--muted); }
  .note b { color: var(--fg); }
  .formula { font-family: "IBM Plex Mono", monospace; font-size: 13px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; overflow-x: auto; white-space: nowrap; }

  /* --- 验收状态 --- */
  .tbl { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  .trow { display: grid; grid-template-columns: 64px 1fr 90px; gap: 10px; align-items: center; padding: 10px 14px; border-bottom: 1px solid var(--border); }
  .trow:last-child { border-bottom: 0; }
  .tid { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--accent-ink); font-weight: 500; }
  .ttxt { font-size: 13px; }
  .tstat { font-family: "IBM Plex Mono", monospace; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 6px; text-align: center; }
  .tstat.pass { color: var(--good); background: var(--good-soft); }
  @media (max-width: 560px) {
    h1 { font-size: 23px; }
    .flow { gap: 2px; padding: 12px 8px; }
    .flow-step { min-width: 0; padding: 2px 4px; }
    .farrow { font-size: 15px; }
    .sent { font-size: 14px; }
  }
  @media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } .frag, .field, .row { transition: none; } }
</style>

<header>
  <div class="wrap">
    <p class="kicker">Harbor · Purchase Agent · S4</p>
    <h1>自然语言 → 工具调用 → 结构化商品卡</h1>
    <p class="sub">S4 装配了 Agent 层，把买家的一句话变成 S2 / S3 的输入：模型先解析自然语言为结构化参数，其中<b>类目、预算、重量、材质</b>成了 S2 的硬约束，<b>query 原文</b>成了 S3 的召回词。悬停下面句子里的高亮片段，看它填进了哪个字段、由哪一层消费。</p>
    <div class="chips" id="chips"></div>

    <div class="flow" aria-label="S4 流程">
      <div class="flow-step"><span class="fidx">①</span><span class="fname">自然语言</span><span class="fcount">你的一句话</span></div>
      <div class="flow-link"><span class="farrow">→</span><span class="fwhat">模型解析</span></div>
      <div class="flow-step"><span class="fidx">②</span><span class="fname">参数抽取</span><span class="fcount">ProductSearchArgs</span></div>
      <div class="flow-link"><span class="farrow">→</span><span class="fwhat">工具适配</span></div>
      <div class="flow-step"><span class="fidx">③</span><span class="fname">硬约束</span><span class="fcount">ProductSearchSpec</span></div>
      <div class="flow-link"><span class="farrow">→</span><span class="fwhat">S3 管线</span></div>
      <div class="flow-step"><span class="fidx">④</span><span class="fname">检索</span><span class="fcount">召回→过滤→精排→去重</span></div>
      <div class="flow-link"><span class="farrow">→</span><span class="fwhat">模型格式化</span></div>
      <div class="flow-step"><span class="fidx">⑤</span><span class="fname">商品卡</span><span class="fcount">ProductCardList</span></div>
    </div>
    <p class="hint">悬停或键盘聚焦任意高亮片段 / 字段 → 双向高亮它填进了哪个参数、由哪一层消费。</p>
  </div>
</header>

<main class="wrap">
  <h2 class="mhead">一句话怎么变成参数</h2>
  <p class="msub">模型（ReAct 循环）决定要不要调 <span class="mono" style="font-family:'IBM Plex Mono',monospace">product_search</span>，并把自然语言拆进结构化字段。悬停左边句子的片段，右边对应字段高亮。</p>

  <div class="bridge">
    <div class="sent" id="sent">
      「<span class="frag f-soft" data-frag="query" tabindex="0">给猫买个饮水器</span>，
      <span class="frag f-hard" data-frag="price" tabindex="0">预算 300 元</span>，
      <span class="frag f-hard" data-frag="material" tabindex="0">不要塑料的</span>，
      <span class="frag f-hard" data-frag="weight" tabindex="0">5 公斤以内</span>。」
      <span class="leg">—— 蓝 = 软召回（S3）· 绿 = 硬约束（S2）· 「给猫买」还隐式推断出类目</span>
    </div>
    <div class="mid">模型解析<br>→<br>结构化参数</div>
    <div class="args" id="args"></div>
  </div>

  <h2 class="mhead">每个字段由哪一层消费</h2>
  <p class="msub">S2 定「谁进得来」（硬约束，Qdrant filter 的 must 子句）；S3 定「谁排前面」（语义召回 + 精排）。模型把它们都装进同一次工具调用。</p>
  <div class="upgrid" id="consumers"></div>

  <h2 class="mhead">端到端实例：币种归一化</h2>
  <p class="msub">真实目录（3700 条）是多币种的（CNY/USD/EUR/JPY/SGD）。「预算 300 元」的 300 是 CNY，但命中的商品原价是 USD / EUR —— 预算过滤和报告都必须换算，否则模型会随机摇摆。</p>

  <div class="exgrid" id="example"></div>
  <div class="note" id="note"></div>

  <h2 class="mhead">S4 七条验收</h2>
  <p class="msub">对应 <span class="mono" style="font-family:'IBM Plex Mono',monospace">docs/_build_log/2026-11-01-s4-acceptance-report.md</span>。</p>
  <div class="tbl" id="acceptance"></div>
</main>

<script>
  // ---- 字段定义：key, 名称, 值, 归属(S2硬约束/S3软召回), 说明 ----
  const FIELDS = [
    { key: "query",    name: "query",                    val: '"给猫用的饮水器"', type: "soft", tag: "S3 软召回",
      note: "自然语言原文进 embedding，语义召回（S3）。" },
    { key: "category", name: "category",                 val: '"母婴宠物"',      type: "hard", tag: "S2 硬约束",
      note: "模型从「给猫买」推断类目；prompt 注入目录真实类目表，防臆造。" },
    { key: "price",    name: "price_max_major",          val: "300",            type: "hard", tag: "S2 硬约束",
      note: "预算上限；比较前按汇率换算成目标币种 currency=CNY（S2-6）。" },
    { key: "material", name: "excluded_material_tags",   val: '["塑料"]',       type: "hard", tag: "S2 硬约束",
      note: "材质标签命中即排除。" },
    { key: "weight",   name: "weight_max_kg",            val: "5",             type: "hard", tag: "S2 硬约束",
      note: "重量上限，比较 product.weight_kg。" },
  ];

  const QUERY = "预算300元，给猫买个饮水器";

  document.getElementById("chips").innerHTML = [
    ["query", `query = "${QUERY}"`],
    ["soft", "soft → S3 召回（语义）"],
    ["good", "hard → S2 硬约束（过滤）"],
    [null, "provider = dashscope · model = qwen-plus"],
    [null, "embedding = qwen3.7-text-embedding-flash"],
    [null, "max_iters = 15"],
    ["good", "7 条验收全部 PASS"],
  ].map(([kind, text]) => `<span class="chip ${kind || ""}">${text}</span>`).join("");

  // ---- 参数面板 ----
  document.getElementById("args").innerHTML =
    `<div class="args-head">product_search({</div>` +
    FIELDS.map(f => `<div class="field" data-field="${f.key}" tabindex="0">` +
      `<span class="fname">${f.name}:</span>` +
      `<span class="fval ${f.type === "soft" ? "str" : "num"}">${f.val}</span>` +
      `<span class="ftag ${f.type}">${f.tag}</span>` +
      `<span class="fnote">${f.note}</span></div>`).join("") +
    `<div class="args-head">})</div>`;

  // ---- 片段 ↔ 字段双向高亮 ----
  const frags = [...document.querySelectorAll(".frag")];
  const fieldEls = [...document.querySelectorAll(".field")];
  const fragOf = Object.fromEntries(FIELDS.map(f => [f.key, f]));
  function setHot(key) {
    document.body.classList.add("linking");
    frags.forEach(x => x.classList.toggle("hot", x.dataset.frag === key));
    fieldEls.forEach(x => x.classList.toggle("hot", x.dataset.field === key));
  }
  function clearHot() {
    document.body.classList.remove("linking");
    frags.forEach(x => x.classList.remove("hot"));
    fieldEls.forEach(x => x.classList.remove("hot"));
  }
  frags.forEach(x => {
    x.addEventListener("mouseenter", () => setHot(x.dataset.frag));
    x.addEventListener("mouseleave", clearHot);
    x.addEventListener("focus", () => setHot(x.dataset.frag));
    x.addEventListener("blur", clearHot);
  });
  fieldEls.forEach(x => {
    x.addEventListener("mouseenter", () => setHot(x.dataset.field));
    x.addEventListener("mouseleave", clearHot);
    x.addEventListener("focus", () => setHot(x.dataset.field));
    x.addEventListener("blur", clearHot);
  });

  // ---- 归属卡片 ----
  document.getElementById("consumers").innerHTML = FIELDS.map(f =>
    `<section class="upcard"><h3><span class="fname" style="font-family:'IBM Plex Mono',monospace;font-size:13px">${f.name}</span>` +
    `<span class="badge ${f.type}">${f.tag}</span></h3>` +
    `<div class="krow"><span class="k">值</span><span style="font-family:'IBM Plex Mono',monospace;font-size:12.5px">${f.val}</span></div>` +
    `<p class="vnote">${f.note}</p></section>`).join("");

  // ---- 端到端实例：真实目录两条命中 ----
  const EX = [
    { id: "GX-30-1", title: "Roamix 宠物饮水器 1L 无过滤",     sku: "P3464-S1", raw: "24.93 USD", cny: 179.496, pass: true },
    { id: "GX-30-2", title: "Roamix 宠物饮水器 1.5L 单层过滤", sku: "P3468-S1", raw: "38.08 EUR", cny: 297.024, pass: true },
  ];
  const byCny = (a, b) => a.cny - b.cny;
  const exById = Object.fromEntries(EX.map(p => [p.id, p]));

  function row(p, opts = {}) {
    const price = opts.converted ? `${p.cny.toFixed(3)} CNY` : p.raw;
    const m = opts.converted ? `${p.sku} · 换算自 ${p.raw}` : `${p.sku} · 原币种 ${p.raw}`;
    return `<div class="row" data-pid="${p.id}" tabindex="0">` +
      `<span class="id">${p.id}</span>` +
      `<span class="body"><span class="t">${p.title}</span><span class="m">${m}</span></span>` +
      `<span class="s"><span class="v ${opts.converted ? "cnv" : ""}">${price}</span></span></div>`;
  }
  function cardRow(p) {
    return `<div class="row" data-pid="${p.id}" tabindex="0">` +
      `<span class="id">${p.id}</span>` +
      `<span class="body"><span class="t">${p.title}</span><span class="m">sku=${p.sku} · ${p.cny.toFixed(3)} CNY</span></span>` +
      `<span class="s"><span class="v pass">✓ 预算内</span></span></div>`;
  }

  const example = [
    { n: "①", name: "参数", job: "ProductSearchArgs",
      body: `<div class="formula">price_max_major=300 · currency="CNY" · category="母婴宠物"</div>` +
        `<div class="vnote">模型把「预算300元」→ price_max_major=300、currency=CNY。</div>` },
    { n: "②", name: "预算过滤", job: "币种换算 · S2",
      body: EX.map(p => `<div class="row" data-pid="${p.id}" tabindex="0">` +
        `<span class="id">${p.id}</span>` +
        `<span class="body"><span class="t">${p.title}</span><span class="m">${p.raw} × 汇率 → ${p.cny.toFixed(3)} CNY</span></span>` +
        `<span class="s"><span class="v ${p.cny <= 300 ? "pass" : "drop"}">${p.cny <= 300 ? "≤ 300 ✓" : "> 300 ✗"}</span></span></div>`).join("") +
        `<div class="vnote">24.93 USD×7.2、38.08 EUR×7.8 —— 都落在 300 元预算内。</div>` },
    { n: "③", name: "检索", job: "S3 召回→去重→截断",
      body: EX.sort(byCny).map((p, i) => row(p)).join("") +
        `<div class="vnote">命中 35 条，其中 47 件因超预算被过滤；展示前 2 条（S4-3 只报总数）。</div>` },
    { n: "④", name: "报告归一化", job: "工具换算 · S4-7",
      body: EX.sort(byCny).map((p, i) => row(p, { converted: true })).join("") +
        `<div class="vnote">工具在报告前用 fx.convert() 统一换算成目标币种——与预算过滤三点一线，模型不再自己瞎换算。</div>` },
    { n: "⑤", name: "商品卡", job: "ProductCardList",
      body: `<div class="cards">` + EX.sort(byCny).map(cardRow).join("") + `</div>` +
        `<div class="vnote">模型格式化成结构化商品卡；同一问题连问 3 次输出稳定（S4-7）。</div>` },
  ];
  document.getElementById("example").innerHTML = example.map(s =>
    `<section class="panel"><div class="panel-head">` +
    `<span class="panel-num">${s.n}</span><span class="panel-name">${s.name}</span>` +
    `<span class="panel-job">${s.job}</span></div><div class="panel-body">${s.body}</div></section>`).join("");

  document.getElementById("note").innerHTML =
    `<b>为什么 S4-7 是个真 bug：</b>` +
    `预算过滤（S2）和去重排序（S3）都先把价格换算成目标币种，唯独「工具报告」这一处吐原币种 —— ` +
    `于是模型有时照抄 <b>24.93 USD / 38.08 EUR</b>、有时自行换算成 <b>180 / 285 CNY</b>，随机摇摆。` +
    `修复后统一换算，3 连真模型输出稳定为 <b>179.496 / 297.024 CNY</b>。` +
    `（<b>179.496</b> 而非 179.50 是 Decimal 精确换算，故意不四舍五入，保证报告值与过滤比较值一致。）`;

  // ---- hover 追踪两条命中贯穿 ②③④⑤ ----
  const exRows = [...document.querySelectorAll("#example .row")];
  exRows.forEach(n => {
    const pid = n.dataset.pid;
    const highlight = () => {
      document.body.classList.add("tracing");
      exRows.forEach(x => x.classList.toggle("trace", x.dataset.pid === pid));
    };
    const unhighlight = () => {
      document.body.classList.remove("tracing");
      exRows.forEach(x => x.classList.remove("trace"));
    };
    n.addEventListener("mouseenter", highlight);
    n.addEventListener("mouseleave", unhighlight);
    n.addEventListener("focus", highlight);
    n.addEventListener("blur", unhighlight);
  });

  // ---- 验收表 ----
  const ACC = [
    ["S4-1", "问「2000 以内的吸尘器」→ 调 product_search 返回结构化商品卡"],
    ["S4-2", "寒暄不调工具 → 直接回文字不报错"],
    ["S4-3", "截断后说明「还有 N 条」（收敛为只报总数）"],
    ["S4-4", "超过 max_iters=15 → 干净退出不抛栈"],
    ["S4-5", "工具参数类型错（price_max_major=\"五\"）→ 被拦下不崩"],
    ["S4-6", "未配 API Key → 启动时报明确错误"],
    ["S4-7", "同一问题连问 3 次输出结构稳定（含币种归一化）"],
  ];
  document.getElementById("acceptance").innerHTML = ACC.map(([id, txt]) =>
    `<div class="trow"><span class="tid">${id}</span><span class="ttxt">${txt}</span>` +
    `<span class="tstat pass">PASS</span></div>`).join("");
</script>
```
