"""S11 guardrails: request, behaviour and output layers.

Nothing here imports the agent layer, so the package stays testable on its
own and cannot create an import cycle with ``harbor.agents``.
"""
