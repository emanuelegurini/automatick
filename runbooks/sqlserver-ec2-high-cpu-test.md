# SQL Server EC2 High CPU Test

## Purpose

Reproduce a ticket-driven runtime diagnostics incident where SQL Server on EC2
pushes host CPU above 60%. This validates the current investigation path:

```text
SQL workload -> CloudWatch alarm -> Freshdesk ticket -> Supervisor -> runtime_diagnostics -> Freshdesk private note
```

This first test intentionally validates OS/process/container evidence. SQL Server
DMV-level query diagnostics are a follow-up capability.

## Deploy Test Target

Deploy the dedicated CloudFormation stack in the customer/test account:

```bash
aws cloudformation deploy \
  --stack-name automatick-sqlserver-ec2-load-test \
  --template-file infrastructure/customer/sqlserver-ec2-load-test.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    VpcId="<vpc-id>" \
    SubnetId="<subnet-id>" \
    InstanceType="t3.large" \
    CpuAlarmThresholdPercent="60" \
    ProjectTagValue="${PROJECT_TAG_VALUE:-mps-ops-utomation-poc}" \
    OwnerTagValue="${OWNER_TAG_VALUE:-simone.ferraro}" \
    EnvironmentTagValue="test" \
    ServiceTagValue="runtime-diagnostics" \
    CustomerTagValue="runtime-test" \
  --tags \
    project="${PROJECT_TAG_VALUE:-mps-ops-utomation-poc}" \
    owner="${OWNER_TAG_VALUE:-simone.ferraro}" \
    ManagedBy="CloudFormation" \
    Environment="test" \
    Service="runtime-diagnostics" \
    Customer="runtime-test" \
    Workload="sqlserver-high-cpu" \
  --region us-east-1
```

Use a subnet with outbound access to:

- SSM endpoints, or public internet/NAT for SSM
- `mcr.microsoft.com` for the SQL Server container image
- Docker Hub for the Python workload image
- PyPI for the `pymssql` workload dependency

The stack and template apply the project-standard tags to all taggable resources
and set `AutomatickDiagnostics=true` on the EC2 instance so the runtime
diagnostics role can use approved SSM command profiles. `AWS::IAM::InstanceProfile`
does not support tags in CloudFormation, so ownership is carried by the stack and
the attached IAM role.

## Read Outputs

```bash
aws cloudformation describe-stacks \
  --stack-name automatick-sqlserver-ec2-load-test \
  --query "Stacks[0].Outputs" \
  --region us-east-1
```

Record:

- `InstanceId`
- `CpuAlarmName`
- `StartLoadDocumentName`
- `StopLoadDocumentName`
- `StatusDocumentName`
- `FreshdeskSubject`
- `FreshdeskDescription`

Verify tag coverage for the workload:

```bash
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Workload,Values=sqlserver-high-cpu \
  --query "ResourceTagMappingList[].{Arn:ResourceARN,Tags:Tags}" \
  --region us-east-1
```

## Wait For Bootstrap

Check SSM and bootstrap status:

```bash
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=<InstanceId>" \
  --region us-east-1

aws ssm send-command \
  --instance-ids "<InstanceId>" \
  --document-name "<StatusDocumentName>" \
  --region us-east-1
```

If bootstrap is still pulling images or initializing the sample database, wait a
few minutes and run the status document again.

## Start CPU Workload

Run the workload long enough for the alarm and ticket investigation window:

```bash
aws ssm send-command \
  --instance-ids "<InstanceId>" \
  --document-name "<StartLoadDocumentName>" \
  --parameters DurationSeconds=1800,Workers=4 \
  --region us-east-1
```

Watch the alarm:

```bash
aws cloudwatch describe-alarms \
  --alarm-names "<CpuAlarmName>" \
  --query "MetricAlarms[0].{State:StateValue,Reason:StateReason,Updated:StateUpdatedTimestamp}" \
  --region us-east-1
```

## Send Freshdesk Ticket

When CPU is above threshold or the alarm enters `ALARM`, open/send a Freshdesk
ticket using the stack outputs. The account name should be `runtime_test`, which
matches the normalized credentials secret name used by the diagnostics runtime.

Webhook payload shape for direct API testing:

```json
{
  "ticket_id": "sqlserver-load-test-001",
  "subject": "SQL Server EC2 high CPU investigation",
  "description": "CloudWatch alarm <CpuAlarmName> is reporting CPU >= 60% on EC2 instance <InstanceId> in us-east-1. Please enter the machine, inspect CPU pressure, processes, and SQL Server container workload.",
  "account_name": "runtime_test",
  "region": "us-east-1",
  "resource_id": "<InstanceId>"
}
```

Expected behavior:

- Backend accepts the Freshdesk webhook.
- Supervisor selects `check_runtime_diagnostics`.
- Runtime Diagnostics inspects the EC2 instance and runs CPU/process evidence.
- Freshdesk private note identifies host CPU pressure from SQL Server/container workload.
- Remediation record is created as `pending`; no AWS remediation is executed.

## Stop Workload

```bash
aws ssm send-command \
  --instance-ids "<InstanceId>" \
  --document-name "<StopLoadDocumentName>" \
  --region us-east-1
```

Confirm the alarm returns to `OK` after CloudWatch receives lower CPU datapoints.

## Cleanup

Delete the stack when the reproducible test target is no longer needed:

```bash
aws cloudformation delete-stack \
  --stack-name automatick-sqlserver-ec2-load-test \
  --region us-east-1
```
