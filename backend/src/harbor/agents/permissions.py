"""Tool permission policy for the purchase agent (S4).

AgentScope 2.0 asks for confirmation before every tool call by default. In an
unattended run that ASK parks the reply with "I'm waiting for your permission or
the external execution to finish" and no structured output is ever produced.

Read-only tools are allowed by a tool-name-level rule (``rule_content=None``
matches every invocation). The built-in ``GenerateStructuredOutput`` tool always
allows itself.
"""

from agentscope.agent import Agent
from agentscope.permission import PermissionBehavior, PermissionRule

PRODUCT_SEARCH_TOOL = "product_search"
DISPATCH_TOOL = "task_dispatch"
ORDER_TOOLS = ("create_order_draft", "get_order")


def allow_tool(agent: Agent, tool_name: str) -> None:
    """Let ``agent`` call ``tool_name`` without a confirmation prompt."""
    allow_rules = agent.state.permission_context.allow_rules
    allow_rules.setdefault(tool_name, []).append(
        PermissionRule(
            tool_name=tool_name,
            rule_content=None,
            behavior=PermissionBehavior.ALLOW,
            source="harbor.agents",
        )
    )


def allow_product_search_tool(agent: Agent) -> None:
    """Let ``agent`` call ``product_search`` without a confirmation prompt."""
    allow_tool(agent, PRODUCT_SEARCH_TOOL)


def allow_dispatch_tool(agent: Agent) -> None:
    """Let ``agent`` call ``task_dispatch`` without a confirmation prompt."""
    allow_tool(agent, DISPATCH_TOOL)


def allow_order_tools(agent: Agent) -> None:
    """Let ``agent`` call the transaction tools without a confirmation prompt.

    These tools write drafts only; the human gate is the application layer's
    confirm endpoint, so no permission prompt is needed here (S10).
    """
    for tool_name in ORDER_TOOLS:
        allow_tool(agent, tool_name)
