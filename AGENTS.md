# Repository Guidelines

## Project Structure & Module Organization

- `backend/app/`: Automatick FastAPI service; endpoints in `api/routes.py`, configuration/AWS clients in `core/`, Freshdesk/headless workflow logic in `services/`.
- `agents/`: Supervisor and specialist AgentCore runtimes, each with its own `requirements.txt`.
- `mcp-servers/`: MCP servers for AWS API, CloudWatch, and knowledge tools.
- `infrastructure/cdk/`: Python CDK app and stacks.
- `frontend/src/`: Legacy optional React app; disabled by default for the headless Freshdesk MVP.
- `runbooks/`, `diagrams/`, `scripts/`, `misc/`: operational docs, assets, helper scripts, and demo utilities.

## Build, Test, and Development Commands

- `./scripts/validate-prerequisites.sh us-east-1`: verifies required local tools, AWS credentials, and Bedrock access.
- `cd backend && python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`: prepares backend dependencies.
- `cd backend && uvicorn app.main:app --reload`: runs the FastAPI backend locally.
- `cd backend && source .venv/bin/activate && python -m unittest discover -s tests`: runs focused backend unit tests.
- `cd infrastructure/cdk && python3.11 -m pip install -r requirements.txt && cdk synth`: validates CDK synthesis.
- `./deploy.sh --email admin@example.com --region us-east-1`: runs the default headless Freshdesk deployment flow.
- `cd frontend && npm install && npm run build && npm run lint`: validates the optional legacy UI when `ENABLE_FRONTEND=true`.

## Coding Style & Naming Conventions

Use 4-space indentation for Python and 2-space indentation for TypeScript/React. Name Python modules in `snake_case`, runtime folders with the existing `runtime_*` pattern, components in `PascalCase`, hooks as `useThing`, and stores as `thingStore.ts`. Keep Python dependencies pinned unless intentionally upgrading related packages together.

## Testing Guidelines

Use `test_*.py` for Python tests under `backend/tests/`. For every change, run the relevant unit, build, lint, or synthesis command. Frontend tests are only required when the optional UI is changed.

## Commit & Pull Request Guidelines

Recent history uses short, imperative messages such as `Bump axios from 1.13.5 to 1.15.0 in /frontend` and `Release: v2`. Keep commits focused and avoid unrelated reformatting. PRs should target `main`, link issues for significant work, describe Freshdesk/webhook/remediation impact, list validation commands, include UI screenshots only when the optional UI changes, and address CI or review feedback.

## Security & Configuration Tips

Copy `backend/.env.example` for local configuration. The default MVP uses `AUTOMATICK_MODE=headless`, `ENABLE_FRONTEND=false`, `ENABLE_JIRA=false`, and `ENABLE_FRESHDESK=true`. Do not commit `.env` files, Freshdesk API keys, webhook secrets, credentials, Jira tokens, AWS account IDs beyond examples, or deployment secrets. Report security issues through the AWS vulnerability process in `CONTRIBUTING.md`, not public issues.
