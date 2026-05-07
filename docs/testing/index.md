---
owner: platform
status: verified
last_verified: 2026-05-07
source: internal-doc
agent_targets: [supervisor, cloudwatch, diagnostics, knowledge]
---

# Testing Catalog

Use this catalog for reproducible end-to-end and specialist-agent validation.

## Current Test Assets

- [CloudWatch specialist agent test plan](../../runbooks/cloudwatch-specialist-agent-test-plan.md)
- [SQL Server EC2 high CPU test](../../runbooks/sqlserver-ec2-high-cpu-test.md)
- [Persistent test history](../../runbooks/test-history.md)

## Test Logging Rules

- Add one dated entry to `runbooks/test-history.md` for every meaningful
  end-to-end validation.
- Include ticket ID, backend request ID, remediation ID, metric evidence, and
  cleanup state when available.
- Never store credentials, API keys, webhook secrets, session tokens, or real
  account IDs in test history.
