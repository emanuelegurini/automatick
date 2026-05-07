"""
AWS Knowledge MCP Server for AgentCore Runtime
==============================================
Proxies to the official AWS Knowledge MCP Server at https://knowledge-mcp.global.api.aws

Per AWS docs: "A fully managed remote MCP server that provides up-to-date
documentation, code samples, knowledge about the regional availability of
AWS APIs and CloudFormation resources, and other official AWS content."

Tools (matching AWS Labs Knowledge MCP):
- search_internal_runbooks  searches Automatick internal Bedrock KB docs/runbooks
- search_aws_docs  maps to search_documentation
- read_aws_doc  maps to read_documentation
- get_regional_availability  maps to get_regional_availability
- list_aws_regions  maps to list_regions
- get_recommendations  maps to recommend

No cross-account access needed  AWS docs are public, no auth required.
No credential_helper.py needed.

Ref: https://knowledge-mcp.global.api.aws
Transport: streamable-http (required by AgentCore Runtime)
"""
from mcp.server.fastmcp import FastMCP
import httpx
import boto3
import os
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Official AWS Knowledge MCP API endpoint (public, no auth required)
KNOWLEDGE_API_URL = "https://knowledge-mcp.global.api.aws"
AWS_REGION = os.getenv("AWS_REGION", os.getenv("REGION", "us-east-1"))
BEDROCK_KNOWLEDGE_BASE_ID = os.getenv("BEDROCK_KNOWLEDGE_BASE_ID", "")


def _load_env_config() -> None:
    """Load optional deploy-time env_config.txt bundled with AgentCore runtime."""
    config_path = os.path.join(os.path.dirname(__file__), "env_config.txt")
    if not os.path.exists(config_path):
        return
    with open(config_path, encoding="utf-8") as config_file:
        for raw_line in config_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_config()
AWS_REGION = os.getenv("AWS_REGION", os.getenv("REGION", "us-east-1"))
BEDROCK_KNOWLEDGE_BASE_ID = os.getenv("BEDROCK_KNOWLEDGE_BASE_ID", "")

mcp = FastMCP(
    name="aws-knowledge-mcp",
    host="0.0.0.0",  # nosec B104 -- intentional: AgentCore Runtime container networking
    stateless_http=True
)


def _call_knowledge_api(tool_name: str, arguments: dict, request_id: str = "req-1") -> dict:
    """
    Call the AWS Knowledge MCP API via JSON-RPC over streamable HTTP.

    Args:
        tool_name: AWS Knowledge MCP tool name (e.g., aws___search_documentation)
        arguments: Tool arguments dict
        request_id: JSON-RPC request ID

    Returns:
        Parsed JSON response from the API
    """
    response = httpx.post(
        f"{KNOWLEDGE_API_URL}/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            },
            "id": request_id
        },
        timeout=30.0,
        headers={"Content-Type": "application/json"}
    )
    response.raise_for_status()
    return response.json()


def _limit_text(value: object, max_chars: int = 3000) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"


@mcp.tool()
def search_internal_runbooks(
    query: str,
    max_results: int = 5,
    min_score: float = 0.25,
) -> dict:
    """
    Search Automatick internal runbooks and docs from the Bedrock Knowledge Base.

    This is the preferred documentation source for MSP-specific investigation
    flow, Freshdesk notes, remediation approval policy, tagging conventions,
    and repeatable test procedures. Use official AWS docs for AWS service/API
    semantics after checking internal runbooks.

    Args:
        query: Focused search query, for example "EC2 high CPU sqlservr CloudWatch".
        max_results: Maximum results, clamped to 1..10.
        min_score: Minimum Bedrock KB relevance score, clamped to 0.0..1.0.

    Returns:
        Dict with ranked internal runbook/doc results.
    """
    if not BEDROCK_KNOWLEDGE_BASE_ID:
        return {
            "success": False,
            "error": "BEDROCK_KNOWLEDGE_BASE_ID is not configured for internal runbook search.",
            "source": "Bedrock Knowledge Base",
        }
    if not str(query or "").strip():
        return {"success": False, "error": "query is required", "source": "Bedrock Knowledge Base"}

    try:
        bounded_results = max(1, min(int(max_results or 5), 10))
    except (TypeError, ValueError):
        bounded_results = 5
    try:
        bounded_score = max(0.0, min(float(min_score), 1.0))
    except (TypeError, ValueError):
        bounded_score = 0.25

    try:
        client = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
        response = client.retrieve(
            knowledgeBaseId=BEDROCK_KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": str(query).strip()},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": bounded_results}
            },
        )

        results = []
        for item in response.get("retrievalResults", []):
            score = float(item.get("score", 0.0))
            if score < bounded_score:
                continue
            location = item.get("location", {})
            s3_location = location.get("s3Location", {}) if isinstance(location, dict) else {}
            results.append(
                {
                    "score": score,
                    "content": _limit_text(item.get("content", {}).get("text", ""), 3000),
                    "source_uri": s3_location.get("uri", ""),
                }
            )

        return {
            "success": True,
            "query": str(query).strip(),
            "knowledge_base_id": BEDROCK_KNOWLEDGE_BASE_ID,
            "min_score": bounded_score,
            "results": results,
            "source": "Bedrock Knowledge Base internal docs",
        }

    except Exception as e:
        logger.error(f"Error searching internal runbooks: {e}")
        return {"success": False, "error": str(e), "error_type": type(e).__name__}


@mcp.tool()
def search_aws_docs(
    query: str,
    topics: Optional[List[str]] = None,
    max_results: int = 10
) -> dict:
    """
    Search AWS documentation using the official AWS Knowledge MCP API.

    Per AWS docs: "Real-time access to AWS documentation, API references,
    troubleshooting guidelines, and architectural guidance"

    Sources searched: AWS docs, API references, What's New posts, Builder Center,
    Blog posts, Architectural references, Well-Architected guidance, Amplify docs,
    CDK/CloudFormation docs.

    Args:
        query: Search term (e.g., "S3 bucket policy", "Lambda cold start optimization")
        topics: Optional topic filter list. Available topics:
            reference_documentation, current_awareness, troubleshooting,
            amplify_docs, cdk_docs, cdk_constructs, cloudformation, general
        max_results: Maximum results (default: 10)

    Returns:
        Dict with ranked search results including url, title, context, relevance score
    """
    try:
        logger.info(f"= search_aws_docs: query={query!r}, topics={topics}")

        payload = {
            "search_phrase": query,
            "limit": max_results
        }
        if topics:
            payload["topics"] = topics

        result = _call_knowledge_api("aws___search_documentation", payload, "search-1")

        return {
            'success': True,
            'query': query,
            'topics': topics or ['general'],
            'results': result.get('result', result),
            'source': 'AWS Knowledge MCP (knowledge-mcp.global.api.aws)'
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error: {e.response.status_code}")
        return {'success': False, 'error': f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        logger.error(f"Error searching AWS docs: {e}")
        return {'success': False, 'error': str(e), 'error_type': type(e).__name__}


@mcp.tool()
def read_aws_doc(url: str, max_length: int = 10000) -> dict:
    """
    Read a full AWS documentation page and convert to markdown.

    Per AWS docs: "Retrieve and convert AWS documentation pages to markdown"

    Args:
        url: AWS documentation URL (must be from docs.aws.amazon.com, aws.amazon.com, etc.)
        max_length: Maximum characters to return (default: 10000)

    Returns:
        Dict with page content in markdown format
    """
    try:
        logger.info(f"= read_aws_doc: {url}")

        result = _call_knowledge_api(
            "aws___read_documentation",
            {"url": url, "max_length": max_length},
            "read-1"
        )

        return {
            'success': True,
            'url': url,
            'content': result.get('result', result),
            'source': 'AWS Knowledge MCP'
        }

    except Exception as e:
        logger.error(f"Error reading doc: {e}")
        return {'success': False, 'error': str(e), 'error_type': type(e).__name__}


@mcp.tool()
def get_regional_availability(
    resource_type: str,
    region: str,
    filters: Optional[List[str]] = None
) -> dict:
    """
    Check AWS service/feature availability in a specific region.

    Per AWS docs: "Regional availability information for AWS APIs and CloudFormation resources"

    Args:
        resource_type: 'product' for AWS products, 'api' for API operations, 'cfn' for CloudFormation
        region: AWS region code (e.g., 'us-east-1')
        filters: Optional list of specific resources (e.g., ['AWS Lambda', 'Amazon S3'])

    Returns:
        Dict with availability status per resource
    """
    try:
        payload = {"resource_type": resource_type, "region": region}
        if filters:
            payload["filters"] = filters

        result = _call_knowledge_api("aws___get_regional_availability", payload, "avail-1")
        return {'success': True, 'region': region, 'results': result.get('result', result)}

    except Exception as e:
        return {'success': False, 'error': str(e), 'error_type': type(e).__name__}


@mcp.tool()
def list_aws_regions() -> dict:
    """
    List all AWS regions with their identifiers and names.

    Per AWS docs: "Retrieve a list of all AWS regions, including their identifiers and names"

    Returns:
        Dict with list of regions (region_id, region_long_name)
    """
    try:
        result = _call_knowledge_api("aws___list_regions", {}, "regions-1")
        return {'success': True, 'results': result.get('result', result)}

    except Exception as e:
        return {'success': False, 'error': str(e), 'error_type': type(e).__name__}


@mcp.tool()
def get_recommendations(url: str) -> dict:
    """
    Get content recommendations for an AWS documentation page.

    Per AWS docs: "Get content recommendations for AWS documentation pages"
    Returns: Highly Rated, New, Similar, and Journey recommendations.

    Args:
        url: AWS documentation page URL (must be from docs.aws.amazon.com)

    Returns:
        Dict with recommendation categories
    """
    try:
        result = _call_knowledge_api("aws___recommend", {"url": url}, "recommend-1")
        return {'success': True, 'url': url, 'results': result.get('result', result)}

    except Exception as e:
        return {'success': False, 'error': str(e), 'error_type': type(e).__name__}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
