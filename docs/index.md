---
owner: platform
status: verified
last_verified: 2026-05-07
source: internal-doc
agent_targets: [supervisor, cloudwatch, diagnostics, knowledge]
---

# Automatick Knowledge Store

This directory is the agent-readable map for Automatick documentation. Keep it
short and navigable: agents should start here, then follow links to focused
sources of truth.

## Core Maps

- [Agent knowledge flow](architecture/agent-knowledge-flow.md): how tickets,
  AWS evidence, incident history, internal docs, and official AWS docs fit
  together.
- [Knowledge policy](agent-guidance/knowledge-policy.md): when agents must use
  internal runbooks, incident history, and official documentation.
- [Operations catalog](operations/index.md): supported incident runbooks and
  their canonical locations.
- [Testing catalog](testing/index.md): reproducible validation plans and test
  history.
- [References catalog](references/index.md): external documentation sources and
  citation rules.

## Repository Sources

- `AGENTS.md` is only the working index for coding agents.
- `docs/` is the structured knowledge map.
- `runbooks/` contains canonical operational and validation runbooks.
- `scripts/lint-docs.py` validates document metadata and basic structure.
- `scripts/sync-runbooks.py` syncs both `docs/` and `runbooks/` Markdown into
  the Bedrock Knowledge Base data source.

## Agent Reading Order

1. Read the current ticket or task.
2. Collect live AWS evidence with the relevant specialist tools.
3. Search incident history for recurrence.
4. Search internal runbooks from the Bedrock Knowledge Base.
5. Search official AWS documentation only when internal docs are missing,
   insufficient, stale, or when AWS behavior/API semantics need confirmation.
6. State which sources were used in the final investigation note.
