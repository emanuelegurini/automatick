import os
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("COGNITO_USER_POOL_ID", "test-pool")
os.environ.setdefault("COGNITO_CLIENT_ID", "test-client")
os.environ.setdefault("FRESHDESK_WEBHOOK_SECRET", "correct-secret")

from fastapi import HTTPException

from app.api.routes import _validate_freshdesk_webhook_secret
from app.core import direct_router
from app.services.freshdesk_service import format_private_note
from app.services import headless_investigation_service as headless_module
from app.services.headless_investigation_service import (
    HeadlessInvestigationService,
    _build_agentcore_session_id,
    _is_json_rpc_error,
    normalize_freshdesk_payload,
    structure_investigation_response,
)


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["request_id"]] = deepcopy(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["request_id"])
        return {"Item": deepcopy(item)} if item else {}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues,
        ExpressionAttributeNames=None,
    ):
        item = self.items.setdefault(Key["request_id"], {"request_id": Key["request_id"]})
        names = ExpressionAttributeNames or {}
        assignments = UpdateExpression.removeprefix("SET ").split(",")
        for assignment in assignments:
            left, right = assignment.split("=")
            attr = left.strip()
            value_key = right.strip()
            attr = names.get(attr, attr)
            item[attr] = deepcopy(ExpressionAttributeValues[value_key])
        return {}


class FakeFreshdeskClient:
    is_configured = True

    def __init__(self):
        self.notes = []

    async def fetch_ticket_details(self, ticket_id):
        return {
            "id": ticket_id,
            "subject": "Fetched subject",
            "description_text": "Fetched description",
        }

    async def post_private_note(self, ticket_id, body):
        self.notes.append({"ticket_id": ticket_id, "body": body})
        return {"id": 99, "private": True}


class FreshdeskWebhookSecretTests(unittest.TestCase):
    def test_missing_secret_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_freshdesk_webhook_secret(None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_secret_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_freshdesk_webhook_secret("wrong")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_correct_secret_is_accepted(self):
        _validate_freshdesk_webhook_secret("correct-secret")


class FreshdeskPayloadTests(unittest.TestCase):
    def test_numeric_ticket_id_builds_valid_agentcore_session_id(self):
        session_id = _build_agentcore_session_id("572358")

        self.assertGreaterEqual(len(session_id), 33)
        self.assertTrue(session_id.startswith("freshdesk-ticket-572358-"))

    def test_json_rpc_error_response_is_detected(self):
        self.assertTrue(_is_json_rpc_error('{"error":{"code":-32603,"message":"Internal error"},"jsonrpc":"2.0"}'))
        self.assertFalse(_is_json_rpc_error("Root cause hypothesis\nNo active alarms."))

    def test_normalizes_explicit_payload(self):
        incident = normalize_freshdesk_payload(
            {
                "ticket_id": "12345",
                "subject": "EC2 instance not responding",
                "description": "Instance i-abc123 appears down in us-east-1",
                "account_name": "Default",
                "region": "us-east-1",
            }
        )

        self.assertEqual(incident.ticket_id, "12345")
        self.assertEqual(incident.account_name, "default")
        self.assertEqual(incident.resource_id, "i-abc123")
        self.assertEqual(incident.region, "us-east-1")

    def test_normalizes_nested_freshdesk_payload(self):
        incident = normalize_freshdesk_payload(
            {
                "ticket": {
                    "id": 777,
                    "subject": "ALB target unhealthy",
                    "description": "<p>Target i-0123456789abcdef0 in eu-west-1</p>",
                    "custom_fields": {
                        "cf_account_name": "Customer One",
                        "cf_region": "eu-west-1",
                    },
                }
            }
        )

        self.assertEqual(incident.ticket_id, "777")
        self.assertEqual(incident.account_name, "customer_one")
        self.assertEqual(incident.region, "eu-west-1")
        self.assertEqual(incident.resource_id, "i-0123456789abcdef0")
        self.assertEqual(incident.description, "Target i-0123456789abcdef0 in eu-west-1")


class FreshdeskNoteTests(unittest.TestCase):
    def test_structures_bold_markdown_headings_and_strips_thinking(self):
        structured = structure_investigation_response(
            """<thinking>internal chain</thinking>
**Root cause hypothesis**
- ECS service is under-utilized.

**Evidence**
- CPUUtilization datapoints are below threshold.

**Proposed fix**
- Review target tracking policy.

**Risk / impact**
- No AWS changes executed.
"""
        )

        self.assertEqual(structured["root_cause_hypothesis"], "ECS service is under-utilized.")
        self.assertEqual(structured["evidence"], "CPUUtilization datapoints are below threshold.")
        self.assertEqual(structured["proposed_fix"], "Review target tracking policy.")
        self.assertNotIn("<thinking>", structured["summary"])

    def test_formats_private_note_with_required_sections(self):
        note = format_private_note(
            ticket_id="123",
            remediation_id="rem-abc",
            investigation={
                "root_cause_hypothesis": "Instance failed status checks",
                "evidence": "EC2 status check failed",
                "proposed_fix": "Reboot the instance after approval",
                "risk_impact": "Brief service interruption",
            },
        )

        self.assertIn("Automatick investigation complete", note)
        self.assertIn("Root cause hypothesis", note)
        self.assertIn("Evidence", note)
        self.assertIn("Proposed fix", note)
        self.assertIn("Approval required", note)
        self.assertIn("rem-abc", note)


class RemediationLifecycleTests(unittest.TestCase):
    def test_pending_remediation_can_be_retrieved_and_approved(self):
        table = FakeTable()
        service = HeadlessInvestigationService(table=table, freshdesk_client=FakeFreshdeskClient())
        incident = normalize_freshdesk_payload(
            {
                "ticket_id": "12345",
                "subject": "EC2 instance not responding",
                "description": "Instance i-abc123 appears down",
                "account_name": "default",
                "region": "us-east-1",
                "resource_id": "i-abc123",
            }
        )
        remediation = service.create_pending_remediation(
            incident,
            {"proposed_action": "Reboot instance after approval"},
        )

        loaded = service.get_remediation(remediation["remediation_id"])
        self.assertEqual(loaded["status"], "pending")
        self.assertEqual(loaded["resource_id"], "i-abc123")

        approved = service.approve_remediation(remediation["remediation_id"], approved_by="ops@example.com")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_by"], "ops@example.com")
        self.assertEqual(approved["execution"], "not_executed")


class DirectRouterTests(unittest.TestCase):
    def test_direct_routing_requires_explicit_flag_even_when_arn_exists(self):
        old_enabled = direct_router.settings.ENABLE_DIRECT_SPECIALIST_ROUTING
        old_arn = direct_router.SPECIALIST_ARN_MAP.get("cloudwatch", "")
        client = object.__new__(direct_router.DirectRouterClient)
        try:
            direct_router.SPECIALIST_ARN_MAP["cloudwatch"] = "arn:aws:bedrock-agentcore:test"

            direct_router.settings.ENABLE_DIRECT_SPECIALIST_ROUTING = False
            self.assertFalse(client.can_route_directly("cloudwatch"))

            direct_router.settings.ENABLE_DIRECT_SPECIALIST_ROUTING = True
            self.assertTrue(client.can_route_directly("cloudwatch"))
        finally:
            direct_router.settings.ENABLE_DIRECT_SPECIALIST_ROUTING = old_enabled
            direct_router.SPECIALIST_ARN_MAP["cloudwatch"] = old_arn


class HeadlessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_investigation_invokes_supervisor_not_direct_router(self):
        table = FakeTable()
        service = HeadlessInvestigationService(table=table, freshdesk_client=FakeFreshdeskClient())
        incident = normalize_freshdesk_payload(
            {
                "ticket_id": "12345",
                "subject": "CloudWatch CPU alarm",
                "description": "CPUUtilization alarm in eu-west-1",
                "account_name": "default",
                "region": "eu-west-1",
            }
        )

        class FakeAgentCoreClient:
            def __init__(self):
                self.calls = []

            async def invoke_runtime(self, runtime_arn, payload, session_id):
                self.calls.append(
                    {
                        "runtime_arn": runtime_arn,
                        "payload": payload,
                        "session_id": session_id,
                    }
                )
                return {
                    "success": True,
                    "agent_type": "cloudwatch",
                    "response": """Root cause hypothesis
The Supervisor selected CloudWatch for alarm investigation.

Evidence
CloudWatch alarm context was inspected.

Proposed fix
Review the alarm and scaling policy.

Risk / impact
No AWS changes executed.

Proposed action
Manual review.""",
                }

        fake_agentcore = FakeAgentCoreClient()
        old_supervisor_arn = headless_module.settings.SUPERVISOR_RUNTIME_ARN
        old_direct_enabled = headless_module.settings.ENABLE_DIRECT_SPECIALIST_ROUTING
        try:
            headless_module.settings.SUPERVISOR_RUNTIME_ARN = "arn:aws:bedrock-agentcore:test:runtime/supervisor"
            headless_module.settings.ENABLE_DIRECT_SPECIALIST_ROUTING = True
            with (
                patch("app.core.agentcore_client.get_agentcore_client", return_value=fake_agentcore),
                patch("app.core.direct_router.get_direct_router") as get_direct_router,
            ):
                result = await service.run_investigation(incident)

            get_direct_router.assert_not_called()
            self.assertEqual(result["agent_type"], "cloudwatch")
            self.assertEqual(len(fake_agentcore.calls), 1)
            self.assertEqual(fake_agentcore.calls[0]["runtime_arn"], headless_module.settings.SUPERVISOR_RUNTIME_ARN)
            self.assertEqual(fake_agentcore.calls[0]["payload"]["account_name"], "default")
            self.assertEqual(fake_agentcore.calls[0]["payload"]["region"], "eu-west-1")
            self.assertFalse(fake_agentcore.calls[0]["payload"]["workflow_enabled"])
        finally:
            headless_module.settings.SUPERVISOR_RUNTIME_ARN = old_supervisor_arn
            headless_module.settings.ENABLE_DIRECT_SPECIALIST_ROUTING = old_direct_enabled

    async def test_webhook_processing_stores_result_posts_note_and_creates_remediation(self):
        table = FakeTable()
        freshdesk = FakeFreshdeskClient()
        service = HeadlessInvestigationService(table=table, freshdesk_client=freshdesk)
        service.run_investigation = AsyncMock(
            return_value=structure_investigation_response(
                """Root cause hypothesis
Instance failed EC2 status checks.

Evidence
CloudWatch StatusCheckFailed_Instance is 1.

Proposed fix
Reboot i-abc123 after approval.

Risk / impact
Brief interruption.

Proposed action
Reboot i-abc123."""
            )
        )

        incident = normalize_freshdesk_payload(
            {
                "ticket_id": "12345",
                "subject": "EC2 instance not responding",
                "description": "Instance i-abc123 appears down",
                "account_name": "default",
                "region": "us-east-1",
                "resource_id": "i-abc123",
            }
        )
        service.create_request("req-1", incident)
        await service.process_freshdesk_ticket("req-1", incident)

        request_item = table.items["req-1"]
        self.assertEqual(request_item["status"], "complete")
        self.assertTrue(request_item["result"]["freshdesk_note_posted"])
        self.assertEqual(freshdesk.notes[0]["ticket_id"], "12345")
        self.assertIn("Approval required", freshdesk.notes[0]["body"])

        remediation_items = [
            item for key, item in table.items.items()
            if key.startswith("remediation-")
        ]
        self.assertEqual(len(remediation_items), 1)
        self.assertEqual(remediation_items[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
