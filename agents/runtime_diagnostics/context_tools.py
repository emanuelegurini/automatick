"""Runtime diagnostics tools for EC2/SSM, ECS, and RDS.

This specialist intentionally exposes only closed, read-only diagnostic profiles.
It must not become a general shell or SQL terminal controlled by the model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

logger = logging.getLogger(__name__)

MODEL = os.getenv("MODEL_ID", os.getenv("MODEL", "amazon.nova-pro-v1:0"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))
BEDROCK_STREAMING = os.getenv("BEDROCK_STREAMING", "false").lower() in ("1", "true", "yes", "on")
NOVA_TOOL_ADDITIONAL_REQUEST_FIELDS = {"inferenceConfig": {"topK": 1}}

SSM_COMMAND_TIMEOUT_SECONDS = int(os.getenv("RUNTIME_DIAGNOSTICS_SSM_TIMEOUT_SECONDS", "90"))
SSM_POLL_INTERVAL_SECONDS = float(os.getenv("RUNTIME_DIAGNOSTICS_SSM_POLL_INTERVAL_SECONDS", "2"))
REQUIRE_DIAGNOSTICS_TAG = os.getenv(
    "RUNTIME_DIAGNOSTICS_REQUIRE_TAG", "true"
).lower() in ("1", "true", "yes", "on")
DIAGNOSTICS_TAG_KEY = os.getenv("RUNTIME_DIAGNOSTICS_TAG_KEY", "AutomatickDiagnostics")
DIAGNOSTICS_TAG_VALUE = os.getenv("RUNTIME_DIAGNOSTICS_TAG_VALUE", "true")
RDS_QUERY_EXECUTION_ENABLED = os.getenv(
    "RUNTIME_DIAGNOSTICS_ENABLE_RDS_QUERIES", "false"
).lower() in ("1", "true", "yes", "on")

_current_ctx = {"account_name": "", "region": "us-east-1"}

_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-fA-F]{8,17}$")
_TERMINAL_COMMAND_STATUSES = {
    "Success",
    "Cancelled",
    "TimedOut",
    "Failed",
    "Cancelling",
    "Undeliverable",
    "Terminated",
}
_SUPPORTED_RDS_QUERY_PROFILES = {
    "connections",
    "locks",
    "slow_queries",
    "replication_lag",
    "database_size",
    "top_waits",
}


@dataclass(frozen=True)
class CommandProfile:
    name: str
    description: str
    command: str


SSM_COMMAND_PROFILES: dict[str, CommandProfile] = {
    "linux_health": CommandProfile(
        name="linux_health",
        description="Basic host uptime, kernel, OS, load, and current user.",
        command=(
            'echo "### uptime"; uptime; '
            'echo "### kernel"; uname -a; '
            'echo "### os"; (cat /etc/os-release 2>/dev/null || true); '
            'echo "### load"; cat /proc/loadavg; '
            'echo "### user"; whoami'
        ),
    ),
    "disk_usage": CommandProfile(
        name="disk_usage",
        description="Filesystem and inode usage.",
        command='echo "### filesystem usage"; df -hT; echo "### inode usage"; df -ih',
    ),
    "memory_pressure": CommandProfile(
        name="memory_pressure",
        description="Memory usage and top memory-consuming processes.",
        command=(
            'echo "### memory"; free -m; '
            'echo "### top memory processes"; '
            "ps -eo pid,ppid,comm,%mem,%cpu --sort=-%mem | head -20"
        ),
    ),
    "cpu_pressure": CommandProfile(
        name="cpu_pressure",
        description="Load average and top CPU-consuming processes.",
        command=(
            'echo "### load"; uptime; '
            'echo "### top cpu processes"; '
            "ps -eo pid,ppid,comm,%cpu,%mem --sort=-%cpu | head -20"
        ),
    ),
    "failed_services": CommandProfile(
        name="failed_services",
        description="Failed systemd units where systemd is available.",
        command='echo "### failed services"; (systemctl --failed --no-pager 2>/dev/null || true)',
    ),
    "recent_syslog": CommandProfile(
        name="recent_syslog",
        description="Recent local OS log lines from journald, syslog, or messages.",
        command=(
            'echo "### recent system logs"; '
            'if command -v journalctl >/dev/null 2>&1; then journalctl -n 120 --no-pager; '
            'elif [ -f /var/log/syslog ]; then tail -n 120 /var/log/syslog; '
            'elif [ -f /var/log/messages ]; then tail -n 120 /var/log/messages; '
            'else echo "No syslog/messages/journal source available"; fi'
        ),
    ),
    "network_listeners": CommandProfile(
        name="network_listeners",
        description="Listening TCP/UDP sockets.",
        command=(
            'echo "### network listeners"; '
            'if command -v ss >/dev/null 2>&1; then ss -tulpen; '
            'elif command -v netstat >/dev/null 2>&1; then netstat -tulpen; '
            'else echo "No ss/netstat command available"; fi'
        ),
    ),
    "process_snapshot": CommandProfile(
        name="process_snapshot",
        description="Top running processes by CPU.",
        command=(
            'echo "### process snapshot"; '
            "ps -eo pid,ppid,user,comm,%cpu,%mem,etime --sort=-%cpu | head -30"
        ),
    ),
}


def set_context(account_name: str, region: str = "us-east-1") -> None:
    """Set current customer context extracted from A2A metadata."""
    global _current_ctx
    _current_ctx = {"account_name": account_name or "", "region": region or "us-east-1"}
    logger.info("Runtime diagnostics context set: account_name=%r, region=%s", account_name, region)


def _safe_region(region: str = "") -> str:
    return str(region or "").strip() or _current_ctx.get("region") or "us-east-1"


def _safe_account_name() -> str:
    return _current_ctx.get("account_name") or "default"


def _limit_text(value: Any, max_chars: int = 6000) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, sort_keys=True)


def _success_payload(**kwargs) -> str:
    return _json_dumps({"ok": True, **kwargs})


def _error_payload(error: str, **kwargs) -> str:
    return _json_dumps({"ok": False, "error": error, **kwargs})


def _validate_instance_id(instance_id: str) -> str:
    candidate = str(instance_id or "").strip()
    if not _INSTANCE_ID_RE.match(candidate):
        raise ValueError("instance_id must be an EC2 instance ID such as i-0123456789abcdef0")
    return candidate


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _parse_secret_time(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _refresh_customer_credentials(secrets_client, secret_name: str, creds: dict, region: str) -> dict | None:
    role_arn = creds.get("role_arn")
    external_id = creds.get("external_id")
    if not role_arn:
        logger.error("No role_arn in %s; cannot refresh customer credentials", secret_name)
        return None

    params = {
        "RoleArn": role_arn,
        "RoleSessionName": f"AutomatickRuntimeDiag-{int(time.time())}",
        "DurationSeconds": 3600,
    }
    if external_id:
        params["ExternalId"] = external_id

    try:
        sts = boto3.client("sts", region_name=region)
        response = sts.assume_role(**params)
        new_creds = response["Credentials"]
        creds.update(
            {
                "aws_access_key_id": new_creds["AccessKeyId"],
                "aws_secret_access_key": new_creds["SecretAccessKey"],
                "aws_session_token": new_creds["SessionToken"],
                "expires_at": new_creds["Expiration"].isoformat(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        secrets_client.put_secret_value(SecretId=secret_name, SecretString=json.dumps(creds))
        logger.info("Refreshed customer credentials for %s", secret_name)
        return creds
    except Exception as exc:
        logger.error("Failed to refresh customer credentials for %s: %s", secret_name, exc)
        return None


def _session_for_account(account_name: str, region: str):
    """Build a boto3 session for default or customer context."""
    if not account_name or account_name == "default":
        return boto3.Session(region_name=region)

    secret_name = f"msp-credentials/{account_name}"
    secrets_client = boto3.client("secretsmanager", region_name=region)
    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
        creds = json.loads(response["SecretString"])
    except Exception as exc:
        raise RuntimeError(f"Could not load customer secret {secret_name}: {exc}") from exc

    expires_at = _parse_secret_time(creds.get("expires_at", ""))
    if expires_at and expires_at <= datetime.now(timezone.utc) + timedelta(minutes=10):
        refreshed = _refresh_customer_credentials(secrets_client, secret_name, creds, region)
        if not refreshed:
            raise RuntimeError(f"Customer credentials for {account_name} are expired and refresh failed")
        creds = refreshed

    required = ("aws_access_key_id", "aws_secret_access_key")
    if any(not creds.get(key) for key in required):
        refreshed = _refresh_customer_credentials(secrets_client, secret_name, creds, region)
        if not refreshed:
            raise RuntimeError(f"Customer secret {secret_name} has no usable STS credentials")
        creds = refreshed

    return boto3.Session(
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        aws_session_token=creds.get("aws_session_token"),
        region_name=region,
    )


def _clients(region: str):
    session = _session_for_account(_safe_account_name(), region)
    return {
        "ec2": session.client("ec2", region_name=region),
        "ssm": session.client("ssm", region_name=region),
        "ecs": session.client("ecs", region_name=region),
        "rds": session.client("rds", region_name=region),
        "cloudwatch": session.client("cloudwatch", region_name=region),
        "sts": session.client("sts", region_name=region),
    }


def _tag_map(tags: list[dict] | None) -> dict[str, str]:
    result = {}
    for tag in tags or []:
        key = str(tag.get("Key", ""))
        if key:
            result[key] = str(tag.get("Value", ""))
    return result


def _essential_tags(tags: dict[str, str]) -> dict[str, str]:
    keep = {"Name", "Environment", "Customer", "Service", DIAGNOSTICS_TAG_KEY}
    return {key: value for key, value in tags.items() if key in keep}


def _describe_instance(ec2_client, instance_id: str) -> dict | None:
    response = ec2_client.describe_instances(InstanceIds=[instance_id])
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            if instance.get("InstanceId") == instance_id:
                return instance
    return None


def _ssm_instance_info(ssm_client, instance_id: str) -> dict | None:
    response = ssm_client.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )
    infos = response.get("InstanceInformationList", [])
    return infos[0] if infos else None


def _account_context(sts_client) -> dict:
    try:
        identity = sts_client.get_caller_identity()
        return {
            "account": identity.get("Account"),
            "arn": identity.get("Arn"),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _instance_summary(instance: dict | None) -> dict:
    if not instance:
        return {}
    tags = _tag_map(instance.get("Tags", []))
    profile = instance.get("IamInstanceProfile") or {}
    return {
        "instance_id": instance.get("InstanceId"),
        "state": (instance.get("State") or {}).get("Name"),
        "instance_type": instance.get("InstanceType"),
        "availability_zone": (instance.get("Placement") or {}).get("AvailabilityZone"),
        "private_ip": instance.get("PrivateIpAddress"),
        "public_ip": instance.get("PublicIpAddress"),
        "platform": instance.get("PlatformDetails") or instance.get("Platform") or "Linux/UNIX",
        "image_id": instance.get("ImageId"),
        "launch_time": instance.get("LaunchTime"),
        "iam_instance_profile": profile.get("Arn"),
        "tags": _essential_tags(tags),
    }


def _ssm_summary(info: dict | None) -> dict:
    if not info:
        return {
            "managed_by_ssm": False,
            "limitation": "The EC2 instance is not managed by SSM, so no in-instance command can be executed.",
        }
    return {
        "managed_by_ssm": True,
        "ping_status": info.get("PingStatus"),
        "last_ping": info.get("LastPingDateTime"),
        "agent_version": info.get("AgentVersion"),
        "platform_type": info.get("PlatformType"),
        "platform_name": info.get("PlatformName"),
        "platform_version": info.get("PlatformVersion"),
    }


def _has_required_diagnostics_tag(instance: dict | None) -> bool:
    if not REQUIRE_DIAGNOSTICS_TAG:
        return True
    tags = _tag_map((instance or {}).get("Tags", []))
    return tags.get(DIAGNOSTICS_TAG_KEY, "").lower() == DIAGNOSTICS_TAG_VALUE.lower()


def _command_invocation_summary(invocation: dict, command_id: str, profile: CommandProfile, duration_seconds: float) -> dict:
    return {
        "command_id": command_id,
        "profile": profile.name,
        "command": profile.command,
        "status": invocation.get("Status"),
        "status_details": invocation.get("StatusDetails"),
        "duration_seconds": round(duration_seconds, 2),
        "stdout": _limit_text(invocation.get("StandardOutputContent", "")),
        "stderr": _limit_text(invocation.get("StandardErrorContent", ""), 3000),
        "failure_reason": invocation.get("StatusDetails") if invocation.get("Status") != "Success" else "",
    }


@tool(
    name="inspect_ec2_instance",
    description="Read-only EC2/SSM metadata inspection for one EC2 instance ID.",
)
def inspect_ec2_instance(instance_id: str, region: str = "") -> str:
    """Inspect EC2 metadata and SSM managed status for an instance.

    Args:
        instance_id: EC2 instance ID.
        region: AWS region. Uses the invocation region if omitted.
    """
    try:
        resolved_region = _safe_region(region)
        resolved_instance_id = _validate_instance_id(instance_id)
        clients = _clients(resolved_region)
        instance = _describe_instance(clients["ec2"], resolved_instance_id)
        if not instance:
            return _error_payload("Instance not found", instance_id=resolved_instance_id, region=resolved_region)
        ssm_error = ""
        try:
            ssm_info = _ssm_instance_info(clients["ssm"], resolved_instance_id)
        except ClientError as exc:
            ssm_info = None
            ssm_error = str(exc)
            logger.warning("Could not inspect SSM status for %s: %s", resolved_instance_id, exc)
        ssm = _ssm_summary(ssm_info)
        if ssm_error:
            ssm["managed_by_ssm"] = None
            ssm["limitation"] = f"Could not determine SSM managed status: {ssm_error}"
        return _success_payload(
            target={
                "type": "ec2",
                "identifier": resolved_instance_id,
                "region": resolved_region,
                "account_context": _account_context(clients["sts"]),
            },
            ec2=_instance_summary(instance),
            ssm=ssm,
        )
    except ClientError as exc:
        return _error_payload(str(exc), instance_id=instance_id, region=_safe_region(region))
    except Exception as exc:
        return _error_payload(str(exc), instance_id=instance_id, region=_safe_region(region))


@tool(
    name="run_ssm_readonly_command",
    description=(
        "Runs one approved read-only SSM diagnostic profile against an EC2 instance. "
        f"Allowed profiles: {', '.join(sorted(SSM_COMMAND_PROFILES))}."
    ),
)
def run_ssm_readonly_command(instance_id: str, command_profile: str, region: str = "") -> str:
    """Run a closed read-only SSM command profile.

    Args:
        instance_id: EC2 instance ID.
        command_profile: One of the approved profile names.
        region: AWS region. Uses the invocation region if omitted.
    """
    resolved_region = _safe_region(region)
    try:
        resolved_instance_id = _validate_instance_id(instance_id)
        profile_name = str(command_profile or "").strip()
        profile = SSM_COMMAND_PROFILES.get(profile_name)
        if not profile:
            return _error_payload(
                "Unsupported command_profile",
                command_profile=profile_name,
                supported_profiles=sorted(SSM_COMMAND_PROFILES),
            )

        clients = _clients(resolved_region)
        instance = _describe_instance(clients["ec2"], resolved_instance_id)
        if not instance:
            return _error_payload("Instance not found", instance_id=resolved_instance_id, region=resolved_region)
        if not _has_required_diagnostics_tag(instance):
            return _error_payload(
                "Instance is not tagged for Automatick diagnostics",
                required_tag={DIAGNOSTICS_TAG_KEY: DIAGNOSTICS_TAG_VALUE},
                instance_id=resolved_instance_id,
                region=resolved_region,
            )

        ssm_info = _ssm_instance_info(clients["ssm"], resolved_instance_id)
        if not ssm_info:
            return _success_payload(
                target={"type": "ec2", "identifier": resolved_instance_id, "region": resolved_region},
                checks_performed=["ec2_describe_instances", "ssm_describe_instance_information"],
                evidence={"ssm": _ssm_summary(None)},
                limitations=[
                    "The EC2 instance is not managed by SSM, so no in-instance command could be executed."
                ],
            )

        timeout = _clamp_int(SSM_COMMAND_TIMEOUT_SECONDS, default=90, minimum=10, maximum=300)
        started = time.time()
        response = clients["ssm"].send_command(
            InstanceIds=[resolved_instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [profile.command]},
            TimeoutSeconds=timeout,
            Comment=f"Automatick runtime diagnostics profile: {profile.name}",
        )
        command_id = response["Command"]["CommandId"]

        last_invocation = {}
        while time.time() - started <= timeout:
            try:
                last_invocation = clients["ssm"].get_command_invocation(
                    CommandId=command_id,
                    InstanceId=resolved_instance_id,
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "InvocationDoesNotExist":
                    time.sleep(SSM_POLL_INTERVAL_SECONDS)
                    continue
                raise

            if last_invocation.get("Status") in _TERMINAL_COMMAND_STATUSES:
                break
            time.sleep(SSM_POLL_INTERVAL_SECONDS)

        if not last_invocation:
            last_invocation = {"Status": "TimedOut", "StatusDetails": "No command invocation was returned"}

        return _success_payload(
            target={"type": "ec2", "identifier": resolved_instance_id, "region": resolved_region},
            checks_performed=["ssm_send_command", "ssm_get_command_invocation"],
            evidence=_command_invocation_summary(last_invocation, command_id, profile, time.time() - started),
            limitations=[] if last_invocation.get("Status") == "Success" else ["SSM command did not complete successfully."],
        )
    except ClientError as exc:
        return _error_payload(str(exc), instance_id=instance_id, region=resolved_region)
    except Exception as exc:
        return _error_payload(str(exc), instance_id=instance_id, region=resolved_region)


@tool(
    name="inspect_ecs_service",
    description="Read-only ECS cluster/service/task/deployment inspection.",
)
def inspect_ecs_service(cluster: str, service: str, region: str = "") -> str:
    """Inspect ECS service state and recent tasks.

    Args:
        cluster: ECS cluster name or ARN.
        service: ECS service name or ARN.
        region: AWS region. Uses the invocation region if omitted.
    """
    resolved_region = _safe_region(region)
    try:
        if not str(cluster or "").strip() or not str(service or "").strip():
            return _error_payload("cluster and service are required", region=resolved_region)
        clients = _clients(resolved_region)
        ecs = clients["ecs"]
        service_response = ecs.describe_services(cluster=cluster, services=[service])
        services = service_response.get("services", [])
        if not services:
            return _error_payload("ECS service not found", cluster=cluster, service=service, region=resolved_region)
        svc = services[0]

        task_arns = []
        stopped_task_arns = []
        for desired_status, bucket in (("RUNNING", task_arns), ("STOPPED", stopped_task_arns)):
            try:
                listed = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus=desired_status, maxResults=10)
                bucket.extend(listed.get("taskArns", []))
            except ClientError as exc:
                logger.warning("Could not list %s ECS tasks for %s/%s: %s", desired_status, cluster, service, exc)

        described_tasks = []
        if task_arns or stopped_task_arns:
            described = ecs.describe_tasks(cluster=cluster, tasks=(task_arns + stopped_task_arns)[:20])
            for task in described.get("tasks", []):
                described_tasks.append(
                    {
                        "task_arn": task.get("taskArn"),
                        "last_status": task.get("lastStatus"),
                        "desired_status": task.get("desiredStatus"),
                        "health_status": task.get("healthStatus"),
                        "stopped_reason": task.get("stoppedReason"),
                        "containers": [
                            {
                                "name": container.get("name"),
                                "last_status": container.get("lastStatus"),
                                "health_status": container.get("healthStatus"),
                                "exit_code": container.get("exitCode"),
                                "reason": container.get("reason"),
                            }
                            for container in task.get("containers", [])
                        ],
                    }
                )

        return _success_payload(
            target={"type": "ecs", "identifier": f"{cluster}/{service}", "region": resolved_region},
            service={
                "service_name": svc.get("serviceName"),
                "cluster_arn": svc.get("clusterArn"),
                "desired_count": svc.get("desiredCount"),
                "running_count": svc.get("runningCount"),
                "pending_count": svc.get("pendingCount"),
                "task_definition": svc.get("taskDefinition"),
                "deployments": [
                    {
                        "id": deployment.get("id"),
                        "status": deployment.get("status"),
                        "rollout_state": deployment.get("rolloutState"),
                        "rollout_state_reason": deployment.get("rolloutStateReason"),
                        "desired_count": deployment.get("desiredCount"),
                        "running_count": deployment.get("runningCount"),
                        "pending_count": deployment.get("pendingCount"),
                        "task_definition": deployment.get("taskDefinition"),
                    }
                    for deployment in svc.get("deployments", [])
                ],
                "recent_events": [
                    {
                        "created_at": event.get("createdAt"),
                        "message": event.get("message"),
                    }
                    for event in svc.get("events", [])[:10]
                ],
            },
            tasks=described_tasks,
        )
    except ClientError as exc:
        return _error_payload(str(exc), cluster=cluster, service=service, region=resolved_region)
    except Exception as exc:
        return _error_payload(str(exc), cluster=cluster, service=service, region=resolved_region)


def _metric_summary(cloudwatch_client, namespace: str, metric_name: str, dimensions: list[dict]) -> dict:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    try:
        response = cloudwatch_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=300,
            Statistics=["Average", "Maximum"],
        )
    except ClientError as exc:
        return {"metric_name": metric_name, "error": str(exc)}

    datapoints = sorted(
        response.get("Datapoints", []),
        key=lambda item: item.get("Timestamp") or datetime.min.replace(tzinfo=timezone.utc),
    )
    if not datapoints:
        return {"metric_name": metric_name, "datapoints": 0}
    latest = datapoints[-1]
    return {
        "metric_name": metric_name,
        "datapoints": len(datapoints),
        "latest_timestamp": latest.get("Timestamp"),
        "latest_average": latest.get("Average"),
        "latest_maximum": latest.get("Maximum"),
        "unit": latest.get("Unit"),
    }


@tool(
    name="inspect_rds_instance",
    description="Read-only RDS DB instance/cluster metadata, event, and metric inspection.",
)
def inspect_rds_instance(db_identifier: str, region: str = "") -> str:
    """Inspect RDS instance or cluster metadata.

    Args:
        db_identifier: DB instance identifier, or cluster identifier if no instance matches.
        region: AWS region. Uses the invocation region if omitted.
    """
    resolved_region = _safe_region(region)
    identifier = str(db_identifier or "").strip()
    if not identifier:
        return _error_payload("db_identifier is required", region=resolved_region)

    try:
        clients = _clients(resolved_region)
        rds = clients["rds"]
        cloudwatch = clients["cloudwatch"]
        target_type = "db-instance"
        target = None
        try:
            response = rds.describe_db_instances(DBInstanceIdentifier=identifier)
            instances = response.get("DBInstances", [])
            target = instances[0] if instances else None
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"DBInstanceNotFound", "DBInstanceNotFoundFault"}:
                raise
            target_type = "db-cluster"
            cluster_response = rds.describe_db_clusters(DBClusterIdentifier=identifier)
            clusters = cluster_response.get("DBClusters", [])
            target = clusters[0] if clusters else None

        if not target:
            return _error_payload("RDS target not found", db_identifier=identifier, region=resolved_region)

        source_type = "db-instance" if target_type == "db-instance" else "db-cluster"
        events_response = rds.describe_events(
            SourceIdentifier=identifier,
            SourceType=source_type,
            Duration=1440,
        )
        events = [
            {
                "date": event.get("Date"),
                "message": event.get("Message"),
                "source_type": event.get("SourceType"),
            }
            for event in events_response.get("Events", [])[:10]
        ]

        if target_type == "db-instance":
            dimensions = [{"Name": "DBInstanceIdentifier", "Value": identifier}]
            metadata = {
                "db_instance_identifier": target.get("DBInstanceIdentifier"),
                "engine": target.get("Engine"),
                "engine_version": target.get("EngineVersion"),
                "db_instance_status": target.get("DBInstanceStatus"),
                "allocated_storage_gib": target.get("AllocatedStorage"),
                "storage_type": target.get("StorageType"),
                "multi_az": target.get("MultiAZ"),
                "endpoint": (target.get("Endpoint") or {}).get("Address"),
                "publicly_accessible": target.get("PubliclyAccessible"),
            }
            metric_names = ["CPUUtilization", "DatabaseConnections", "FreeStorageSpace"]
        else:
            dimensions = [{"Name": "DBClusterIdentifier", "Value": identifier}]
            metadata = {
                "db_cluster_identifier": target.get("DBClusterIdentifier"),
                "engine": target.get("Engine"),
                "engine_version": target.get("EngineVersion"),
                "status": target.get("Status"),
                "multi_az": target.get("MultiAZ"),
                "endpoint": target.get("Endpoint"),
                "reader_endpoint": target.get("ReaderEndpoint"),
            }
            metric_names = ["CPUUtilization", "DatabaseConnections"]

        return _success_payload(
            target={"type": target_type, "identifier": identifier, "region": resolved_region},
            metadata=metadata,
            recent_events=events,
            cloudwatch_metric_summary=[
                _metric_summary(cloudwatch, "AWS/RDS", metric_name, dimensions)
                for metric_name in metric_names
            ],
        )
    except ClientError as exc:
        return _error_payload(str(exc), db_identifier=identifier, region=resolved_region)
    except Exception as exc:
        return _error_payload(str(exc), db_identifier=identifier, region=resolved_region)


@tool(
    name="run_rds_readonly_query",
    description=(
        "Validates an approved RDS query profile. SQL execution is disabled in v1 unless "
        "RUNTIME_DIAGNOSTICS_ENABLE_RDS_QUERIES=true is explicitly configured."
    ),
)
def run_rds_readonly_query(db_identifier: str, query_profile: str, region: str = "") -> str:
    """Validate a closed RDS query profile; v1 does not execute SQL by default.

    Args:
        db_identifier: RDS DB instance identifier.
        query_profile: One of connections, locks, slow_queries, replication_lag, database_size, top_waits.
        region: AWS region. Uses the invocation region if omitted.
    """
    resolved_region = _safe_region(region)
    identifier = str(db_identifier or "").strip()
    profile = str(query_profile or "").strip()
    if not identifier:
        return _error_payload("db_identifier is required", region=resolved_region)
    if profile not in _SUPPORTED_RDS_QUERY_PROFILES:
        return _error_payload(
            "Unsupported query_profile",
            query_profile=profile,
            supported_profiles=sorted(_SUPPORTED_RDS_QUERY_PROFILES),
        )
    if not RDS_QUERY_EXECUTION_ENABLED:
        return _success_payload(
            target={"type": "rds", "identifier": identifier, "region": resolved_region},
            query_profile=profile,
            executed=False,
            limitations=[
                "RDS SQL execution is disabled in runtime diagnostics v1.",
                "Use inspect_rds_instance for read-only RDS API evidence until DB secrets, engine support, and network reachability are configured.",
            ],
        )
    return _error_payload(
        "RDS SQL execution feature flag is set, but engine-specific query execution is not implemented yet",
        db_identifier=identifier,
        query_profile=profile,
        region=resolved_region,
    )


def _extract_metadata_prompt(original_prompt: str) -> str:
    """Extract metadata JSON prefix from prompt if present, set context, return clean prompt."""
    try:
        if original_prompt.startswith('{"__metadata__":'):
            nl_pos = original_prompt.find("\n")
            if nl_pos == -1:
                logger.warning("Metadata prefix found but no newline delimiter, using prompt as-is")
                return original_prompt
            meta_line = original_prompt[:nl_pos]
            meta = json.loads(meta_line).get("__metadata__", {})
            set_context(meta.get("account_name", ""), meta.get("region", "us-east-1"))
            return original_prompt[nl_pos + 1:]
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse metadata prefix: %s", exc)
    return original_prompt


def create_runtime_diagnostics_agent(system_prompt: str, max_tokens: int | None = None) -> Agent:
    """Create the runtime diagnostics Agent with only native safe tools."""
    tools = [
        inspect_ec2_instance,
        run_ssm_readonly_command,
        inspect_ecs_service,
        inspect_rds_instance,
        run_rds_readonly_query,
    ]
    logger.info("Creating runtime diagnostics agent with %d safe tools", len(tools))
    return Agent(
        name="Runtime Diagnostics",
        description="Collects read-only EC2/SSM, ECS, and RDS runtime evidence across customer AWS accounts",
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


def create_a2a_server(agent, runtime_url: str):
    """Create A2AServer with metadata extraction hook."""
    original_stream = agent.stream_async

    async def patched_stream(content_blocks, **kwargs):
        if content_blocks:
            block = content_blocks[0]
            if hasattr(block, "text"):
                block.text = _extract_metadata_prompt(block.text)
            elif isinstance(block, dict) and "text" in block:
                block["text"] = _extract_metadata_prompt(block["text"])
        async for event in original_stream(content_blocks, **kwargs):
            yield event

    agent.stream_async = patched_stream
    return A2AServer(agent=agent, http_url=runtime_url, serve_at_root=True)
