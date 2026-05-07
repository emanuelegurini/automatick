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
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

logger = logging.getLogger(__name__)

MODEL = os.getenv('MODEL_ID', os.getenv('MODEL', 'amazon.nova-pro-v1:0'))
MAX_TOKENS = int(os.getenv('MAX_TOKENS', '4096'))
BEDROCK_STREAMING = os.getenv('BEDROCK_STREAMING', 'false').lower() in ('1', 'true', 'yes', 'on')
CLOUDWATCH_USE_WRAPPER_TOOLS = os.getenv(
    'CLOUDWATCH_USE_WRAPPER_TOOLS', 'true'
).lower() in ('1', 'true', 'yes', 'on')
INCIDENT_HISTORY_TABLE = os.getenv("CHAT_REQUESTS_TABLE", "msp-assistant-chat-requests")
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


def _tool_result_to_text(result):
    """Extract text content from a Strands ToolResult-like object."""
    status = result.get("status") if isinstance(result, dict) else getattr(result, "status", "success")
    content = result.get("content", []) if isinstance(result, dict) else getattr(result, "content", [])
    parts = []
    for item in content or []:
        if isinstance(item, dict) and "text" in item:
            parts.append(str(item["text"]))
        elif hasattr(item, "text"):
            parts.append(str(item.text))
        else:
            parts.append(str(item))

    text = "\n".join(part for part in parts if part).strip()
    if not text:
        text = json.dumps(result, default=str)
    if status == "error":
        return f"MCP tool error: {text}"
    return text


def _clamp_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_region():
    return _current_ctx.get("region") or "us-east-1"


def _base_mcp_arguments():
    return {
        "account_name": _current_ctx.get("account_name", ""),
        "region": _safe_region(),
    }


def _call_mcp_tool(mcp_client, tool_name, arguments):
    logger.info(
        "Wrapper invoking MCP tool %s with arguments=%s",
        tool_name,
        json.dumps(arguments, default=str),
    )
    result = mcp_client.call_tool_sync(
        tool_use_id=f"cw-wrapper-{uuid.uuid4().hex}",
        name=tool_name,
        arguments=arguments,
    )
    return _tool_result_to_text(result)


def _call_aws_cli(mcp_client, aws_api_tool, cli_command):
    if not aws_api_tool:
        return "MCP tool error: aws-api-mcp___call_aws is not available for CloudWatch evidence lookup."
    arguments = {
        **_base_mcp_arguments(),
        "cli_command": cli_command,
    }
    return _call_mcp_tool(mcp_client, aws_api_tool, arguments)


def _limit_text(value, max_chars=8000):
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"


def _json_dumps(value):
    return json.dumps(value, indent=2, default=str)


def _dynamodb_attr_to_python(value):
    """Convert DynamoDB low-level AttributeValue JSON into plain Python values."""
    if not isinstance(value, dict) or len(value) != 1:
        return value

    attr_type, attr_value = next(iter(value.items()))
    if attr_type == "S":
        return attr_value
    if attr_type == "N":
        number = str(attr_value)
        try:
            return int(number) if "." not in number else float(number)
        except ValueError:
            return number
    if attr_type == "BOOL":
        return bool(attr_value)
    if attr_type == "NULL":
        return None
    if attr_type == "M":
        return {
            key: _dynamodb_attr_to_python(nested_value)
            for key, nested_value in (attr_value or {}).items()
        }
    if attr_type == "L":
        return [_dynamodb_attr_to_python(item) for item in (attr_value or [])]
    if attr_type in {"SS", "NS"}:
        return list(attr_value or [])
    return attr_value


def _normalize_dynamodb_item(item):
    if not isinstance(item, dict):
        return {}
    if any(key in {"S", "N", "M", "L", "BOOL", "NULL"} for value in item.values() if isinstance(value, dict) for key in value):
        return {key: _dynamodb_attr_to_python(value) for key, value in item.items()}
    return item


def _text_blob(*values):
    return "\n".join(str(value or "") for value in values if value is not None).lower()


def _epoch_to_iso(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _history_keywords(keywords):
    if isinstance(keywords, str):
        raw = keywords.replace(",", "\n").splitlines()
    else:
        raw = keywords or []
    normalized = []
    for item in raw:
        value = str(item or "").strip().lower()
        if len(value) >= 3 and value not in normalized:
            normalized.append(value)
    return normalized[:10]


def _score_history_item(
    item,
    alarm_name="",
    resource_id="",
    namespace="",
    metric_name="",
    keywords="",
    customer_account_name="",
    region="",
):
    incident = item.get("incident") if isinstance(item.get("incident"), dict) else item
    investigation = item.get("investigation") if isinstance(item.get("investigation"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    if not investigation and isinstance(result.get("investigation"), dict):
        investigation = result["investigation"]

    subject = incident.get("subject") or item.get("subject") or ""
    description = incident.get("description") or item.get("description") or ""
    evidence = investigation.get("evidence") or ""
    root_cause = investigation.get("root_cause_hypothesis") or ""
    proposed_action = (
        investigation.get("proposed_action")
        or investigation.get("proposed_fix")
        or item.get("proposed_action")
        or ""
    )
    searchable = _text_blob(subject, description, evidence, root_cause, proposed_action)

    matched_on = []
    score = 0
    normalized_alarm = str(alarm_name or "").strip().lower()
    normalized_resource = str(resource_id or "").strip().lower()
    normalized_namespace = str(namespace or "").strip().lower()
    normalized_metric = str(metric_name or "").strip().lower()
    normalized_account = str(customer_account_name or "").strip().lower()
    normalized_region = str(region or "").strip().lower()

    item_resource = str(incident.get("resource_id") or item.get("resource_id") or "").strip().lower()
    item_account = str(incident.get("account_name") or item.get("account_name") or "").strip().lower()
    item_region = str(incident.get("region") or item.get("region") or "").strip().lower()

    if normalized_resource and normalized_resource == item_resource:
        score += 5
        matched_on.append("resource_id")
    elif normalized_resource and normalized_resource in searchable:
        score += 3
        matched_on.append("resource_id_text")

    if normalized_alarm and normalized_alarm in searchable:
        score += 4
        matched_on.append("alarm_name")

    if normalized_metric and normalized_metric in searchable:
        score += 2
        matched_on.append("metric_name")
    if normalized_namespace and normalized_namespace in searchable:
        score += 1
        matched_on.append("namespace")

    if normalized_account and normalized_account == item_account:
        score += 1
        matched_on.append("account_name")
    if normalized_region and normalized_region == item_region:
        score += 1
        matched_on.append("region")

    keyword_hits = []
    for keyword in _history_keywords(keywords):
        if keyword in searchable:
            keyword_hits.append(keyword)
    if keyword_hits:
        score += min(len(keyword_hits), 4)
        matched_on.append("keywords:" + ",".join(keyword_hits[:4]))

    return score, matched_on


def _summarize_incident_history(
    raw_text,
    alarm_name="",
    resource_id="",
    namespace="",
    metric_name="",
    keywords="",
    current_ticket_id="",
    customer_account_name="",
    region="",
    lookback_days=30,
):
    payload = _extract_aws_api_json(raw_text)
    if not payload:
        return _limit_text(raw_text)

    raw_items = payload.get("Items") or []
    items = [_normalize_dynamodb_item(item) for item in raw_items if isinstance(item, dict)]
    current_ticket = str(current_ticket_id or "").strip()
    now = datetime.now(timezone.utc)
    matches = []
    for item in items:
        incident = item.get("incident") if isinstance(item.get("incident"), dict) else item
        ticket_id = str(incident.get("ticket_id") or item.get("ticket_id") or "").strip()
        if current_ticket and ticket_id == current_ticket:
            continue

        score, matched_on = _score_history_item(
            item,
            alarm_name=alarm_name,
            resource_id=resource_id,
            namespace=namespace,
            metric_name=metric_name,
            keywords=keywords,
            customer_account_name=customer_account_name,
            region=region,
        )
        if score <= 0:
            continue

        investigation = item.get("investigation") if isinstance(item.get("investigation"), dict) else {}
        remediation = item.get("remediation") if isinstance(item.get("remediation"), dict) else {}
        created_at = item.get("created_at") or incident.get("created_at")
        created_iso = _epoch_to_iso(created_at)
        age_days = None
        try:
            age_days = (now - datetime.fromtimestamp(int(created_at), tz=timezone.utc)).days
        except (TypeError, ValueError, OSError):
            pass

        matches.append(
            {
                "ticket_id": ticket_id,
                "request_id": item.get("request_id"),
                "created_at": created_iso,
                "age_days": age_days,
                "status": item.get("status"),
                "subject": _limit_text(incident.get("subject") or item.get("subject"), 180),
                "resource_id": incident.get("resource_id") or item.get("resource_id"),
                "account_name": incident.get("account_name") or item.get("account_name"),
                "region": incident.get("region") or item.get("region"),
                "score": score,
                "matched_on": matched_on,
                "root_cause": _limit_text(
                    investigation.get("root_cause_hypothesis") or item.get("root_cause_hypothesis"),
                    220,
                ),
                "proposed_action": _limit_text(
                    investigation.get("proposed_action")
                    or investigation.get("proposed_fix")
                    or item.get("proposed_action"),
                    220,
                ),
                "remediation_id": remediation.get("remediation_id") or item.get("remediation_id"),
                "remediation_status": remediation.get("status") or item.get("remediation_status"),
            }
        )

    matches.sort(key=lambda item: (item["score"], item.get("created_at") or ""), reverse=True)
    matched_created = [match for match in matches if isinstance(match.get("age_days"), int)]
    last_7_days = sum(1 for match in matched_created if match["age_days"] <= 7)
    last_30_days = sum(1 for match in matched_created if match["age_days"] <= 30)
    if len(matches) >= 3 or last_7_days >= 2:
        classification = "frequent_recurring"
    elif len(matches) >= 2:
        classification = "recurring"
    elif len(matches) == 1:
        classification = "sporadic_or_rare"
    else:
        classification = "no_prior_history_found"

    return _json_dumps(
        {
            "incident_history": {
                "classification": classification,
                "lookback_days": lookback_days,
                "table": INCIDENT_HISTORY_TABLE,
                "scanned_items": payload.get("ScannedCount"),
                "returned_items": len(items),
                "matches_found": len(matches),
                "frequency": {
                    "last_7_days": last_7_days,
                    "last_30_days": last_30_days,
                    "oldest_match": matches[-1]["created_at"] if matches else None,
                    "newest_match": matches[0]["created_at"] if matches else None,
                },
                "query": {
                    "alarm_name": alarm_name,
                    "resource_id": resource_id,
                    "namespace": namespace,
                    "metric_name": metric_name,
                    "keywords": _history_keywords(keywords),
                    "current_ticket_id": current_ticket_id,
                    "customer_account_name": customer_account_name,
                    "region": region,
                },
                "matches": matches[:10],
                "caveats": [
                    "History is based on incident_history rows in the shared DynamoDB table.",
                    "A historical match is evidence of recurrence, not proof of identical root cause.",
                ],
            }
        }
    )


def _summarize_aws_docs(raw_text):
    payload = _extract_aws_api_json(raw_text)
    if not payload:
        try:
            payload = json.loads(raw_text)
        except (TypeError, json.JSONDecodeError):
            return _limit_text(raw_text)

    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, dict):
        results = results.get("content") or results.get("results") or results.get("documents")
    if not isinstance(results, list):
        results = payload.get("content") if isinstance(payload, dict) else []
    compact = []
    for result in (results or [])[:5]:
        if not isinstance(result, dict):
            compact.append({"text": _limit_text(result, 400)})
            continue
        compact.append(
            {
                "title": result.get("title") or result.get("page_title"),
                "url": result.get("url"),
                "context": _limit_text(result.get("context") or result.get("excerpt") or result.get("text"), 500),
            }
        )
    return _json_dumps({"aws_documentation": compact, "shown": len(compact)})


def _extract_aws_api_json(raw_text):
    """Extract the nested AWS CLI JSON payload from aws-api-mcp output."""
    try:
        parsed = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError):
        return None

    response = parsed.get("response") if isinstance(parsed, dict) else None
    if isinstance(response, dict):
        nested_json = response.get("json")
        if isinstance(nested_json, str) and nested_json.strip():
            try:
                return json.loads(nested_json)
            except json.JSONDecodeError:
                return None
        return response

    return parsed if isinstance(parsed, dict) else None


def _extract_state_reason_data(alarm):
    data = alarm.get("StateReasonData")
    if not isinstance(data, str) or not data.strip():
        return {}
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _datapoint_stats(datapoints):
    values = [
        coerced
        for datapoint in datapoints or []
        if isinstance(datapoint, dict)
        for coerced in [_coerce_float(datapoint.get("value"))]
        if coerced is not None
    ]
    if not values:
        return {}

    return {
        "count": len(values),
        "latest": values[0],
        "minimum": min(values),
        "maximum": max(values),
        "average": round(sum(values) / len(values), 4),
    }


def _first_present_value(mapping, keys):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _interpret_alarm(alarm, recent_datapoints):
    alarm_name = str(alarm.get("AlarmName") or "")
    namespace = str(alarm.get("Namespace") or "")
    metric_name = str(alarm.get("MetricName") or "")
    comparison = str(alarm.get("ComparisonOperator") or "")
    threshold = alarm.get("Threshold")
    stats = _datapoint_stats(recent_datapoints)

    is_ecs_target_tracking_low = (
        namespace == "AWS/ECS"
        and metric_name == "CPUUtilization"
        and "TargetTracking-" in alarm_name
        and "-AlarmLow-" in alarm_name
        and comparison == "LessThanThreshold"
    )
    if is_ecs_target_tracking_low:
        return {
            "classification": "ecs_target_tracking_low_utilization",
            "recommended_framing": (
                "Treat this as an ECS target tracking scale-in/low-utilization signal, "
                "not as an application outage by itself."
            ),
            "evidence_hint": (
                f"Recent CPU datapoints are below the target threshold {threshold}."
                if threshold is not None
                else "Recent CPU datapoints are below the target tracking lower bound."
            ),
            "metric_stats": stats,
        }

    return {
        "classification": "cloudwatch_alarm",
        "recommended_framing": "Investigate the alarm against metric trend, resource health, and logs.",
        "metric_stats": stats,
    }


def _summarize_alarm_details(raw_text):
    payload = _extract_aws_api_json(raw_text)
    if not payload:
        return _limit_text(raw_text)

    alarms = payload.get("MetricAlarms") or []
    if not alarms:
        return _json_dumps({"metric_alarms": [], "message": "No matching CloudWatch alarm found."})

    alarm = alarms[0]
    reason_data = _extract_state_reason_data(alarm)
    evaluated = reason_data.get("evaluatedDatapoints") or []
    recent_datapoints = [
        {
            "timestamp": point.get("timestamp"),
            "value": point.get("value"),
            "sample_count": point.get("sampleCount"),
        }
        for point in evaluated[:10]
        if isinstance(point, dict)
    ]
    if not recent_datapoints:
        values = reason_data.get("recentDatapoints") or []
        recent_datapoints = [{"value": value} for value in values[:10]]

    summary = {
        "alarm": {
            "alarm_name": alarm.get("AlarmName"),
            "state": alarm.get("StateValue"),
            "state_updated_timestamp": alarm.get("StateUpdatedTimestamp"),
            "state_reason": _limit_text(alarm.get("StateReason"), 900),
            "namespace": alarm.get("Namespace"),
            "metric_name": alarm.get("MetricName"),
            "dimensions": alarm.get("Dimensions", [])[:10],
            "threshold": alarm.get("Threshold"),
            "comparison_operator": alarm.get("ComparisonOperator"),
            "statistic": alarm.get("Statistic") or alarm.get("ExtendedStatistic"),
            "period": alarm.get("Period"),
            "evaluation_periods": alarm.get("EvaluationPeriods"),
            "datapoints_to_alarm": alarm.get("DatapointsToAlarm"),
            "recent_datapoints": recent_datapoints,
        }
    }
    summary["alarm_interpretation"] = _interpret_alarm(alarm, recent_datapoints)
    return _json_dumps(summary)


def _summarize_metric_history(raw_text, statistic):
    payload = _extract_aws_api_json(raw_text)
    if not payload:
        return _limit_text(raw_text)

    datapoints = payload.get("Datapoints") or []
    sorted_points = sorted(
        [point for point in datapoints if isinstance(point, dict)],
        key=lambda point: str(point.get("Timestamp", "")),
        reverse=True,
    )
    compact_points = []
    for point in sorted_points[:20]:
        value = _first_present_value(
            point,
            [statistic, "Average", "Maximum", "Minimum", "Sum", "SampleCount"],
        )
        compact_points.append(
            {
                "timestamp": point.get("Timestamp"),
                "value": value,
                "unit": point.get("Unit"),
            }
        )

    return _json_dumps(
        {
            "metric_history": {
                "datapoint_count": len(datapoints),
                "shown_datapoints": len(compact_points),
                "statistic": statistic,
                "stats": _datapoint_stats(compact_points),
                "datapoints": compact_points,
            }
        }
    )


def _summarize_log_groups(raw_text):
    payload = _extract_aws_api_json(raw_text)
    if not payload:
        return _limit_text(raw_text)

    groups = payload.get("logGroups") or []
    return _json_dumps(
        {
            "log_groups": [
                {
                    "log_group_name": group.get("logGroupName"),
                    "retention_in_days": group.get("retentionInDays"),
                    "stored_bytes": group.get("storedBytes"),
                }
                for group in groups[:20]
                if isinstance(group, dict)
            ],
            "shown": min(len(groups), 20),
            "has_more": len(groups) > 20,
        }
    )


def _summarize_log_events(raw_text):
    payload = _extract_aws_api_json(raw_text)
    if not payload:
        return _limit_text(raw_text)

    events = payload.get("events") or []
    return _json_dumps(
        {
            "log_events": [
                {
                    "timestamp": event.get("timestamp"),
                    "log_stream_name": event.get("logStreamName"),
                    "message": _limit_text(event.get("message"), 500),
                }
                for event in events[:20]
                if isinstance(event, dict)
            ],
            "shown": min(len(events), 20),
            "has_more": len(events) > 20,
        }
    )


def _quote_cli_value(value):
    return shlex.quote(str(value or ""))


def _parse_dimensions(dimensions_json):
    if not dimensions_json:
        return []
    try:
        parsed = json.loads(dimensions_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"dimensions_json must be a JSON array of objects: {exc}") from exc

    if not isinstance(parsed, list):
        raise ValueError("dimensions_json must be a JSON array")

    dimensions = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("Each metric dimension must be an object")
        name = str(item.get("Name") or item.get("name") or "").strip()
        value = str(item.get("Value") or item.get("value") or "").strip()
        if not name or not value:
            raise ValueError("Each metric dimension must include Name and Value")
        dimensions.append({"Name": name, "Value": value})
    return dimensions[:10]


def _metric_dimensions_cli(dimensions_json):
    dimensions = _parse_dimensions(dimensions_json)
    if not dimensions:
        return ""
    parts = [
        _quote_cli_value(f"Name={dimension['Name']},Value={dimension['Value']}")
        for dimension in dimensions
    ]
    return " --dimensions " + " ".join(parts)


def _utc_window(minutes):
    bounded_minutes = _clamp_int(minutes, default=60, minimum=5, maximum=1440)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=bounded_minutes)
    return start.isoformat(), end.isoformat(), bounded_minutes


def _create_nova_wrapper_tools(mcp_client, mcp_tools):
    """Expose simple Strands-native tools to Nova and call MCP behind the scenes."""
    tool_names = {
        name for mcp_tool in mcp_tools
        if isinstance((name := (getattr(mcp_tool, "tool_spec", {}) or {}).get("name")), str)
    }
    active_alarms_tool = "cloudwatch-mcp___get_active_alarms"
    aws_api_tool = "aws-api-mcp___call_aws" if "aws-api-mcp___call_aws" in tool_names else ""
    aws_docs_tool = "aws-knowledge-mcp___search_aws_docs" if "aws-knowledge-mcp___search_aws_docs" in tool_names else ""
    if active_alarms_tool not in tool_names and not aws_api_tool:
        logger.warning(
            "Nova wrapper requires either %r or %r, but available MCP tools are %s",
            active_alarms_tool,
            "aws-api-mcp___call_aws",
            sorted(tool_names),
        )
        return []

    @tool(
        name="get_active_alarms",
        description=(
            "Read-only CloudWatch tool. Lists active CloudWatch alarms in ALARM "
            "or INSUFFICIENT_DATA state for the current account and region."
        ),
    )
    def get_active_alarms(state_filter: str = "ALARM,INSUFFICIENT_DATA") -> str:
        """List active CloudWatch alarms.

        Args:
            state_filter: Requested alarm states. Defaults to ALARM,INSUFFICIENT_DATA.
        """
        logger.info("Wrapper tool call intercepted: get_active_alarms")
        if active_alarms_tool in tool_names:
            return _call_mcp_tool(mcp_client, active_alarms_tool, _base_mcp_arguments())

        requested_states = [
            state.strip().upper()
            for state in str(state_filter or "ALARM,INSUFFICIENT_DATA").split(",")
            if state.strip()
        ]
        states = [state for state in requested_states if state in {"ALARM", "INSUFFICIENT_DATA", "OK"}]
        states = states or ["ALARM", "INSUFFICIENT_DATA"]
        sections = []
        for state in states[:3]:
            command = (
                "aws cloudwatch describe-alarms "
                f"--state-value {_quote_cli_value(state)} "
                "--max-records 20 --output json"
            )
            sections.append(f"## {state}\n{_call_aws_cli(mcp_client, aws_api_tool, command)}")
        return "\n\n".join(sections)

    @tool(
        name="get_alarm_details",
        description=(
            "Read-only CloudWatch tool. Fetches full configuration and current state "
            "for one CloudWatch alarm by name."
        ),
    )
    def get_alarm_details(alarm_name: str) -> str:
        """Get one alarm's details.

        Args:
            alarm_name: Exact CloudWatch alarm name.
        """
        logger.info("Wrapper tool call intercepted: get_alarm_details")
        if not str(alarm_name or "").strip():
            return "MCP tool error: alarm_name is required."
        command = (
            "aws cloudwatch describe-alarms "
            f"--alarm-names {_quote_cli_value(alarm_name)} "
            "--output json"
        )
        raw_result = _call_aws_cli(mcp_client, aws_api_tool, command)
        return _summarize_alarm_details(raw_result)

    def _get_metric_history_result(
        namespace: str,
        metric_name: str,
        dimensions_json: str = "[]",
        minutes: int = 60,
        period_seconds: int = 60,
        statistic: str = "Average",
    ) -> str:
        if not str(namespace or "").strip() or not str(metric_name or "").strip():
            return "MCP tool error: namespace and metric_name are required."

        allowed_statistics = {"Average", "Maximum", "Minimum", "Sum", "SampleCount"}
        selected_statistic = str(statistic or "Average").strip()
        if selected_statistic not in allowed_statistics:
            selected_statistic = "Average"

        try:
            dimensions_clause = _metric_dimensions_cli(dimensions_json)
        except ValueError as exc:
            logger.warning("Ignoring invalid metric dimensions_json to avoid retry loops: %s", exc)
            dimensions_clause = ""

        start_time, end_time, _ = _utc_window(minutes)
        period = _clamp_int(period_seconds, default=60, minimum=60, maximum=3600)
        command = (
            "aws cloudwatch get-metric-statistics "
            f"--namespace {_quote_cli_value(namespace)} "
            f"--metric-name {_quote_cli_value(metric_name)} "
            f"--start-time {_quote_cli_value(start_time)} "
            f"--end-time {_quote_cli_value(end_time)} "
            f"--period {period} "
            f"--statistics {_quote_cli_value(selected_statistic)}"
            f"{dimensions_clause} "
            "--output json"
        )
        raw_result = _call_aws_cli(mcp_client, aws_api_tool, command)
        return _summarize_metric_history(raw_result, selected_statistic)

    @tool(
        name="get_metric_history",
        description=(
            "Read-only CloudWatch tool. Retrieves recent datapoints for one metric. "
            "For EC2 instance metrics, prefer get_ec2_metric_history. For other "
            "namespaces, pass dimensions_json as a JSON array such as "
            "[{\"Name\":\"ClusterName\",\"Value\":\"cluster-a\"}]."
        ),
    )
    def get_metric_history(
        namespace: str,
        metric_name: str,
        dimensions_json: str = "[]",
        minutes: int = 60,
        period_seconds: int = 60,
        statistic: str = "Average",
    ) -> str:
        """Get recent metric datapoints.

        Args:
            namespace: CloudWatch namespace, for example AWS/ECS.
            metric_name: Metric name, for example CPUUtilization.
            dimensions_json: JSON array of dimension objects with Name and Value.
            minutes: Lookback window in minutes, clamped to 5..1440.
            period_seconds: CloudWatch period, clamped to 60..3600.
            statistic: Statistic such as Average, Maximum, Minimum, Sum, SampleCount.
        """
        logger.info("Wrapper tool call intercepted: get_metric_history")
        return _get_metric_history_result(
            namespace=namespace,
            metric_name=metric_name,
            dimensions_json=dimensions_json,
            minutes=minutes,
            period_seconds=period_seconds,
            statistic=statistic,
        )

    @tool(
        name="get_ec2_metric_history",
        description=(
            "Read-only CloudWatch tool. Retrieves recent EC2 instance metric datapoints. "
            "Use this when the affected resource ID starts with i-; pass only the "
            "instance_id, metric_name, lookback minutes, period, and statistic."
        ),
    )
    def get_ec2_metric_history(
        instance_id: str,
        metric_name: str = "CPUUtilization",
        minutes: int = 60,
        period_seconds: int = 60,
        statistic: str = "Average",
    ) -> str:
        """Get recent EC2 metric datapoints for a single instance.

        Args:
            instance_id: EC2 instance ID, for example i-0123456789abcdef0.
            metric_name: EC2 metric name, for example CPUUtilization.
            minutes: Lookback window in minutes, clamped to 5..1440.
            period_seconds: CloudWatch period, clamped to 60..3600.
            statistic: Statistic such as Average, Maximum, Minimum, Sum, SampleCount.
        """
        logger.info("Wrapper tool call intercepted: get_ec2_metric_history")
        normalized_instance_id = str(instance_id or "").strip()
        if not normalized_instance_id:
            return "MCP tool error: instance_id is required."
        dimensions_json = json.dumps([{"Name": "InstanceId", "Value": normalized_instance_id}])
        return _get_metric_history_result(
            namespace="AWS/EC2",
            metric_name=metric_name,
            dimensions_json=dimensions_json,
            minutes=minutes,
            period_seconds=period_seconds,
            statistic=statistic,
        )

    @tool(
        name="list_log_groups",
        description=(
            "Read-only CloudWatch Logs tool. Lists log groups, optionally filtered by prefix."
        ),
    )
    def list_log_groups(prefix: str = "", limit: int = 20) -> str:
        """List CloudWatch log groups.

        Args:
            prefix: Optional log group name prefix.
            limit: Maximum groups to return, clamped to 1..50.
        """
        logger.info("Wrapper tool call intercepted: list_log_groups")
        bounded_limit = _clamp_int(limit, default=20, minimum=1, maximum=50)
        prefix_clause = (
            f"--log-group-name-prefix {_quote_cli_value(prefix)} "
            if str(prefix or "").strip()
            else ""
        )
        command = (
            "aws logs describe-log-groups "
            f"{prefix_clause}"
            f"--limit {bounded_limit} --output json"
        )
        raw_result = _call_aws_cli(mcp_client, aws_api_tool, command)
        return _summarize_log_groups(raw_result)

    @tool(
        name="search_log_events",
        description=(
            "Read-only CloudWatch Logs tool. Searches recent events in one log group. "
            "Use only when the ticket or alarm identifies a relevant log group."
        ),
    )
    def search_log_events(
        log_group_name: str,
        filter_pattern: str = "",
        minutes: int = 60,
        limit: int = 20,
    ) -> str:
        """Search recent CloudWatch Logs events.

        Args:
            log_group_name: Exact CloudWatch log group name.
            filter_pattern: Optional CloudWatch Logs filter pattern.
            minutes: Lookback window in minutes, clamped to 5..1440.
            limit: Maximum events to return, clamped to 1..50.
        """
        logger.info("Wrapper tool call intercepted: search_log_events")
        if not str(log_group_name or "").strip():
            return "MCP tool error: log_group_name is required."

        start = datetime.now(timezone.utc) - timedelta(
            minutes=_clamp_int(minutes, default=60, minimum=5, maximum=1440)
        )
        start_millis = int(start.timestamp() * 1000)
        bounded_limit = _clamp_int(limit, default=20, minimum=1, maximum=50)
        filter_clause = (
            f"--filter-pattern {_quote_cli_value(filter_pattern)} "
            if str(filter_pattern or "").strip()
            else ""
        )
        command = (
            "aws logs filter-log-events "
            f"--log-group-name {_quote_cli_value(log_group_name)} "
            f"{filter_clause}"
            f"--start-time {start_millis} "
            f"--limit {bounded_limit} --output json"
        )
        raw_result = _call_aws_cli(mcp_client, aws_api_tool, command)
        return _summarize_log_events(raw_result)

    @tool(
        name="search_incident_history",
        description=(
            "Read-only incident-history tool. Searches prior Freshdesk/headless incident "
            "records in the MSP DynamoDB ticket table to classify whether an alarm or "
            "resource problem is new, sporadic, recurring, or frequent."
        ),
    )
    def search_incident_history(
        alarm_name: str = "",
        resource_id: str = "",
        namespace: str = "",
        metric_name: str = "",
        keywords: str = "",
        current_ticket_id: str = "",
        lookback_days: int = 30,
        max_items: int = 100,
    ) -> str:
        """Search previous incident records.

        Args:
            alarm_name: Current CloudWatch alarm name, when known.
            resource_id: Affected AWS resource ID, when known.
            namespace: Metric namespace, for example AWS/EC2.
            metric_name: Metric name, for example CPUUtilization.
            keywords: Comma- or newline-separated extra search terms.
            current_ticket_id: Current Freshdesk ticket ID to exclude from matches.
            lookback_days: History window, clamped to 1..365.
            max_items: Maximum DynamoDB rows to inspect, clamped to 20..500.
        """
        logger.info("Wrapper tool call intercepted: search_incident_history")
        if not aws_api_tool:
            return "MCP tool error: aws-api-mcp___call_aws is not available for incident history lookup."

        bounded_days = _clamp_int(lookback_days, default=30, minimum=1, maximum=365)
        bounded_items = _clamp_int(max_items, default=100, minimum=20, maximum=500)
        since_epoch = int((datetime.now(timezone.utc) - timedelta(days=bounded_days)).timestamp())
        expression_names = {
            "#record_type": "record_type",
            "#created_at": "created_at",
            "#status": "status",
            "#subject": "subject",
            "#description": "description",
            "#region": "region",
        }
        expression_values = {
            ":record_type": {"S": "incident_history"},
            ":since": {"N": str(since_epoch)},
        }
        command = (
            "aws dynamodb scan "
            f"--table-name {_quote_cli_value(INCIDENT_HISTORY_TABLE)} "
            "--filter-expression '#record_type = :record_type AND #created_at >= :since' "
            f"--expression-attribute-names {_quote_cli_value(json.dumps(expression_names, separators=(',', ':')))} "
            f"--expression-attribute-values {_quote_cli_value(json.dumps(expression_values, separators=(',', ':')))} "
            "--projection-expression 'request_id,#record_type,ticket_id,#status,#created_at,updated_at,#subject,#description,account_name,#region,resource_id,investigation,remediation' "
            f"--max-items {bounded_items} "
            "--output json"
        )
        # Incident history is stored in the MSP account, so force default credentials
        # even when the current CloudWatch investigation targets a customer account.
        raw_result = _call_mcp_tool(
            mcp_client,
            aws_api_tool,
            {
                "account_name": "default",
                "region": _safe_region(),
                "cli_command": command,
            },
        )
        return _summarize_incident_history(
            raw_result,
            alarm_name=alarm_name,
            resource_id=resource_id,
            namespace=namespace,
            metric_name=metric_name,
            keywords=keywords,
            current_ticket_id=current_ticket_id,
            customer_account_name=_current_ctx.get("account_name", ""),
            region=_safe_region(),
            lookback_days=bounded_days,
        )

    wrapper_tools = [
        get_active_alarms,
        get_alarm_details,
        get_metric_history,
        get_ec2_metric_history,
        list_log_groups,
        search_log_events,
        search_incident_history,
    ]
    if aws_docs_tool:
        @tool(
            name="search_aws_docs",
            description=(
                "Read-only AWS Knowledge MCP tool. Searches official AWS documentation "
                "for troubleshooting guidance related to the alarm, metric, or service."
            ),
        )
        def search_aws_docs(query: str, topics: str = "troubleshooting,general", max_results: int = 5) -> str:
            """Search official AWS documentation.

            Args:
                query: Focused AWS troubleshooting query.
                topics: Comma-separated topic filters, for example troubleshooting,general.
                max_results: Maximum results, clamped to 1..10.
            """
            logger.info("Wrapper tool call intercepted: search_aws_docs")
            if not str(query or "").strip():
                return "MCP tool error: query is required."
            topic_values = [
                topic.strip()
                for topic in str(topics or "troubleshooting,general").split(",")
                if topic.strip()
            ][:5]
            raw_result = _call_mcp_tool(
                mcp_client,
                aws_docs_tool,
                {
                    "query": str(query).strip(),
                    "topics": topic_values or ["troubleshooting", "general"],
                    "max_results": _clamp_int(max_results, default=5, minimum=1, maximum=10),
                },
            )
            return _summarize_aws_docs(raw_result)

        wrapper_tools.append(search_aws_docs)
    logger.info(
        "Using Nova wrapper tools: exposed=%s, backing_mcp_tools=%s",
        [wrapped.tool_name for wrapped in wrapper_tools],
        sorted(name for name in [active_alarms_tool, aws_api_tool, aws_docs_tool] if name),
    )
    return wrapper_tools


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
    mcp_tools = mcp_client.list_tools_sync()
    if CLOUDWATCH_USE_WRAPPER_TOOLS:
        tools = _create_nova_wrapper_tools(mcp_client, mcp_tools)
        if not tools:
            logger.warning("Nova wrapper tool creation failed; falling back to direct MCP tools")
            tools = _filter_tools_for_nova(mcp_tools)
            _inject_context_into_tools(tools)
    else:
        tools = _filter_tools_for_nova(mcp_tools)
        _inject_context_into_tools(tools)
    _sanitize_tool_specs_for_nova(tools)
    _log_tool_specs_for_nova(tools)
    logger.info(
        f"Bedrock model configured: model={MODEL}, streaming={BEDROCK_STREAMING}, "
        f"temperature=0, topK=1, wrapper_tools={CLOUDWATCH_USE_WRAPPER_TOOLS}, "
        f"max_tokens={max_tokens or MAX_TOKENS}"
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
