"""Wrap MCP tools to auto-inject account_id/region from A2A message metadata.

Instead of wrapping MCPAgentTool (which breaks Strands Agent tool registry
isinstance checks), we monkey-patch each tool's stream() method in-place.
The tool remains a valid MCPAgentTool instance throughout.

Ref: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-agent-integration.html
     Official pattern: tools = mcp_client.list_tools_sync(); Agent(tools=tools)

Usage in specialist runtimes:
    from context_tools import create_context_agent, create_a2a_server
"""
import json
import logging
import os
from strands import Agent
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

logger = logging.getLogger(__name__)

MODEL = os.getenv('MODEL_ID', os.getenv('MODEL', 'us.amazon.nova-pro-v1:0'))
MAX_TOKENS = int(os.getenv('MAX_TOKENS', '4096'))
BEDROCK_STREAMING = os.getenv('BEDROCK_STREAMING', 'false').lower() in ('1', 'true', 'yes', 'on')
CLOUDWATCH_TOOL_ALLOWLIST = {
    name.strip()
    for name in os.getenv('CLOUDWATCH_TOOL_ALLOWLIST', '').split(',')
    if name.strip()
}
NOVA_TOOL_ADDITIONAL_REQUEST_FIELDS = {"inferenceConfig": {"topK": 1}}
_NOVA_TOP_LEVEL_SCHEMA_KEYS = {"type", "properties", "required"}
_NOVA_PROPERTY_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required"}
_JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
# Module-level context — set before each agent invocation
_current_ctx = {"account_name": "", "region": "us-east-1"}


def set_context(account_name: str, region: str = "us-east-1"):
    """Set current account context (called before agent invocation)."""
    global _current_ctx
    _current_ctx = {"account_name": account_name, "region": region}
    logger.info(f"Context set: account_name={account_name!r}, region={region}")
    logger.info(f"   This context will be injected into all MCP tool calls")


def _inject_context_into_tools(tools):
    """Monkey-patch each MCPAgentTool's stream() to auto-inject account_id/region.

    The tool object stays the same MCPAgentTool instance (passes isinstance checks).
    Only the stream method is replaced with one that injects context before delegating.
    """
    patched_count = 0
    for tool in tools:
        props = tool.tool_spec.get('inputSchema', {}).get('json', {}).get('properties', {})
        if 'account_name' not in props and 'region' not in props:
            continue  # Tool doesn't accept context params, skip

        patched_count += 1
        original_stream = tool.stream
        accepts_account = 'account_name' in props
        accepts_region = 'region' in props

        def _make_patched(orig, acc, reg):
            def patched_stream(tool_use, invocation_state=None, **kwargs):
                tool_input = tool_use.get("input", {})
                tool_name = tool_use.get("name", "unknown")
                
                # Log before injection
                logger.info(f"Tool call intercepted: {tool_name}")
                logger.info(f"   Original input: {json.dumps(tool_input, default=str)}")
                
                if acc and "account_name" not in tool_input:
                    tool_input["account_name"] = _current_ctx["account_name"]
                    logger.info(f"  Injected account_name={_current_ctx['account_name']!r}")
                if reg and "region" not in tool_input:
                    tool_input["region"] = _current_ctx["region"]
                    logger.info(f"  Injected region={_current_ctx['region']}")
                
                tool_use["input"] = tool_input
                logger.info(f"   Final input: {json.dumps(tool_input, default=str)}")
                
                return orig(tool_use, invocation_state, **kwargs)
            return patched_stream

        tool.stream = _make_patched(original_stream, accepts_account, accepts_region)

    logger.info(f"Patched {patched_count}/{len(tools)} tools with context injection")


def _filter_tools_for_nova(tools):
    """Optionally expose a smaller MCP tool set to Nova for isolation tests."""
    if not CLOUDWATCH_TOOL_ALLOWLIST:
        return tools

    filtered = [
        tool for tool in tools
        if (getattr(tool, "tool_spec", {}) or {}).get("name") in CLOUDWATCH_TOOL_ALLOWLIST
    ]
    logger.info(
        f"Filtered CloudWatch tools for Nova: {len(filtered)}/{len(tools)} retained, "
        f"allowlist={sorted(CLOUDWATCH_TOOL_ALLOWLIST)}"
    )
    if not filtered:
        logger.warning("CloudWatch tool allowlist matched no tools; using full tool set")
        return tools
    return filtered


def _flatten_schema_variant(schema):
    """Flatten composition/nullability into a single schema Nova can reliably use."""
    if not isinstance(schema, dict):
        return {"type": "string"}

    for keyword in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(keyword)
        if not isinstance(variants, list) or not variants:
            continue
        non_null = [
            variant for variant in variants
            if not (isinstance(variant, dict) and variant.get("type") == "null")
        ]
        if non_null:
            selected = dict(non_null[0])
            for key in ("description", "default"):
                if key in schema and key not in selected:
                    selected[key] = schema[key]
            return selected

    return schema


def _normalize_schema_type(schema):
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), None)
    if schema_type in _JSON_SCHEMA_TYPES:
        return schema_type
    if isinstance(schema.get("properties"), dict):
        return "object"
    if "items" in schema:
        return "array"
    return "string"


def _sanitize_property_schema_for_nova(schema):
    """Return a conservative JSON schema subset for nested Nova tool properties."""
    schema = _flatten_schema_variant(schema)
    if not isinstance(schema, dict):
        return {"type": "string"}

    schema_type = _normalize_schema_type(schema)
    clean = {"type": schema_type}

    description = schema.get("description")
    if isinstance(description, str) and description.strip():
        clean["description"] = description.strip()[:1000]

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        primitive_enum = [
            value for value in enum
            if isinstance(value, (str, int, float, bool))
        ]
        if primitive_enum:
            clean["enum"] = primitive_enum[:100]

    if schema_type == "array":
        clean["items"] = _sanitize_property_schema_for_nova(schema.get("items", {"type": "string"}))
    elif schema_type == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            clean_properties = {
                key: _sanitize_property_schema_for_nova(value)
                for key, value in properties.items()
                if isinstance(key, str)
            }
            if clean_properties:
                clean["properties"] = clean_properties
                required = schema.get("required")
                if isinstance(required, list):
                    clean_required = [
                        item for item in required
                        if isinstance(item, str) and item in clean_properties
                    ]
                    if clean_required:
                        clean["required"] = clean_required

    return {key: value for key, value in clean.items() if key in _NOVA_PROPERTY_SCHEMA_KEYS}


def _sanitize_tool_specs_for_nova(tools):
    """Mutate Strands tool specs to a Nova-friendly JSON schema subset."""
    sanitized_count = 0
    for tool in tools:
        spec = getattr(tool, "tool_spec", None)
        if not isinstance(spec, dict):
            continue
        input_schema = spec.get("inputSchema", {}).get("json")
        if not isinstance(input_schema, dict):
            continue

        properties = input_schema.get("properties", {})
        clean_properties = {}
        if isinstance(properties, dict):
            clean_properties = {
                key: _sanitize_property_schema_for_nova(value)
                for key, value in properties.items()
                if isinstance(key, str)
            }

        clean_schema = {
            "type": "object",
            "properties": clean_properties,
        }

        required = input_schema.get("required")
        if isinstance(required, list):
            clean_required = [
                item for item in required
                if isinstance(item, str) and item in clean_properties
            ]
            if clean_required:
                clean_schema["required"] = clean_required

        spec.setdefault("inputSchema", {})["json"] = clean_schema
        sanitized_count += 1

    logger.info(f"Sanitized {sanitized_count}/{len(tools)} tool schemas for Nova")


def _log_tool_specs_for_nova(tools):
    """Log safe tool schema metadata before handing MCP tools to Nova."""
    logger.info(f"Loaded {len(tools)} MCP tools for Nova tool use")
    for tool in tools:
        spec = getattr(tool, "tool_spec", {}) or {}
        name = spec.get("name", "unknown")
        schema = spec.get("inputSchema", {}).get("json", {})
        if not isinstance(schema, dict):
            logger.warning(f"Tool schema check: {name} has non-dict schema type={type(schema).__name__}")
            continue

        top_keys = sorted(schema.keys())
        unsupported = sorted(k for k in top_keys if k not in _NOVA_TOP_LEVEL_SCHEMA_KEYS)
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        property_count = len(properties) if isinstance(properties, dict) else 0
        required_count = len(required) if isinstance(required, list) else 0
        message = (
            f"Tool schema check: name={name}, type={schema.get('type')!r}, "
            f"properties={property_count}, required={required_count}, "
            f"top_keys={top_keys}, unsupported_top_keys={unsupported}"
        )
        if unsupported or schema.get("type") != "object":
            logger.warning(message)
        else:
            logger.info(message)


def _extract_metadata_prompt(original_prompt: str) -> str:
    """Extract metadata JSON prefix from prompt if present, set context, return clean prompt."""
    try:
        if original_prompt.startswith('{"__metadata__":'):
            nl_pos = original_prompt.find('\n')
            if nl_pos == -1:
                logger.warning("Metadata prefix found but no newline delimiter, using prompt as-is")
                return original_prompt
            meta_line = original_prompt[:nl_pos]
            meta = json.loads(meta_line).get("__metadata__", {})

            logger.info("Extracted metadata from prompt")
            logger.info(f"   Raw metadata: {meta_line}")
            logger.info(f"   account_name: {meta.get('account_name', 'NOT SET')!r}")
            logger.info(f"   region: {meta.get('region', 'us-east-1')}")

            set_context(meta.get("account_name", ""), meta.get("region", "us-east-1"))
            clean_prompt = original_prompt[nl_pos + 1:]
            logger.info(f"   Clean prompt (first 100 chars): {clean_prompt[:100]}...")
            return clean_prompt
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to parse metadata prefix: {e}, using original prompt")
        return original_prompt

    logger.info("No metadata prefix found in prompt, using defaults")
    return original_prompt


def create_context_agent(name, description, system_prompt, mcp_client, max_tokens=None):
    """Create an Agent with context-injected MCP tools.

    Follows the official Strands + Gateway pattern:
      tools = mcp_client.list_tools_sync()
      Agent(tools=tools)
    but patches stream() on each tool to inject account_id/region.

    Args:
        name: Agent name
        description: Agent description
        system_prompt: System prompt for the agent
        mcp_client: Connected MCPClient instance
        max_tokens: Maximum output tokens. Defaults to MAX_TOKENS env var (default 4096).
    """
    tools = mcp_client.list_tools_sync()
    tools = _filter_tools_for_nova(tools)
    _inject_context_into_tools(tools)
    _sanitize_tool_specs_for_nova(tools)
    _log_tool_specs_for_nova(tools)
    logger.info(
        f"Bedrock model configured: model={MODEL}, streaming={BEDROCK_STREAMING}, "
        f"temperature=0, topK=1, max_tokens={max_tokens or MAX_TOKENS}"
    )

    return Agent(
        name=name,
        description=description,
        model=BedrockModel(
            model_id=MODEL,
            max_tokens=max_tokens or MAX_TOKENS,
            streaming=BEDROCK_STREAMING,
            temperature=0,
            additional_request_fields=NOVA_TOOL_ADDITIONAL_REQUEST_FIELDS,
        ),
        tools=tools,
        system_prompt=system_prompt,
        callback_handler=None,
    )


def create_a2a_server(agent, runtime_url):
    """Create A2AServer with metadata extraction hook."""
    original_stream = agent.stream_async

    async def patched_stream(content_blocks, **kwargs):
        if content_blocks:
            block = content_blocks[0]
            if hasattr(block, 'text'):
                block.text = _extract_metadata_prompt(block.text)
            elif isinstance(block, dict) and 'text' in block:
                block['text'] = _extract_metadata_prompt(block['text'])
        async for event in original_stream(content_blocks, **kwargs):
            yield event

    agent.stream_async = patched_stream
    return A2AServer(agent=agent, http_url=runtime_url, serve_at_root=True)
