# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Execute generated Lambda apply/verify/rollback scripts against local stubs.

The generation-only suite catches text regressions. These tests cover the boundary it cannot:
the argv seen by AWS, the zip actually uploaded, and the alias/code state after failures,
retries, verification, and rollback.
"""

import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

import pytest


sys.dont_write_bytecode = True

PATCH = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compile_lambda", PATCH / "factory" / "scripts" / "_compile_lambda.py"
)
compiler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compiler)


def _supports_recipes():
    result = subprocess.run(
        ["bash", "-c", "printf '%s' \"${BASH_VERSINFO[0]}\""],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and int(result.stdout or 0) >= 3


recipe_shell_only = pytest.mark.skipif(
    not _supports_recipes(), reason="generated recipes require bash 3+"
)

ACCOUNT = "111111111111"
OTHER_ACCOUNT = "222222222222"
REGION = "ap-southeast-1"
FUNCTION = "fixture-fn"
ALIAS = "live"
PACKAGE_ROOT = "deploy/lambda/api"
TARGET = "core/auth.py"
UNCHANGED_FIRST_PARTY = "core/kept.py"
UNCHANGED_THIRD_PARTY = "aws_lambda_powertools/__init__.py"
VALID_PAYLOAD = {"httpMethod": "GET", "resource": "/__oc_patch_probe"}
EXPECTED_RESPONSE = {"statusCode": 404}
PATCHED_RESPONSE = {"statusCode": 201, "contract": "patched"}
ROLLBACK_RESPONSE = {"statusCode": 404, "contract": "original"}
ROLLBACK_PAYLOAD = {"httpMethod": "GET", "resource": "/__oc_rollback_probe"}
FIXED_ENV = {"FIXED_MODE": "enabled"}
GENERATED_ENV = {"SIGNING_SECRET": "random_base64_32"}
READ_TABLES = ["tenant-table", "job-table"]
ORIGINAL_ENV = {"UNRELATED": "keep-me", "FIXED_MODE": "old"}
ROLE_NAME = "fixture-lambda-role"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/service-role/{ROLE_NAME}"

BASE_TARGET = b"# base auth\n"
PATCHED_TARGET = b"# patched auth\n"
FIRST_PARTY_BYTES = b"# unchanged first party\n"
THIRD_PARTY_BYTES = b"# unchanged third party dependency\n"


STUB_TOOL = r'''#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

command = Path(sys.argv[0]).name
argv = sys.argv[1:]
state_path = Path(os.environ["STUB_STATE"])
state = json.loads(state_path.read_text())
expected_payload = state.get("expected_payload", "")
record = {
    "command": command,
    "argv": argv,
    "payload_in_env": bool(expected_payload) and any(
        expected_payload == value for value in os.environ.values()
    ),
}
with open(os.environ["STUB_LOG"], "a") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\n")


def save():
    state_path.write_text(json.dumps(state, sort_keys=True))


def die(message, code=254):
    save()
    sys.stderr.write(message + "\n")
    raise SystemExit(code)


def has(*words):
    return all(word in argv for word in words)


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def local_path(value):
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return value


def code_sha(path):
    digest = hashlib.sha256(Path(path).read_bytes()).digest()
    return base64.b64encode(digest).decode()


def copy_artifact(source, label):
    sequence = state.get("artifact_sequence", 0) + 1
    state["artifact_sequence"] = sequence
    target = Path(state["artifact_dir"]) / ("%s-%02d.zip" % (label, sequence))
    shutil.copyfile(source, target)
    return str(target)


for argument in argv:
    if argument.strip() == "" or argument != argument.lstrip():
        die("Unknown options: %r" % argument, 252)

if command == "curl":
    source = next((value for value in argv if value.startswith("file://")), None)
    if source is None or "-o" not in argv:
        die("stub curl needs file:// source and -o", 3)
    shutil.copyfile(local_path(source), argv[argv.index("-o") + 1])
    raise SystemExit(0)

if command == "unzip":
    archive = next((value for value in argv if not value.startswith("-")), None)
    if archive is None:
        die("stub unzip needs an archive", 3)
    with zipfile.ZipFile(archive) as package:
        package.extractall(os.getcwd())
    raise SystemExit(0)

if command == "zip":
    try:
        output = argv[argv.index("-qr") + 1]
    except (ValueError, IndexError):
        die("stub zip expects -qr OUTPUT .", 3)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(Path.cwd().rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(Path.cwd()))
    raise SystemExit(0)

if command == "jq":
    raise SystemExit(0)

if command != "aws":
    die("unhandled stub command %s" % command, 3)

if has("sts", "get-caller-identity"):
    print(state["account"])

elif has("lambda", "get-function-configuration"):
    query = opt("--query")
    qualifier = opt("--qualifier")
    if qualifier:
        package = state["versions"].get(qualifier)
        if package is None:
            die("ResourceNotFoundException: version %s" % qualifier)
        code = package["code_sha"]
        revision = "published-" + qualifier
    else:
        code = state["latest_code_sha"]
        revision = state["revision_id"]
    if query == "RevisionId":
        print(revision)
    elif query == "CodeSha256":
        print(code)
    elif query == "Version":
        print(qualifier or "$LATEST")
    elif query is None:
        print(json.dumps({
            "FunctionName": state["function_name"],
            "CodeSha256": code,
            "RevisionId": revision,
            "Role": state["role_arn"],
            "Environment": {"Variables": state["environment"]},
        }, sort_keys=True))
    else:
        die("unhandled configuration query %r" % query, 3)

elif has("lambda", "get-alias"):
    query = opt("--query")
    if query == "FunctionVersion":
        print(state["alias_version"])
    elif query == "RevisionId":
        print(state["alias_revision_id"])
    else:
        die("unhandled alias query %r" % query, 3)

elif has("lambda", "list-event-source-mappings"):
    print("\t".join(state.get("esm_targets", [])))

elif has("lambda", "publish-version"):
    if "--revision-id" in argv:
        if opt("--revision-id") != state["revision_id"]:
            die("PreconditionFailedException: stale RevisionId")
        if opt("--code-sha256") != state["latest_code_sha"]:
            die("PreconditionFailedException: stale CodeSha256")
    version = str(state["next_version"])
    state["next_version"] += 1
    version_zip = copy_artifact(state["latest_zip"], "version-" + version)
    state["versions"][version] = {
        "code_sha": state["latest_code_sha"],
        "zip": version_zip,
    }
    save()
    print(version)

elif has("lambda", "get-function"):
    qualifier = opt("--qualifier")
    if qualifier:
        package = state["versions"].get(qualifier)
        if package is None:
            die("ResourceNotFoundException: version %s" % qualifier)
        path = package["zip"]
    else:
        path = state["latest_zip"]
    print(Path(path).resolve().as_uri())

elif has("lambda", "update-function-code"):
    if state.get("fail_update_revision"):
        die("PreconditionFailedException: function changed under us")
    if opt("--revision-id") != state["revision_id"]:
        die("PreconditionFailedException: stale RevisionId")
    source = opt("--zip-file")
    if not source or not source.startswith("fileb://"):
        die("stub requires --zip-file fileb://...", 252)
    uploaded = copy_artifact(source[len("fileb://"):], "upload")
    state["latest_zip"] = uploaded
    state["latest_code_sha"] = code_sha(uploaded)
    state["uploaded_shas"] = state.get("uploaded_shas", []) + [
        state["latest_code_sha"]
    ]
    state["code_update_wait_pending"] = True
    state["revision_sequence"] += 1
    state["revision_id"] = "rev-%d" % state["revision_sequence"]
    save()
    if opt("--query") == "CodeSha256":
        print(state["latest_code_sha"])
    else:
        print(json.dumps({"CodeSha256": state["latest_code_sha"]}))

elif has("lambda", "update-function-configuration"):
    if state.get("fail_configuration_revision"):
        die("PreconditionFailedException: stale RevisionId")
    if opt("--revision-id") != state["revision_id"]:
        die("PreconditionFailedException: stale RevisionId")
    source = opt("--environment")
    if not source or not source.startswith("file://"):
        die("stub requires --environment file://...", 252)
    environment = json.loads(Path(local_path(source)).read_text())
    state["environment"] = environment["Variables"]
    state["revision_sequence"] += 1
    state["revision_id"] = "rev-%d" % state["revision_sequence"]
    state["configuration_updates"] = state.get("configuration_updates", 0) + 1
    save()
    print("{}")

elif has("lambda", "wait", "function-updated"):
    if state.pop("code_update_wait_pending", False):
        if state.get("inject_concurrent_after_update") and not state.get(
            "concurrent_injected"
        ):
            state["latest_code_sha"] = state["concurrent_code_sha"]
            state["revision_sequence"] += 1
            state["revision_id"] = "rev-%d" % state["revision_sequence"]
            state["concurrent_injected"] = True
        save()

elif has("lambda", "invoke"):
    payload_ref = opt("--payload")
    if not payload_ref or not payload_ref.startswith(("file://", "fileb://")):
        die("payload must travel through a file, never argv or env", 252)
    payload_path = payload_ref.split("://", 1)[1]
    try:
        raw_payload = Path(payload_path).read_text()
        payload = json.loads(raw_payload)
    except (OSError, json.JSONDecodeError) as error:
        die("invalid payload file: %s" % error, 252)
    if any(raw_payload == value for value in os.environ.values()):
        die("payload leaked into the environment", 252)
    qualifier = opt("--function-name").split(":", 1)[1]
    output_path = Path(argv[-1])
    state["payload_paths"] = state.get("payload_paths", []) + [payload_path]
    missing_shape = "httpMethod" not in payload
    forced_failure = (
        state.get("fail_latest_probe")
        and qualifier == "$LATEST"
        and Path(payload_path).name == "verify-payload.json"
    )
    if missing_shape:
        state["last_invoke_error"] = "missing httpMethod"
    elif forced_failure:
        state["last_invoke_error"] = "forced latest probe failure"
        state["latest_probe_failures"] = state.get("latest_probe_failures", 0) + 1
    if missing_shape or forced_failure:
        output_path.write_text(json.dumps({"errorMessage": state["last_invoke_error"]}))
        save()
        print("Unhandled")
    else:
        if qualifier == "$LATEST":
            invoke_sha = state["latest_code_sha"]
        else:
            version = state["alias_version"]
            invoke_sha = state["versions"][version]["code_sha"]
        response = (
            state["original_response"]
            if invoke_sha == state["original_code_sha"]
            else state["patched_response"]
        )
        output_path.write_text(json.dumps(response))
        save()
        print("None")

elif has("lambda", "update-alias"):
    if opt("--revision-id") != state["alias_revision_id"]:
        die("PreconditionFailedException: stale alias RevisionId")
    version = opt("--function-version")
    if version not in state["versions"]:
        die("ResourceNotFoundException: version %s" % version)
    state["alias_version"] = version
    state["alias_revision_sequence"] += 1
    state["alias_revision_id"] = "alias-rev-%d" % state["alias_revision_sequence"]
    save()
    print("{}")

elif has("iam", "get-role-policy"):
    if opt("--role-name") != state["role_name"]:
        die("NoSuchEntity: role not found")
    policy = state["policies"].get(opt("--policy-name"))
    if policy is None:
        die("NoSuchEntity: policy not found")
    print(json.dumps(policy, sort_keys=True))

elif has("iam", "put-role-policy"):
    if state.get("fail_put_policy"):
        die("AccessDenied: put-role-policy denied")
    if opt("--role-name") != state["role_name"]:
        die("NoSuchEntity: role not found")
    source = opt("--policy-document")
    if not source or not source.startswith("file://"):
        die("stub requires --policy-document file://...", 252)
    state["policies"][opt("--policy-name")] = json.loads(
        Path(local_path(source)).read_text()
    )
    state["policy_puts"] = state.get("policy_puts", 0) + 1
    save()
    print("{}")

elif has("iam", "delete-role-policy"):
    if state.get("fail_delete_policy"):
        die("AccessDenied: delete-role-policy denied")
    if opt("--role-name") != state["role_name"]:
        die("NoSuchEntity: role not found")
    policy = opt("--policy-name")
    if policy not in state["policies"]:
        die("NoSuchEntity: policy not found")
    del state["policies"][policy]
    state["policy_deletes"] = state.get("policy_deletes", 0) + 1
    save()
    print("{}")

else:
    die("stub aws: unhandled call %s" % " ".join(argv), 3)
'''


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _code_sha(path):
    digest = hashlib.sha256(path.read_bytes()).digest()
    return base64.b64encode(digest).decode()


def _desired_policy():
    resources = []
    for table in sorted(READ_TABLES):
        arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{table}"
        resources.extend((arn, arn + "/index/*"))
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PatchOwnedDynamoDbRead",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:BatchGetItem",
                ],
                "Resource": resources,
            }
        ],
    }


def _write_live_zip(path):
    files = {
        TARGET: BASE_TARGET,
        UNCHANGED_FIRST_PARTY: FIRST_PARTY_BYTES,
        UNCHANGED_THIRD_PARTY: THIRD_PARTY_BYTES,
        "handler.py": b"# base handler\n",
        "requirements.txt": b"aws-lambda-powertools\n",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for relative, content in files.items():
            package.writestr(relative, content)
    return files


def _case(
    tmp_path,
    verify_payload=None,
    verify_expect=None,
    rollback_verify_payload=None,
    rollback_verify_expect=None,
):
    payload = VALID_PAYLOAD if verify_payload is None else verify_payload
    expected = EXPECTED_RESPONSE if verify_expect is None else verify_expect
    repo = tmp_path / "repo"
    source = repo / PACKAGE_ROOT / TARGET
    source.parent.mkdir(parents=True)
    source.write_bytes(BASE_TARGET)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "lambda-test@example.invalid")
    _git(repo, "config", "user.name", "Lambda Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    source.write_bytes(PATCHED_TARGET)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "patch")
    patch = _git(repo, "rev-parse", "HEAD")

    kit = tmp_path / "kit"
    kit.mkdir()
    manifest = {
        "id": "fixture-lambda",
        "base_sha": base,
        "patch_sha": patch,
        "status": "READY",
        "kit_files": {},
        "paths": {
            f"{PACKAGE_ROOT}/{TARGET}": {
                "change": "M",
                "layer": "C-lambda",
                "artifact_status": "SHIPPED",
                "base_sha256": hashlib.sha256(BASE_TARGET).hexdigest(),
                "patch_sha256": hashlib.sha256(PATCHED_TARGET).hexdigest(),
                "operations": [{"class": "AUTO_CLI"}],
            }
        },
        "lambda_functions": [
            {
                "function_name": FUNCTION,
                "package_root": PACKAGE_ROOT,
                "alias": ALIAS,
                "verify_payload": payload,
                "verify_expect": expected,
                "environment_updates": FIXED_ENV,
                "generated_environment": GENERATED_ENV,
                "iam_read_tables": READ_TABLES,
                "target_account": ACCOUNT,
                "target_region": REGION,
            }
        ],
    }
    if rollback_verify_payload is not None:
        manifest["lambda_functions"][0]["rollback_verify_payload"] = (
            rollback_verify_payload
        )
    if rollback_verify_expect is not None:
        manifest["lambda_functions"][0]["rollback_verify_expect"] = (
            rollback_verify_expect
        )
    (kit / "manifest.json").write_text(json.dumps(manifest))
    result = compiler.compile_lambda_kit(str(kit), str(repo))
    (kit / "REVIEW.json").write_text(json.dumps({"kit_fingerprint": "a" * 64}))
    policy_name = compiler._policy_name(manifest, manifest["lambda_functions"][0])

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for command in ("aws", "curl", "unzip", "zip", "jq"):
        stub = bindir / command
        stub.write_text(STUB_TOOL)
        stub.chmod(0o755)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    original_zip = artifact_dir / "original.zip"
    live_files = _write_live_zip(original_zip)
    original_sha = _code_sha(original_zip)
    state_path = tmp_path / "stub-state.json"
    state_path.write_text(
        json.dumps(
            {
                "account": ACCOUNT,
                "function_name": FUNCTION,
                "artifact_dir": str(artifact_dir),
                "artifact_sequence": 0,
                "latest_zip": str(original_zip),
                "latest_code_sha": original_sha,
                "original_code_sha": original_sha,
                "revision_id": "rev-1",
                "revision_sequence": 1,
                "alias_version": "7",
                "alias_revision_id": "alias-rev-1",
                "alias_revision_sequence": 1,
                "role_arn": ROLE_ARN,
                "role_name": ROLE_NAME,
                "environment": ORIGINAL_ENV,
                "policies": {},
                "versions": {
                    "7": {"code_sha": original_sha, "zip": str(original_zip)}
                },
                "next_version": 8,
                "expected_payload": json.dumps(payload),
                "original_response": (
                    ROLLBACK_RESPONSE
                    if rollback_verify_expect is not None
                    else expected
                ),
                "patched_response": expected,
                "uploaded_shas": [],
                "payload_paths": [],
                "esm_targets": [],
            }
        )
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "STUB_LOG": str(tmp_path / "calls.jsonl"),
            "STUB_STATE": str(state_path),
            "OC_PATCH_REGION": REGION,
            "OC_PATCH_STATE_ROOT": str(tmp_path / "recipe-state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return {
        "kit": kit,
        "result": result,
        "env": env,
        "log": tmp_path / "calls.jsonl",
        "state": state_path,
        "original_sha": original_sha,
        "live_files": live_files,
        "payload": payload,
        "policy_name": policy_name,
        "original_environment": ORIGINAL_ENV,
    }


def _run(case, stage):
    script = (
        case["kit"]
        / "lib"
        / "compiled"
        / case["result"]["resource_id"]
        / f"{stage}.sh"
    )
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=case["env"],
        timeout=30,
    )


def _state(case):
    return json.loads(case["state"].read_text())


def _write_state(case, state):
    case["state"].write_text(json.dumps(state))


def _set_state(case, **updates):
    state = _state(case)
    state.update(updates)
    _write_state(case, state)


def _calls(case, command=None):
    if not case["log"].exists():
        return []
    calls = [json.loads(line) for line in case["log"].read_text().splitlines()]
    if command is not None:
        calls = [call for call in calls if call["command"] == command]
    return calls


def _assert_ok(run):
    assert run.returncode == 0, run.stdout + run.stderr


def _recipe_state_dir(case):
    manifest = json.loads((case["kit"] / "manifest.json").read_text())
    review = json.loads((case["kit"] / "REVIEW.json").read_text())
    return (
        Path(case["env"]["OC_PATCH_STATE_ROOT"])
        / ACCOUNT
        / REGION
        / manifest["id"]
        / manifest["patch_sha"]
        / review["kit_fingerprint"]
        / case["result"]["resource_id"]
    )


def _replace_live_zip(case, entries):
    path = Path(_state(case)["latest_zip"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as package:
            for entry, content in entries:
                package.writestr(entry, content)
    state = _state(case)
    state["latest_code_sha"] = _code_sha(path)
    state["original_code_sha"] = state["latest_code_sha"]
    state["versions"]["7"] = {"code_sha": state["latest_code_sha"], "zip": str(path)}
    _write_state(case, state)


def _zip_entry(name, file_type):
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = (file_type | 0o644) << 16
    return entry


@recipe_shell_only
def test_no_aws_call_receives_a_blank_or_space_prefixed_argument(tmp_path):
    case = _case(tmp_path)
    run = _run(case, "apply")
    _assert_ok(run)
    aws_calls = _calls(case, "aws")
    assert aws_calls
    for call in aws_calls:
        for argument in call["argv"]:
            assert argument.strip(), call
            assert argument == argument.lstrip(), call


@recipe_shell_only
def test_overlay_replaces_only_the_target_file(tmp_path):
    case = _case(tmp_path)
    run = _run(case, "apply")
    _assert_ok(run)
    state = _state(case)
    with zipfile.ZipFile(state["latest_zip"]) as package:
        assert package.read(TARGET) == PATCHED_TARGET
        assert package.read(UNCHANGED_FIRST_PARTY) == FIRST_PARTY_BYTES
        assert package.read(UNCHANGED_THIRD_PARTY) == THIRD_PARTY_BYTES
        assert set(case["live_files"]).issubset(package.namelist())
    commands = {call["command"] for call in _calls(case)}
    assert {"aws", "curl", "zip"}.issubset(commands)
    assert "unzip" not in commands


@recipe_shell_only
def test_every_stage_calls_sts_even_with_an_account_override(tmp_path):
    case = _case(tmp_path)
    case["env"]["OC_PATCH_ACCOUNT"] = ACCOUNT
    for stage in ("apply", "verify", "rollback"):
        before = len(_calls(case))
        run = _run(case, stage)
        _assert_ok(run)
        calls = _calls(case)[before:]
        sts = [
            call
            for call in calls
            if call["command"] == "aws"
            and {"sts", "get-caller-identity"}.issubset(call["argv"])
        ]
        assert len(sts) == 1, (stage, calls)


@recipe_shell_only
@pytest.mark.parametrize("stage", ["apply", "verify", "rollback"])
def test_every_stage_rejects_a_runtime_region_other_than_the_kit_target(
    tmp_path, stage
):
    case = _case(tmp_path)
    case["env"]["OC_PATCH_REGION"] = "us-west-2"
    run = _run(case, stage)
    assert run.returncode == 3, run.stdout + run.stderr
    assert not _calls(case), "target-region rejection must happen before AWS calls"


@recipe_shell_only
@pytest.mark.parametrize(
    "override,sts_account",
    [
        (OTHER_ACCOUNT, ACCOUNT),
        (ACCOUNT, OTHER_ACCOUNT),
        (None, OTHER_ACCOUNT),
    ],
)
def test_apply_compares_sts_override_and_kit_accounts(
    tmp_path, override, sts_account
):
    case = _case(tmp_path)
    if override is not None:
        case["env"]["OC_PATCH_ACCOUNT"] = override
    _set_state(case, account=sts_account)
    run = _run(case, "apply")
    assert run.returncode == 3, run.stdout + run.stderr
    calls = _calls(case)
    assert any(
        call["command"] == "aws"
        and {"sts", "get-caller-identity"}.issubset(call["argv"])
        for call in calls
    )
    assert not any("lambda" in call["argv"] for call in calls)


@recipe_shell_only
@pytest.mark.parametrize(
    "entries",
    [
        [("/tmp/oc-patch-escape", b"escape")],
        [("../../oc-patch-escape", b"escape")],
        [("dir\\escape", b"escape")],
        [(_zip_entry("link", stat.S_IFLNK), b"../../oc-patch-escape")],
        [(_zip_entry("fifo", stat.S_IFIFO), b"")],
        [("duplicate", b"one"), ("duplicate", b"two")],
        [("parent", b"file"), ("parent/child", b"child")],
    ],
    ids=[
        "absolute",
        "parent-traversal",
        "backslash",
        "symlink",
        "special-file",
        "duplicate",
        "parent-child-conflict",
    ],
)
def test_malicious_live_zip_is_rejected_before_any_write_outside_pkg(
    tmp_path, entries
):
    case = _case(tmp_path)
    _replace_live_zip(case, entries)
    outside = tmp_path / "oc-patch-escape"
    run = _run(case, "apply")
    assert run.returncode == 49, run.stdout + run.stderr
    assert not outside.exists()
    assert not (_recipe_state_dir(case) / "work" / "pkg").exists()
    assert not any(
        {
            "update-function-code",
            "update-alias",
        }.intersection(call["argv"])
        for call in _calls(case, "aws")
    )


def test_overlay_rejects_a_symlinked_parent_without_writing_through_it(tmp_path):
    case = _case(tmp_path)
    compiled = (
        case["kit"] / "lib" / "compiled" / case["result"]["resource_id"]
    )
    helper = compiled / "lambda-state.py"
    root = tmp_path / "pkg"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    payload = tmp_path / "overlay.json"
    payload.write_text(
        json.dumps(
            {
                "base_hashes": {"linked/escape.py": None},
                "patch_hashes": {
                    "linked/escape.py": hashlib.sha256(b"patched").hexdigest()
                },
                "sources": {
                    "linked/escape.py": base64.b64encode(b"patched").decode()
                },
            }
        )
    )
    run = subprocess.run(
        [sys.executable, str(helper), "apply-overlay", str(root), str(payload)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 49, run.stdout + run.stderr
    assert not (outside / "escape.py").exists()


def test_extractor_rejects_a_symlinked_destination_parent(tmp_path):
    case = _case(tmp_path)
    compiled = (
        case["kit"] / "lib" / "compiled" / case["result"]["resource_id"]
    )
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("handler.py", b"safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    run = subprocess.run(
        [
            sys.executable,
            str(compiled / "lambda-state.py"),
            "safe-extract",
            str(archive),
            str(linked / "pkg"),
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 49, run.stdout + run.stderr
    assert not (outside / "pkg").exists()


@recipe_shell_only
def test_probe_payload_travels_by_file_not_argv_or_environment(tmp_path):
    case = _case(tmp_path)
    run = _run(case, "apply")
    _assert_ok(run)
    expected = json.dumps(case["payload"])
    invokes = [
        call
        for call in _calls(case, "aws")
        if "lambda" in call["argv"] and "invoke" in call["argv"]
    ]
    assert len(invokes) == 4
    qualifiers = [
        call["argv"][call["argv"].index("--function-name") + 1].split(":", 1)[1]
        for call in invokes
    ]
    assert qualifiers == ["$LATEST", ALIAS, "$LATEST", ALIAS]
    for call in invokes:
        assert not call["payload_in_env"], call
        assert expected not in call["argv"], call
        payload_ref = call["argv"][call["argv"].index("--payload") + 1]
        assert payload_ref.startswith(("file://", "fileb://")), call
        payload_path = Path(payload_ref.split("://", 1)[1])
        assert json.loads(payload_path.read_text()) == case["payload"]


@recipe_shell_only
def test_revision_failure_does_not_move_the_alias(tmp_path):
    case = _case(tmp_path)
    _set_state(case, fail_update_revision=True)
    run = _run(case, "apply")
    assert run.returncode == 40, run.stdout + run.stderr
    assert _state(case)["alias_version"] == "7"
    aws_calls = _calls(case, "aws")
    updates = [call for call in aws_calls if "update-function-code" in call["argv"]]
    assert len(updates) == 1
    assert "--revision-id" in updates[0]["argv"]
    assert not any("update-alias" in call["argv"] for call in aws_calls)


@recipe_shell_only
def test_latest_probe_failure_does_not_move_the_alias(tmp_path):
    case = _case(tmp_path)
    _set_state(case, fail_latest_probe=True)
    run = _run(case, "apply")
    assert run.returncode == 43, run.stdout + run.stderr
    state = _state(case)
    assert state["alias_version"] == "7"
    assert state["latest_code_sha"] != case["original_sha"]
    assert state["latest_probe_failures"] == 1
    assert not any(
        "update-alias" in call["argv"] for call in _calls(case, "aws")
    )


@recipe_shell_only
def test_apply_verify_skip_and_rollback_restore_code_and_alias(tmp_path):
    case = _case(tmp_path)
    apply_run = _run(case, "apply")
    _assert_ok(apply_run)
    assert "APPLIED" in apply_run.stdout
    patched = _state(case)
    assert patched["latest_code_sha"] != case["original_sha"]
    assert patched["alias_version"] != "7"
    assert patched["environment"]["UNRELATED"] == "keep-me"
    assert patched["environment"]["FIXED_MODE"] == "enabled"
    assert "SIGNING_SECRET" in patched["environment"]
    assert patched["policies"][case["policy_name"]] == _desired_policy()

    verify_run = _run(case, "verify")
    _assert_ok(verify_run)
    assert "VERIFIED" in verify_run.stdout

    calls_before_skip = len(_calls(case, "aws"))
    second_apply = _run(case, "apply")
    _assert_ok(second_apply)
    assert "SKIP" in second_apply.stdout
    calls_after_skip = _calls(case, "aws")[calls_before_skip:]
    assert not any("update-function-code" in call["argv"] for call in calls_after_skip)
    assert not any("publish-version" in call["argv"] for call in calls_after_skip)
    assert not any(
        "update-function-configuration" in call["argv"] for call in calls_after_skip
    )
    assert not any("put-role-policy" in call["argv"] for call in calls_after_skip)

    rollback_run = _run(case, "rollback")
    _assert_ok(rollback_run)
    assert "ROLLED_BACK" in rollback_run.stdout
    restored = _state(case)
    assert restored["latest_code_sha"] == case["original_sha"]
    assert restored["alias_version"] == "7"
    assert restored["environment"] == case["original_environment"]
    assert case["policy_name"] not in restored["policies"]
    updates = [
        call
        for call in _calls(case, "aws")
        if "update-function-configuration" in call["argv"]
    ]
    assert len(updates) == 2
    assert all("--revision-id" in call["argv"] for call in updates)
    state_dir = _recipe_state_dir(case)
    metadata = json.loads((state_dir / "backup.meta").read_text())
    assert metadata["code_sha256"] == case["original_sha"]
    assert metadata["backup_zip_sha256"] == hashlib.sha256(
        (state_dir / "backup.zip").read_bytes()
    ).hexdigest()
    assert json.loads((state_dir / "rollback-expect-latest.json").read_text()) == (
        EXPECTED_RESPONSE
    )


@recipe_shell_only
def test_environment_preserves_unrelated_keys_and_secures_state(tmp_path):
    case = _case(tmp_path)
    run = _run(case, "apply")
    _assert_ok(run)
    state = _state(case)
    assert state["environment"]["UNRELATED"] == ORIGINAL_ENV["UNRELATED"]
    assert state["environment"]["FIXED_MODE"] == FIXED_ENV["FIXED_MODE"]

    state_dir = _recipe_state_dir(case)
    assert state_dir.stat().st_mode & 0o777 == 0o700
    backup = json.loads((state_dir / "environment-backup.json").read_text())
    assert set(backup) == {"FIXED_MODE", "SIGNING_SECRET"}
    merged = json.loads((state_dir / "merged-environment.json").read_text())
    assert merged["Variables"]["UNRELATED"] == ORIGINAL_ENV["UNRELATED"]
    for name in (
        "environment-backup.json",
        "merged-environment.json",
        "generated-hashes.json",
    ):
        assert (state_dir / name).stat().st_mode & 0o777 == 0o600


@recipe_shell_only
def test_stale_configuration_revision_does_not_update_code_or_alias(tmp_path):
    case = _case(tmp_path)
    _set_state(case, fail_configuration_revision=True)
    run = _run(case, "apply")
    assert run.returncode == 40, run.stdout + run.stderr
    state = _state(case)
    assert state["latest_code_sha"] == case["original_sha"]
    assert state["alias_version"] == "7"
    assert not any(
        "update-function-code" in call["argv"] for call in _calls(case, "aws")
    )
    assert not any("update-alias" in call["argv"] for call in _calls(case, "aws"))


@recipe_shell_only
def test_existing_different_iam_policy_is_rejected_before_any_update(tmp_path):
    case = _case(tmp_path)
    state = _state(case)
    state["policies"][case["policy_name"]] = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
    }
    _write_state(case, state)
    run = _run(case, "apply")
    assert run.returncode == 49, run.stdout + run.stderr
    state = _state(case)
    assert state["environment"] == ORIGINAL_ENV
    assert state["latest_code_sha"] == case["original_sha"]
    assert state["alias_version"] == "7"


@recipe_shell_only
def test_iam_failure_does_not_update_code_or_alias(tmp_path):
    case = _case(tmp_path)
    _set_state(case, fail_put_policy=True)
    run = _run(case, "apply")
    assert run.returncode == 49, run.stdout + run.stderr
    state = _state(case)
    assert state["latest_code_sha"] == case["original_sha"]
    assert state["alias_version"] == "7"
    assert not any(
        "update-function-code" in call["argv"] for call in _calls(case, "aws")
    )


@recipe_shell_only
def test_verify_detects_environment_and_iam_drift_before_invoke(tmp_path):
    env_case = _case(tmp_path / "environment")
    _assert_ok(_run(env_case, "apply"))
    state = _state(env_case)
    state["environment"]["FIXED_MODE"] = "drifted"
    _write_state(env_case, state)
    invokes_before = sum(
        "invoke" in call["argv"] for call in _calls(env_case, "aws")
    )
    verify = _run(env_case, "verify")
    assert verify.returncode == 40, verify.stdout + verify.stderr
    invokes_after = sum(
        "invoke" in call["argv"] for call in _calls(env_case, "aws")
    )
    assert invokes_after == invokes_before

    generated_case = _case(tmp_path / "generated")
    _assert_ok(_run(generated_case, "apply"))
    state = _state(generated_case)
    state["environment"]["SIGNING_SECRET"] = "replacement"
    _write_state(generated_case, state)
    verify = _run(generated_case, "verify")
    assert verify.returncode == 40, verify.stdout + verify.stderr

    iam_case = _case(tmp_path / "iam")
    _assert_ok(_run(iam_case, "apply"))
    state = _state(iam_case)
    state["policies"][iam_case["policy_name"]]["Statement"][0]["Action"].append(
        "dynamodb:PutItem"
    )
    _write_state(iam_case, state)
    verify = _run(iam_case, "verify")
    assert verify.returncode == 49, verify.stdout + verify.stderr


@recipe_shell_only
def test_generated_secret_never_enters_stdout_or_argv(tmp_path):
    case = _case(tmp_path)
    run = _run(case, "apply")
    _assert_ok(run)
    secret = _state(case)["environment"]["SIGNING_SECRET"]
    assert len(base64.b64decode(secret)) == 32
    assert secret not in run.stdout
    assert secret not in run.stderr
    assert all(
        secret not in argument
        for call in _calls(case)
        for argument in call["argv"]
    )


@recipe_shell_only
def test_rollback_leaves_preexisting_identical_policy(tmp_path):
    case = _case(tmp_path)
    state = _state(case)
    state["policies"][case["policy_name"]] = _desired_policy()
    _write_state(case, state)
    _assert_ok(_run(case, "apply"))
    _assert_ok(_run(case, "rollback"))
    state = _state(case)
    assert state["policies"][case["policy_name"]] == _desired_policy()
    assert state.get("policy_deletes", 0) == 0


@recipe_shell_only
def test_iam_delete_failure_is_not_swallowed(tmp_path):
    case = _case(tmp_path)
    _assert_ok(_run(case, "apply"))
    patched_sha = _state(case)["latest_code_sha"]
    _set_state(case, fail_delete_policy=True)
    rollback = _run(case, "rollback")
    assert rollback.returncode == 49, rollback.stdout + rollback.stderr
    state = _state(case)
    assert case["policy_name"] in state["policies"]
    assert state["latest_code_sha"] == patched_sha
    assert state["alias_version"] != "7"
    assert state["environment"] == ORIGINAL_ENV


@recipe_shell_only
def test_wrong_probe_payload_shape_is_caught_before_alias_move(tmp_path):
    case = _case(tmp_path, verify_payload={"version": "2.0", "routeKey": "GET /"})
    run = _run(case, "apply")
    assert run.returncode == 43, run.stdout + run.stderr
    state = _state(case)
    assert state["last_invoke_error"] == "missing httpMethod"
    assert state["alias_version"] == "7"
    assert not any(
        "update-alias" in call["argv"] for call in _calls(case, "aws")
    )


@recipe_shell_only
def test_concurrent_code_after_wait_is_never_claimed_or_published(tmp_path):
    case = _case(tmp_path)
    concurrent_sha = base64.b64encode(hashlib.sha256(b"concurrent").digest()).decode()
    _set_state(
        case,
        inject_concurrent_after_update=True,
        concurrent_code_sha=concurrent_sha,
    )
    run = _run(case, "apply")
    assert run.returncode == 40, run.stdout + run.stderr
    state = _state(case)
    assert state["latest_code_sha"] == concurrent_sha
    assert state["uploaded_shas"]
    expected_sha = state["uploaded_shas"][0]
    assert expected_sha != concurrent_sha
    state_dir = _recipe_state_dir(case)
    assert not (state_dir / "applied.sha256").exists()
    publish_calls = [
        call
        for call in _calls(case, "aws")
        if "publish-version" in call["argv"]
    ]
    assert len(publish_calls) == 1, "only the pre-update backup anchor may publish"


@recipe_shell_only
def test_incomplete_backup_metadata_stops_before_any_live_write(tmp_path):
    case = _case(tmp_path)
    state_dir = _recipe_state_dir(case)
    state_dir.mkdir(parents=True)
    (state_dir / "backup.meta").write_text('{"schema_version":1}\n')

    run = _run(case, "apply")
    assert run.returncode == 44, run.stdout + run.stderr
    writes = {
        "publish-version",
        "update-function-configuration",
        "put-role-policy",
        "update-function-code",
        "update-alias",
    }
    assert not any(
        writes.intersection(call["argv"]) for call in _calls(case, "aws")
    )


@recipe_shell_only
def test_rollback_uses_old_contract_when_patch_behavior_differs(tmp_path):
    case = _case(
        tmp_path,
        verify_expect=PATCHED_RESPONSE,
        rollback_verify_payload=ROLLBACK_PAYLOAD,
        rollback_verify_expect=ROLLBACK_RESPONSE,
    )
    _assert_ok(_run(case, "apply"))
    rollback = _run(case, "rollback")
    _assert_ok(rollback)
    assert _state(case)["latest_code_sha"] == case["original_sha"]
    state_dir = _recipe_state_dir(case)
    assert json.loads((state_dir / "rollback-expect-latest.json").read_text()) == (
        ROLLBACK_RESPONSE
    )
    rollback_invokes = [
        call
        for call in _calls(case, "aws")
        if "invoke" in call["argv"]
        and call["argv"][call["argv"].index("--payload") + 1].endswith(
            "rollback-verify-payload.json"
        )
    ]
    assert len(rollback_invokes) >= 4


@recipe_shell_only
def test_review_fingerprint_gets_a_distinct_state_directory(tmp_path):
    case = _case(tmp_path)
    _assert_ok(_run(case, "apply"))
    first_state = _recipe_state_dir(case)
    assert (first_state / "complete").exists()

    (case["kit"] / "REVIEW.json").write_text(
        json.dumps({"kit_fingerprint": "b" * 64})
    )
    second_state = _recipe_state_dir(case)
    verify = _run(case, "verify")
    assert verify.returncode == 44, verify.stdout + verify.stderr
    assert second_state != first_state
    assert not (second_state / "complete").exists()
