---
owner: platform
status: verified
last_verified: 2026-05-07
source: internal-doc
agent_targets: [supervisor, cloudwatch, diagnostics, knowledge]
---

# References Catalog

This catalog records external documentation families that agents may use after
checking live evidence, incident history, and internal runbooks.

## Official Sources

- AWS Documentation through AWS Knowledge MCP
- AWS API references through AWS Knowledge MCP
- AWS troubleshooting and Well-Architected content through AWS Knowledge MCP
- OpenAI harness engineering guidance for repository-readable documentation:
  <https://openai.com/it-IT/index/harness-engineering/>

## Citation Rules

- Prefer official AWS sources for AWS service behavior and API semantics.
- Use internal runbooks for MSP workflow, Freshdesk notes, remediation approval,
  tagging, and test procedures.
- In incident notes, mention official AWS docs only when the agent actually
  searched and found relevant documentation.
