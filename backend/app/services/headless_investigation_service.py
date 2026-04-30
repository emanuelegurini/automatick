"""
Headless Freshdesk-to-AWS investigation workflow for Automatick.

This service reuses the existing AgentCore specialist path, persists request
state in the chat DynamoDB table, posts a Freshdesk private note, and creates a
pending remediation record. It intentionally never executes AWS write actions.
"""

import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3

from app.core.config import settings
from app.services.freshdesk_service import FreshdeskClient, format_private_note, get_freshdesk_client

logger = logging.getLogger(__name__)

TABLE_NAME = os.getenv("CHAT_REQUESTS_TABLE", "msp-assistant-chat-requests")
REQUEST_TTL_SECONDS = 7 * 24 * 60 * 60
REMEDIATION_TTL_SECONDS = 30 * 24 * 60 * 60

_dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
_table = _dynamodb.Table(TABLE_NAME)

_ACCOUNT_NAME_UNSAFE_CHARS = re.compile(r"[^a-z0-9_]")
_RESOURCE_ID_PATTERN = re.compile(
    r"\b("
    r"i-[0-9a-f]{6,17}|"
    r"vol-[0-9a-f]{6,17}|"
    r"eni-[0-9a-f]{6,17}|"
    r"sg-[0-9a-f]{6,17}|"
    r"subnet-[0-9a-f]{6,17}|"
    r"vpc-[0-9a-f]{6,17}|"
    r"arn:aws:[A-Za-z0-9:/_.+=,@-]+"
    r")\b",
    re.IGNORECASE,
)
_REGION_PATTERN = re.compile(r"\b([a-z]{2}-[a-z]+-\d)\b")


@dataclass
class Incident:
    ticket_id: str
    subject: str
    description: str
    account_name: str = "default"
    region: str = "us-east-1"
    resource_id: Optional[str] = None


def _decimal_to_python(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_python(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_python(i) for i in obj]
    return obj


def _convert_floats(obj: Any) -> Any:
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _convert_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_floats(i) for i in obj]
    return obj


def _first_non_empty(*values: Any) -> Optional[Any]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _clean_html(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _sanitize_account_name(value: Any) -> str:
    raw = str(value or "default").strip().lower().replace("-", "_").replace(" ", "_")
    sanitized = _ACCOUNT_NAME_UNSAFE_CHARS.sub("", raw)
    return sanitized or "default"


def _extract_resource_id(*texts: Any) -> Optional[str]:
    for text in texts:
        match = _RESOURCE_ID_PATTERN.search(str(text or ""))
        if match:
            return match.group(1)
    return None


def _extract_region(*texts: Any) -> Optional[str]:
    for text in texts:
        match = _REGION_PATTERN.search(str(text or ""))
        if match:
            return match.group(1)
    return None


def _custom_field(custom_fields: Dict[str, Any], *names: str) -> Optional[Any]:
    lower_fields = {str(k).lower(): v for k, v in (custom_fields or {}).items()}
    for name in names:
        candidates = {name, f"cf_{name}", name.replace("_", "-"), f"cf_{name.replace('_', '-')}"}
        for candidate in candidates:
            value = lower_fields.get(candidate.lower())
            if value not in (None, ""):
                return value
    return None


def normalize_freshdesk_payload(payload: Dict[str, Any]) -> Incident:
    """
    Normalize a direct or Freshdesk-shaped webhook payload into an Incident.

    Supports the explicit MVP contract and common Freshdesk webhook variants
    where ticket data is nested under `ticket`, `data.ticket`, or
    `freshdesk_webhook.ticket`.
    """
    data_ticket = payload.get("data", {}).get("ticket") if isinstance(payload.get("data"), dict) else None
    webhook_ticket = (
        payload.get("freshdesk_webhook", {}).get("ticket")
        if isinstance(payload.get("freshdesk_webhook"), dict)
        else None
    )
    ticket = payload.get("ticket") or data_ticket or webhook_ticket or payload
    if not isinstance(ticket, dict):
        raise ValueError("Freshdesk payload must contain a ticket object")

    custom_fields = ticket.get("custom_fields") or payload.get("custom_fields") or {}
    description = _first_non_empty(
        ticket.get("description_text"),
        ticket.get("description"),
        payload.get("description_text"),
        payload.get("description"),
    )
    subject = _first_non_empty(ticket.get("subject"), payload.get("subject"), "Freshdesk ticket")

    ticket_id = _first_non_empty(
        payload.get("ticket_id"),
        ticket.get("ticket_id"),
        ticket.get("id"),
        payload.get("id"),
    )
    if ticket_id is None:
        raise ValueError("ticket_id is required")

    combined_text = f"{subject}\n{description or ''}"
    account_name = _first_non_empty(
        payload.get("account_name"),
        ticket.get("account_name"),
        _custom_field(custom_fields, "account_name", "aws_account", "customer_account"),
        "default",
    )
    region = _first_non_empty(
        payload.get("region"),
        ticket.get("region"),
        _custom_field(custom_fields, "region", "aws_region"),
        _extract_region(combined_text),
        settings.AWS_REGION,
    )
    resource_id = _first_non_empty(
        payload.get("resource_id"),
        ticket.get("resource_id"),
        _custom_field(custom_fields, "resource_id", "aws_resource_id", "instance_id"),
        _extract_resource_id(combined_text),
    )

    return Incident(
        ticket_id=str(ticket_id),
        subject=str(subject).strip(),
        description=_clean_html(description),
        account_name=_sanitize_account_name(account_name),
        region=str(region or settings.AWS_REGION).strip(),
        resource_id=str(resource_id).strip() if resource_id else None,
    )


def _extract_markdown_section(text: str, section_name: str) -> str:
    pattern = re.compile(
        rf"(?:^|\n)\s*(?:#+\s*)?{re.escape(section_name)}\s*:?\s*\n?(.*?)(?=\n\s*(?:#+\s*)?(?:Root cause hypothesis|Evidence|Proposed fix|Proposed action|Risk / impact|Risk and impact|Approval required)\s*:?\s*\n|$)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text or "")
    return match.group(1).strip(" \n:-") if match else ""


def structure_investigation_response(raw_response: str) -> Dict[str, Any]:
    """Extract standard investigation sections from the agent response."""
    root_cause = _extract_markdown_section(raw_response, "Root cause hypothesis")
    evidence = _extract_markdown_section(raw_response, "Evidence")
    proposed_fix = _extract_markdown_section(raw_response, "Proposed fix") or _extract_markdown_section(raw_response, "Proposed action")
    risk_impact = _extract_markdown_section(raw_response, "Risk / impact") or _extract_markdown_section(raw_response, "Risk and impact")

    return {
        "summary": raw_response[:1000],
        "root_cause_hypothesis": root_cause or "The investigation did not return a separate root cause section.",
        "evidence": evidence or raw_response,
        "proposed_fix": proposed_fix or "Review the evidence and approve a manual remediation plan.",
        "risk_impact": risk_impact or "No AWS changes have been executed. Human approval is required before remediation.",
        "proposed_action": proposed_fix or "Manual review required",
        "raw_response": raw_response,
    }


class HeadlessInvestigationService:
    """Coordinates Freshdesk intake, AgentCore investigation, and approval state."""

    def __init__(
        self,
        table: Any = None,
        freshdesk_client: Optional[FreshdeskClient] = None,
    ):
        self.table = table or _table
        self.freshdesk_client = freshdesk_client or get_freshdesk_client()

    async def build_incident(self, payload: Dict[str, Any]) -> Incident:
        incident = normalize_freshdesk_payload(payload)
        return await self.enrich_incident(incident)

    async def enrich_incident(self, incident: Incident) -> Incident:
        # Some Freshdesk webhook configurations only send an ID. Enrich from
        # Freshdesk when credentials are available and key fields are sparse.
        if (
            self.freshdesk_client
            and self.freshdesk_client.is_configured
            and (incident.subject == "Freshdesk ticket" or not incident.description)
        ):
            try:
                details = await self.freshdesk_client.fetch_ticket_details(incident.ticket_id)
                freshdesk_incident = normalize_freshdesk_payload({"ticket": details, "ticket_id": incident.ticket_id})
                incident = Incident(
                    ticket_id=incident.ticket_id,
                    subject=freshdesk_incident.subject if incident.subject == "Freshdesk ticket" else incident.subject,
                    description=freshdesk_incident.description if not incident.description else incident.description,
                    account_name=freshdesk_incident.account_name if incident.account_name == "default" else incident.account_name,
                    region=freshdesk_incident.region if incident.region == settings.AWS_REGION else incident.region,
                    resource_id=incident.resource_id or freshdesk_incident.resource_id,
                )
            except Exception as exc:
                logger.warning("Could not enrich Freshdesk ticket %s: %s", incident.ticket_id, exc)

        return incident

    def create_request(self, request_id: str, incident: Incident) -> None:
        now = int(time.time())
        self.table.put_item(
            Item={
                "request_id": request_id,
                "source": "freshdesk",
                "status": "processing",
                "incident": asdict(incident),
                "created_at": now,
                "updated_at": now,
                "ttl": now + REQUEST_TTL_SECONDS,
            }
        )

    def _mark_request_complete(self, request_id: str, result: Dict[str, Any]) -> None:
        now = int(time.time())
        self.table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET #s = :status, #r = :result, updated_at = :updated_at",
            ExpressionAttributeNames={"#s": "status", "#r": "result"},
            ExpressionAttributeValues={
                ":status": "complete",
                ":result": _convert_floats(result),
                ":updated_at": now,
            },
        )

    def _mark_request_failed(self, request_id: str, error_message: str) -> None:
        now = int(time.time())
        self.table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET #s = :status, #r = :result, updated_at = :updated_at",
            ExpressionAttributeNames={"#s": "status", "#r": "result"},
            ExpressionAttributeValues={
                ":status": "error",
                ":result": {"success": False, "error": error_message},
                ":updated_at": now,
            },
        )

    def _update_request_incident(self, request_id: str, incident: Incident) -> None:
        now = int(time.time())
        self.table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET incident = :incident, updated_at = :updated_at",
            ExpressionAttributeValues={
                ":incident": asdict(incident),
                ":updated_at": now,
            },
        )

    def create_pending_remediation(self, incident: Incident, investigation: Dict[str, Any]) -> Dict[str, Any]:
        remediation_id = f"rem-{uuid.uuid4()}"
        now = int(time.time())
        item = {
            "request_id": f"remediation-{remediation_id}",
            "record_type": "remediation",
            "remediation_id": remediation_id,
            "ticket_id": incident.ticket_id,
            "proposed_action": investigation.get("proposed_action") or investigation.get("proposed_fix") or "Manual review required",
            "resource_id": incident.resource_id or "",
            "region": incident.region,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "ttl": now + REMEDIATION_TTL_SECONDS,
        }
        self.table.put_item(Item=_convert_floats(item))
        return item

    def get_remediation(self, remediation_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"request_id": f"remediation-{remediation_id}"})
        item = response.get("Item")
        if not item:
            return None
        return _decimal_to_python(item)

    def approve_remediation(self, remediation_id: str, approved_by: str = "unknown") -> Optional[Dict[str, Any]]:
        existing = self.get_remediation(remediation_id)
        if not existing:
            return None
        now = int(time.time())
        self.table.update_item(
            Key={"request_id": f"remediation-{remediation_id}"},
            UpdateExpression="SET #s = :status, approved_by = :approved_by, approved_at = :approved_at, updated_at = :updated_at",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "approved",
                ":approved_by": approved_by,
                ":approved_at": now,
                ":updated_at": now,
            },
        )
        updated = self.get_remediation(remediation_id)
        if updated:
            updated["execution"] = "not_executed"
        return updated

    @staticmethod
    def build_investigation_prompt(incident: Incident) -> str:
        resource_clause = (
            f"- Inspect EC2 state/status and related CloudWatch metrics for resource `{incident.resource_id}`.\n"
            if incident.resource_id
            else "- Identify likely AWS resources from the ticket text before checking evidence.\n"
        )
        return f"""You are Automatick, a headless AWS incident investigator.

Analyze this Freshdesk ticket using read-only AWS evidence. Do not execute remediation, do not call AWS write APIs, and do not close or update external tickets.

Ticket:
- Freshdesk ticket ID: {incident.ticket_id}
- Subject: {incident.subject}
- Description: {incident.description}
- Account name: {incident.account_name}
- Region: {incident.region}
- Resource ID: {incident.resource_id or "not provided"}

Evidence to inspect:
{resource_clause}- Check CloudWatch alarms in ALARM/INSUFFICIENT_DATA state.
- Check relevant CloudWatch metrics and recent logs when available.
- Use AWS API read-only inspection for related EC2, load balancer, ECS, Lambda, RDS, or other resource context when useful.

Return concise Markdown with exactly these headings:
Root cause hypothesis
Evidence
Proposed fix
Risk / impact
Proposed action

The proposed action must be a human-readable remediation proposal only. It must not imply that any AWS change has already been made."""

    async def run_investigation(self, incident: Incident) -> Dict[str, Any]:
        """Invoke CloudWatch specialist when available, otherwise Supervisor."""
        from app.core.direct_router import get_direct_router

        prompt = self.build_investigation_prompt(incident)
        session_id = f"freshdesk-{incident.ticket_id}-{uuid.uuid4().hex[:8]}"
        direct_router = get_direct_router()

        result = None
        if direct_router.can_route_directly("cloudwatch"):
            result = await direct_router.invoke_specialist(
                agent_key="cloudwatch",
                prompt=prompt,
                account_name=incident.account_name,
                region=incident.region,
                session_id=session_id,
            )

        if result is None and settings.SUPERVISOR_RUNTIME_ARN:
            from app.core.agentcore_client import get_agentcore_client

            agentcore = get_agentcore_client(region=incident.region or settings.AWS_REGION)
            result = await agentcore.invoke_runtime(
                runtime_arn=settings.SUPERVISOR_RUNTIME_ARN,
                payload={
                    "prompt": prompt,
                    "account_name": incident.account_name,
                    "workflow_enabled": False,
                    "full_automation": False,
                    "session_id": session_id,
                    "user_context": {
                        "user_id": "freshdesk",
                        "email": "",
                        "account_name": incident.account_name,
                    },
                },
                session_id=session_id,
            )

        if result is None:
            raise RuntimeError("AgentCore investigation runtime is not configured")

        raw_response = result.get("response", "") if isinstance(result, dict) else str(result)
        if not raw_response:
            raise RuntimeError("AgentCore returned an empty investigation response")

        structured = structure_investigation_response(raw_response)
        structured["agent_type"] = result.get("agent_type", "cloudwatch") if isinstance(result, dict) else "unknown"
        return structured

    async def process_freshdesk_ticket(self, request_id: str, incident: Incident) -> None:
        """Background worker for the Freshdesk webhook."""
        try:
            incident = await self.enrich_incident(incident)
            self._update_request_incident(request_id, incident)
            investigation = await self.run_investigation(incident)
            remediation = self.create_pending_remediation(incident, investigation)
            note_body = format_private_note(
                ticket_id=incident.ticket_id,
                investigation=investigation,
                remediation_id=remediation["remediation_id"],
            )

            note_result = await self.freshdesk_client.post_private_note(incident.ticket_id, note_body)
            result = {
                "success": True,
                "incident": asdict(incident),
                "investigation": investigation,
                "remediation": remediation,
                "freshdesk_note": note_result,
                "freshdesk_note_posted": True,
            }
            self._mark_request_complete(request_id, result)
        except Exception as exc:
            logger.error("Freshdesk investigation failed for %s: %s", request_id, exc, exc_info=True)
            self._mark_request_failed(request_id, "Freshdesk investigation failed")


_headless_service: Optional[HeadlessInvestigationService] = None


def get_headless_investigation_service() -> HeadlessInvestigationService:
    """Return a process-wide headless investigation service."""
    global _headless_service
    if _headless_service is None:
        _headless_service = HeadlessInvestigationService()
    return _headless_service
