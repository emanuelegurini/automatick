#!/usr/bin/env bash
set -euo pipefail

# Tags existing AWS resources that belong to this project.
# Defaults are intentionally the governance tags requested for this POC.

REGION="${1:-${AWS_REGION:-us-east-1}}"
PROJECT_TAG_VALUE="${PROJECT_TAG_VALUE:-mps-ops-utomation-poc}"
OWNER_TAG_VALUE="${OWNER_TAG_VALUE:-simone.ferraro}"

PROJECT_TAG_ARGS=("Key=Project,Value=${PROJECT_TAG_VALUE}" "Key=owner,Value=${OWNER_TAG_VALUE}")
PROJECT_TAG_MAP="Project=${PROJECT_TAG_VALUE},owner=${OWNER_TAG_VALUE}"
ECS_TAG_ARGS=("key=Project,value=${PROJECT_TAG_VALUE}" "key=owner,value=${OWNER_TAG_VALUE}")

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'ERROR: missing required command: %s\n' "$1" >&2
    exit 1
  }
}

tag_rgta() {
  local arn="$1"
  local label="$2"
  if [ -z "$arn" ] || [ "$arn" = "None" ] || [ "$arn" = "null" ]; then
    return 0
  fi
  aws resourcegroupstaggingapi tag-resources \
    --region "$REGION" \
    --resource-arn-list "$arn" \
    --tags "$PROJECT_TAG_MAP" >/dev/null 2>&1 && \
    log "tagged: $label" || warn "could not tag via RGTA: $label ($arn)"
}

tag_agentcore() {
  local arn="$1"
  local label="$2"
  if [ -z "$arn" ] || [ "$arn" = "None" ] || [ "$arn" = "null" ]; then
    return 0
  fi
  aws bedrock-agentcore-control tag-resource \
    --region "$REGION" \
    --resource-arn "$arn" \
    --tags "$PROJECT_TAG_MAP" >/dev/null 2>&1 && \
    log "tagged: $label" || warn "could not tag AgentCore resource: $label ($arn)"
}

tag_iam_role() {
  local role_name="$1"
  aws iam tag-role --role-name "$role_name" --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && \
    log "tagged: IAM role $role_name" || warn "could not tag IAM role: $role_name"
}

tag_iam_policy() {
  local policy_arn="$1"
  aws iam tag-policy --policy-arn "$policy_arn" --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && \
    log "tagged: IAM policy $policy_arn" || warn "could not tag IAM policy: $policy_arn"
}

tag_log_group() {
  local log_group_name="$1"
  local arn="arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:${log_group_name}"
  aws logs tag-resource --resource-arn "$arn" --tags "$PROJECT_TAG_MAP" --region "$REGION" >/dev/null 2>&1 && \
    log "tagged: log group $log_group_name" || warn "could not tag log group: $log_group_name"
}

tag_s3_bucket() {
  local bucket="$1"
  local current merged
  current=$(aws s3api get-bucket-tagging --bucket "$bucket" --output json 2>/dev/null || echo '{"TagSet":[]}')
  merged=$(jq -c \
    --arg project "$PROJECT_TAG_VALUE" \
    --arg owner "$OWNER_TAG_VALUE" \
    '(.TagSet // [])
      | map(select(.Key != "Project" and .Key != "owner"))
      | . + [{Key:"Project",Value:$project},{Key:"owner",Value:$owner}]
      | {TagSet:.}' <<<"$current")
  aws s3api put-bucket-tagging --bucket "$bucket" --tagging "$merged" >/dev/null 2>&1 && \
    log "tagged: S3 bucket $bucket" || warn "could not tag S3 bucket: $bucket"
}

tag_ec2_by_stack() {
  local stack_name="$1"
  local ids

  ids=$(aws ec2 describe-vpcs --region "$REGION" \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${stack_name}" \
    --query 'Vpcs[].VpcId' --output text 2>/dev/null || true)
  if [ -n "$ids" ]; then
    aws ec2 create-tags --region "$REGION" --resources $ids --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null && log "tagged: EC2 VPCs for $stack_name"
  fi

  ids=$(aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${stack_name}" \
    --query 'Subnets[].SubnetId' --output text 2>/dev/null || true)
  if [ -n "$ids" ]; then
    aws ec2 create-tags --region "$REGION" --resources $ids --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null && log "tagged: EC2 subnets for $stack_name"
  fi

  ids=$(aws ec2 describe-route-tables --region "$REGION" \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${stack_name}" \
    --query 'RouteTables[].RouteTableId' --output text 2>/dev/null || true)
  if [ -n "$ids" ]; then
    aws ec2 create-tags --region "$REGION" --resources $ids --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null && log "tagged: EC2 route tables for $stack_name"
  fi

  ids=$(aws ec2 describe-internet-gateways --region "$REGION" \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${stack_name}" \
    --query 'InternetGateways[].InternetGatewayId' --output text 2>/dev/null || true)
  if [ -n "$ids" ]; then
    aws ec2 create-tags --region "$REGION" --resources $ids --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null && log "tagged: EC2 internet gateways for $stack_name"
  fi

  ids=$(aws ec2 describe-nat-gateways --region "$REGION" \
    --filter "Name=tag:aws:cloudformation:stack-name,Values=${stack_name}" \
    --query 'NatGateways[].NatGatewayId' --output text 2>/dev/null || true)
  if [ -n "$ids" ]; then
    aws ec2 create-tags --region "$REGION" --resources $ids --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null && log "tagged: EC2 NAT gateways for $stack_name"
  fi

  ids=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${stack_name}" \
    --query 'SecurityGroups[].GroupId' --output text 2>/dev/null || true)
  if [ -n "$ids" ]; then
    aws ec2 create-tags --region "$REGION" --resources $ids --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null && log "tagged: EC2 security groups for $stack_name"
  fi

  ids=$(aws ec2 describe-network-acls --region "$REGION" \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${stack_name}" \
    --query 'NetworkAcls[].NetworkAclId' --output text 2>/dev/null || true)
  if [ -n "$ids" ]; then
    aws ec2 create-tags --region "$REGION" --resources $ids --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null && log "tagged: EC2 network ACLs for $stack_name"
  fi
}

require_cmd aws
require_cmd jq

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")

log "Tagging project resources in account ${ACCOUNT_ID}, region ${REGION}"
log "Project=${PROJECT_TAG_VALUE}"
log "owner=${OWNER_TAG_VALUE}"

for stack_name in MSPAssistantAgentCoreStack MSPAssistantBackendStack MSPAssistantFrontendStack; do
  stack_arn=$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$stack_name" \
    --query 'Stacks[0].StackId' \
    --output text 2>/dev/null || true)
  [ -n "$stack_arn" ] && [ "$stack_arn" != "None" ] && tag_rgta "$stack_arn" "CloudFormation stack $stack_name"
  tag_ec2_by_stack "$stack_name"
done

log "Tagging ECR repositories..."
aws ecr describe-repositories --region "$REGION" --output json 2>/dev/null | jq -r \
  '.repositories[]
    | select(.repositoryName == "msp-assistant-backend" or (.repositoryName | startswith("bedrock-agentcore-")))
    | [.repositoryArn, .repositoryName] | @tsv' |
while IFS=$'\t' read -r arn name; do
  aws ecr tag-resource --region "$REGION" --resource-arn "$arn" --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && \
    log "tagged: ECR repository $name" || warn "could not tag ECR repository: $name"
done

log "Tagging AgentCore runtimes and gateway..."
aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" --output json 2>/dev/null | jq -r \
  '.agentRuntimes[]
    | select((.agentRuntimeName | test("^(msp_supervisor_agent|.*_mcp|.*_a2a_runtime)$")))
    | [.agentRuntimeArn, .agentRuntimeName] | @tsv' |
while IFS=$'\t' read -r arn name; do
  tag_agentcore "$arn" "AgentCore runtime $name"
done

aws bedrock-agentcore-control list-gateways --region "$REGION" --output json 2>/dev/null | jq -r \
  '.items[]? | select(.name == "msp-assistant-gateway") | [.gatewayId, .name] | @tsv' |
while IFS=$'\t' read -r gateway_id name; do
  arn=$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$gateway_id" --region "$REGION" --query 'gatewayArn' --output text 2>/dev/null || true)
  tag_agentcore "$arn" "AgentCore gateway $name"
done

aws bedrock-agentcore-control list-memories --region "$REGION" --output json 2>/dev/null | jq -r \
  '.memories[]? | [.id, .arn] | @tsv' |
while IFS=$'\t' read -r memory_id memory_arn; do
  name=$(aws bedrock-agentcore-control get-memory --memory-id "$memory_id" --region "$REGION" --query 'memory.name' --output text 2>/dev/null || true)
  if [ "$name" = "msp_assistant_memory" ] || [[ "$name" == *_runtime_mem-* ]]; then
    tag_agentcore "$memory_arn" "AgentCore memory $name"
  fi
done

log "Tagging ECS resources..."
aws ecs list-clusters --region "$REGION" --output json 2>/dev/null | jq -r \
  '.clusterArns[] | select(test("MSPAssistantBackendStack"))' |
while read -r cluster_arn; do
  aws ecs tag-resource --region "$REGION" --resource-arn "$cluster_arn" --tags "${ECS_TAG_ARGS[@]}" >/dev/null 2>&1 && \
    log "tagged: ECS cluster $cluster_arn" || warn "could not tag ECS cluster: $cluster_arn"
  aws ecs list-services --region "$REGION" --cluster "$cluster_arn" --output json 2>/dev/null | jq -r '.serviceArns[]?' |
  while read -r service_arn; do
    aws ecs tag-resource --region "$REGION" --resource-arn "$service_arn" --tags "${ECS_TAG_ARGS[@]}" >/dev/null 2>&1 && \
      log "tagged: ECS service $service_arn" || warn "could not tag ECS service: $service_arn"
  done
done

aws ecs list-task-definitions --region "$REGION" --family-prefix MSPAssistantBackendStack --status ACTIVE --output json 2>/dev/null | jq -r '.taskDefinitionArns[]?' |
while read -r task_definition_arn; do
  aws ecs tag-resource --region "$REGION" --resource-arn "$task_definition_arn" --tags "${ECS_TAG_ARGS[@]}" >/dev/null 2>&1 && \
    log "tagged: ECS task definition $task_definition_arn" || warn "could not tag ECS task definition: $task_definition_arn"
done

log "Tagging ALB and target groups..."
aws elbv2 describe-load-balancers --region "$REGION" --output json 2>/dev/null | jq -r \
  '.LoadBalancers[] | select(.LoadBalancerName | startswith("MSPAss")) | [.LoadBalancerArn, .LoadBalancerName] | @tsv' |
while IFS=$'\t' read -r arn name; do
  aws elbv2 add-tags --region "$REGION" --resource-arns "$arn" --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && log "tagged: ALB $name"
done

aws elbv2 describe-target-groups --region "$REGION" --output json 2>/dev/null | jq -r \
  '.TargetGroups[] | select(.TargetGroupName | startswith("MSPAss")) | [.TargetGroupArn, .TargetGroupName] | @tsv' |
while IFS=$'\t' read -r arn name; do
  aws elbv2 add-tags --region "$REGION" --resource-arns "$arn" --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && log "tagged: target group $name"
done

log "Tagging DynamoDB, Cognito, API Gateway, CloudWatch alarms..."
aws dynamodb tag-resource \
  --region "$REGION" \
  --resource-arn "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/msp-assistant-chat-requests" \
  --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && log "tagged: DynamoDB msp-assistant-chat-requests" || true

aws cognito-idp list-user-pools --region "$REGION" --max-results 60 --output json 2>/dev/null | jq -r \
  '.UserPools[] | select(.Name == "msp-assistant-users") | .Id' |
while read -r user_pool_id; do
  aws cognito-idp tag-resource \
    --region "$REGION" \
    --resource-arn "arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${user_pool_id}" \
    --tags "$PROJECT_TAG_MAP" >/dev/null 2>&1 && log "tagged: Cognito user pool $user_pool_id"
done

aws apigateway get-rest-apis --region "$REGION" --output json 2>/dev/null | jq -r \
  '.items[] | select(.name == "msp-assistant-api") | .id' |
while read -r api_id; do
  aws apigateway tag-resource --region "$REGION" --resource-arn "arn:aws:apigateway:${REGION}::/restapis/${api_id}" --tags "$PROJECT_TAG_MAP" >/dev/null 2>&1 && log "tagged: API Gateway REST API $api_id"
  aws apigateway tag-resource --region "$REGION" --resource-arn "arn:aws:apigateway:${REGION}::/restapis/${api_id}/stages/prod" --tags "$PROJECT_TAG_MAP" >/dev/null 2>&1 && log "tagged: API Gateway stage ${api_id}/prod" || true
done

aws cloudwatch describe-alarms --region "$REGION" --output json 2>/dev/null | jq -r \
  '.MetricAlarms[] | select(.AlarmName | test("MSPAssistant|msp-assistant")) | .AlarmArn' |
while read -r alarm_arn; do
  aws cloudwatch tag-resource --region "$REGION" --resource-arn "$alarm_arn" --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && log "tagged: CloudWatch alarm $alarm_arn" || true
done

log "Tagging CloudWatch log groups..."
for prefix in \
  "/aws/bedrock-agentcore/" \
  "/aws/codebuild/bedrock-agentcore-" \
  "MSPAssistantBackendStack-"; do
  aws logs describe-log-groups --region "$REGION" --log-group-name-prefix "$prefix" --output json 2>/dev/null | jq -r '.logGroups[].logGroupName' |
  while read -r log_group_name; do
    tag_log_group "$log_group_name"
  done
done

log "Tagging IAM roles and policies..."
aws iam list-roles --output json 2>/dev/null | jq -r \
  '.Roles[]
    | select((.RoleName | startswith("MSPAssistant"))
      or (.RoleName | startswith("msp-"))
      or (.RoleName | startswith("automatick-"))
      or (.RoleName | startswith("AmazonBedrockAgentCoreSDK")))
    | .RoleName' |
while read -r role_name; do
  tag_iam_role "$role_name"
done

aws iam list-policies --scope Local --output json 2>/dev/null | jq -r \
  '.Policies[]
    | select((.PolicyName | startswith("MSPAssistant"))
      or (.PolicyName | startswith("msp-"))
      or (.PolicyName | startswith("automatick-"))
      or (.PolicyName | startswith("AmazonBedrockAgentCoreSDK")))
    | .Arn' |
while read -r policy_arn; do
  tag_iam_policy "$policy_arn"
done

log "Tagging Secrets Manager secrets..."
aws secretsmanager list-secrets --region "$REGION" --output json 2>/dev/null | jq -r \
  '.SecretList[]
    | select((.Name | startswith("msp-credentials/"))
      or (.Name | startswith("bedrock-agentcore-identity")))
    | [.ARN, .Name] | @tsv' |
while IFS=$'\t' read -r arn name; do
  aws secretsmanager tag-resource --region "$REGION" --secret-id "$arn" --tags "${PROJECT_TAG_ARGS[@]}" >/dev/null 2>&1 && log "tagged: secret $name" || true
done

log "Tagging S3 buckets with project-like names..."
aws s3api list-buckets --output json 2>/dev/null | jq -r \
  '.Buckets[].Name
    | select(test("mspassistant|msp-assistant|bedrock-agentcore"; "i"))' |
while read -r bucket; do
  tag_s3_bucket "$bucket"
done

log "Done."
