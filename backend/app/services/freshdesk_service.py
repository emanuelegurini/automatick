"""
Freshdesk integration helpers for Automatick.

The backend only needs two Freshdesk operations for the headless MVP:
fetching ticket details for sparse webhook payloads and posting a private
investigation note after AgentCore finishes analysis.
"""

import html
import logging
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class FreshdeskConfigurationError(RuntimeError):
    """Raised when Freshdesk settings are missing for an operation."""


class FreshdeskClient:
    """Small Freshdesk v2 API client using API-key basic authentication."""

    def __init__(
        self,
        domain: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: float = 15.0,
    ):
        self.domain = self._normalize_domain(domain or settings.FRESHDESK_DOMAIN or "")
        self.api_key = api_key or settings.FRESHDESK_API_KEY or ""
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        domain = domain.strip().rstrip("/")
        if not domain:
            return ""
        if not domain.startswith(("http://", "https://")):
            domain = f"https://{domain}"
        return domain

    @property
    def is_configured(self) -> bool:
        return bool(self.domain and self.api_key)

    def _require_config(self) -> None:
        if not self.is_configured:
            raise FreshdeskConfigurationError("Freshdesk domain and API key must be configured")

    async def fetch_ticket_details(self, ticket_id: str) -> Dict[str, Any]:
        """Fetch one Freshdesk ticket by ID."""
        self._require_config()
        url = f"{self.domain}/api/v2/tickets/{ticket_id}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(url, auth=(self.api_key, "X"))
            response.raise_for_status()
            return response.json()

    async def post_private_note(self, ticket_id: str, body: str) -> Dict[str, Any]:
        """Post an internal/private note to a Freshdesk ticket."""
        self._require_config()
        url = f"{self.domain}/api/v2/tickets/{ticket_id}/notes"
        payload = {
            "body": body,
            "private": True,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, json=payload, auth=(self.api_key, "X"))
            response.raise_for_status()
            return response.json()


def _html_paragraph(title: str, content: str) -> str:
    safe_title = html.escape(title)
    safe_content = html.escape(content or "Not available").replace("\n", "<br>")
    return f"<p><strong>{safe_title}</strong><br>{safe_content}</p>"


def format_private_note(
    *,
    ticket_id: str,
    investigation: Dict[str, Any],
    remediation_id: str,
) -> str:
    """
    Format an Automatick investigation as a Freshdesk private note.

    Freshdesk accepts HTML bodies for notes; escaping all dynamic content keeps
    ticket text and model output from being interpreted as markup.
    """
    root_cause = investigation.get("root_cause_hypothesis") or investigation.get("summary") or "See investigation summary."
    evidence = investigation.get("evidence") or investigation.get("raw_response") or "No evidence returned."
    proposed_fix = investigation.get("proposed_fix") or investigation.get("proposed_action") or "No action proposed."
    risk_impact = investigation.get("risk_impact") or "Review required before any AWS change."

    sections = [
        "<p><strong>Automatick investigation complete</strong></p>",
        _html_paragraph("Freshdesk ticket", str(ticket_id)),
        _html_paragraph("Root cause hypothesis", str(root_cause)),
        _html_paragraph("Evidence", str(evidence)),
        _html_paragraph("Proposed fix", str(proposed_fix)),
        _html_paragraph("Risk / impact", str(risk_impact)),
        _html_paragraph(
            "Approval required",
            f"Pending remediation record {remediation_id} was created. Approval records state only in v1; Automatick will not execute AWS remediation.",
        ),
    ]
    return "\n".join(sections)


_freshdesk_client: Optional[FreshdeskClient] = None


def get_freshdesk_client() -> FreshdeskClient:
    """Return a process-wide Freshdesk client."""
    global _freshdesk_client
    if _freshdesk_client is None:
        _freshdesk_client = FreshdeskClient()
    return _freshdesk_client
