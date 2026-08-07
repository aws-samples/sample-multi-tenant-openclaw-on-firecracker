#!/usr/bin/env python3
"""Bound command context and shell-free execution for claw-patch-v2."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from _patch_model import (
    JsonObject,
    PatchError,
    canonical_bytes,
    file_sha256,
    fingerprint,
    interpreter_name,
    resolve_placeholders,
    selected_operation_order,
)

RUNNER_VERSION = "2.0.0-mvp1"
DEFAULT_STATE_EXIT_CODES = {
    "0": "TARGET",
    "10": "BASE",
    "11": "ABSENT",
    "12": "DRIFT",
    "13": "UNKNOWN",
}
BACKUP_METADATA_FIELDS = {
    "schema_version",
    "target_sha256",
    "locator_sha256",
    "version_sha256",
    "backup_sha256",
}
TARGET_IDENTITY_FIELDS = {"account", "region"}
ACCEPTANCE_EVIDENCE_FIELDS = {
    "schema_version",
    "check_id",
    "challenge_sha256",
    "proofs",
}
ACCEPTANCE_PROOF_FIELDS = {
    "id",
    "observation_sha256",
    "evidence_sha256",
}
ACCEPTANCE_CHALLENGE_ENV = "CLAW_PATCH_ACCEPTANCE_CHALLENGE"
AWS_IDENTITY_ENV_NAMES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_CA_BUNDLE",
    "AWS_CONFIG_FILE",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_DEFAULT_PROFILE",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
    "AWS_ENDPOINT_URL_STS",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_SDK_LOAD_CONFIG",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_STS_REGIONAL_ENDPOINTS",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT_RE = re.compile(r"^[0-9]{12}$")
REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$")
SENSITIVE_RESOURCE_RE = re.compile(
    r"(?:^|[._-])(?:credential|password|private[_-]?key|secret|token)"
    r"(?:$|[._-])",
    re.IGNORECASE,
)


def runner_fingerprint() -> str:
    script_dir = Path(__file__).resolve().parent
    paths = [
        script_dir / "_patch_context.py",
        script_dir / "_patch_model.py",
        script_dir / "_patch_runtime.py",
        script_dir / "_patch_store.py",
        script_dir / "patchctl.py",
    ]
    inventory = {path.name: file_sha256(path) for path in paths}
    return fingerprint(inventory)


def redacted_resources(resources: JsonObject) -> JsonObject:
    redacted: JsonObject = {}
    for key, value in sorted(resources.items()):
        redacted[key] = {
            "type": type(value).__name__,
            "sha256": hashlib.sha256(canonical_bytes(value)).hexdigest(),
            **({} if SENSITIVE_RESOURCE_RE.search(key) else {"value": value}),
        }
    return redacted


def command_context(
    manifest: JsonObject,
    environment: JsonObject,
    kit_dir: Path,
    run_dir: Path,
) -> JsonObject:
    return {
        "environment": environment,
        "patch": manifest["patch"],
        "kit_dir": str(kit_dir),
        "run_dir": str(run_dir),
    }


def _resolve_command(
    command: JsonObject,
    context: JsonObject,
    kit_dir: Path,
) -> JsonObject:
    argv = [resolve_placeholders(item, context) for item in command["argv"]]
    cwd_value = resolve_placeholders(command.get("cwd", str(kit_dir)), context)
    cwd = Path(cwd_value)
    if not cwd.is_absolute():
        cwd = kit_dir / cwd
    try:
        cwd = cwd.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PatchError("command working directory does not exist") from exc
    if not cwd.is_dir():
        raise PatchError("command working directory is not a directory")

    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    for name in command.get("inherit_env", []):
        if name not in os.environ:
            raise PatchError(
                f"required inherited environment variable is missing: {name}"
            )
        environment[name] = os.environ[name]
    for name, value in command.get("env", {}).items():
        environment[name] = resolve_placeholders(value, context)
    return {
        "argv": argv,
        "cwd": cwd,
        "env": environment,
        "immutable_inputs": [
            resolve_placeholders(item, context)
            for item in command.get("immutable_inputs", [])
        ],
        "timeout_seconds": command.get("timeout_seconds", 300),
    }


def _assert_registered_read_only(resolved: JsonObject) -> None:
    argv = resolved["argv"]
    executable = Path(argv[0]).name
    if executable == "aws":
        if argv[1:3] != ["sts", "get-caller-identity"]:
            raise PatchError("plan-time AWS command is not registered read-only")
        return
    interpreter = interpreter_name(argv[0])
    if not interpreter or len(argv) < 3:
        raise PatchError("plan-time command is not a registered read-only handler")
    script_path = _immutable_input(argv[1], resolved["cwd"])
    script = script_path.name
    allowed = {
        "aws_probe.py": {"state", "verify"},
        "manual_gate.py": {"state", "verify"},
        "fake_handler.py": {
            "check",
            "identity",
            "identity-config",
            "identity-env",
            "identity-no-token",
        },
    }
    trusted_paths = {
        "aws_probe.py": Path(__file__).resolve().parent / "aws_probe.py",
        "manual_gate.py": Path(__file__).resolve().parent / "manual_gate.py",
        "fake_handler.py": Path(__file__).resolve().parent.parent
        / "test"
        / "fake_handler.py",
    }
    trusted = trusted_paths.get(script)
    if (
        trusted is None
        or not trusted.is_file()
        or file_sha256(script_path) != file_sha256(trusted)
    ):
        raise PatchError("plan-time handler does not match the trusted registry")
    if argv[2] not in allowed.get(script, set()):
        raise PatchError("plan-time command is not a registered read-only action")


def run_command(
    command: JsonObject,
    context: JsonObject,
    kit_dir: Path,
    *,
    metadata: bool = False,
    target_identity: bool = False,
    environment_source: JsonObject | None = None,
    acceptance: JsonObject | None = None,
    read_only: bool = False,
) -> JsonObject:
    """Run an argv command without a shell and return only redacted metadata."""

    resolved = _resolve_command(command, context, kit_dir)
    if environment_source is not None:
        source = _resolve_command(environment_source, context, kit_dir)
        probe_cwd = resolved["cwd"]
        probe_executable = _executable_identity(resolved, {})
        resolved["argv"][0] = probe_executable["path"]
        if interpreter_name(probe_executable["path"]):
            script = _immutable_input(resolved["argv"][1], probe_cwd)
            resolved["argv"][1] = str(script)
        phase_selectors = (
            set(environment_source.get("env", {}))
            | set(environment_source.get("inherit_env", []))
        )
        probe_selectors = set(command.get("env", {})) | set(
            command.get("inherit_env", [])
        )
        explicit_selectors = (phase_selectors & probe_selectors) | (
            phase_selectors & AWS_IDENTITY_ENV_NAMES
        )
        for name in explicit_selectors:
            resolved["env"].pop(name, None)
            if name in source["env"]:
                resolved["env"][name] = source["env"][name]
        resolved["cwd"] = source["cwd"]
    acceptance_challenge = None
    if acceptance is not None:
        acceptance_challenge = secrets.token_hex(32)
        resolved["env"][ACCEPTANCE_CHALLENGE_ENV] = acceptance_challenge
    if target_identity or read_only:
        _assert_registered_read_only(resolved)
    started = time.monotonic()
    try:
        result = subprocess.run(
            resolved["argv"],
            cwd=resolved["cwd"],
            env=resolved["env"],
            capture_output=True,
            timeout=resolved["timeout_seconds"],
            check=False,
        )
    except FileNotFoundError as exc:
        raise PatchError("command executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PatchError(
            "command timed out; live state is UNKNOWN until re-probed"
        ) from exc

    command_result = {
        "command_fingerprint": fingerprint(command),
        "returncode": result.returncode,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout_bytes": len(result.stdout),
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_bytes": len(result.stderr),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
    }
    if metadata and result.returncode == 0:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PatchError(
                "backup command did not return valid JSON metadata"
            ) from exc
        validate_backup_metadata(value)
        command_result["metadata"] = value
    if target_identity and result.returncode == 0:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PatchError("target probe did not return valid JSON") from exc
        validate_target_identity(value)
        command_result["target_identity"] = value
    if acceptance is not None and result.returncode == 0:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PatchError(
                f"acceptance {acceptance['id']} did not return valid JSON evidence"
            ) from exc
        validate_acceptance_evidence(value, acceptance, acceptance_challenge)
        command_result["acceptance_evidence"] = value
    return command_result


def validate_backup_metadata(value: object) -> None:
    if not isinstance(value, dict) or set(value) != BACKUP_METADATA_FIELDS:
        raise PatchError("backup metadata has invalid fields")
    if value["schema_version"] != 1:
        raise PatchError("backup metadata schema_version must be 1")
    for field in BACKUP_METADATA_FIELDS - {"schema_version"}:
        if not isinstance(value[field], str) or not SHA256_RE.fullmatch(value[field]):
            raise PatchError(f"backup metadata {field} must be SHA-256")


def validate_target_identity(value: object) -> None:
    if not isinstance(value, dict) or set(value) != TARGET_IDENTITY_FIELDS:
        raise PatchError("target probe identity has invalid fields")
    if not isinstance(value["account"], str) or not ACCOUNT_RE.fullmatch(
        value["account"]
    ):
        raise PatchError("target probe account must be a 12-digit string")
    if not isinstance(value["region"], str) or not REGION_RE.fullmatch(value["region"]):
        raise PatchError("target probe region is invalid")


def validate_acceptance_evidence(
    value: object,
    check: JsonObject,
    challenge: str | None,
) -> None:
    if not isinstance(value, dict) or set(value) != ACCEPTANCE_EVIDENCE_FIELDS:
        raise PatchError(f"acceptance {check['id']} evidence has invalid fields")
    if value["schema_version"] != 1 or value["check_id"] != check["id"]:
        raise PatchError(f"acceptance {check['id']} evidence identity is invalid")
    expected_challenge = hashlib.sha256((challenge or "").encode()).hexdigest()
    if value["challenge_sha256"] != expected_challenge:
        raise PatchError(f"acceptance {check['id']} evidence is stale or replayed")
    proofs = value["proofs"]
    if not isinstance(proofs, list):
        raise PatchError(f"acceptance {check['id']} proofs must be an array")
    observed: set[str] = set()
    for proof in proofs:
        if not isinstance(proof, dict) or set(proof) != ACCEPTANCE_PROOF_FIELDS:
            raise PatchError(f"acceptance {check['id']} proof has invalid fields")
        proof_id = proof["id"]
        observation_sha256 = proof["observation_sha256"]
        evidence_sha256 = proof["evidence_sha256"]
        if (
            not isinstance(proof_id, str)
            or proof_id in observed
            or not isinstance(observation_sha256, str)
            or not SHA256_RE.fullmatch(observation_sha256)
            or observation_sha256 == "0" * 64
            or not isinstance(evidence_sha256, str)
            or not SHA256_RE.fullmatch(evidence_sha256)
        ):
            raise PatchError(f"acceptance {check['id']} proof is invalid")
        expected_evidence = fingerprint(
            {
                "challenge": challenge,
                "check_id": check["id"],
                "proof_id": proof_id,
                "observation_sha256": observation_sha256,
            }
        )
        if evidence_sha256 != expected_evidence:
            raise PatchError(f"acceptance {check['id']} proof is not challenge-bound")
        observed.add(proof_id)
    if observed != set(check["proves"]):
        raise PatchError(
            f"acceptance {check['id']} evidence does not cover declared proofs"
        )


def assert_target_identity(
    manifest: JsonObject,
    environment: JsonObject,
    context: JsonObject,
    kit_dir: Path,
    execution_command: JsonObject | None = None,
) -> JsonObject:
    result = run_command(
        manifest["target_probe"],
        context,
        kit_dir,
        target_identity=True,
        environment_source=execution_command,
    )
    if result["returncode"] != 0:
        raise PatchError("target identity probe failed")
    observed = result["target_identity"]
    expected = {
        "account": environment["account"],
        "region": environment["region"],
    }
    if observed != expected:
        raise PatchError(
            "target identity mismatch: observed account/region do not match "
            "the approved environment"
        )
    return observed


def command_state(command: JsonObject, result: JsonObject) -> str:
    mapping = command.get("state_exit_codes", DEFAULT_STATE_EXIT_CODES)
    state = mapping.get(str(result["returncode"]))
    if state is None:
        raise PatchError(
            f"check returned unmapped exit code {result['returncode']}; state is UNKNOWN"
        )
    return state


def _selected_commands(
    manifest: JsonObject,
    model: JsonObject,
) -> list[JsonObject]:
    commands: list[JsonObject] = [manifest["target_probe"]]
    for operation_id in selected_operation_order(model):
        operation = model["operations"][operation_id]
        commands.extend(
            operation["phases"][phase] for phase in sorted(operation["phases"])
        )
    for check_id in sorted(model["acceptance"]):
        check = model["acceptance"][check_id]
        if model["enabled_features"].intersection(check["features"]):
            commands.append(check["command"])
    return commands


def _executable_identity(
    resolved: JsonObject,
    hash_cache: dict[str, str],
) -> JsonObject:
    value = resolved["argv"][0]
    executable = Path(value)
    if (
        executable.is_absolute()
        or executable.parent != Path(".")
        or value.startswith(".")
    ):
        if not executable.is_absolute():
            executable = resolved["cwd"] / executable
        try:
            executable = executable.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PatchError("command executable was not found") from exc
    else:
        search_entries = []
        for entry in resolved["env"]["PATH"].split(os.pathsep):
            candidate = Path(entry) if entry else Path(".")
            if not candidate.is_absolute():
                candidate = resolved["cwd"] / candidate
            search_entries.append(str(candidate))
        found = shutil.which(value, path=os.pathsep.join(search_entries))
        if found is None:
            raise PatchError("command executable was not found")
        executable = Path(found).resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise PatchError("command executable is not an executable file")
    path = str(executable)
    if path not in hash_cache:
        hash_cache[path] = file_sha256(executable)
    return {
        "path": path,
        "sha256": hash_cache[path],
    }


def _validate_resolved_interpreter(
    resolved: JsonObject,
    executable: JsonObject,
) -> None:
    interpreter = interpreter_name(executable["path"])
    if not interpreter:
        return
    argv = resolved["argv"]
    if "-c" in argv[1:]:
        raise PatchError("resolved interpreter must not execute generated code")
    if len(argv) < 2 or argv[1].startswith("-"):
        raise PatchError("resolved interpreter must execute one explicit script file")
    script = _immutable_input(argv[1], resolved["cwd"])
    immutable_inputs = {
        _immutable_input(value, resolved["cwd"])
        for value in resolved["immutable_inputs"]
    }
    if script not in immutable_inputs:
        raise PatchError(f"resolved interpreter script is not immutable: {argv[1]}")


def _immutable_input(value: str, cwd: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    try:
        path = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise PatchError(f"immutable input does not exist: {value}") from exc
    if not path.is_file():
        raise PatchError(f"immutable input is not a file: {value}")
    return path


def _approved_path(value: str) -> str:
    """Remove Codex's per-session apply_patch shim from the PATH fingerprint."""

    stable_entries = []
    for entry in value.split(os.pathsep):
        path = Path(entry)
        if (
            path.name.startswith("codex-arg0")
            and path.parent.name == "arg0"
            and path.parent.parent.name == "tmp"
            and path.parent.parent.parent.name == ".codex"
        ):
            continue
        stable_entries.append(entry)
    return os.pathsep.join(stable_entries)


def execution_context_inventory(
    manifest: JsonObject,
    environment: JsonObject,
    model: JsonObject,
    kit_dir: Path,
    run_dir: Path,
) -> JsonObject:
    context = command_context(manifest, environment, kit_dir, run_dir)
    commands = _selected_commands(manifest, model)
    inherited_names = sorted(
        {name for command in commands for name in command.get("inherit_env", [])}
    )
    inherited = {}
    for name in inherited_names:
        if name not in os.environ:
            raise PatchError(
                f"required inherited environment variable is missing: {name}"
            )
        inherited[name] = hashlib.sha256(canonical_bytes(os.environ[name])).hexdigest()
    executables = []
    executable_hashes: dict[str, str] = {}
    input_hashes: dict[str, str] = {}
    for command in commands:
        resolved = _resolve_command(command, context, kit_dir)
        input_files = []
        for value in resolved["immutable_inputs"]:
            path = _immutable_input(value, resolved["cwd"])
            name = str(path)
            if name not in input_hashes:
                input_hashes[name] = file_sha256(path)
            input_files.append({"path": name, "sha256": input_hashes[name]})
        executable = _executable_identity(resolved, executable_hashes)
        _validate_resolved_interpreter(resolved, executable)
        executables.append(
            {
                "command_fingerprint": fingerprint(command),
                "cwd": str(resolved["cwd"]),
                "input_files": input_files,
                **executable,
            }
        )
    return {
        "path_sha256": hashlib.sha256(
            canonical_bytes(_approved_path(os.environ.get("PATH", os.defpath)))
        ).hexdigest(),
        "inherited_env": inherited,
        "executables": executables,
    }
