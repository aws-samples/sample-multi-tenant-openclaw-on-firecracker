#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compile the complete tenant-statistics backend into an executable patch lane."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path


TAG_KEY = "oc-patch-marker"
SOURCE_PATH = "deploy/lambda/tenant_stats/handler.py"
REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.:+=,@/-]+$")


def fail(message: str) -> None:
    raise SystemExit(f"TENANT_STATS_COMPILE_FAILED: {message}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_object(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object")
    return value


def require_string(value: object, label: str, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        fail(f"{label} must be a non-empty string without surrounding whitespace")
    if pattern is not None and not pattern.fullmatch(value):
        fail(f"{label} has an invalid value: {value!r}")
    return value


def require_exact(value: object, expected: object, label: str) -> None:
    if value != expected:
        fail(f"{label} must be {expected!r}, got {value!r}")


def validate(manifest: dict[str, object]) -> dict[str, object]:
    require_string(manifest.get("id"), "manifest.id", NAME_RE)
    patch_sha = require_string(manifest.get("patch_sha"), "manifest.patch_sha")
    if not re.fullmatch(r"[0-9a-f]{40}", patch_sha):
        fail("manifest.patch_sha must be a full lowercase Git SHA")
    require_exact(manifest.get("status"), "READY", "manifest.status")
    specs = manifest.get("tenant_stats_backends")
    if not isinstance(specs, list) or len(specs) != 1 or not isinstance(specs[0], dict):
        fail("manifest.tenant_stats_backends must contain exactly one object")
    spec = specs[0]

    account = require_string(spec.get("target_account"), "target_account")
    if not re.fullmatch(r"[0-9]{12}", account):
        fail("target_account must be a 12-digit account id")
    require_string(spec.get("target_region"), "target_region", REGION_RE)
    marker = require_string(spec.get("marker"), "marker", NAME_RE)
    if len(marker) > 256:
        fail("marker exceeds the AWS tag value limit")
    require_string(spec.get("cfn_follow_up"), "cfn_follow_up")

    table = spec.get("table")
    writer = spec.get("writer")
    schedule = spec.get("schedule")
    if not all(isinstance(value, dict) for value in (table, writer, schedule)):
        fail("table, writer, and schedule must be objects")
    require_exact(table.get("name"), "openclaw-tenant-stats", "table.name")
    require_exact(
        table.get("partition_key"), {"name": "id", "type": "S"}, "table.partition_key"
    )
    require_exact(table.get("billing_mode"), "PAY_PER_REQUEST", "table.billing_mode")
    require_exact(table.get("pitr"), True, "table.pitr")

    exact_writer = {
        "source_path": SOURCE_PATH,
        "function_name": "openclaw-tenant-stats-writer",
        "role_name": "openclaw-tenant-stats-writer-role",
        "policy_name": "openclaw-tenant-stats-writer",
        "runtime": "python3.12",
        "architecture": "arm64",
        "handler": "handler.lambda_handler",
        "timeout": 50,
        "memory_size": 8192,
        "reserved_concurrency": 1,
    }
    for key, expected in exact_writer.items():
        require_exact(writer.get(key), expected, f"writer.{key}")
    environment = writer.get("environment")
    expected_keys = {
        "TENANTS_TABLE",
        "TENANT_STATS_TABLE",
        "ASSETS_BUCKET",
        "ROOTFS_PREFIX",
        "STATS_SCAN_SEGMENTS",
    }
    if not isinstance(environment, dict) or set(environment) != expected_keys:
        fail(f"writer.environment must contain exactly {sorted(expected_keys)}")
    for key, value in environment.items():
        require_string(value, f"writer.environment.{key}")
    require_exact(
        environment["TENANT_STATS_TABLE"], table["name"], "environment.TENANT_STATS_TABLE"
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", environment["TENANTS_TABLE"]):
        fail("environment.TENANTS_TABLE is not a valid DynamoDB table name")
    bucket = environment["ASSETS_BUCKET"]
    if (
        len(bucket) > 63
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", bucket)
        or ".." in bucket
    ):
        fail("environment.ASSETS_BUCKET is not a valid S3 bucket name")
    prefix = environment["ROOTFS_PREFIX"]
    if (
        len(prefix) > 900
        or prefix.startswith("/")
        or prefix.endswith("/")
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", prefix)
        or any(part in {"", ".", ".."} for part in prefix.split("/"))
    ):
        fail("environment.ROOTFS_PREFIX is not a safe S3 object prefix")
    if not environment["STATS_SCAN_SEGMENTS"].isdigit() or not (
        1 <= int(environment["STATS_SCAN_SEGMENTS"]) <= 100
    ):
        fail("environment.STATS_SCAN_SEGMENTS must be an integer string from 1 through 100")

    exact_schedule = {
        "rule_name": "openclaw-tenant-stats-schedule",
        "expression": "rate(1 minute)",
        "target_id": "TenantStatsWriter",
        "permission_statement_id": "ocpatch-tenant-stats-schedule",
    }
    for key, expected in exact_schedule.items():
        require_exact(schedule.get(key), expected, f"schedule.{key}")
    return spec


def git_source(repo: Path, patch_sha: str, source_path: str) -> bytes:
    check = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0 or check.stdout.strip() != "true":
        fail(f"source repo is not a Git worktree: {repo}")
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{patch_sha}:{source_path}"],
        capture_output=True,
    )
    if result.returncode != 0:
        fail(f"{source_path} is unavailable at manifest patch_sha {patch_sha}")
    return result.stdout


def writer_zip(source: bytes) -> bytes:
    output = io.BytesIO()
    info = zipfile.ZipInfo("handler.py", date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(info, source)
    return output.getvalue()


def policy(spec: dict[str, object]) -> dict[str, object]:
    account = spec["target_account"]
    region = spec["target_region"]
    table = spec["table"]["name"]
    writer = spec["writer"]
    env = writer["environment"]
    tenants = env["TENANTS_TABLE"]
    function = writer["function_name"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "LogGroup",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup"],
                "Resource": [f"arn:aws:logs:{region}:{account}:*"],
            },
            {
                "Sid": "LogEvents",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": [
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function}:*"
                ],
            },
            {
                "Sid": "TenantsRead",
                "Effect": "Allow",
                "Action": ["dynamodb:DescribeTable", "dynamodb:Scan"],
                "Resource": [f"arn:aws:dynamodb:{region}:{account}:table/{tenants}"],
            },
            {
                "Sid": "StatsReadWrite",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                ],
                "Resource": [f"arn:aws:dynamodb:{region}:{account}:table/{table}"],
            },
            {
                "Sid": "ManifestRead",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    "arn:aws:s3:::"
                    + env["ASSETS_BUCKET"]
                    + "/"
                    + env["ROOTFS_PREFIX"].rstrip("/")
                    + "/manifest.json"
                ],
            },
        ],
    }


RUNTIME = Path(__file__).with_name("tenant-stats-backend-runtime.py").read_text(encoding="utf-8")

def wrapper(action: str) -> bytes:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
        f'exec python3 "$HERE/payload/backend.py" {action}\n'
    ).encode()


def compile_tenant_stats_backend(kit: Path, repo: Path) -> dict[str, object]:
    manifest_path = kit / "manifest.json"
    manifest = load_object(manifest_path, "manifest")
    spec = validate(manifest)
    source = git_source(repo, manifest["patch_sha"], spec["writer"]["source_path"])
    archive = writer_zip(source)
    identity = {
        "artifact_id": manifest["id"],
        "patch_sha": manifest["patch_sha"],
        "marker": spec["marker"],
        "target_account": spec["target_account"],
        "target_region": spec["target_region"],
        "table": spec["table"]["name"],
        "function": spec["writer"]["function_name"],
        "role": spec["writer"]["role_name"],
        "rule": spec["schedule"]["rule_name"],
        "source_sha256": sha256(source),
    }
    recipe_sha = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    )
    resource_id = f"tenantstats-{recipe_sha[:12]}"
    output = kit / "lib" / "compiled" / resource_id
    if output.exists() or output.is_symlink():
        fail(f"compiled output already exists: {output}")

    config = {
        "schema_version": 1,
        **identity,
        "resource_id": resource_id,
        "recipe_sha256": recipe_sha,
        "tag_key": TAG_KEY,
        "table": spec["table"],
        "writer": spec["writer"],
        "schedule": spec["schedule"],
        "writer_code_sha256": base64.b64encode(
            hashlib.sha256(archive).digest()
        ).decode(),
        "writer_zip_sha256": sha256(archive),
    }
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    environment = {"Variables": spec["writer"]["environment"]}
    files = {
        "apply.sh": wrapper("apply"),
        "verify.sh": wrapper("verify"),
        "rollback.sh": wrapper("rollback"),
        "payload/backend.py": RUNTIME.encode(),
        "payload/config.json": (
            json.dumps(config, indent=2, sort_keys=True) + "\n"
        ).encode(),
        "payload/trust-policy.json": (
            json.dumps(trust, indent=2, sort_keys=True) + "\n"
        ).encode(),
        "payload/inline-policy.json": (
            json.dumps(policy(spec), indent=2, sort_keys=True) + "\n"
        ).encode(),
        "payload/environment.json": (
            json.dumps(environment, indent=2, sort_keys=True) + "\n"
        ).encode(),
        "payload/source.json": (
            json.dumps(
                {
                    "patch_sha": manifest["patch_sha"],
                    "path": spec["writer"]["source_path"],
                    "sha256": sha256(source),
                    "writer_zip_sha256": sha256(archive),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
        "payload/writer.zip": archive,
    }
    for relative, content in files.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        if relative.endswith(".sh") or relative == "payload/backend.py":
            destination.chmod(0o755)

    inventory = manifest.setdefault("kit_files", {})
    if not isinstance(inventory, dict):
        fail("manifest.kit_files must be an object")
    for relative, content in files.items():
        key = str((Path("lib") / "compiled" / resource_id / relative).as_posix())
        inventory[key] = {"sha256": sha256(content)}
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, manifest_path)
    return {
        "resource_id": resource_id,
        "table": spec["table"]["name"],
        "function": spec["writer"]["function_name"],
        "files": sorted(files),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: _compile_tenant_stats_backend.py <patch-kit> <source-repo>",
            file=sys.stderr,
        )
        return 2
    result = compile_tenant_stats_backend(
        Path(argv[1]).expanduser().resolve(),
        Path(argv[2]).expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
