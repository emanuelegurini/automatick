#!/usr/bin/env python3
"""
Automatick AgentCore CDK App Entry Point
========================================
Orchestrates the deployment for the headless Freshdesk-first operations platform.

Stack deployment order (enforced by add_dependency):
  1. AgentCoreStack  — AgentCore Memory, Gateway, Observability.
                       Deployed first; exports ARNs consumed by BackendStack.
                       Resources are created imperatively via boto3 (CDK L2
                       constructs for AgentCore are not yet available).

  2. BackendStack    — ECS Fargate service + ALB + API Gateway + Cognito.
                       Receives agentcore_resources dict (ARNs/IDs) from
                       AgentCoreStack and bakes them into ECS environment vars.
                       Depends on: AgentCoreStack.

  3. FrontendStack   — React SPA on S3 + CloudFront CDN.
                       Receives the API Gateway URL and Cognito config from
                       BackendStack at synthesis time, and embeds them in a
                       runtime config.json served alongside the SPA.
                       Depends on: BackendStack.

Context variables (passed via --context or cdk.json):
  account                 — AWS account ID (defaults to CDK caller account)
  region                  — AWS region (defaults to us-east-1)
  supervisor_runtime_arn  — AgentCore Supervisor Runtime ARN (set by deploy.sh)
  cloudwatch_a2a_arn, security_a2a_arn, cost_a2a_arn,
  advisor_a2a_arn, jira_a2a_arn, knowledge_a2a_arn
                          — A2A Specialist Runtime ARNs for Supervisor routing
                            (optional; populated by deploy.sh Step 9)
  alb_dns                 — ALB DNS name for CloudFront → ALB SSE streaming
                            origin (empty on first deploy; set on re-deploys)

Deploy: cdk deploy --all
"""
import aws_cdk as cdk
import os
from stacks.backend_stack import BackendStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.frontend_stack import FrontendStack

app = cdk.App()

PROJECT_TAG_VALUE = os.getenv("PROJECT_TAG_VALUE", "mps-ops-utomation-poc")
OWNER_TAG_VALUE = os.getenv("OWNER_TAG_VALUE", "simone.ferraro")

# Get config
env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-east-1"
)

enable_frontend_context = app.node.try_get_context("enable_frontend")
enable_frontend = (
    str(enable_frontend_context).lower() == "true"
    if enable_frontend_context is not None
    else os.getenv("ENABLE_FRONTEND", "false").lower() == "true"
)

# 1. AgentCore Infrastructure
agentcore_stack = AgentCoreStack(
    app, "MSPAssistantAgentCoreStack",
    env=env,
    description="AgentCore Runtime, Gateway, Memory, Identity, Policy, Observability (uksb-lfevfsxkwc)(tag:agentcore)"
)

# 2. Backend (ECS Fargate + ALB + API Gateway)
backend_stack = BackendStack(
    app, "MSPAssistantBackendStack",
    agentcore_resources=agentcore_stack.resources,
    env=env,
    description="FastAPI on ECS Fargate with ALB and API Gateway (uksb-lfevfsxkwc)(tag:backend)"
)
backend_stack.add_dependency(agentcore_stack)

stacks = [agentcore_stack, backend_stack]

# 3. Optional Frontend (S3 + CloudFront)
if enable_frontend:
    frontend_stack = FrontendStack(
        app, "MSPAssistantFrontendStack",
        api_url=backend_stack.api_url,
        alb_dns=app.node.try_get_context("alb_dns") or "",
        cognito_config=backend_stack.cognito_config,
        env=env,
        description="React SPA on S3 with CloudFront CDN (uksb-lfevfsxkwc)(tag:frontend)"
    )
    frontend_stack.add_dependency(backend_stack)
    stacks.append(frontend_stack)

# Tags
for stack in stacks:
    cdk.Tags.of(stack).add("Project", PROJECT_TAG_VALUE)
    cdk.Tags.of(stack).add("owner", OWNER_TAG_VALUE)
    cdk.Tags.of(stack).add("ManagedBy", "CDK")

app.synth()
