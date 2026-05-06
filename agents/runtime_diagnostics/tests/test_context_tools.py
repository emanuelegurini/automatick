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
                "description": description,
                "inputSchema": {"json": {"type": "object", "properties": {}}},
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


def _install_fake_aws_modules():
    boto3 = types.ModuleType("boto3")
    boto3.Session = lambda **kwargs: None
    boto3.client = lambda *args, **kwargs: None

    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")

    class FakeClientError(Exception):
        def __init__(self, error_response=None, operation_name=""):
            super().__init__(str(error_response or {}))
            self.response = error_response or {"Error": {"Code": "FakeError", "Message": "Fake error"}}
            self.operation_name = operation_name

    exceptions.ClientError = FakeClientError
    botocore.exceptions = exceptions

    sys.modules["boto3"] = boto3
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = exceptions


def _load_context_tools():
    _install_fake_strands_modules()
    _install_fake_aws_modules()
    module_name = "runtime_diagnostics_context_tools_under_test"
    sys.modules.pop(module_name, None)
    module_path = Path(__file__).resolve().parents[1] / "context_tools.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeEC2Client:
    def __init__(self, tags=None):
        self.tags = tags if tags is not None else [{"Key": "AutomatickDiagnostics", "Value": "true"}]

    def describe_instances(self, InstanceIds):
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": InstanceIds[0],
                            "State": {"Name": "running"},
                            "InstanceType": "t3.micro",
                            "PrivateIpAddress": "10.0.0.10",
                            "PlatformDetails": "Linux/UNIX",
                            "IamInstanceProfile": {"Arn": "arn:aws:iam::123:instance-profile/test"},
                            "Tags": self.tags,
                        }
                    ]
                }
            ]
        }


class FakeSSMClient:
    def __init__(self, managed=True):
        self.managed = managed
        self.sent_commands = []

    def describe_instance_information(self, Filters):
        if not self.managed:
            return {"InstanceInformationList": []}
        return {
            "InstanceInformationList": [
                {
                    "InstanceId": Filters[0]["Values"][0],
                    "PingStatus": "Online",
                    "AgentVersion": "3.2.0",
                    "PlatformType": "Linux",
                    "PlatformName": "Amazon Linux",
                }
            ]
        }

    def send_command(self, **kwargs):
        self.sent_commands.append(kwargs)
        return {"Command": {"CommandId": "cmd-123"}}

    def get_command_invocation(self, CommandId, InstanceId):
        return {
            "CommandId": CommandId,
            "InstanceId": InstanceId,
            "Status": "Success",
            "StatusDetails": "Success",
            "StandardOutputContent": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/xvda1      8G   4G   4G   50% /",
            "StandardErrorContent": "",
        }


class FakeSTSClient:
    def get_caller_identity(self):
        return {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/test/session"}


class FakeSession:
    def __init__(self, ec2=None, ssm=None):
        self.ec2 = ec2 or FakeEC2Client()
        self.ssm = ssm or FakeSSMClient()
        self.sts = FakeSTSClient()

    def client(self, service_name, region_name=None):
        return {
            "ec2": self.ec2,
            "ssm": self.ssm,
            "sts": self.sts,
            "ecs": object(),
            "rds": object(),
            "cloudwatch": object(),
        }[service_name]


class RuntimeDiagnosticsToolTests(unittest.TestCase):
    def setUp(self):
        self.context_tools = _load_context_tools()
        self.context_tools.set_context("default", "us-east-1")
        self.old_sleep = self.context_tools.time.sleep
        self.context_tools.time.sleep = lambda _: None

    def tearDown(self):
        self.context_tools.time.sleep = self.old_sleep

    def test_command_profiles_are_closed_and_read_only(self):
        profiles = self.context_tools.SSM_COMMAND_PROFILES
        self.assertEqual(
            set(profiles),
            {
                "linux_health",
                "disk_usage",
                "memory_pressure",
                "cpu_pressure",
                "failed_services",
                "recent_syslog",
                "network_listeners",
                "process_snapshot",
            },
        )

        forbidden_patterns = [" rm ", " mv ", " kill ", " reboot", " shutdown", " yum install", " apt install"]
        for profile in profiles.values():
            command = f" {profile.command.lower()} "
            for forbidden in forbidden_patterns:
                self.assertNotIn(forbidden, command)
            self.assertNotIn("systemctl restart", command)
            self.assertNotIn("systemctl start", command)
            self.assertNotIn("systemctl stop", command)

    def test_unsupported_command_profile_is_rejected_before_aws_calls(self):
        calls = []

        def fail_session_factory(account_name, region):
            calls.append((account_name, region))
            raise AssertionError("AWS session should not be created for invalid profile")

        self.context_tools._session_for_account = fail_session_factory

        result = json.loads(
            self.context_tools.run_ssm_readonly_command(
                "i-0123456789abcdef0",
                "freeform_shell",
                "us-east-1",
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Unsupported command_profile")
        self.assertEqual(calls, [])

    def test_run_ssm_uses_exact_mapped_command(self):
        fake_ssm = FakeSSMClient()
        fake_session = FakeSession(ssm=fake_ssm)
        self.context_tools._session_for_account = lambda account_name, region: fake_session

        result = json.loads(
            self.context_tools.run_ssm_readonly_command(
                "i-0123456789abcdef0",
                "disk_usage",
                "us-east-1",
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(fake_ssm.sent_commands[0]["DocumentName"], "AWS-RunShellScript")
        self.assertEqual(
            fake_ssm.sent_commands[0]["Parameters"]["commands"],
            [self.context_tools.SSM_COMMAND_PROFILES["disk_usage"].command],
        )
        self.assertEqual(result["evidence"]["profile"], "disk_usage")
        self.assertEqual(result["evidence"]["status"], "Success")

    def test_run_ssm_requires_diagnostics_tag(self):
        fake_session = FakeSession(ec2=FakeEC2Client(tags=[{"Key": "Name", "Value": "untagged"}]))
        self.context_tools._session_for_account = lambda account_name, region: fake_session

        result = json.loads(
            self.context_tools.run_ssm_readonly_command(
                "i-0123456789abcdef0",
                "disk_usage",
                "us-east-1",
            )
        )

        self.assertFalse(result["ok"])
        self.assertIn("not tagged", result["error"])

    def test_non_ssm_managed_instance_returns_limitation(self):
        fake_session = FakeSession(ssm=FakeSSMClient(managed=False))
        self.context_tools._session_for_account = lambda account_name, region: fake_session

        result = json.loads(
            self.context_tools.run_ssm_readonly_command(
                "i-0123456789abcdef0",
                "linux_health",
                "us-east-1",
            )
        )

        self.assertTrue(result["ok"])
        self.assertIn("not managed by SSM", result["limitations"][0])

    def test_inspect_ec2_reports_ssm_status(self):
        fake_session = FakeSession()
        self.context_tools._session_for_account = lambda account_name, region: fake_session

        result = json.loads(
            self.context_tools.inspect_ec2_instance(
                "i-0123456789abcdef0",
                "us-east-1",
            )
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["ssm"]["managed_by_ssm"])
        self.assertEqual(result["ec2"]["tags"]["AutomatickDiagnostics"], "true")

    def test_rds_query_profiles_are_validated_but_not_executed_by_default(self):
        invalid = json.loads(
            self.context_tools.run_rds_readonly_query("db-prod", "select_star", "us-east-1")
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"], "Unsupported query_profile")

        valid = json.loads(
            self.context_tools.run_rds_readonly_query("db-prod", "connections", "us-east-1")
        )
        self.assertTrue(valid["ok"])
        self.assertFalse(valid["executed"])
        self.assertIn("SQL execution is disabled", valid["limitations"][0])


if __name__ == "__main__":
    unittest.main()
