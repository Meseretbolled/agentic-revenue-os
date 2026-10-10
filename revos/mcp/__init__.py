"""Read-only tools for agents, served in-process over MCP.

Nothing in this package can send, sign, approve or change a stage. Agents return proposals;
only the policy engine, the send gate and the state machine act on them.
"""
