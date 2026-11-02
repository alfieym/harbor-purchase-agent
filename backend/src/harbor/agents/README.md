# Agents

装配采购 Agent 的横切层，用 AgentScope 2.0.11。此层可以调用 feature service；
feature 不得反向依赖 `agents/`。

## 当前已实现（S4）

- `tools.py` —— `make_product_search_tool(service)`：把 S3 检索包成 AgentScope
  `FunctionTool`（`name="product_search"`），输入 `ProductSearchArgs`，返回 `SearchResult`
  的紧凑 JSON。
- `cards.py` —— `ProductCard` / `ProductCardList`：agent 最终输出的结构化商品卡 schema
  （作为 `structured_schema` 传给 `Agent.reply`）。
- `prompts.py` —— 系统提示词。
- `models.py` —— `build_chat_model(settings)`：按 `HARBOR_MODEL_PROVIDER`
  选择 `DeepSeekChatModel` 或 `DashScopeChatModel`（千问）。
- `permissions.py` —— 给只读的 `product_search` 放行，否则 AgentScope 2.0 会把
  每次工具调用挂起在权限确认上（`structured_output` 会是 `None`）。
- `cli.py` + `__main__.py` —— 交互式 REPL：`python -m harbor.agents`。索引持久化在
  `.cache/catalog-index/`；目录文件或 embedding 配置变化时按指纹自动重建。

运行需要 `OPENAI_API_KEY`，以及一个支持 `/embeddings` 的端点（建索引用一次；
查询期 embedding 失败仍会降级 keyword_2gram）。DeepSeek 端点没有 `/embeddings`，
千问（DashScope compatible-mode）有；切换方法见根目录 README 的配置表。

## 尚未实现（原 Phase 2 计划）

- `/api/chat/stream` SSE 路由（`features/chat/` 仍是 README 占位）。
- `guardrails.py`：重复工具调用检测、`structured_schema` 断言等中间件。
- 会话状态（`AgentState` + 持久化）。

## AgentScope 2.0.11 关键 API（已按真包核实）

- `Agent(name, system_prompt, model, toolkit, react_config)`；`reply(inputs, structured_schema=...)`
  是 **async**，返回 `Msg`，结构化结果在 `Msg.structured_output`（dict）。
- 原生 DeepSeek：`DeepSeekChatModel(credential, model)` + `DeepSeekCredential(api_key, base_url)`。
- 千问：`DashScopeChatModel(credential, model)` + `DashScopeCredential(api_key, base_url)`，
  `base_url` 默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
- 工具：`FunctionTool(func, name, description, input_schema)`，`func(**kwargs)` 返回
  `ToolChunk(content=[TextBlock(text=...)], state=ToolResultState.SUCCESS)`。
- 消息：`UserMsg(name, content)`；`Msg.get_text_content()` 取文本。
