# CloudWatch Specialist Agent Test Plan

## Objective

Validate the CloudWatch A2A specialist independently from Freshdesk and the Supervisor, then verify the suspected Nova tool-use fix in a controlled way.

The target failure is:

```text
ModelErrorException when calling Converse or ConverseStream:
Model produced invalid sequence as part of ToolUse.
```

The critical signal is that normal text generation works, but prompts that require CloudWatch or AWS evidence fail before this runtime log appears:

```text
Tool call intercepted
```

If that log is absent, the MCP tool was not dispatched. The failure is happening while Nova generates the Bedrock `toolUse` block.

## Scope

- Test `cloudwatch_a2a_runtime` directly through AgentCore Runtime.
- Bypass Freshdesk, backend direct routing, and the Supervisor.
- Validate Nova Pro with MCP tools attached.
- Confirm whether tool dispatch reaches `context_tools.py` and the CloudWatch MCP runtime.
- Keep all tests read-only.

## Non-Goals

- Do not test Freshdesk webhook intake.
- Do not test remediation approval or execution.
- Do not test Jira.
- Do not execute AWS write APIs.
- Do not commit account IDs, secrets, tokens, or `.env` files.

## Working Hypothesis

Nova Pro is valid for plain generation, but fails when it must emit a tool call for the CloudWatch MCP tools. The most likely causes are:

- Nova tool-use decoding is not deterministic enough for reliable `toolUse` generation.
- The final Strands tool schemas are too permissive or include fields Nova handles poorly.
- The switch from `ConverseStream` to `Converse` changed where the error is observed, but is not the primary root cause.

The first proposed fix is to configure the CloudWatch specialist's `BedrockModel` with greedy decoding:

```python
BedrockModel(
    model_id=MODEL,
    max_tokens=max_tokens or MAX_TOKENS,
    temperature=0,
    additional_request_fields={"inferenceConfig": {"topK": 1}},
)
```

If this does not resolve the failure, sanitize the final `tool.tool_spec["inputSchema"]["json"]` after `mcp_client.list_tools_sync()` so Nova receives only supported top-level schema fields: `type`, `properties`, and `required`.

## Acceptance Criteria

- Plain direct invocation returns a normal text response.
- Tool-required direct invocation returns a CloudWatch evidence response.
- CloudWatch specialist logs include `Tool call intercepted`.
- CloudWatch MCP logs include a tool execution log or credential-injection log.
- No `Model produced invalid sequence as part of ToolUse` appears in the CloudWatch specialist logs after the fix.
- The returned response contains concise evidence and does not claim that remediation was executed.

## Prerequisites

Use a shell with AWS credentials for the target account and region.

```bash
export REGION=us-east-1
export ACCOUNT_ID="<aws_account_id>"
export CLOUDWATCH_RUNTIME_ID="<cloudwatch_a2a_runtime_id>"
export CLOUDWATCH_LOG_GROUP="<cloudwatch_a2a_runtime_log_group>"
export CLOUDWATCH_MCP_LOG_GROUP="<cloudwatch_mcp_runtime_log_group>"
export ACCOUNT_NAME=default
```

Discover values when they are not known:

```bash
aws sts get-caller-identity --region "$REGION"

aws bedrock-agentcore-control list-agent-runtimes \
  --region "$REGION" \
  --query "agentRuntimes[?contains(agentRuntimeName, 'cloudwatch_a2a_runtime')].[agentRuntimeName,agentRuntimeArn,status]" \
  --output table

aws logs describe-log-groups \
  --region "$REGION" \
  --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/" \
  --query "logGroups[?contains(logGroupName, 'cloudwatch')].logGroupName" \
  --output table
```

Validate the runtime ID before invoking. For AWS CLI direct tests, use runtime ID plus `--account-id` instead of the full ARN. The full ARN contains `runtime/<id>` and can fail through the CLI invocation path with a misleading default-qualifier error.

```bash
printf 'Runtime ID: %s\n' "$CLOUDWATCH_RUNTIME_ID"

aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$CLOUDWATCH_RUNTIME_ID" \
  --region "$REGION" \
  --query "{id:agentRuntimeId,name:agentRuntimeName,status:status,version:agentRuntimeVersion,arn:agentRuntimeArn}" \
  --output table
```

## Phase 1 - Baseline Direct Invocation

Create a plain prompt payload that should not use tools.

```bash
export SESSION_ID="cw-plain-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"

python3 - <<'PY'
import json
import os

session_id = os.environ["SESSION_ID"]
account_name = os.environ.get("ACCOUNT_NAME", "default")
region = os.environ.get("REGION", "us-east-1")
prompt = "Say hello in one short sentence and do not use any tools."
meta = json.dumps({"__metadata__": {"account_name": account_name, "region": region}})

payload = {
    "jsonrpc": "2.0",
    "id": session_id,
    "method": "message/send",
    "params": {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": f"{meta}\n{prompt}"}],
            "messageId": session_id,
        }
    },
}

with open("/tmp/cloudwatch-a2a-payload.json", "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY
```

Invoke the runtime.

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$CLOUDWATCH_RUNTIME_ID" \
  --account-id "$ACCOUNT_ID" \
  --runtime-session-id "$SESSION_ID" \
  --content-type application/json \
  --accept application/json \
  --payload fileb:///tmp/cloudwatch-a2a-payload.json \
  --region "$REGION" \
  /tmp/cloudwatch-a2a-response.json

cat /tmp/cloudwatch-a2a-response.json
```

Expected result:

- Invocation succeeds.
- Response is plain text.
- No `Tool call intercepted` log is required for this baseline.

## Phase 2 - Reproduce Tool-Use Failure

Create a prompt that should require CloudWatch MCP tools.

```bash
export SESSION_ID="cw-tools-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"

python3 - <<'PY'
import json
import os

session_id = os.environ["SESSION_ID"]
account_name = os.environ.get("ACCOUNT_NAME", "default")
region = os.environ.get("REGION", "us-east-1")
prompt = """Check CloudWatch alarms in ALARM or INSUFFICIENT_DATA state in this account and region.
Return a concise summary with alarm names, state, metric, and timestamp context.
Use read-only evidence only."""
meta = json.dumps({"__metadata__": {"account_name": account_name, "region": region}})

payload = {
    "jsonrpc": "2.0",
    "id": session_id,
    "method": "message/send",
    "params": {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": f"{meta}\n{prompt}"}],
            "messageId": session_id,
        }
    },
}

with open("/tmp/cloudwatch-a2a-payload.json", "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY

aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$CLOUDWATCH_RUNTIME_ID" \
  --account-id "$ACCOUNT_ID" \
  --runtime-session-id "$SESSION_ID" \
  --content-type application/json \
  --accept application/json \
  --payload fileb:///tmp/cloudwatch-a2a-payload.json \
  --region "$REGION" \
  /tmp/cloudwatch-a2a-response.json

cat /tmp/cloudwatch-a2a-response.json
```

Check logs around the test window.

```bash
aws logs tail "$CLOUDWATCH_LOG_GROUP" \
  --region "$REGION" \
  --since 15m \
  --format short

aws logs tail "$CLOUDWATCH_MCP_LOG_GROUP" \
  --region "$REGION" \
  --since 15m \
  --format short
```

Expected failure before the fix:

- CloudWatch specialist log shows `Model produced invalid sequence as part of ToolUse`.
- CloudWatch specialist log does not show `Tool call intercepted`.
- CloudWatch MCP log does not show a corresponding tool call.

## Phase 3 - Instrument Tool Specs

Before changing behavior, add temporary or guarded logging after `mcp_client.list_tools_sync()` in the CloudWatch specialist runtime.

Minimum data to log:

- Number of tools.
- Each tool name.
- Each top-level input schema key.
- Whether unsupported top-level keys are present.
- Whether schema has `type: object`.

Do not log credentials, request payloads containing secrets, or full customer data.

Expected result:

- The runtime logs enough schema metadata to confirm what Nova receives.
- The log should be safe to keep at `INFO` if it only includes schema keys and tool names.

## Phase 4 - Apply Nova Tool-Use Configuration

Patch the CloudWatch specialist model construction in `agents/runtime_cloudwatch/context_tools.py`.

Required configuration:

```python
BedrockModel(
    model_id=MODEL,
    max_tokens=max_tokens or MAX_TOKENS,
    temperature=0,
    additional_request_fields={"inferenceConfig": {"topK": 1}},
)
```

Recommended follow-up if this fixes CloudWatch:

- Apply the same model configuration to the other specialist runtimes that use Nova with tools.
- Keep Supervisor changes separate unless the Supervisor also reproduces Nova tool-use failures.

## Phase 5 - Optional Schema Hardening

If Phase 4 is not enough, sanitize the final Strands tool specs in `agents/runtime_cloudwatch/context_tools.py`.

Rules:

- Top-level schema must be `type: object`.
- Preserve only top-level `type`, `properties`, and `required`.
- Remove top-level `title`, `description`, `additionalProperties`, `$schema`, `$defs`, `$ref`, `anyOf`, `oneOf`, and `allOf`.
- Keep property-level `description`, `type`, `enum`, `items`, and simple object fields when needed.
- Log before/after top-level schema keys for each tool.

Expected result:

- Tool schemas remain expressive enough for Nova to choose and fill arguments.
- Nova no longer fails before Strands dispatches the MCP tool.

## Phase 6 - Redeploy CloudWatch Specialist

Redeploy only the CloudWatch specialist runtime first. Avoid redeploying the full system until the direct test passes.

```bash
cd agents/runtime_cloudwatch

agentcore configure \
  --entrypoint cloudwatch_a2a_runtime.py \
  --protocol A2A \
  --name cloudwatch_a2a_runtime \
  --region "$REGION" \
  --requirements-file requirements.txt \
  --non-interactive

agentcore deploy --auto-update-on-conflict

agentcore status
```

After deployment, update `CLOUDWATCH_RUNTIME_ID` if AgentCore returns a new runtime ID.

## Phase 7 - Validation Matrix

Run each prompt directly against `cloudwatch_a2a_runtime`.

| Test | Prompt | Expected Tool Use | Expected Result |
| --- | --- | --- | --- |
| Plain response | `Say hello and do not use tools.` | No | Short text response |
| Alarm list | `List CloudWatch alarms in ALARM or INSUFFICIENT_DATA state.` | Yes | Alarm summary or "0 active alarms" |
| EC2 CPU metrics | `Check CPUUtilization for instance i-xxxxxxxxxxxxxxxxx over the last hour.` | Yes | Metric trend or clear not-found/error evidence |
| Logs discovery | `List relevant CloudWatch log groups for Lambda functions.` | Yes | Concise log group list |
| Invalid resource | `Check CPUUtilization for instance i-00000000000000000.` | Yes | Graceful not-found response |

For each test, record:

- Session ID.
- Prompt.
- Runtime ARN.
- Whether response succeeded.
- Whether `Tool call intercepted` appeared.
- Whether MCP runtime logs show execution.
- Whether the final answer includes read-only evidence.

## Phase 8 - Freshdesk Re-Entry Check

Only after direct CloudWatch tests pass, run one Freshdesk-style investigation through the backend.

Use a payload with an explicit resource and region:

```json
{
  "ticket_id": "manual-test-001",
  "subject": "EC2 high CPU investigation",
  "description": "Please investigate instance i-xxxxxxxxxxxxxxxxx in us-east-1. Do not remediate.",
  "account_name": "default",
  "region": "us-east-1",
  "resource_id": "i-xxxxxxxxxxxxxxxxx"
}
```

Expected result:

- Request moves to `complete`.
- Freshdesk note, if configured, contains evidence and proposed action.
- Pending remediation record is created with `status: pending`.
- No AWS remediation action is executed.

## Decision Points

- If plain direct invocation fails, debug runtime deployment, model access, region, or AgentCore invocation payload before tool-use work.
- If plain direct invocation passes and tool prompts fail before `Tool call intercepted`, continue with Nova decoding and schema hardening.
- If `Tool call intercepted` appears but MCP fails, debug Gateway auth, MCP target sync, credential injection, or CloudWatch permissions.
- If direct CloudWatch passes but Freshdesk fails, debug backend direct routing, payload normalization, Freshdesk formatting, or DynamoDB state.

## Rollback

Rollback is low risk because the proposed changes affect model request configuration and schema formatting only.

To rollback:

- Revert the `BedrockModel` parameters to the previous constructor.
- Remove temporary schema logging if it is too noisy.
- Redeploy `cloudwatch_a2a_runtime`.
- Re-run Phase 1 and Phase 2 to confirm the previous behavior is restored.
