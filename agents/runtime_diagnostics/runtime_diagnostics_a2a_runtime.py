#!/usr/bin/env python3
"""Runtime Diagnostics A2A Runtime.

Collects read-only runtime evidence for EC2/SSM, ECS, and RDS. This runtime does
not perform remediation and does not expose arbitrary shell or SQL execution.
"""
import logging
import os
import uvicorn
from fastapi import FastAPI
from context_tools import create_a2a_server, create_runtime_diagnostics_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RUNTIME_DIAGNOSTICS_PROMPT = """You are a runtime diagnostics specialist for AWS operations.

Your job is to collect read-only runtime evidence and return a concise Markdown
summary to the Supervisor. You never perform remediation.

Tool boundaries:
- Use inspect_ec2_instance for EC2 metadata and SSM managed status.
- Use run_ssm_readonly_command only with approved command_profile values. Never ask
  for or invent a shell command.
- Use inspect_ecs_service for ECS service, deployment, task, health, and stopped
  reason evidence.
- Use inspect_rds_instance for RDS status, events, endpoint metadata, and recent
  CloudWatch metrics.
- Use run_rds_readonly_query only to report its current v1 limitation unless SQL
  execution has been explicitly enabled and implemented.

When to use EC2/SSM:
- Disk pressure: run disk_usage and usually linux_health.
- Memory pressure: run memory_pressure and linux_health.
- CPU pressure: run cpu_pressure and linux_health.
- Service/process issues: run failed_services or process_snapshot.
- Network/listener issues: run network_listeners.
- Host log investigation: run recent_syslog.

Return exactly this Markdown shape:

Runtime diagnostics summary

Target
- Type:
- Identifier:
- Region:
- Account context:

Checks performed
- ...

Evidence
- ...

Findings
- ...

Limitations
- ...

Recommended next step
- ...

If an instance is not managed by SSM, state that clearly in Limitations. If any
tool fails or times out, report the status and failure reason without inventing evidence.
Keep the response under 500 words unless explicitly asked for more detail.
"""

_runtime_url = os.environ.get("AGENTCORE_RUNTIME_URL", "http://127.0.0.1:9000/")
_a2a_server = None


def _get_a2a_server():
    global _a2a_server
    if _a2a_server is None:
        agent = create_runtime_diagnostics_agent(RUNTIME_DIAGNOSTICS_PROMPT)
        _a2a_server = create_a2a_server(agent, _runtime_url)
        logger.info("Runtime Diagnostics A2A server initialized")
    return _a2a_server


def ping():
    return {"status": "healthy", "agent": "runtime_diagnostics"}


class _LazyA2AApp:
    """ASGI wrapper that initializes the A2A server on first request."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        await _get_a2a_server().to_fastapi_app()(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="Runtime Diagnostics A2A Runtime")
    app.add_api_route("/ping", ping, methods=["GET"])
    app.mount("/", _LazyA2AApp())
    logger.info("Runtime Diagnostics A2A Runtime ready")
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)  # nosec B104 -- containerized runtime
