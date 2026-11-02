"""Tool permission policy for the purchase agent (S4).

AgentScope 2.0 asks for confirmation before every tool call by default. In an
unattended run that ASK parks the reply with "I'm waiting for your permission or
the external execution to finish" and no structured output is ever produced.

``product_search`` is a read-only catalog lookup, so it is allowed by a
tool-name-level rule (``rule_content=None`` matches every invocation). The
built-in ``GenerateStructuredOutput`` tool always allows itself.
"""

from agentscope.agent import Agent
from agentscope.permission import PermissionBehavior, PermissionRule

PRODUCT_SEARCH_TOOL = "product_search"


def allow_product_search_tool(agent: Agent) -> None:
    """Let ``agent`` call ``product_search`` without a confirmation prompt."""
    allow_rules = agent.state.permission_context.allow_rules
    allow_rules.setdefault(PRODUCT_SEARCH_TOOL, []).append(
        PermissionRule(
            tool_name=PRODUCT_SEARCH_TOOL,
            rule_content=None,
            behavior=PermissionBehavior.ALLOW,
            source="harbor.agents",
        )
    )
