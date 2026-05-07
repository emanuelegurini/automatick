---
owner: platform
status: verified
last_verified: 2026-05-07
source: internal-doc
agent_targets: [supervisor, cloudwatch, diagnostics, knowledge]
---

# Agent Knowledge Flow

Automatick investigations should separate live evidence from historical and
documented guidance. A source can support a hypothesis, but only live evidence
from AWS tools can confirm the current incident state.

## Ticket Flow

1. Freshdesk creates or updates an incident ticket.
2. The backend normalizes ticket fields into an internal incident.
3. The supervisor delegates to the relevant specialist.
4. Specialists collect live AWS evidence through MCP tools.
5. Specialists search incident history for recurrence and frequency.
6. Specialists search internal runbooks for known investigation and remediation
   guidance.
7. Specialists search official AWS documentation when the service behavior,
   metric semantics, or recommended AWS action needs external confirmation.
8. The backend posts a private Freshdesk note and stores incident history plus
   any pending remediation proposal.

## Source Roles

- Live AWS evidence: current state, metrics, logs, SSM/runtime output, resource
  health, and alarm configuration.
- Incident history: recurrence signal and related ticket IDs. It is not proof of
  identical root cause.
- Internal runbooks: preferred operational guidance for known Automatick/MSP
  workflows.
- Official AWS documentation: public source of truth for AWS service behavior,
  troubleshooting guidance, limits, and APIs.

## Final Answer Contract

Incident answers should include:

- Root cause hypothesis
- Evidence
- Incident history
- Internal documentation used, or `no internal runbook found`
- Official AWS documentation used, or `not checked`
- Proposed fix
- Risk / impact

The agent must not claim documentation support unless a documentation tool
returned a relevant result.
