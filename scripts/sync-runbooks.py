#!/usr/bin/env python3
"""Sync local knowledge docs to the Bedrock Knowledge Base S3 data source.

This script is the operational tool for keeping the Bedrock Knowledge Base used by the
MSP Ops Automation platform up to date with the latest documentation content from the
local ``docs/`` and ``runbooks/`` directories.

Workflow:
  1. Resolve the target Knowledge Base ID from ``--kb-id`` or the ``backend/.env`` file.
  2. Discover the S3 data source bucket attached to the Knowledge Base.
  3. Discover Markdown files in the configured local knowledge directories.
  4. Diff each local ``.md`` file against its S3 counterpart using MD5/ETag comparison.
  5. Upload only new or changed files (or all files with ``--force``).
  6. If any files were uploaded, start a Bedrock ingestion job to re-index the KB.
  7. Poll the ingestion job until COMPLETE or FAILED (unless ``--no-wait`` is set).

Assumptions:
  - Knowledge documents live in ``<repo_root>/docs/`` and ``<repo_root>/runbooks/``.
  - The KB has exactly one S3 data source; additional data sources are ignored.
  - S3 ETag for single-part uploads equals the MD5 of the file content, which is
    valid for files below the multipart threshold (~8 MB). Larger documents would
    require a different comparison strategy.
  - The caller's AWS credentials must have s3:PutObject on the bucket,
    bedrock-agent:StartIngestionJob on the KB, and bedrock-agent:GetIngestionJob
    for polling.

Usage:
    python scripts/sync-runbooks.py [--force] [--no-wait] [--region us-east-1]
                                    [--env-file backend/.env] [--kb-id <KB_ID>]
                                    [--include-dir docs --include-dir runbooks]
"""

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ModuleNotFoundError:
    boto3 = None

    class ClientError(Exception):
        """Fallback so --help works when boto3 is not installed locally."""

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE_DIRS = ("docs", "runbooks")


def load_env_file(env_file):
    """Parse KEY=VALUE lines from an env file (ignores comments and empty lines)."""
    env = {}
    if not os.path.exists(env_file):
        return env
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def discover_data_source(bedrock_agent, kb_id):
    """Find the S3 data source and its bucket for the given KB."""
    resp = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id)
    for ds in resp.get("dataSourceSummaries", []):
        ds_id = ds["dataSourceId"]
        detail = bedrock_agent.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)
        ds_config = detail["dataSource"]["dataSourceConfiguration"]
        if ds_config.get("type") == "S3":
            bucket_arn = ds_config["s3Configuration"]["bucketArn"]
            bucket_name = bucket_arn.split(":::")[-1]
            prefix = ds_config["s3Configuration"].get("inclusionPrefixes", [""])[0]
            return ds_id, bucket_name, prefix
    return None, None, None


def compute_md5(file_path):
    """Return hex MD5 digest of a local file."""
    with open(file_path, "rb") as f:
        return hashlib.md5(f.read(), usedforsecurity=False).hexdigest()


def collect_markdown_files(include_dirs):
    """Return sorted (absolute_path, repo_relative_s3_key) pairs for Markdown docs."""
    discovered = []
    for include_dir in include_dirs:
        root = (REPO_ROOT / include_dir).resolve()
        if not root.exists():
            print(f"  [missing] {include_dir}/")
            continue
        if not root.is_dir():
            print(f"  [skipped] {include_dir} is not a directory")
            continue
        for path in sorted(root.rglob("*.md")):
            if path.is_file():
                discovered.append((path, path.relative_to(REPO_ROOT).as_posix()))
    return discovered


def get_s3_etag(s3_client, bucket, key):
    """Return the S3 ETag (stripped of quotes) or None if the object doesn't exist."""
    try:
        resp = s3_client.head_object(Bucket=bucket, Key=key)
        return resp["ETag"].strip('"')
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise


def sync_runbooks(args):
    """Diff, upload, and ingest knowledge docs into the Bedrock Knowledge Base.

    Orchestrates the full sync lifecycle: validates the KB, discovers its S3 data
    source, diffs local Markdown files from ``docs/`` and ``runbooks/`` against
    S3, uploads changed files, and triggers a Bedrock ingestion job. Polls the
    job to completion unless ``args.no_wait`` is set.

    Args:
        args: Parsed argparse.Namespace with the following attributes:
            kb_id (str): Bedrock Knowledge Base ID.
            region (str): AWS region.
            force (bool): If True, upload all files regardless of diff.
            no_wait (bool): If True, start ingestion and return without polling.
    """
    kb_id = args.kb_id
    if not kb_id:
        print("Error: BEDROCK_KNOWLEDGE_BASE_ID is not set. Provide --kb-id or set it in the env file.")
        sys.exit(1)
    if boto3 is None:
        print("Error: boto3 is required to sync the Bedrock Knowledge Base. Install backend/CDK dependencies first.")
        sys.exit(1)

    session = boto3.Session(region_name=args.region)
    bedrock_agent = session.client("bedrock-agent")
    s3 = session.client("s3")

    # Validate KB exists
    try:
        bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
    except ClientError as e:
        print(f"Error: Could not access Knowledge Base '{kb_id}': {e}")
        sys.exit(1)

    print(f"Knowledge Base: {kb_id}")

    # Discover S3 data source
    ds_id, bucket, prefix = discover_data_source(bedrock_agent, kb_id)
    if not ds_id:
        print("Error: No S3 data source found for this Knowledge Base.")
        sys.exit(1)

    print(f"Data Source: {ds_id}")
    print(f"S3 Bucket: {bucket}")
    if prefix:
        print(f"Data Source inclusion prefix: {prefix}")

    local_files = collect_markdown_files(args.include_dir)
    if not local_files:
        print(f"No .md files found in: {', '.join(args.include_dir)}")
        sys.exit(1)

    print(f"Found {len(local_files)} local knowledge document(s)\n")

    # Diff and upload
    new, updated, unchanged, failed = 0, 0, 0, 0
    uploaded_any = False

    for local_path, s3_key in local_files:
        local_md5 = compute_md5(local_path)

        if not args.force:
            remote_etag = get_s3_etag(s3, bucket, s3_key)
            if remote_etag and remote_etag == local_md5:
                print(f"  [unchanged] {filename}")
                unchanged += 1
                continue
            status = "new" if remote_etag is None else "updated"
        else:
            status = "force"

        try:
            with open(local_path, "rb") as f:
                s3.put_object(Bucket=bucket, Key=s3_key, Body=f.read(), ContentType="text/markdown")
            print(f"  [{status}] {s3_key}")
            uploaded_any = True
            if status == "new":
                new += 1
            else:
                updated += 1
        except ClientError as e:
            print(f"  [FAILED] {s3_key}: {e}")
            failed += 1

    print(f"\nSummary: {new} new, {updated} updated, {unchanged} unchanged, {failed} failed")

    if not uploaded_any:
        print("All runbooks up to date, ingestion not needed.")
        return

    # Start ingestion job
    print(f"\nStarting ingestion job...")
    try:
        resp = bedrock_agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
        job_id = resp["ingestionJob"]["ingestionJobId"]
        print(f"Ingestion job started: {job_id}")
    except ClientError as e:
        print(f"Error starting ingestion: {e}")
        sys.exit(1)

    if args.no_wait:
        print("--no-wait specified, not polling. Track manually with:")
        print(f"  aws bedrock-agent get-ingestion-job --knowledge-base-id {kb_id} --data-source-id {ds_id} --ingestion-job-id {job_id}")
        return

    # Poll for completion
    timeout = 300  # 5 minutes
    interval = 10
    elapsed = 0

    while elapsed < timeout:
        time.sleep(interval)  # nosemgrep: arbitrary-sleep
        elapsed += interval
        try:
            resp = bedrock_agent.get_ingestion_job(
                knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
            )
            job = resp["ingestionJob"]
            status = job["status"]
            print(f"  Ingestion status: {status} ({elapsed}s)")

            if status == "COMPLETE":
                stats = job.get("statistics", {})
                print(f"  Documents scanned: {stats.get('numberOfDocumentsScanned', 'N/A')}")
                print(f"  Documents indexed: {stats.get('numberOfNewDocumentsIndexed', 'N/A')}")
                print(f"  Documents updated: {stats.get('numberOfModifiedDocumentsIndexed', 'N/A')}")
                print(f"  Documents failed: {stats.get('numberOfDocumentsFailed', 'N/A')}")
                print("\nIngestion complete.")
                return

            if status == "FAILED":
                reasons = job.get("failureReasons", ["Unknown"])
                print(f"  Ingestion FAILED: {reasons}")
                sys.exit(1)

        except ClientError as e:
            print(f"  Error polling ingestion: {e}")

    print(f"Timed out after {timeout}s. Check ingestion job manually: {job_id}")
    sys.exit(1)


def main():
    """Parse CLI arguments, resolve KB ID from env file if needed, and run sync_runbooks."""
    parser = argparse.ArgumentParser(description="Sync docs/runbooks to Bedrock Knowledge Base S3 data source")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for ingestion to complete")
    parser.add_argument("--force", action="store_true", help="Upload all files regardless of diff")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    parser.add_argument("--env-file", default="backend/.env", help="Path to .env file (default: backend/.env)")
    parser.add_argument("--kb-id", default=None, help="Override KB ID (otherwise read from env file)")
    parser.add_argument(
        "--include-dir",
        action="append",
        default=None,
        help="Knowledge directory to sync, relative to repo root. Repeatable. Defaults to docs and runbooks.",
    )

    args = parser.parse_args()
    if not args.include_dir:
        args.include_dir = list(DEFAULT_KNOWLEDGE_DIRS)

    # Load KB ID from env file if not provided via CLI
    if not args.kb_id:
        env = load_env_file(args.env_file)
        args.kb_id = env.get("BEDROCK_KNOWLEDGE_BASE_ID", "")

    sync_runbooks(args)


if __name__ == "__main__":
    main()
