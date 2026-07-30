# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest


PATCH = Path(__file__).resolve().parents[1]
REPO = PATCH.parents[1]
COMPILER = PATCH / "factory" / "scripts" / "_compile_tenant_stats_backend.py"
PATCH_SHA = "f8b9e14e5f456a24dc8fc597528a7b1b1540a9f3"
ACCOUNT = "111111111111"
REGION = "us-east-1"
MARKER = f"ocpatch:114-tenant-stats-table:{PATCH_SHA}"
SOURCE_PATH = "deploy/lambda/tenant_stats/handler.py"


def manifest() -> dict[str, object]:
    return {
        "id": "114-tenant-stats-table",
        "patch_sha": PATCH_SHA,
        "status": "READY",
        "kit_files": {},
        "tenant_stats_backends": [
            {
                "target_account": ACCOUNT,
                "target_region": REGION,
                "marker": MARKER,
                "table": {
                    "name": "openclaw-tenant-stats",
                    "partition_key": {"name": "id", "type": "S"},
                    "billing_mode": "PAY_PER_REQUEST",
                    "pitr": True,
                },
                "writer": {
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
                    "environment": {
                        "TENANTS_TABLE": "openclaw-tenants",
                        "TENANT_STATS_TABLE": "openclaw-tenant-stats",
                        "ASSETS_BUCKET": "customer-assets-bucket",
                        "ROOTFS_PREFIX": "deployment/rootfs",
                        "STATS_SCAN_SEGMENTS": "8",
                    },
                },
                "schedule": {
                    "rule_name": "openclaw-tenant-stats-schedule",
                    "expression": "rate(1 minute)",
                    "target_id": "TenantStatsWriter",
                    "permission_statement_id": "ocpatch-tenant-stats-schedule",
                },
                "cfn_follow_up": "import the retained table before CDK deployment",
            }
        ],
    }


def compile_kit(tmp_path: Path) -> tuple[Path, Path]:
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "manifest.json").write_text(
        json.dumps(manifest(), indent=2) + "\n", encoding="utf-8"
    )
    result = subprocess.run(
        ["python3", str(COMPILER), str(kit), str(REPO)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    (kit / "REVIEW.json").write_text(
        json.dumps({"kit_fingerprint": "a" * 64}), encoding="utf-8"
    )
    entries = list((kit / "lib" / "compiled").glob("tenantstats-*"))
    assert len(entries) == 1
    return kit, entries[0]


AWS_STUB = r'''#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ["AWS_STUB_STATE"])
log_path = Path(os.environ["AWS_STUB_LOG"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if any(not arg or arg[:1].isspace() for arg in args):
    print("stub rejected empty or leading-space argv", file=sys.stderr)
    raise SystemExit(97)
op = " ".join(args[:2])
failure = (state.get("fail") or {}).get(op)
if failure:
    print(failure, file=sys.stderr)
    raise SystemExit(254)

def save():
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

def opt(name, default=None):
    try:
        return args[args.index(name) + 1]
    except ValueError:
        return default

def as_json(value):
    print(json.dumps(value, separators=(",", ":")))

def missing(kind):
    messages = {
        "ddb": "ResourceNotFoundException: requested resource not found in DescribeTable",
        "iam": "NoSuchEntity: role or policy does not exist",
        "lambda": "ResourceNotFoundException: function or policy not found",
        "events": "ResourceNotFoundException: rule does not exist",
    }
    print(messages[kind], file=sys.stderr)
    raise SystemExit(254)

def file_json(value):
    assert value.startswith("file://")
    return json.loads(Path(value[7:]).read_text())

def tags_from(start):
    result = {}
    for raw in args[start:]:
        if raw.startswith("--"):
            break
        parts = dict(part.split("=", 1) for part in raw.split(","))
        result[parts["Key"]] = parts["Value"]
    return result

if op == "sts get-caller-identity":
    print(state.get("account", "111111111111"))

elif op == "dynamodb describe-table":
    table = state.get("table")
    if table is None:
        missing("ddb")
    as_json({"Table": table})

elif op == "dynamodb create-table":
    name = opt("--table-name")
    arn = f"arn:aws:dynamodb:{opt('--region')}:{state['account']}:table/{name}"
    state["table"] = {
        "TableName": name,
        "TableArn": arn,
        "TableStatus": "ACTIVE",
        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "id", "AttributeType": "S"}],
        "BillingModeSummary": {"BillingMode": opt("--billing-mode")},
    }
    state["table_tags"] = tags_from(args.index("--tags") + 1)
    save()
    as_json({"TableDescription": state["table"]})

elif op == "dynamodb wait":
    if state.get("table") is None:
        missing("ddb")

elif op == "dynamodb list-tags-of-resource":
    as_json(
        {
            "Tags": [
                {"Key": key, "Value": value}
                for key, value in (state.get("table_tags") or {}).items()
            ]
        }
    )

elif op == "dynamodb update-continuous-backups":
    state["pitr"] = "ENABLING" if state.get("pitr_stuck") else "ENABLED"
    save()
    as_json({})

elif op == "dynamodb describe-continuous-backups":
    as_json(
        {
            "ContinuousBackupsDescription": {
                "PointInTimeRecoveryDescription": {
                    "PointInTimeRecoveryStatus": state.get("pitr", "DISABLED")
                }
            }
        }
    )

elif op == "dynamodb get-item":
    as_json({"Item": state["snapshot"]} if state.get("snapshot") else {})

elif op == "iam get-role":
    if state.get("role") is None:
        missing("iam")
    as_json({"Role": state["role"]})

elif op == "iam create-role":
    role_name = opt("--role-name")
    state["role"] = {
        "RoleName": role_name,
        "Arn": f"arn:aws:iam::{state['account']}:role/{role_name}",
        "AssumeRolePolicyDocument": file_json(opt("--assume-role-policy-document")),
        "Tags": [
            {"Key": key, "Value": value}
            for key, value in tags_from(args.index("--tags") + 1).items()
        ],
    }
    save()
    as_json({"Role": state["role"]})

elif op == "iam list-role-tags":
    if state.get("role") is None:
        missing("iam")
    as_json({"Tags": state["role"].get("Tags", [])})

elif op == "iam get-role-policy":
    if state.get("role_policy") is None:
        missing("iam")
    as_json(
        {
            "RoleName": opt("--role-name"),
            "PolicyName": opt("--policy-name"),
            "PolicyDocument": state["role_policy"],
        }
    )

elif op == "iam put-role-policy":
    state["role_policy"] = file_json(opt("--policy-document"))
    save()

elif op == "lambda get-function":
    if state.get("function") is None:
        missing("lambda")
    as_json(
        {
            "Configuration": state["function"],
            "Code": {},
            "Tags": state.get("function_tags", {}),
        }
    )

elif op == "lambda create-function":
    function_name = opt("--function-name")
    code_path = Path(opt("--zip-file")[8:])
    state["function"] = {
        "FunctionName": function_name,
        "FunctionArn": (
            f"arn:aws:lambda:{opt('--region')}:{state['account']}:"
            f"function:{function_name}"
        ),
        "Runtime": opt("--runtime"),
        "Architectures": [opt("--architectures")],
        "Handler": opt("--handler"),
        "Role": opt("--role"),
        "Timeout": int(opt("--timeout")),
        "MemorySize": int(opt("--memory-size")),
        "Environment": file_json(opt("--environment")),
        "CodeSha256": base64.b64encode(
            hashlib.sha256(code_path.read_bytes()).digest()
        ).decode(),
        "State": "Active",
        "LastUpdateStatus": "Successful",
    }
    key, value = opt("--tags").split("=", 1)
    state["function_tags"] = {key: value}
    save()
    as_json(state["function"])

elif op == "lambda put-function-concurrency":
    state["concurrency"] = int(opt("--reserved-concurrent-executions"))
    save()
    as_json({"ReservedConcurrentExecutions": state["concurrency"]})

elif op == "lambda get-function-concurrency":
    as_json({"ReservedConcurrentExecutions": state.get("concurrency")})

elif op == "lambda get-policy":
    if state.get("permission") is None:
        missing("lambda")
    as_json({"Policy": json.dumps({"Statement": [state["permission"]]})})

elif op == "lambda add-permission":
    rule_arn = opt("--source-arn")
    state["permission"] = {
        "Sid": opt("--statement-id"),
        "Effect": "Allow",
        "Principal": {"Service": opt("--principal")},
        "Action": opt("--action"),
        "Resource": state["function"]["FunctionArn"],
        "Condition": {
            "ArnLike": {"AWS:SourceArn": rule_arn},
            "StringEquals": {"AWS:SourceAccount": opt("--source-account")},
        },
    }
    save()
    as_json({"Statement": json.dumps(state["permission"])})

elif op == "lambda remove-permission":
    state.pop("permission", None)
    save()

elif op == "lambda invoke":
    response = Path(args[-1])
    response.write_text(
        json.dumps({"refreshed_at": "2026-07-30T00:00:00Z", "active_tenant_count": 1})
    )
    if state.get("snapshot_after_invoke", True):
        state["snapshot"] = {
            "id": {"S": "current"},
            "refreshed_at": {"S": "2026-07-30T00:00:00Z"},
        }
    save()
    as_json({"StatusCode": 200, "ExecutedVersion": "$LATEST"})

elif op == "events describe-rule":
    if state.get("rule") is None:
        missing("events")
    as_json(state["rule"])

elif op == "events put-rule":
    name = opt("--name")
    state["rule"] = {
        "Name": name,
        "Arn": (
            f"arn:aws:events:{opt('--region')}:{state['account']}:rule/{name}"
        ),
        "ScheduleExpression": opt("--schedule-expression"),
        "State": opt("--state"),
    }
    state["rule_tags"] = tags_from(args.index("--tags") + 1)
    save()
    as_json({"RuleArn": state["rule"]["Arn"]})

elif op == "events list-tags-for-resource":
    as_json(
        {
            "Tags": [
                {"Key": key, "Value": value}
                for key, value in (state.get("rule_tags") or {}).items()
            ]
        }
    )

elif op == "events list-targets-by-rule":
    as_json({"Targets": state.get("targets", [])})

elif op == "events put-targets":
    state["targets"] = json.loads(opt("--targets"))
    save()
    as_json({"FailedEntryCount": 0, "FailedEntries": []})

elif op == "events enable-rule":
    state["rule"]["State"] = "ENABLED"
    save()

elif op == "events disable-rule":
    state["rule"]["State"] = "DISABLED"
    save()

elif op == "events remove-targets":
    wanted = set(json.loads(opt("--ids")))
    state["targets"] = [
        target for target in state.get("targets", []) if target["Id"] not in wanted
    ]
    save()
    as_json({"FailedEntryCount": 0, "FailedEntries": []})

else:
    print(f"unsupported aws stub operation: {op}: {args}", file=sys.stderr)
    raise SystemExit(98)
'''


def stub_environment(
    tmp_path: Path,
    *,
    initial: dict[str, object] | None = None,
) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(AWS_STUB, encoding="utf-8")
    aws.chmod(0o755)
    state = tmp_path / "aws-state.json"
    state.write_text(
        json.dumps({"account": ACCOUNT, **(initial or {})}), encoding="utf-8"
    )
    log = tmp_path / "aws-argv.jsonl"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_STUB_STATE": str(state),
            "AWS_STUB_LOG": str(log),
            "OC_PATCH_ACCOUNT": ACCOUNT,
            "OC_PATCH_REGION": REGION,
            "OC_PATCH_STATE_ROOT": str(tmp_path / "state-root"),
            "OC_PATCH_POLL_SECONDS": "0",
            "OC_PATCH_TIMEOUT_SECONDS": "2",
        }
    )
    return env, state, log


def run_script(entry: Path, name: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(entry / name)],
        cwd=entry,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def argv_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def is_write(argv: list[str]) -> bool:
    return tuple(argv[:2]) in {
        ("dynamodb", "create-table"),
        ("dynamodb", "update-continuous-backups"),
        ("iam", "create-role"),
        ("iam", "put-role-policy"),
        ("lambda", "create-function"),
        ("lambda", "put-function-concurrency"),
        ("lambda", "add-permission"),
        ("lambda", "remove-permission"),
        ("lambda", "invoke"),
        ("events", "put-rule"),
        ("events", "put-targets"),
        ("events", "enable-rule"),
        ("events", "disable-rule"),
        ("events", "remove-targets"),
    }


def test_compiler_packages_patch_sha_and_hashes_every_output(tmp_path):
    kit, entry = compile_kit(tmp_path)
    source = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{PATCH_SHA}:{SOURCE_PATH}"],
        capture_output=True,
        check=True,
    ).stdout
    with zipfile.ZipFile(entry / "payload" / "writer.zip") as archive:
        assert archive.namelist() == ["handler.py"]
        assert archive.read("handler.py") == source

    compiled = {
        str(path.relative_to(kit)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in entry.rglob("*")
        if path.is_file()
    }
    inventory = json.loads((kit / "manifest.json").read_text())["kit_files"]
    assert {name: value["sha256"] for name, value in inventory.items()} == compiled
    assert {"apply.sh", "verify.sh", "rollback.sh"} <= {
        path.name for path in entry.iterdir()
    }

    policy = json.loads((entry / "payload" / "inline-policy.json").read_text())
    statements = {statement["Sid"]: statement for statement in policy["Statement"]}
    assert statements["TenantsRead"]["Action"] == [
        "dynamodb:DescribeTable",
        "dynamodb:Scan",
    ]
    assert statements["StatsReadWrite"]["Action"] == [
        "dynamodb:DescribeTable",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    ]
    assert statements["ManifestRead"]["Action"] == ["s3:GetObject"]
    assert statements["ManifestRead"]["Resource"] == [
        "arn:aws:s3:::customer-assets-bucket/deployment/rootfs/manifest.json"
    ]
    environment = json.loads((entry / "payload" / "environment.json").read_text())
    assert environment == {
        "Variables": manifest()["tenant_stats_backends"][0]["writer"]["environment"]
    }


def test_generated_apply_verify_second_apply_skip_and_rollback(tmp_path):
    _, entry = compile_kit(tmp_path)
    env, state_path, log_path = stub_environment(tmp_path)

    applied = run_script(entry, "apply.sh", env)
    assert applied.returncode == 0, applied.stderr
    assert "APPLIED" in applied.stdout

    verified = run_script(entry, "verify.sh", env)
    assert verified.returncode == 0, verified.stderr
    assert "VERIFIED" in verified.stdout

    before_skip = len(argv_log(log_path))
    skipped = run_script(entry, "apply.sh", env)
    assert skipped.returncode == 0, skipped.stderr
    assert "SKIP" in skipped.stdout
    assert not any(is_write(argv) for argv in argv_log(log_path)[before_skip:])

    calls = argv_log(log_path)
    create = next(argv for argv in calls if argv[:2] == ["lambda", "create-function"])
    assert create[create.index("--runtime") + 1] == "python3.12"
    assert create[create.index("--architectures") + 1] == "arm64"
    assert create[create.index("--handler") + 1] == "handler.lambda_handler"
    assert create[create.index("--timeout") + 1] == "50"
    assert create[create.index("--memory-size") + 1] == "8192"
    rule = next(argv for argv in calls if argv[:2] == ["events", "put-rule"])
    assert rule[rule.index("--schedule-expression") + 1] == "rate(1 minute)"
    marker_arguments = {
        tuple(argv[:2]): argv[argv.index("--tags") + 1]
        for argv in calls
        if tuple(argv[:2])
        in {
            ("dynamodb", "create-table"),
            ("iam", "create-role"),
            ("lambda", "create-function"),
            ("events", "put-rule"),
        }
    }
    assert all(MARKER in argument for argument in marker_arguments.values())
    assert len(marker_arguments) == 4
    assert calls.index(next(a for a in calls if a[:2] == ["events", "put-targets"])) < (
        calls.index(next(a for a in calls if a[:2] == ["lambda", "invoke"]))
    )
    backups = list((tmp_path / "state-root").rglob("backup.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["resources"] == {
        "table": None,
        "role": None,
        "function": None,
        "rule": None,
    }

    rolled_back = run_script(entry, "rollback.sh", env)
    assert rolled_back.returncode == 0, rolled_back.stderr
    state = json.loads(state_path.read_text())
    assert state["rule"]["State"] == "DISABLED"
    assert state["targets"] == []
    assert "permission" not in state
    assert state["table"]["TableName"] == "openclaw-tenant-stats"
    assert state["function"]["FunctionName"] == "openclaw-tenant-stats-writer"
    assert state["role"]["RoleName"] == "openclaw-tenant-stats-writer-role"
    assert not any("delete" in part for argv in argv_log(log_path) for part in argv[:2])
    cleanup = list((tmp_path / "state-root").rglob("manual-cleanup.json"))
    assert len(cleanup) == 1


def test_unmarked_same_name_resource_is_rejected_before_any_write(tmp_path):
    _, entry = compile_kit(tmp_path)
    table = {
        "TableName": "openclaw-tenant-stats",
        "TableArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/openclaw-tenant-stats",
        "TableStatus": "ACTIVE",
        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "id", "AttributeType": "S"}],
        "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
    }
    env, _, log_path = stub_environment(
        tmp_path, initial={"table": table, "table_tags": {}, "pitr": "ENABLED"}
    )
    result = run_script(entry, "apply.sh", env)
    assert result.returncode == 40, result.stderr
    assert not any(is_write(argv) for argv in argv_log(log_path))


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("ThrottlingException: slow down", 41),
        ("opaque endpoint read failure", 46),
        ("AccessDeniedException: denied", 49),
    ),
)
def test_aws_read_errors_are_classified(tmp_path, failure, expected):
    _, entry = compile_kit(tmp_path)
    env, _, _ = stub_environment(
        tmp_path, initial={"fail": {"dynamodb describe-table": failure}}
    )
    result = run_script(entry, "apply.sh", env)
    assert result.returncode == expected, result.stderr


def test_verify_without_owned_anchor_is_44(tmp_path):
    _, entry = compile_kit(tmp_path)
    env, _, _ = stub_environment(tmp_path)
    env.pop("OC_PATCH_STATE_ROOT")
    env["HOME"] = str(tmp_path / "home")
    result = run_script(entry, "verify.sh", env)
    assert result.returncode == 44, result.stderr
    assert list((tmp_path / "home" / ".local" / "state" / "openclaw-patches").rglob(".lock"))


def test_missing_snapshot_after_invoke_is_43(tmp_path):
    _, entry = compile_kit(tmp_path)
    env, _, _ = stub_environment(
        tmp_path, initial={"snapshot_after_invoke": False}
    )
    result = run_script(entry, "apply.sh", env)
    assert result.returncode == 43, result.stderr


def test_pitr_wait_timeout_is_42(tmp_path):
    _, entry = compile_kit(tmp_path)
    env, _, _ = stub_environment(tmp_path, initial={"pitr_stuck": True})
    env["OC_PATCH_TIMEOUT_SECONDS"] = "0"
    result = run_script(entry, "apply.sh", env)
    assert result.returncode == 42, result.stderr


def test_account_or_region_mismatch_is_40_without_aws_calls(tmp_path):
    _, entry = compile_kit(tmp_path)
    env, _, log_path = stub_environment(tmp_path)
    env["OC_PATCH_ACCOUNT"] = "222222222222"
    result = run_script(entry, "apply.sh", env)
    assert result.returncode == 3, result.stderr
    assert argv_log(log_path) == []
