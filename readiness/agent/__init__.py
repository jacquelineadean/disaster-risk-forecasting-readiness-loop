"""The agent plane: orchestrator, subagents, and the loop they run.

Nothing in this package may write to `readiness/harness/`,
`readiness/contracts.py`, `readiness/config.py` or `contracts/`. That
separation is the entire safety argument (report §7: "treat any agent
write-access to it as a security bug").
"""
