---
owner: platform
status: verified
last_verified: 2026-05-07
source: internal-doc
agent_targets: [supervisor, cloudwatch, diagnostics, knowledge]
---

# Operations Catalog

The canonical operational runbooks live in `runbooks/` and are synchronized into
the Bedrock Knowledge Base. Each runbook carries frontmatter metadata so agents
can filter by service, incident type, owner, and verification status.

## Incident Runbooks

- [ALB unhealthy targets](../../runbooks/alb-unhealthy-targets.md)
- [API Gateway 5XX errors](../../runbooks/apigateway-5xx-errors.md)
- [DynamoDB throttling](../../runbooks/dynamodb-throttling.md)
- [EC2 high CPU](../../runbooks/ec2-high-cpu.md)
- [ECS task failures](../../runbooks/ecs-task-failures.md)
- [Lambda throttling](../../runbooks/lambda-throttling.md)
- [RDS high connections](../../runbooks/rds-high-connections.md)
- [S3 access denied](../../runbooks/s3-access-denied.md)

## Runtime And Validation Runbooks

- [Runtime diagnostics agent](../../runbooks/runtime-diagnostics-agent.md)
- [CloudWatch specialist agent test plan](../../runbooks/cloudwatch-specialist-agent-test-plan.md)
- [SQL Server EC2 high CPU test](../../runbooks/sqlserver-ec2-high-cpu-test.md)
- [Test history](../../runbooks/test-history.md)

## Required Runbook Sections

Operational incident runbooks should include:

- Overview
- Applicable Alarms
- Diagnosis Steps
- Remediation
- Validation
- Rollback or risk notes when remediation changes AWS resources
