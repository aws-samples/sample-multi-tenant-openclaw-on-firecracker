#!/usr/bin/env python3
"""Validation, feature closure, DAG ordering, and immutable plan helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.@-]{1,127}$")
PATCH_ID_RE = re.compile(r"^[0-9a-z][0-9a-z-]{2,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT_RE = re.compile(r"^[0-9]{12}$")
REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z0-9_.-]+)\}\}")
INTERPRETER_NAMES = {
    "bash",
    "dash",
    "ksh",
    "node",
    "perl",
    "python",
    "python3",
    "ruby",
    "sh",
    "zsh",
}
RESERVED_RUNTIME_ENV_NAMES = {"CLAW_PATCH_ACCEPTANCE_CHALLENGE"}
SENSITIVE_ARG_FLAGS = {
    "--api-key",
    "--password",
    "--private-key",
    "--secret-string",
    "--token",
}
SECRET_LITERAL_RE = re.compile(
    r"(?i)(?:api[_-]?key|password|private[_-]?key|secret|token)\s*[:=]\s*[^\s{]+"
)

COMMAND_FIELDS = {
    "argv",
    "cwd",
    "env",
    "immutable_inputs",
    "inherit_env",
    "timeout_seconds",
    "state_exit_codes",
}
PHASES = {
    "check",
    "backup",
    "backup_verify",
    "apply",
    "verify",
    "rollback",
    "rollback_verify",
}


class PatchError(RuntimeError):
    """Raised for a deterministic contract or safety failure."""


def load_json(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PatchError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PatchError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PatchError(f"{path} must contain a JSON object")
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(value) + b"\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def _require_fields(
    value: JsonObject, allowed: set[str], required: set[str], label: str
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise PatchError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise PatchError(f"{label} is missing fields: {', '.join(missing)}")


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise PatchError(f"{label} must match {ID_RE.pattern}")
    return value


def _string_set(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PatchError(f"{label} must be an array of strings")
    if nonempty and not value:
        raise PatchError(f"{label} must not be empty")
    if len(value) != len(set(value)):
        raise PatchError(f"{label} contains duplicates")
    for index, item in enumerate(value):
        _require_id(item, f"{label}[{index}]")
    return value


def _path_set(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise PatchError(f"{label} must be a unique non-empty string array")
    return value


def interpreter_name(value: str) -> str | None:
    name = Path(value).name.lower()
    for interpreter in INTERPRETER_NAMES:
        if name == interpreter or name.startswith(interpreter + "."):
            return interpreter
        if interpreter in name and value.startswith("{{environment."):
            return interpreter
    return None


def _validate_command(value: Any, label: str, *, check: bool = False) -> None:
    if not isinstance(value, dict):
        raise PatchError(f"{label} must be an object")
    _require_fields(value, COMMAND_FIELDS, {"argv"}, label)
    argv = value["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(arg, str) for arg in argv)
    ):
        raise PatchError(f"{label}.argv must be a non-empty string array")
    if any("\x00" in arg for arg in argv):
        raise PatchError(f"{label}.argv must not contain NUL")
    executable = Path(argv[0]).name
    if executable in {"echo", "printf", "true"}:
        raise PatchError(f"{label}.argv must not use a no-op executable")
    if executable == "env":
        raise PatchError(
            f"{label}.argv must use the interpreter directly, not an env wrapper"
        )
    interpreter = interpreter_name(argv[0])
    if interpreter and "-c" in argv[1:]:
        raise PatchError(f"{label}.argv must not execute a generated shell string")
    if "--profile" in argv or any(arg.startswith("--profile=") for arg in argv):
        raise PatchError(
            f"{label}.argv must not select an AWS profile; use the bound "
            "environment context"
        )
    for index, argument in enumerate(argv):
        if argument in SENSITIVE_ARG_FLAGS and (
            index + 1 >= len(argv)
            or (
                (match := PLACEHOLDER_RE.fullmatch(argv[index + 1])) is None
                or not match.group(1).startswith("environment.")
            )
        ):
            raise PatchError(
                f"{label}.argv sensitive values must use environment placeholders"
            )
        for flag in SENSITIVE_ARG_FLAGS:
            if argument.startswith(flag + "="):
                value = argument.split("=", 1)[1]
                match = PLACEHOLDER_RE.fullmatch(value)
                if match is None or not match.group(1).startswith("environment."):
                    raise PatchError(
                        f"{label}.argv sensitive values must use "
                        "environment placeholders"
                    )
        if argument == "--region":
            if index + 1 >= len(argv) or argv[index + 1] != "{{environment.region}}":
                raise PatchError(
                    f"{label}.argv --region must use {{{{environment.region}}}}"
                )
        elif argument.startswith("--region=") and argument != (
            "--region={{environment.region}}"
        ):
            raise PatchError(
                f"{label}.argv --region must use {{{{environment.region}}}}"
            )
    if "cwd" in value and not isinstance(value["cwd"], str):
        raise PatchError(f"{label}.cwd must be a string")
    immutable_inputs = value.get("immutable_inputs", [])
    if (
        not isinstance(immutable_inputs, list)
        or len(immutable_inputs) != len(set(immutable_inputs))
        or not all(isinstance(item, str) and item for item in immutable_inputs)
    ):
        raise PatchError(f"{label}.immutable_inputs must be a unique string array")
    if any("\x00" in item for item in immutable_inputs):
        raise PatchError(f"{label}.immutable_inputs must not contain NUL")
    if interpreter:
        if len(argv) < 2 or argv[1].startswith("-"):
            raise PatchError(
                f"{label}.argv interpreter must execute one explicit script file"
            )
        if argv[1] not in immutable_inputs:
            raise PatchError(
                f"{label}.immutable_inputs must include interpreted script {argv[1]}"
            )
    env = value.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and ENV_NAME_RE.fullmatch(key) and isinstance(item, str)
        for key, item in env.items()
    ):
        raise PatchError(f"{label}.env must map environment names to strings")
    for name, item in env.items():
        if name == "PATH":
            raise PatchError(
                f"{label}.env.PATH must not override the approved executable path"
            )
        match = PLACEHOLDER_RE.fullmatch(item)
        if match is None or not match.group(1).startswith("environment."):
            raise PatchError(
                f"{label}.env.{name} must be one environment placeholder; "
                "do not place literal values in the manifest"
            )
    inherited = value.get("inherit_env", [])
    if not isinstance(inherited, list) or len(inherited) != len(set(inherited)):
        raise PatchError(f"{label}.inherit_env must be a unique array")
    if not all(
        isinstance(item, str) and ENV_NAME_RE.fullmatch(item) for item in inherited
    ):
        raise PatchError(f"{label}.inherit_env contains an invalid environment name")
    reserved = sorted(
        RESERVED_RUNTIME_ENV_NAMES.intersection(set(env) | set(inherited))
    )
    if reserved:
        raise PatchError(
            f"{label} uses runner-reserved environment: {', '.join(reserved)}"
        )
    timeout = value.get("timeout_seconds", 300)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 7200
    ):
        raise PatchError(f"{label}.timeout_seconds must be 1..7200")
    mapping = value.get("state_exit_codes")
    if mapping is not None:
        if not check or not isinstance(mapping, dict) or not mapping:
            raise PatchError(f"{label}.state_exit_codes is valid only for check")
        allowed = {"BASE", "TARGET", "ABSENT", "DRIFT", "UNKNOWN"}
        for code, state in mapping.items():
            if not isinstance(code, str) or not code.isdigit() or int(code) > 255:
                raise PatchError(
                    f"{label}.state_exit_codes has invalid exit code {code!r}"
                )
            if state not in allowed:
                raise PatchError(
                    f"{label}.state_exit_codes has invalid state {state!r}"
                )


def _validate_contract(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise PatchError(f"{label} must be an object")
    _require_fields(
        value,
        {"id", "required_capabilities", "required_acceptance"},
        {"id", "required_capabilities", "required_acceptance"},
        label,
    )
    _require_id(value["id"], f"{label}.id")
    _string_set(
        value["required_capabilities"], f"{label}.required_capabilities", nonempty=True
    )
    _string_set(
        value["required_acceptance"], f"{label}.required_acceptance", nonempty=True
    )
    return value


def _validate_operation(
    value: Any,
    label: str,
    feature_ids: set[str],
    artifact_ids: set[str],
) -> JsonObject:
    if not isinstance(value, dict):
        raise PatchError(f"{label} must be an object")
    allowed = {
        "id",
        "summary",
        "features",
        "provides",
        "artifacts",
        "source_changes",
        "depends_on",
        "execution",
        "handler",
        "approval",
        "rollback",
        "idempotent",
        "manual",
        "phases",
    }
    required = allowed - {"manual"}
    _require_fields(value, allowed, required, label)
    _require_id(value["id"], f"{label}.id")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise PatchError(f"{label}.summary must not be empty")
    features = _string_set(value["features"], f"{label}.features")
    unknown_features = sorted(set(features) - feature_ids)
    if unknown_features:
        raise PatchError(
            f"{label} references unknown features: {', '.join(unknown_features)}"
        )
    _string_set(value["provides"], f"{label}.provides")
    artifacts = value["artifacts"]
    if (
        not isinstance(artifacts, list)
        or not all(isinstance(item, str) for item in artifacts)
        or len(artifacts) != len(set(artifacts))
    ):
        raise PatchError(f"{label}.artifacts must be a unique string array")
    unknown_artifacts = sorted(set(artifacts) - artifact_ids)
    if unknown_artifacts:
        raise PatchError(
            f"{label} references unknown artifacts: {', '.join(unknown_artifacts)}"
        )
    _path_set(value["source_changes"], f"{label}.source_changes")
    _string_set(value["depends_on"], f"{label}.depends_on")
    execution = value["execution"]
    handler = value["handler"]
    if execution not in {"COMPILED", "MANUAL"}:
        raise PatchError(f"{label}.execution is invalid")
    if handler not in {"exec.argv", "manual"}:
        raise PatchError(f"{label}.handler is unsupported; use a MANUAL node")
    if execution == "COMPILED" and handler != "exec.argv":
        raise PatchError(f"{label} compiled operations require handler exec.argv")
    if execution == "MANUAL" and handler != "manual":
        raise PatchError(f"{label} manual operations require handler manual")
    if value["approval"] not in {"AUTO", "REQUIRE_APPROVAL", "BLOCKED"}:
        raise PatchError(f"{label}.approval is invalid")
    rollback = value["rollback"]
    if rollback not in {"RESTORE", "RETAIN", "IRREVERSIBLE"}:
        raise PatchError(f"{label}.rollback is invalid")
    if not isinstance(value["idempotent"], bool):
        raise PatchError(f"{label}.idempotent must be boolean")
    phases = value["phases"]
    if not isinstance(phases, dict) or set(phases) - PHASES:
        raise PatchError(f"{label}.phases contains an unknown phase")
    for phase, command in phases.items():
        _validate_command(command, f"{label}.phases.{phase}", check=phase == "check")
    if execution == "COMPILED":
        missing = {"check", "apply", "verify"} - set(phases)
        if missing:
            raise PatchError(
                f"{label} compiled operation misses phases: {', '.join(sorted(missing))}"
            )
    else:
        manual = value.get("manual")
        if not isinstance(manual, dict) or set(manual) != {"instructions"}:
            raise PatchError(f"{label}.manual must contain only instructions")
        if (
            not isinstance(manual["instructions"], str)
            or not manual["instructions"].strip()
        ):
            raise PatchError(f"{label}.manual.instructions must not be empty")
        if SECRET_LITERAL_RE.search(manual["instructions"]):
            raise PatchError(f"{label}.manual.instructions contains a secret literal")
        missing = {"check", "verify"} - set(phases)
        if missing:
            raise PatchError(
                f"{label} manual operation misses phases: {', '.join(sorted(missing))}"
            )
    if rollback == "RESTORE" and not {
        "backup",
        "backup_verify",
        "rollback",
        "rollback_verify",
    } <= set(phases):
        raise PatchError(
            f"{label} RESTORE requires backup, backup_verify, rollback, "
            "and rollback_verify phases"
        )
    if rollback == "IRREVERSIBLE" and value["approval"] != "REQUIRE_APPROVAL":
        raise PatchError(f"{label} IRREVERSIBLE requires REQUIRE_APPROVAL")
    command_text = "\n".join(
        item
        for command in phases.values()
        for item in [*command["argv"], command.get("cwd", "")]
    )
    if execution == "MANUAL":
        command_text += "\n" + value["manual"]["instructions"]
    unconsumed = sorted(
        artifact for artifact in artifacts if artifact not in command_text
    )
    if unconsumed:
        raise PatchError(
            f"{label} declares artifacts not referenced by its commands or "
            f"instructions: {', '.join(unconsumed)}"
        )
    return value


def _validate_acceptance(value: Any, label: str, feature_ids: set[str]) -> JsonObject:
    if not isinstance(value, dict):
        raise PatchError(f"{label} must be an object")
    required = {"id", "summary", "features", "proves", "command"}
    _require_fields(value, required, required, label)
    _require_id(value["id"], f"{label}.id")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise PatchError(f"{label}.summary must not be empty")
    features = _string_set(value["features"], f"{label}.features", nonempty=True)
    unknown = sorted(set(features) - feature_ids)
    if unknown:
        raise PatchError(f"{label} references unknown features: {', '.join(unknown)}")
    _string_set(value["proves"], f"{label}.proves", nonempty=True)
    _validate_command(value["command"], f"{label}.command")
    return value


def load_catalog(path: Path) -> JsonObject:
    catalog = load_json(path)
    _require_fields(
        catalog,
        {"schema_version", "contracts"},
        {"schema_version", "contracts"},
        "catalog",
    )
    if catalog["schema_version"] != 1 or not isinstance(catalog["contracts"], list):
        raise PatchError("catalog schema_version/contracts are invalid")
    contracts: dict[str, JsonObject] = {}
    for index, value in enumerate(catalog["contracts"]):
        contract = _validate_contract(value, f"catalog.contracts[{index}]")
        if contract["id"] in contracts:
            raise PatchError(f"duplicate catalog contract {contract['id']}")
        contracts[contract["id"]] = contract
    return {"schema_version": 1, "contracts": contracts}


def validate_environment(environment: JsonObject) -> None:
    _require_fields(
        environment,
        {
            "schema_version",
            "account",
            "region",
            "resources",
            "baseline",
            "policy",
        },
        {"schema_version", "account", "region", "resources", "policy"},
        "environment",
    )
    if environment["schema_version"] != 1:
        raise PatchError("environment.schema_version must be 1")
    if not isinstance(environment["account"], str) or not ACCOUNT_RE.fullmatch(
        environment["account"]
    ):
        raise PatchError("environment.account must be a 12-digit string")
    if not isinstance(environment["region"], str) or not REGION_RE.fullmatch(
        environment["region"]
    ):
        raise PatchError("environment.region is invalid")
    resources = environment["resources"]
    if not isinstance(resources, dict):
        raise PatchError("environment.resources must be an object")
    for key, value in resources.items():
        if not isinstance(key, str) or not re.fullmatch(r"^[a-z][a-z0-9_.-]*$", key):
            raise PatchError(f"environment resource key is invalid: {key!r}")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise PatchError(f"environment resource {key} must be scalar")
    baseline = environment.get("baseline")
    if baseline is not None:
        _require_fields(
            baseline,
            {
                "authority",
                "snapshot",
                "sha256",
                "preserve_unowned",
                "captured_at",
            },
            {"authority", "snapshot", "sha256", "preserve_unowned"},
            "environment.baseline",
        )
        if baseline["authority"] != "customer-live":
            raise PatchError("environment.baseline.authority must be customer-live")
        if (
            not isinstance(baseline["snapshot"], str)
            or not baseline["snapshot"]
            or "\x00" in baseline["snapshot"]
        ):
            raise PatchError("environment.baseline.snapshot is invalid")
        if not isinstance(baseline["sha256"], str) or not SHA256_RE.fullmatch(
            baseline["sha256"]
        ):
            raise PatchError("environment.baseline.sha256 is invalid")
        if baseline["preserve_unowned"] is not True:
            raise PatchError("environment.baseline.preserve_unowned must be true")
        captured_at = baseline.get("captured_at")
        if captured_at is not None and (
            not isinstance(captured_at, str) or not captured_at
        ):
            raise PatchError("environment.baseline.captured_at is invalid")
    policy = environment["policy"]
    _require_fields(
        policy,
        {"write_mode", "preserve_unowned"},
        {"write_mode", "preserve_unowned"},
        "environment.policy",
    )
    if policy["write_mode"] not in {
        "QUALIFY_ONLY",
        "DISPOSABLE",
        "CUSTOMER_APPROVED",
    }:
        raise PatchError("environment.policy.write_mode is invalid")
    if policy["preserve_unowned"] is not True:
        raise PatchError("environment.policy.preserve_unowned must be true")
    if policy["write_mode"] == "CUSTOMER_APPROVED" and baseline is None:
        raise PatchError("CUSTOMER_APPROVED requires a customer baseline")


def _validate_source_changes(
    source_changes: Any,
    artifacts: JsonObject,
) -> dict[str, JsonObject]:
    if not isinstance(source_changes, list):
        raise PatchError("manifest.source_changes must be an array")
    changes: dict[str, JsonObject] = {}
    for index, value in enumerate(source_changes):
        label = f"source_changes[{index}]"
        if not isinstance(value, dict):
            raise PatchError(f"{label} must be an object")
        required = {"status", "path", "artifact"}
        _require_fields(
            value,
            required | {"old_path", "delivery_role"},
            required,
            label,
        )
        if value["status"] not in {"A", "M", "D", "R", "T"}:
            raise PatchError(f"{label}.status is invalid")
        if not isinstance(value["path"], str) or not value["path"]:
            raise PatchError(f"{label}.path is invalid")
        if value["path"] in changes:
            raise PatchError(f"duplicate source change path: {value['path']}")
        artifact = value["artifact"]
        if artifact is not None and artifact not in artifacts:
            raise PatchError(f"{label} references unknown artifact {artifact}")
        if value["status"] == "D" and artifact is not None:
            raise PatchError(f"{label} deleted path must not have an artifact")
        if value["status"] != "D" and artifact is None:
            raise PatchError(f"{label} changed path requires an artifact")
        old_path = value.get("old_path")
        if value["status"] == "R":
            if not isinstance(old_path, str) or not old_path:
                raise PatchError(f"{label} rename requires old_path")
        elif old_path is not None:
            raise PatchError(f"{label} old_path is valid only for a rename")
        if value.get("delivery_role", "DELIVERABLE") not in {
            "DELIVERABLE",
            "PUBLISH_EVIDENCE",
            "SOURCE_EVIDENCE",
        }:
            raise PatchError(f"{label}.delivery_role is invalid")
        changes[value["path"]] = value
    return changes


def _validate_provenance(value: Any, patch: JsonObject) -> None:
    if not isinstance(value, dict):
        raise PatchError("manifest.provenance must be an object")
    kind = value.get("kind")
    if kind == "reviewed-git-range":
        _validate_reviewed_range_provenance(value, patch)
        return
    fields = {
        "schema_version",
        "kind",
        "internal_source",
        "public_gateway",
        "publish_receipt",
        "baseline",
        "target",
    }
    _require_fields(value, fields, fields, "provenance")
    if value["schema_version"] != 1 or value["kind"] != "opensource-publish":
        raise PatchError("manifest.provenance version or kind is invalid")

    internal = value["internal_source"]
    if not isinstance(internal, dict):
        raise PatchError("provenance.internal_source must be an object")
    _require_fields(
        internal,
        {"base_sha", "source_sha", "commit_count", "file_count"},
        {"base_sha", "source_sha", "commit_count", "file_count"},
        "provenance.internal_source",
    )
    for field in ("base_sha", "source_sha"):
        if not isinstance(internal[field], str) or not SHA_RE.fullmatch(
            internal[field]
        ):
            raise PatchError(f"provenance.internal_source.{field} is invalid")
    for field in ("commit_count", "file_count"):
        if not isinstance(internal[field], int) or internal[field] < 0:
            raise PatchError(f"provenance.internal_source.{field} is invalid")

    gateway = value["public_gateway"]
    if not isinstance(gateway, dict):
        raise PatchError("provenance.public_gateway must be an object")
    _require_fields(
        gateway,
        {"repository", "base_sha", "patch_sha", "revision_sha"},
        {"repository", "base_sha", "patch_sha", "revision_sha"},
        "provenance.public_gateway",
    )
    if not isinstance(gateway["repository"], str) or not gateway["repository"]:
        raise PatchError("provenance.public_gateway.repository is invalid")
    for field in ("base_sha", "patch_sha", "revision_sha"):
        if not isinstance(gateway[field], str) or not SHA_RE.fullmatch(
            gateway[field]
        ):
            raise PatchError(f"provenance.public_gateway.{field} is invalid")
    if (
        gateway["base_sha"] != patch["base_sha"]
        or gateway["patch_sha"] != patch["patch_sha"]
    ):
        raise PatchError("provenance public gateway range does not match patch range")

    receipt = value["publish_receipt"]
    if not isinstance(receipt, dict):
        raise PatchError("provenance.publish_receipt must be an object")
    _require_fields(
        receipt,
        {"manifest", "marker"},
        {"manifest", "marker"},
        "provenance.publish_receipt",
    )
    for name in ("manifest", "marker"):
        item = receipt[name]
        if not isinstance(item, dict):
            raise PatchError(f"provenance.publish_receipt.{name} is invalid")
        _require_fields(
            item,
            {"path", "sha256"},
            {"path", "sha256"},
            f"provenance.publish_receipt.{name}",
        )
        if not isinstance(item["path"], str) or not item["path"]:
            raise PatchError(f"provenance.publish_receipt.{name}.path is invalid")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(
            item["sha256"]
        ):
            raise PatchError(f"provenance.publish_receipt.{name}.sha256 is invalid")

    baseline = value["baseline"]
    if not isinstance(baseline, dict):
        raise PatchError("provenance.baseline must be an object")
    source = baseline.get("source")
    if source == "EXPLICIT_PUBLIC_BASE":
        _require_fields(
            baseline,
            {"source"},
            {"source"},
            "provenance.baseline",
        )
    elif source == "PREVIOUS_DELIVERY":
        _require_fields(
            baseline,
            {"source", "receipt_sha256", "delivery_fingerprint"},
            {"source", "receipt_sha256", "delivery_fingerprint"},
            "provenance.baseline",
        )
        for field in ("receipt_sha256", "delivery_fingerprint"):
            if not isinstance(baseline[field], str) or not SHA256_RE.fullmatch(
                baseline[field]
            ):
                raise PatchError(f"provenance.baseline.{field} is invalid")
    else:
        raise PatchError("provenance.baseline.source is invalid")

    target = value["target"]
    if not isinstance(target, dict):
        raise PatchError("provenance.target must be an object")
    _require_fields(
        target,
        {"account", "region", "profile_sha256", "baseline_snapshot_sha256"},
        {"account", "region", "profile_sha256"},
        "provenance.target",
    )
    if not isinstance(target["account"], str) or not ACCOUNT_RE.fullmatch(
        target["account"]
    ):
        raise PatchError("provenance.target.account is invalid")
    if not isinstance(target["region"], str) or not REGION_RE.fullmatch(
        target["region"]
    ):
        raise PatchError("provenance.target.region is invalid")
    for field in ("profile_sha256", "baseline_snapshot_sha256"):
        item = target.get(field)
        if item is not None and (
            not isinstance(item, str) or not SHA256_RE.fullmatch(item)
        ):
            raise PatchError(f"provenance.target.{field} is invalid")


def _validate_reviewed_range_provenance(
    value: JsonObject, patch: JsonObject
) -> None:
    fields = {
        "schema_version",
        "kind",
        "public_gateway",
        "range_scope",
        "review",
        "target",
    }
    _require_fields(value, fields, fields, "provenance")
    if value["schema_version"] != 1:
        raise PatchError("manifest.provenance version is invalid")

    gateway = value["public_gateway"]
    if not isinstance(gateway, dict):
        raise PatchError("provenance.public_gateway must be an object")
    _require_fields(
        gateway,
        {"repository", "base_sha", "patch_sha", "revision_sha"},
        {"repository", "base_sha", "patch_sha", "revision_sha"},
        "provenance.public_gateway",
    )
    if not isinstance(gateway["repository"], str) or not gateway["repository"]:
        raise PatchError("provenance.public_gateway.repository is invalid")
    for field in ("base_sha", "patch_sha", "revision_sha"):
        if not isinstance(gateway[field], str) or not SHA_RE.fullmatch(
            gateway[field]
        ):
            raise PatchError(f"provenance.public_gateway.{field} is invalid")
    if (
        gateway["base_sha"] != patch["base_sha"]
        or gateway["patch_sha"] != patch["patch_sha"]
    ):
        raise PatchError("provenance public gateway range does not match patch range")

    scope = value["range_scope"]
    if not isinstance(scope, dict):
        raise PatchError("provenance.range_scope must be an object")
    scope_fields = {
        "source_manifest",
        "included_path_count",
        "included_paths_sha256",
        "excluded_path_count",
        "excluded_paths_sha256",
    }
    _require_fields(scope, scope_fields, scope_fields, "provenance.range_scope")
    evidence = scope["source_manifest"]
    if not isinstance(evidence, dict):
        raise PatchError("provenance.range_scope.source_manifest must be an object")
    _require_fields(
        evidence,
        {"path", "sha256"},
        {"path", "sha256"},
        "provenance.range_scope.source_manifest",
    )
    if not isinstance(evidence["path"], str) or not evidence["path"]:
        raise PatchError("provenance.range_scope.source_manifest.path is invalid")
    if not isinstance(evidence["sha256"], str) or not SHA256_RE.fullmatch(
        evidence["sha256"]
    ):
        raise PatchError("provenance.range_scope.source_manifest.sha256 is invalid")
    for field in ("included_path_count", "excluded_path_count"):
        minimum = 1 if field == "included_path_count" else 0
        if not isinstance(scope[field], int) or scope[field] < minimum:
            raise PatchError(f"provenance.range_scope.{field} is invalid")
    for field in ("included_paths_sha256", "excluded_paths_sha256"):
        if not isinstance(scope[field], str) or not SHA256_RE.fullmatch(scope[field]):
            raise PatchError(f"provenance.range_scope.{field} is invalid")

    review = value["review"]
    if (
        not isinstance(review, dict)
        or set(review) != {"reason"}
        or not isinstance(review["reason"], str)
        or not review["reason"].strip()
    ):
        raise PatchError("provenance.review.reason is invalid")

    target = value["target"]
    if not isinstance(target, dict):
        raise PatchError("provenance.target must be an object")
    _require_fields(
        target,
        {"account", "region", "profile_sha256", "baseline_snapshot_sha256"},
        {"account", "region", "profile_sha256"},
        "provenance.target",
    )
    if not isinstance(target["account"], str) or not ACCOUNT_RE.fullmatch(
        target["account"]
    ):
        raise PatchError("provenance.target.account is invalid")
    if not isinstance(target["region"], str) or not REGION_RE.fullmatch(
        target["region"]
    ):
        raise PatchError("provenance.target.region is invalid")
    for field in ("profile_sha256", "baseline_snapshot_sha256"):
        item = target.get(field)
        if item is not None and (
            not isinstance(item, str) or not SHA256_RE.fullmatch(item)
        ):
            raise PatchError(f"provenance.target.{field} is invalid")


def validate_manifest(manifest: JsonObject, catalog: JsonObject) -> JsonObject:
    allowed = {
        "schema_version",
        "patch",
        "target_probe",
        "artifacts",
        "contracts",
        "features",
        "operations",
        "acceptance_checks",
        "source_changes",
        "provenance",
        "execution_profile_sha256",
    }
    _require_fields(
        manifest,
        allowed,
        allowed - {"provenance", "execution_profile_sha256"},
        "manifest",
    )
    if manifest["schema_version"] != 2:
        raise PatchError("manifest.schema_version must be 2")
    execution_profile = manifest.get("execution_profile_sha256")
    if execution_profile is not None and (
        not isinstance(execution_profile, str)
        or not SHA256_RE.fullmatch(execution_profile)
    ):
        raise PatchError("manifest.execution_profile_sha256 is invalid")
    patch = manifest["patch"]
    if not isinstance(patch, dict):
        raise PatchError("manifest.patch must be an object")
    _require_fields(
        patch, {"id", "base_sha", "patch_sha"}, {"id", "base_sha", "patch_sha"}, "patch"
    )
    if not isinstance(patch["id"], str) or not PATCH_ID_RE.fullmatch(patch["id"]):
        raise PatchError("patch.id is invalid")
    for field in ("base_sha", "patch_sha"):
        if not isinstance(patch[field], str) or not SHA_RE.fullmatch(patch[field]):
            raise PatchError(f"patch.{field} must be a full Git SHA")
    if "provenance" in manifest:
        _validate_provenance(manifest["provenance"], patch)
    _validate_command(manifest["target_probe"], "target_probe")

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict):
        raise PatchError("manifest.artifacts must be an object")
    for path, metadata in artifacts.items():
        pure = Path(path)
        if (
            not isinstance(path, str)
            or not path.startswith("artifacts/")
            or pure.is_absolute()
            or ".." in pure.parts
        ):
            raise PatchError(f"unsafe artifact path: {path!r}")
        if not isinstance(metadata, dict) or set(metadata) != {"sha256", "git_mode"}:
            raise PatchError(f"artifact {path} must contain sha256 and git_mode")
        if not isinstance(metadata["sha256"], str) or not SHA256_RE.fullmatch(
            metadata["sha256"]
        ):
            raise PatchError(f"artifact {path} has invalid sha256")
        if metadata["git_mode"] not in {"100644", "100755", "120000", "160000"}:
            raise PatchError(f"artifact {path} has unsupported git_mode")
    source_changes = _validate_source_changes(manifest["source_changes"], artifacts)

    if not isinstance(manifest["contracts"], list):
        raise PatchError("manifest.contracts must be an array")
    contracts = dict(catalog["contracts"])
    for index, value in enumerate(manifest["contracts"]):
        contract = _validate_contract(value, f"contracts[{index}]")
        if contract["id"] in contracts:
            raise PatchError(f"manifest cannot override contract {contract['id']}")
        contracts[contract["id"]] = contract

    if not isinstance(manifest["features"], list) or not manifest["features"]:
        raise PatchError("manifest.features must be a non-empty array")
    features: dict[str, JsonObject] = {}
    for index, value in enumerate(manifest["features"]):
        label = f"features[{index}]"
        if not isinstance(value, dict):
            raise PatchError(f"{label} must be an object")
        _require_fields(
            value, {"id", "contract", "enabled"}, {"id", "contract", "enabled"}, label
        )
        feature_id = _require_id(value["id"], f"{label}.id")
        if feature_id in features:
            raise PatchError(f"duplicate feature {feature_id}")
        contract_id = _require_id(value["contract"], f"{label}.contract")
        if contract_id not in contracts:
            raise PatchError(f"{label} references unknown contract {contract_id}")
        if not isinstance(value["enabled"], bool):
            raise PatchError(f"{label}.enabled must be boolean")
        features[feature_id] = value

    feature_ids = set(features)
    if not isinstance(manifest["operations"], list):
        raise PatchError("manifest.operations must be an array")
    operations: dict[str, JsonObject] = {}
    for index, value in enumerate(manifest["operations"]):
        operation = _validate_operation(
            value,
            f"operations[{index}]",
            feature_ids,
            set(artifacts),
        )
        if operation["id"] in operations:
            raise PatchError(f"duplicate operation {operation['id']}")
        unsupported_modes = sorted(
            artifact
            for artifact in operation["artifacts"]
            if artifacts[artifact]["git_mode"] in {"120000", "160000"}
        )
        if operation["execution"] == "COMPILED" and unsupported_modes:
            raise PatchError(
                f"operation {operation['id']} has unsupported compiled artifact "
                "modes: " + ", ".join(unsupported_modes)
            )
        operations[operation["id"]] = operation
    for operation in operations.values():
        unknown = sorted(set(operation["depends_on"]) - set(operations))
        if unknown:
            raise PatchError(
                f"operation {operation['id']} has unknown dependencies: {', '.join(unknown)}"
            )
        if operation["rollback"] in {"RETAIN", "IRREVERSIBLE"}:
            restored_dependencies = sorted(
                dependency
                for dependency in operation["depends_on"]
                if operations[dependency]["rollback"] == "RESTORE"
            )
            if restored_dependencies:
                raise PatchError(
                    f"operation {operation['id']} persists after rollback but depends "
                    "on RESTORE operations: " + ", ".join(restored_dependencies)
                )
        unknown_source_changes = sorted(
            set(operation["source_changes"]) - set(source_changes)
        )
        if unknown_source_changes:
            raise PatchError(
                f"operation {operation['id']} references unknown source changes: "
                + ", ".join(unknown_source_changes)
            )
        evidence_source_changes = sorted(
            source_path
            for source_path in operation["source_changes"]
            if source_changes[source_path].get(
                "delivery_role", "DELIVERABLE"
            )
            != "DELIVERABLE"
        )
        if evidence_source_changes:
            raise PatchError(
                f"operation {operation['id']} must not deliver publish evidence "
                "or source evidence: "
                + ", ".join(evidence_source_changes)
            )
        for source_path in operation["source_changes"]:
            artifact = source_changes[source_path]["artifact"]
            if artifact is not None and artifact not in operation["artifacts"]:
                raise PatchError(
                    f"operation {operation['id']} claims source change {source_path} "
                    f"without its artifact {artifact}"
                )
    if not isinstance(manifest["acceptance_checks"], list):
        raise PatchError("manifest.acceptance_checks must be an array")
    acceptance: dict[str, JsonObject] = {}
    for index, value in enumerate(manifest["acceptance_checks"]):
        check = _validate_acceptance(value, f"acceptance_checks[{index}]", feature_ids)
        if check["id"] in acceptance:
            raise PatchError(f"duplicate acceptance check {check['id']}")
        acceptance[check["id"]] = check

    enabled = {feature_id for feature_id, value in features.items() if value["enabled"]}
    _topological_order(operations, set(operations))
    selected = _selected_operation_ids(operations, enabled)
    claimed_source_changes = {
        path
        for operation_id in selected
        for path in operations[operation_id]["source_changes"]
    }
    deliverable_source_changes = {
        path
        for path, change in source_changes.items()
        if change.get("delivery_role", "DELIVERABLE") == "DELIVERABLE"
    }
    unclaimed_source_changes = sorted(
        deliverable_source_changes - claimed_source_changes
    )
    if unclaimed_source_changes:
        raise PatchError(
            "source changes are not claimed by a selected operation: "
            + ", ".join(unclaimed_source_changes)
        )
    claimed_artifacts = {
        artifact
        for operation_id in selected
        for artifact in operations[operation_id]["artifacts"]
    }
    evidence_artifacts = {
        change["artifact"]
        for change in source_changes.values()
        if change.get("delivery_role", "DELIVERABLE") != "DELIVERABLE"
        and change["artifact"] is not None
    }
    unclaimed_artifacts = sorted(
        set(artifacts) - claimed_artifacts - evidence_artifacts
    )
    if unclaimed_artifacts:
        raise PatchError(
            "manifest artifacts are not claimed by a selected operation: "
            + ", ".join(unclaimed_artifacts)
        )
    for feature_id in sorted(enabled):
        contract = contracts[features[feature_id]["contract"]]
        _validate_feature_closure(feature_id, contract, operations, acceptance)

    return {
        "contracts": contracts,
        "features": features,
        "operations": operations,
        "acceptance": acceptance,
        "enabled_features": enabled,
    }


def _validate_feature_closure(
    feature_id: str,
    contract: JsonObject,
    operations: dict[str, JsonObject],
    acceptance: dict[str, JsonObject],
) -> None:
    providers: dict[str, list[str]] = {}
    for operation in operations.values():
        if feature_id not in operation["features"]:
            continue
        for capability in operation["provides"]:
            providers.setdefault(capability, []).append(operation["id"])
    proofs: dict[str, list[str]] = {}
    for check in acceptance.values():
        if feature_id not in check["features"]:
            continue
        for proof in check["proves"]:
            proofs.setdefault(proof, []).append(check["id"])
    missing_capabilities = sorted(
        set(contract["required_capabilities"]) - set(providers)
    )
    missing_acceptance = sorted(set(contract["required_acceptance"]) - set(proofs))
    if missing_capabilities:
        raise PatchError(
            f"feature {feature_id} misses capabilities: {', '.join(missing_capabilities)}"
        )
    if missing_acceptance:
        raise PatchError(
            f"feature {feature_id} misses acceptance: {', '.join(missing_acceptance)}"
        )


def selected_operation_ids(model: JsonObject) -> set[str]:
    return _selected_operation_ids(
        model["operations"],
        model["enabled_features"],
    )


def _selected_operation_ids(
    operations: dict[str, JsonObject],
    enabled: set[str],
) -> set[str]:
    selected = {
        operation_id
        for operation_id, operation in operations.items()
        if enabled.intersection(operation["features"])
    }
    pending = list(selected)
    while pending:
        operation_id = pending.pop()
        for dependency in operations[operation_id]["depends_on"]:
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return selected


def _topological_order(
    operations: dict[str, JsonObject],
    selected: set[str],
) -> list[str]:
    permanent: set[str] = set()
    temporary: set[str] = set()
    ordered: list[str] = []

    def visit(operation_id: str) -> None:
        if operation_id in permanent:
            return
        if operation_id in temporary:
            raise PatchError(f"operation dependency cycle at {operation_id}")
        temporary.add(operation_id)
        for dependency in operations[operation_id]["depends_on"]:
            if dependency in selected:
                visit(dependency)
        temporary.remove(operation_id)
        permanent.add(operation_id)
        ordered.append(operation_id)

    for operation_id in sorted(selected):
        visit(operation_id)
    return ordered


def selected_operation_order(model: JsonObject) -> list[str]:
    return _topological_order(model["operations"], selected_operation_ids(model))


def validate_artifacts(manifest: JsonObject, kit_dir: Path) -> None:
    root = kit_dir.resolve()
    for relative, metadata in manifest["artifacts"].items():
        path = kit_dir / relative
        current = path
        while True:
            if current.is_symlink():
                raise PatchError(f"artifact path contains a symlink: {relative}")
            if current == kit_dir:
                break
            if current == current.parent:
                raise PatchError(f"artifact path escapes kit directory: {relative}")
            current = current.parent
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PatchError(f"missing artifact: {relative}") from exc
        if resolved == root or root not in resolved.parents:
            raise PatchError(f"artifact escapes kit directory: {relative}")
        actual = file_sha256(resolved)
        if actual != metadata["sha256"]:
            raise PatchError(
                f"artifact hash mismatch for {relative}: {actual} != {metadata['sha256']}"
            )


def resolve_placeholders(value: str, context: JsonObject) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        current: Any = context
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                raise PatchError(f"unresolved placeholder: {match.group(0)}")
            current = current[part]
        if current is None or isinstance(current, (dict, list)):
            raise PatchError(f"placeholder {match.group(0)} is not scalar")
        return str(current)

    return PLACEHOLDER_RE.sub(replace, value)
