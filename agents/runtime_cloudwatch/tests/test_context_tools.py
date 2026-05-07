import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


def _install_fake_strands_modules():
    strands = types.ModuleType("strands")

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def fake_tool(name=None, description=""):
        def decorator(func):
            tool_name = name or func.__name__
            func.tool_name = tool_name
            func.tool_spec = {
                "name": tool_name,
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {},
                    }
                },
                "description": description,
            }
            return func

        return decorator

    strands.Agent = FakeAgent
    strands.tool = fake_tool

    models = types.ModuleType("strands.models")

    class FakeBedrockModel:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    models.BedrockModel = FakeBedrockModel

    multiagent = types.ModuleType("strands.multiagent")
    a2a = types.ModuleType("strands.multiagent.a2a")

    class FakeA2AServer:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    a2a.A2AServer = FakeA2AServer

    sys.modules["strands"] = strands
    sys.modules["strands.models"] = models
    sys.modules["strands.multiagent"] = multiagent
    sys.modules["strands.multiagent.a2a"] = a2a


def _load_context_tools():
    _install_fake_strands_modules()
    module_name = "runtime_cloudwatch_context_tools_under_test"
    sys.modules.pop(module_name, None)
    module_path = Path(__file__).resolve().parents[1] / "context_tools.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeMCPTool:
    def __init__(self, name):
        self.tool_spec = {"name": name}


class FakeMCPClient:
    def __init__(self, result_text=None):
        self.calls = []
        self.result_text = result_text

    def call_tool_sync(self, tool_use_id, name, arguments):
        self.calls.append(
            {
                "tool_use_id": tool_use_id,
                "name": name,
                "arguments": arguments,
            }
        )
        return {
            "status": "success",
            "content": [
                {
                    "text": self.result_text
                    or json.dumps(
                        {
                            "called": name,
                            "arguments": arguments,
                        },
                        sort_keys=True,
                    )
                }
            ],
        }


class CloudWatchNovaWrapperTests(unittest.TestCase):
    def setUp(self):
        self.context_tools = _load_context_tools()
        self.context_tools.set_context("default", "us-east-1")
        self.client = FakeMCPClient()

    def _create_wrappers(self, tool_names):
        mcp_tools = [FakeMCPTool(name) for name in tool_names]
        wrappers = self.context_tools._create_nova_wrapper_tools(self.client, mcp_tools)
        return {wrapper.tool_name: wrapper for wrapper in wrappers}

    def test_exposes_expected_nova_safe_tool_names(self):
        wrappers = self._create_wrappers(
            [
                "cloudwatch-mcp___get_active_alarms",
                "aws-api-mcp___call_aws",
            ]
        )

        self.assertEqual(
            set(wrappers),
            {
                "get_active_alarms",
                "get_alarm_details",
                "get_metric_history",
                "get_ec2_metric_history",
                "list_log_groups",
                "search_log_events",
                "search_incident_history",
            },
        )

    def test_get_active_alarms_prefers_cloudwatch_mcp_tool(self):
        wrappers = self._create_wrappers(
            [
                "cloudwatch-mcp___get_active_alarms",
                "aws-api-mcp___call_aws",
            ]
        )

        wrappers["get_active_alarms"]()

        self.assertEqual(self.client.calls[0]["name"], "cloudwatch-mcp___get_active_alarms")
        self.assertEqual(
            self.client.calls[0]["arguments"],
            {"account_name": "default", "region": "us-east-1"},
        )

    def test_get_alarm_details_uses_read_only_cloudwatch_cli(self):
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        wrappers["get_alarm_details"]("CPU Alarm")

        call = self.client.calls[0]
        self.assertEqual(call["name"], "aws-api-mcp___call_aws")
        self.assertEqual(call["arguments"]["account_name"], "default")
        self.assertEqual(call["arguments"]["region"], "us-east-1")
        self.assertIn("aws cloudwatch describe-alarms", call["arguments"]["cli_command"])
        self.assertIn("--alarm-names 'CPU Alarm'", call["arguments"]["cli_command"])

    def test_get_metric_history_builds_dimensions_and_clamps_period(self):
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        wrappers["get_metric_history"](
            namespace="AWS/ECS",
            metric_name="CPUUtilization",
            dimensions_json=json.dumps(
                [
                    {"Name": "ClusterName", "Value": "cluster-a"},
                    {"Name": "ServiceName", "Value": "service-a"},
                ]
            ),
            minutes=30,
            period_seconds=10,
            statistic="Maximum",
        )

        command = self.client.calls[0]["arguments"]["cli_command"]
        self.assertIn("aws cloudwatch get-metric-statistics", command)
        self.assertIn("--namespace AWS/ECS", command)
        self.assertIn("--metric-name CPUUtilization", command)
        self.assertIn("--period 60", command)
        self.assertIn("--statistics Maximum", command)
        self.assertIn("--dimensions Name=ClusterName,Value=cluster-a Name=ServiceName,Value=service-a", command)

    def test_get_metric_history_ignores_invalid_dimensions_json(self):
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        wrappers["get_metric_history"](
            namespace="AWS/ECS",
            metric_name="CPUUtilization",
            dimensions_json="not-json",
        )

        command = self.client.calls[0]["arguments"]["cli_command"]
        self.assertIn("aws cloudwatch get-metric-statistics", command)
        self.assertNotIn("--dimensions", command)

    def test_get_ec2_metric_history_builds_instance_dimension(self):
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        wrappers["get_ec2_metric_history"](
            instance_id="i-abc123",
            metric_name="CPUUtilization",
            minutes=30,
            statistic="Maximum",
        )

        command = self.client.calls[0]["arguments"]["cli_command"]
        self.assertIn("--namespace AWS/EC2", command)
        self.assertIn("--metric-name CPUUtilization", command)
        self.assertIn("--statistics Maximum", command)
        self.assertIn("--dimensions Name=InstanceId,Value=i-abc123", command)

    def test_search_log_events_requires_log_group_name(self):
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        result = wrappers["search_log_events"](log_group_name="")

        self.assertIn("log_group_name is required", result)
        self.assertEqual(self.client.calls, [])

    def test_search_incident_history_scans_msp_ticket_history_with_default_credentials(self):
        self.context_tools.set_context("runtime_test", "us-east-1")
        raw_history_payload = {
            "response": {
                "json": json.dumps(
                    {
                        "Items": [
                            {
                                "request_id": {"S": "incident-history-111"},
                                "record_type": {"S": "incident_history"},
                                "ticket_id": {"S": "111"},
                                "created_at": {"N": "1778080000"},
                                "status": {"S": "complete"},
                                "subject": {"S": "EC2 CPU alarm"},
                                "account_name": {"S": "runtime_test"},
                                "region": {"S": "us-east-1"},
                                "resource_id": {"S": "i-abc123"},
                                "investigation": {
                                    "M": {
                                        "root_cause_hypothesis": {"S": "sqlservr consumed CPU"},
                                        "proposed_action": {"S": "Inspect SQL workload"},
                                    }
                                },
                            }
                        ],
                        "ScannedCount": 1,
                    }
                )
            }
        }
        self.client = FakeMCPClient(result_text=json.dumps(raw_history_payload))
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        result = wrappers["search_incident_history"](
            alarm_name="EC2 CPU alarm",
            resource_id="i-abc123",
            namespace="AWS/EC2",
            metric_name="CPUUtilization",
            keywords="sqlservr,cpu",
            current_ticket_id="222",
            lookback_days=90,
        )

        call = self.client.calls[0]
        self.assertEqual(call["name"], "aws-api-mcp___call_aws")
        self.assertEqual(call["arguments"]["account_name"], "default")
        self.assertIn("aws dynamodb scan", call["arguments"]["cli_command"])
        self.assertIn("msp-assistant-chat-requests", call["arguments"]["cli_command"])
        self.assertIn('"classification": "sporadic_or_rare"', result)
        self.assertIn('"ticket_id": "111"', result)
        self.assertIn('"matched_on"', result)

    def test_search_incident_history_classifies_multiple_matches_as_recurring(self):
        now = 1778160000
        raw_history_payload = {
            "response": {
                "json": json.dumps(
                    {
                        "Items": [
                            {
                                "request_id": {"S": "incident-history-111"},
                                "record_type": {"S": "incident_history"},
                                "ticket_id": {"S": "111"},
                                "created_at": {"N": str(now)},
                                "subject": {"S": "CPUUtilization high on i-abc123"},
                                "account_name": {"S": "runtime_test"},
                                "region": {"S": "us-east-1"},
                                "resource_id": {"S": "i-abc123"},
                            },
                            {
                                "request_id": {"S": "incident-history-112"},
                                "record_type": {"S": "incident_history"},
                                "ticket_id": {"S": "112"},
                                "created_at": {"N": str(now - 3600)},
                                "subject": {"S": "CPUUtilization high on i-abc123"},
                                "account_name": {"S": "runtime_test"},
                                "region": {"S": "us-east-1"},
                                "resource_id": {"S": "i-abc123"},
                            },
                            {
                                "request_id": {"S": "incident-history-113"},
                                "record_type": {"S": "incident_history"},
                                "ticket_id": {"S": "113"},
                                "created_at": {"N": str(now - 7200)},
                                "subject": {"S": "CPUUtilization high on i-abc123"},
                                "account_name": {"S": "runtime_test"},
                                "region": {"S": "us-east-1"},
                                "resource_id": {"S": "i-abc123"},
                            },
                        ],
                        "ScannedCount": 3,
                    }
                )
            }
        }

        result = self.context_tools._summarize_incident_history(
            json.dumps(raw_history_payload),
            resource_id="i-abc123",
            metric_name="CPUUtilization",
            current_ticket_id="999",
            customer_account_name="runtime_test",
            region="us-east-1",
            lookback_days=30,
        )

        self.assertIn('"classification": "frequent_recurring"', result)
        self.assertIn('"matches_found": 3', result)

    def test_optional_aws_docs_wrapper_is_exposed_when_knowledge_tool_exists(self):
        self.client = FakeMCPClient(
            result_text=json.dumps(
                {
                    "success": True,
                    "results": [
                        {
                            "title": "Troubleshoot EC2 CPU",
                            "url": "https://docs.aws.amazon.com/example",
                            "context": "Use CloudWatch metrics and process-level evidence.",
                        }
                    ],
                }
            )
        )
        wrappers = self._create_wrappers(
            [
                "aws-api-mcp___call_aws",
                "aws-knowledge-mcp___search_aws_docs",
            ]
        )

        result = wrappers["search_aws_docs"]("EC2 CPUUtilization troubleshooting")

        self.assertIn('"aws_documentation"', result)
        self.assertEqual(self.client.calls[0]["name"], "aws-knowledge-mcp___search_aws_docs")

    def test_optional_internal_runbooks_wrapper_is_exposed_when_knowledge_tool_exists(self):
        self.client = FakeMCPClient(
            result_text=json.dumps(
                {
                    "success": True,
                    "query": "EC2 high CPU",
                    "results": [
                        {
                            "score": 0.82,
                            "source_uri": "s3://kb/runbooks/ec2-high-cpu.md",
                            "content": "Use CloudWatch metrics and SSM process evidence.",
                        }
                    ],
                }
            )
        )
        wrappers = self._create_wrappers(
            [
                "aws-api-mcp___call_aws",
                "aws-knowledge-mcp___search_internal_runbooks",
            ]
        )

        result = wrappers["search_internal_runbooks"]("EC2 high CPU")

        self.assertIn('"internal_runbooks"', result)
        self.assertIn("ec2-high-cpu.md", result)
        self.assertEqual(self.client.calls[0]["name"], "aws-knowledge-mcp___search_internal_runbooks")

    def test_alarm_details_returns_compact_summary_not_raw_alarm_payload(self):
        raw_alarm_payload = {
            "response": {
                "json": json.dumps(
                    {
                        "MetricAlarms": [
                            {
                                "AlarmName": "cpu-alarm",
                                "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:cpu-alarm",
                                "StateValue": "ALARM",
                                "StateReason": "Threshold Crossed",
                                "StateReasonData": json.dumps(
                                    {
                                        "evaluatedDatapoints": [
                                            {
                                                "timestamp": "2026-05-05T10:00:00.000+0000",
                                                "value": 91.2,
                                                "sampleCount": 1.0,
                                            }
                                        ]
                                    }
                                ),
                                "Namespace": "AWS/EC2",
                                "MetricName": "CPUUtilization",
                                "Dimensions": [{"Name": "InstanceId", "Value": "i-abc"}],
                                "Threshold": 80.0,
                                "ComparisonOperator": "GreaterThanThreshold",
                                "Period": 60,
                                "EvaluationPeriods": 3,
                            }
                        ]
                    }
                )
            }
        }
        self.client = FakeMCPClient(result_text=json.dumps(raw_alarm_payload))
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        result = wrappers["get_alarm_details"]("cpu-alarm")

        self.assertIn('"alarm_name": "cpu-alarm"', result)
        self.assertIn('"recent_datapoints"', result)
        self.assertNotIn("AlarmArn", result)

    def test_ecs_target_tracking_alarm_low_gets_specific_interpretation(self):
        raw_alarm_payload = {
            "response": {
                "json": json.dumps(
                    {
                        "MetricAlarms": [
                            {
                                "AlarmName": "TargetTracking-service/cluster/service-AlarmLow-abc",
                                "StateValue": "ALARM",
                                "StateReasonData": json.dumps(
                                    {
                                        "evaluatedDatapoints": [
                                            {
                                                "timestamp": "2026-05-05T10:00:00.000+0000",
                                                "value": 0.3,
                                            },
                                            {
                                                "timestamp": "2026-05-05T09:59:00.000+0000",
                                                "value": 1.7,
                                            },
                                        ]
                                    }
                                ),
                                "Namespace": "AWS/ECS",
                                "MetricName": "CPUUtilization",
                                "Dimensions": [
                                    {"Name": "ClusterName", "Value": "cluster"},
                                    {"Name": "ServiceName", "Value": "service"},
                                ],
                                "Threshold": 63.0,
                                "ComparisonOperator": "LessThanThreshold",
                            }
                        ]
                    }
                )
            }
        }
        self.client = FakeMCPClient(result_text=json.dumps(raw_alarm_payload))
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        result = wrappers["get_alarm_details"]("TargetTracking-service/cluster/service-AlarmLow-abc")

        self.assertIn('"classification": "ecs_target_tracking_low_utilization"', result)
        self.assertIn("scale-in/low-utilization signal", result)
        self.assertIn('"latest": 0.3', result)
        self.assertIn('"maximum": 1.7', result)

    def test_metric_history_returns_compact_datapoints(self):
        raw_metric_payload = {
            "response": {
                "json": json.dumps(
                    {
                        "Datapoints": [
                            {
                                "Timestamp": "2026-05-05T10:00:00+00:00",
                                "Average": 10.5,
                                "Unit": "Percent",
                            }
                        ],
                        "Label": "CPUUtilization",
                    }
                )
            }
        }
        self.client = FakeMCPClient(result_text=json.dumps(raw_metric_payload))
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        result = wrappers["get_metric_history"]("AWS/EC2", "CPUUtilization")

        self.assertIn('"metric_history"', result)
        self.assertIn('"datapoint_count": 1', result)
        self.assertIn('"value": 10.5', result)

    def test_metric_history_includes_compact_statistics(self):
        raw_metric_payload = {
            "response": {
                "json": json.dumps(
                    {
                        "Datapoints": [
                            {
                                "Timestamp": "2026-05-05T10:00:00+00:00",
                                "Average": 2.0,
                                "Unit": "Percent",
                            },
                            {
                                "Timestamp": "2026-05-05T10:01:00+00:00",
                                "Average": 4.0,
                                "Unit": "Percent",
                            },
                        ]
                    }
                )
            }
        }
        self.client = FakeMCPClient(result_text=json.dumps(raw_metric_payload))
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        result = wrappers["get_metric_history"]("AWS/ECS", "CPUUtilization")

        self.assertIn('"stats"', result)
        self.assertIn('"latest": 4.0', result)
        self.assertIn('"minimum": 2.0', result)
        self.assertIn('"average": 3.0', result)

    def test_metric_history_preserves_zero_values(self):
        raw_metric_payload = {
            "response": {
                "json": json.dumps(
                    {
                        "Datapoints": [
                            {
                                "Timestamp": "2026-05-05T10:00:00+00:00",
                                "Average": 0.0,
                                "Unit": "Percent",
                            }
                        ]
                    }
                )
            }
        }
        self.client = FakeMCPClient(result_text=json.dumps(raw_metric_payload))
        wrappers = self._create_wrappers(["aws-api-mcp___call_aws"])

        result = wrappers["get_metric_history"]("AWS/ECS", "CPUUtilization")

        self.assertIn('"value": 0.0', result)
        self.assertIn('"latest": 0.0', result)


if __name__ == "__main__":
    unittest.main()
