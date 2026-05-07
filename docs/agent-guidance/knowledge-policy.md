---
owner: platform
status: verified
last_verified: 2026-05-07
source: internal-doc
agent_targets: [supervisor, cloudwatch, diagnostics, knowledge]
---

# Knowledge Policy

Automatick follows a progressive-disclosure documentation model. Agents should
use small indexes first, then load only the documents needed for the incident.

## Mandatory Order For Incidents

1. Gather current AWS evidence.
2. Search incident history for recurrence.
3. Search internal runbooks through the Bedrock Knowledge Base.
4. Search official AWS documentation when internal docs do not answer the issue,
   when the incident is recurring/frequent, or when AWS service semantics need
   confirmation.

## Source Labels

Use these labels in investigation notes:

- `live_aws_evidence`: current AWS metrics, logs, SSM output, or resource state.
- `incident_history`: prior tickets and recurrence classification.
- `internal_runbook`: versioned repository documentation from `docs/` or
  `runbooks/`.
- `official_aws_docs`: AWS documentation retrieved through AWS Knowledge MCP.
- `no_doc_found`: no relevant internal or official documentation was found.

## Guardrails

- Do not use stale, unverifiable, or missing documentation as evidence.
- Do not treat prior incidents as proof of the current root cause.
- Prefer internal runbooks for MSP-specific workflow, tagging, ticket, and
  remediation policy.
- Prefer official AWS docs for AWS metric meaning, limits, API behavior, and
  managed-service troubleshooting guidance.
- Never include secrets, credentials, webhook tokens, or real account IDs in
  documentation.
