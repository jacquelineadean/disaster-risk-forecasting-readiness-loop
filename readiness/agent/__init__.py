"""The agent plane: orchestrator, subagents, and the loop they run.

Nothing in this package may write to `readiness/harness/`,
`readiness/contracts.py`, `readiness/config.py`, `readiness/verify.py`,
`readiness/connectors/` or `contracts/`. That separation is the entire safety
argument (report §7: "treat any agent write-access to it as a security bug").
Because the claude backend holds Bash, the separation is enforced rather than
requested: `readiness.agent.guard` hashes those paths before and after every
run and fails the run if any byte moved.
"""
