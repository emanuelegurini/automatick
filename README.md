# Automatick — Headless AWS Operations Investigation

> **Note:** This is a sample application intended for demonstration purposes only. It is not intended for production use without further review and hardening.

Automatick receives Freshdesk tickets, investigates AWS evidence through Amazon Bedrock AgentCore agents and MCP tools, posts an internal Freshdesk note with findings and a proposed fix, and creates a pending remediation approval record. The MVP is proposal-only: it does not execute AWS remediation.

## What It Does

- Accepts Freshdesk webhooks at `POST /api/v1/integrations/freshdesk/tickets`
- Validates `X-Automatick-Webhook-Secret`
- Normalizes ticket fields into an internal incident
- Uses existing AgentCore/A2A/MCP paths to inspect AWS evidence
- Posts a Freshdesk private note with root cause hypothesis, evidence, proposed fix, risk/impact, and approval requirement
- Stores request results and pending remediation proposals in DynamoDB
- Records approval state only (`pending -> approved`); no AWS write action is run

## Architecture

The default deployment is headless: FastAPI on ECS Fargate behind API Gateway, DynamoDB for async request and approval state, AgentCore Supervisor and specialist runtimes, and MCP servers for CloudWatch, AWS API, and knowledge tools. Cognito remains available for operator API routes such as remediation approval. The React frontend and Jira integration are optional and disabled by default.

Customer account context uses the existing `account_name` and `region` metadata path through the agents. For the demo target, use `account_name=default` to inspect the same AWS account where Automatick is deployed.

## Freshdesk Webhook

```text
POST /api/v1/integrations/freshdesk/tickets
X-Automatick-Webhook-Secret: <shared-secret>
Content-Type: application/json
```

Expected payload:

```json
{
  "ticket_id": "12345",
  "subject": "EC2 instance not responding",
  "description": "Instance i-abc123 appears down",
  "account_name": "default",
  "region": "us-east-1",
  "resource_id": "i-abc123"
}
```

Response:

```json
{
  "success": true,
  "request_id": "uuid",
  "status": "processing",
  "ticket_id": "12345"
}
```

## Remediation Approval API

Approval is state-only in this MVP.

```text
GET  /api/v1/remediations/{remediation_id}
POST /api/v1/remediations/{remediation_id}/approve
```

The approve endpoint records `approved`, `approved_by`, and `approved_at`. It never invokes AWS remediation.

## Configuration

Copy the example file and set Freshdesk values:

```bash
cp backend/.env.example backend/.env
```

Required for the default headless path:

| Variable | Description |
| --- | --- |
| `AUTOMATICK_MODE=headless` | Product mode |
| `ENABLE_FRONTEND=false` | Skip frontend stack/build/deploy |
| `ENABLE_JIRA=false` | Skip Jira validation, runtime, and Gateway target |
| `ENABLE_FRESHDESK=true` | Enable Freshdesk webhook flow |
| `FRESHDESK_DOMAIN` | Freshdesk domain, such as `your-company.freshdesk.com` |
| `FRESHDESK_API_KEY` | Freshdesk API key for ticket reads and private notes |
| `FRESHDESK_WEBHOOK_SECRET` | Shared secret expected in webhook header |
| `MODEL` | Bedrock model ID for AgentCore runtimes. Default: `us.amazon.nova-pro-v1:0` |

Optional:

| Variable | Description |
| --- | --- |
| `BEDROCK_KNOWLEDGE_BASE_ID` | Enables knowledge-base lookup |
| `ENABLE_FRONTEND=true` | Deploys the legacy React operator UI |
| `ENABLE_JIRA=true` | Deploys Jira specialist runtime and Gateway target |

## Deployment

Validate local prerequisites:

```bash
./scripts/validate-prerequisites.sh us-east-1
```

Deploy:

```bash
./deploy.sh --email admin@example.com --region us-east-1
```

In headless mode the deploy script skips frontend build/deploy and Jira setup. It still deploys backend, API Gateway, DynamoDB, Cognito, AgentCore Gateway/Memory, CloudWatch MCP, AWS API MCP, knowledge MCP, and the enabled specialist runtimes.

## Local Backend

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Validation

Run the focused backend tests:

```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s tests
```

Deployment dry check:

```bash
cd infrastructure/cdk
pip install -r requirements.txt
ENABLE_FRONTEND=false ENABLE_JIRA=false ENABLE_FRESHDESK=true cdk synth
```

## Project Structure

```text
backend/app/api/routes.py                         FastAPI routes
backend/app/services/freshdesk_service.py         Freshdesk API and note formatting
backend/app/services/headless_investigation_service.py
                                                   Freshdesk normalization, AgentCore investigation, remediation state
agents/                                          Supervisor and specialist AgentCore runtimes
mcp-servers/                                     CloudWatch, AWS API, and knowledge MCP servers
infrastructure/cdk/                              CDK app and stacks
scripts/                                         Validation and helper scripts
```

## Security Notes

Do not commit `.env`, Freshdesk API keys, webhook secrets, AWS credentials, Jira tokens, account IDs beyond examples, or deployment secrets. Freshdesk webhooks are unauthenticated at API Gateway by design, but the backend requires `X-Automatick-Webhook-Secret` and rejects missing or incorrect values.
