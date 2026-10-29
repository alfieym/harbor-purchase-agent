# Agents (Phase 2)

Phase 1 does **not** install AgentScope and contains no agent code.

Phase 2 will add, with AgentScope 2.0.11:

- `purchase_agent.py`: assemble `Agent(name, system_prompt, model, toolkit, react_config)`.
- `prompts.py`: system prompt and task instructions.
- `tools.py`: `product_search` exposed through `FunctionTool` / `Toolkit`.
- `guardrails.py`: repeated tool-call detection via `ToolMiddlewareBase.on_tool_call`,
  `ReActConfig(max_iters=...)` as a hard cap, and structured output assertions.

Rule of dependency: this layer may call feature services; features must not import
from `agents/`.
