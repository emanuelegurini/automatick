#!/usr/bin/env python3
"""Validate Automatick documentation metadata and basic knowledge-store hygiene."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOC_DIRS = ("docs", "runbooks")
COMMON_REQUIRED_KEYS = {
    "owner",
    "status",
    "last_verified",
    "source",
    "agent_targets",
}
RUNBOOK_REQUIRED_KEYS = COMMON_REQUIRED_KEYS | {"services", "incident_types"}
VALID_STATUSES = {"draft", "verified", "deprecated"}
SECRET_PATTERNS = (
    re.compile(r"AWS_ACCESS_KEY_ID\s*=", re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY\s*=", re.IGNORECASE),
    re.compile(r"AWS_SESSION_TOKEN\s*=", re.IGNORECASE),
    re.compile(r"FRESHDESK_API_KEY\s*=", re.IGNORECASE),
    re.compile(r"WEBHOOK_SECRET\s*=", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return simple YAML-like frontmatter and body.

    The docs intentionally use a small key/value subset so the linter can run
    without PyYAML or other extra dependencies.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + 5 :]
    metadata = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            metadata[line] = ""
            continue
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip()
    return metadata, body


def markdown_files(directories: list[str]) -> list[Path]:
    files: list[Path] = []
    for directory in directories:
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        files.extend(path for path in root.rglob("*.md") if path.is_file())
    return sorted(files)


def first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line
    return ""


def has_required_section(body: str, names: tuple[str, ...]) -> bool:
    lowered = body.lower()
    return any(f"\n## {name.lower()}" in lowered for name in names)


def check_local_links(path: Path, body: str) -> list[str]:
    errors: list[str] = []
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", body):
        if "://" in target or target.startswith("#") or target.startswith("mailto:"):
            continue
        clean_target = target.split("#", 1)[0].strip()
        if not clean_target:
            continue
        resolved = (path.parent / clean_target).resolve()
        try:
            resolved.relative_to(REPO_ROOT)
        except ValueError:
            errors.append(f"local link escapes repo: {target}")
            continue
        if not resolved.exists():
            errors.append(f"broken local link: {target}")
    return errors


def validate_file(path: Path) -> list[str]:
    rel = path.relative_to(REPO_ROOT)
    text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    errors: list[str] = []

    if not metadata:
        errors.append("missing frontmatter")
    required = RUNBOOK_REQUIRED_KEYS if rel.parts[0] == "runbooks" else COMMON_REQUIRED_KEYS
    missing = sorted(key for key in required if not metadata.get(key))
    if missing:
        errors.append(f"missing frontmatter keys: {', '.join(missing)}")

    status = metadata.get("status", "")
    if status and status not in VALID_STATUSES:
        errors.append(f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}")

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata.get("last_verified", "")):
        errors.append("last_verified must use YYYY-MM-DD")

    if not first_heading(body):
        errors.append("missing H1 heading")

    if rel.parts[0] == "runbooks" and metadata.get("source") == "internal-runbook":
        if not has_required_section(body, ("Overview",)):
            errors.append("internal runbook missing Overview section")
        if not has_required_section(body, ("Diagnosis Steps", "Diagnosis")):
            errors.append("internal runbook missing Diagnosis section")
        if not has_required_section(body, ("Remediation Steps", "Remediation")):
            errors.append("internal runbook missing Remediation section")
        if not has_required_section(body, ("Verification", "Validation")):
            errors.append("internal runbook missing Verification/Validation section")

    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.append(f"possible secret pattern found: {pattern.pattern}")

    errors.extend(check_local_links(path, body))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint docs/ and runbooks/ knowledge files")
    parser.add_argument(
        "directories",
        nargs="*",
        default=list(DEFAULT_DOC_DIRS),
        help="Directories to lint, relative to the repo root",
    )
    args = parser.parse_args()

    files = markdown_files(args.directories)
    if not files:
        print("No Markdown files found.")
        return 1

    failures: list[tuple[Path, list[str]]] = []
    for path in files:
        errors = validate_file(path)
        if errors:
            failures.append((path, errors))

    if failures:
        print("Documentation lint failed:")
        for path, errors in failures:
            print(f"- {path.relative_to(REPO_ROOT)}")
            for error in errors:
                print(f"  - {error}")
        return 1

    print(f"Documentation lint passed: {len(files)} Markdown files checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
