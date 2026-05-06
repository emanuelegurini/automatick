# Runtime Diagnostics Agent Runbook

## Purpose

Validate the `runtime_diagnostics` A2A specialist with a real customer-style
account target. The agent uses cross-account STS plus SSM Run Command profiles;
the backend does not assume customer roles.

## Customer Test Target

Deploy the CloudFormation template in the customer account:

```bash
aws cloudformation deploy \
  --stack-name automatick-runtime-diagnostics-test \
  --template-file infrastructure/customer/runtime-diagnostics-test-target.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    AgentPrincipalArn="<agentcore_runtime_diagnostics_role_arn>" \
    ExternalId="<customer-specific-external-id>" \
    VpcId="<vpc-id>" \
    SubnetId="<subnet-id>" \
    KeyName="" \
  --region us-east-1
```

Use a subnet with outbound access to SSM for the PoC. For production-style
private subnets, provide VPC endpoints for `ssm`, `ssmmessages`, and
`ec2messages`.

`AgentPrincipalArn` must be the role that will call `sts:AssumeRole` at runtime.
For this feature, prefer the AgentCore execution role of `runtime_diagnostics`,
not the backend ECS task role.

Read outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name automatick-runtime-diagnostics-test \
  --query "Stacks[0].Outputs" \
  --region us-east-1
```

## Agent Account Secret

Create or update the existing customer credential secret in the Agent account:

```bash
aws secretsmanager create-secret \
  --name "msp-credentials/test-customer" \
  --secret-string '{
    "account_id": "<customer_account_id>",
    "customer_name": "test-customer",
    "role_arn": "<DiagnosticsRoleArn output>",
    "role_name": "AutomatickRuntimeDiagnosticsRole",
    "external_id": "<customer-specific-external-id>"
  }' \
  --region us-east-1
```

If the secret already exists, use `aws secretsmanager update-secret` with the same
JSON. The runtime will assume the role and store refreshed STS credentials back
into this secret.

## Manual Validation

From the Agent account, validate cross-account access:

```bash
aws sts assume-role \
  --role-arn "<DiagnosticsRoleArn output>" \
  --role-session-name automatick-runtime-diag-test \
  --external-id "<customer-specific-external-id>" \
  --region us-east-1
```

With the assumed-role environment variables set:

```bash
aws ec2 describe-instances \
  --instance-ids "<InstanceId output>" \
  --region us-east-1

aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=<InstanceId output>" \
  --region us-east-1

aws ssm send-command \
  --instance-ids "<InstanceId output>" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["df -hT","df -ih"]' \
  --region us-east-1
```

## Agent Acceptance

Direct specialist prompt:

```text
Inspect EC2 instance <InstanceId> in us-east-1 for disk pressure. Use runtime
diagnostics only and do not remediate.
```

Expected behavior:

- Supervisor selects `check_runtime_diagnostics`.
- Runtime Diagnostics confirms EC2 metadata and SSM managed status.
- Runtime Diagnostics runs `disk_usage` and usually `linux_health`.
- Response contains `Runtime diagnostics summary`.
- Limitations mention SSM/tag/permission failures if command execution is not possible.
- No remediation is executed.
