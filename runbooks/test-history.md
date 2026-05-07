# Automatick Test History

This file is the persistent historical log for reproducible Automatick tests.
Keep one dated entry per meaningful end-to-end validation. Do not add secrets,
temporary AWS credentials, API keys, webhook secrets, or account IDs.

## 2026-05-07 - SQL Server EC2 High CPU Freshdesk Investigation

### Objective

Validate that Automatick can receive a real Freshdesk incident for an EC2-hosted
SQL Server workload, investigate current AWS evidence through the supervisor and
specialist agents, post a private Freshdesk note, and create a pending remediation
record without executing remediation.

### Test Infrastructure

- CloudFormation stack: `automatick-sqlserver-ec2-load-test`
- Template: `infrastructure/customer/sqlserver-ec2-load-test.yaml`
- Runbook: `runbooks/sqlserver-ec2-high-cpu-test.md`
- Region: `us-east-1`
- EC2 instance: `i-01dcd676368d3bccf`
- Workload: SQL Server 2022 container with synthetic sample database and join-heavy load generator
- Control plane: SSM documents for start, stop, and status
- Alarm: `automatick-sqlserver-ec2-load-test-ec2-sqlserver-cpu-high`
- Threshold: EC2 `CPUUtilization >= 60%`
- Required tags: `project=mps-ops-utomation-poc`, `owner=simone.ferraro`

### Execution Summary

1. Deployed the CloudFormation stack with the same project/owner tags used by the existing test resources.
2. Verified the EC2 instance was online in SSM.
3. Started the SQL Server load generator with 8 workers.
4. Confirmed CloudWatch EC2 CPU datapoints reached sustained high utilization.
5. Enabled detailed monitoring on the EC2 instance so the 60-second alarm configuration receives 1-minute datapoints.
6. Confirmed the CloudWatch alarm transitioned to `ALARM`.
7. Created a real Freshdesk ticket with type `Incident Request`.
8. Sent the ticket to the backend Freshdesk webhook at `/api/v1/integrations/freshdesk/tickets`.
9. Verified the asynchronous investigation completed in DynamoDB.
10. Verified Automatick posted a private Freshdesk investigation note.
11. Verified a pending remediation record was created.
12. Stopped the synthetic load container via SSM.

### Evidence

- Freshdesk ticket: `572545`
- Backend request ID: `e51d5790-273b-423d-96f4-3efc78fb7a5d`
- Freshdesk private note: `22294970771`
- Specialist selected by supervisor: `runtime_diagnostics`
- Remediation ID: `rem-7a63c85c-acea-4e94-968f-0417d5b30e1c`
- Remediation status: `pending`
- CloudWatch alarm state during validation: `ALARM`
- CloudWatch alarm reason: 2 of 2 datapoints were above the 60% threshold

### Agent Finding

The investigation identified high CPU pressure on the EC2 instance and correlated
the symptom with the SQL Server process (`sqlservr`). It confirmed the instance was
managed by SSM and returned a remediation proposal for human review. No AWS
remediation action was executed.

### Issues Found During Test

- The first workload query hit a SQL Server arithmetic overflow when summing
  `CHECKSUM` values. The workload query was corrected to cast checksums to `bigint`.
- The EC2 alarm was configured for 60-second periods, but the instance initially
  used basic monitoring. Detailed monitoring was enabled live and added to the
  CloudFormation template.
- The Freshdesk ticket creation API required the `type` field. The successful test
  used `Incident Request`.
- The first webhook attempt used the wrong API Gateway path. The public webhook is
  exposed under `/api/v1/integrations/freshdesk/tickets`.

### Follow-Up Changes

- Added Docker container and `docker stats` evidence to runtime diagnostics command profiles.
- Added detailed monitoring to the SQL Server EC2 load-test template.
- Added a status-script `exit 0` so the status document remains successful when
  optional log content is absent.
- Added durable `incident_history` records and CloudWatch incident-history lookup
  so future investigations can classify recurrence and frequency.

### Cleanup State

The load generator container was stopped after the test. The SQL Server container
and EC2 test stack were left available for repeatable future tests.

## 2026-05-07 - SQL Server EC2 High CPU Retest With Incident History

### Objective

Repeat the SQL Server EC2 high-CPU scenario after adding persistent incident
history lookup, validating that the agent can classify recurrence while the alarm
is backed by live CloudWatch and SSM evidence.

### Execution Summary

1. Verified the EC2 instance `i-01dcd676368d3bccf` was online in SSM.
2. Started the SQL Server load generator with 16 workers for 1800 seconds.
3. Confirmed CloudWatch EC2 CPU datapoints exceeded the 60% threshold.
4. Confirmed the alarm transitioned to `ALARM`.
5. Created a real Freshdesk incident ticket and sent it through the backend webhook.
6. Verified the backend request completed and posted a private Freshdesk note.
7. Verified a new `incident_history` record and a pending remediation record were created.
8. Stopped the synthetic load container via SSM.

### Evidence

- Freshdesk ticket: `572556`
- Backend request ID: `2660db86-8354-4d1a-b167-83a0f187701a`
- Freshdesk private note: `22294980227`
- Incident history record: `incident-history-572556`
- Remediation ID: `rem-4717faf7-e5d1-45a4-80ee-a726423d75a4`
- Remediation status: `pending`
- CloudWatch alarm state: `ALARM`
- Alarm transition time: `2026-05-07T19:06:42+02:00`
- CPU datapoints: `84.05%` at `19:04`, `99.99%` at `19:05`, `99.98%` at `19:06`, and `100.00%` at `19:09` Europe/Rome
- SSM status during load: SQL Server container around `197-200%` Docker CPU
- SSM status after cleanup: load container stopped; SQL Server container around `1.69%` Docker CPU
- Post-stop CloudWatch state: alarm returned to `OK` at `2026-05-07T19:20:42+02:00`

### Agent Finding

The investigation identified high CPU pressure on the EC2 instance, connected the
symptom to the SQL Server process, cited a recent CloudWatch CPU spike, and
classified similar EC2 high-CPU incidents as frequent recurring based on incident
history. The response proposed SQL query review/optimization and instance sizing
review, then created a remediation record in `pending` state without executing
AWS changes.

### Cleanup State

The load generator container `automatick-sql-load-1778173399` was stopped through
SSM. CloudWatch CPU dropped to about `2%` after the workload stopped. The SQL
Server container and EC2 test stack remain available for repeatable future tests.
