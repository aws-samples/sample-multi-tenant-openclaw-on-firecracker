#!/usr/bin/env python3
"""RESTORE-capable Lambda, S3, and ASG/SSM mutation handlers."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

KIT_DIR = Path(os.environ.get("CLAW_PATCH_KIT_DIR", ".")).resolve()
REGION = os.environ.get("AWS_REGION", "")


class SSMCommandUnknown(RuntimeError):
    """Raised when a timed-out command cannot be reconciled as terminal."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical(value) + b"\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def artifact(value: str) -> Path:
    path = (KIT_DIR / value).resolve(strict=True)
    if KIT_DIR not in path.parents or not path.is_file():
        raise RuntimeError(f"unsafe or missing artifact: {value}")
    return path


def aws_json(*args: str) -> Any:
    result = subprocess.run(
        ["aws", *args, "--no-cli-pager"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"AWS command failed ({result.returncode}): {result.stderr.strip()}"
        )
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def aws_call(*args: str) -> None:
    result = subprocess.run(
        ["aws", *args, "--no-cli-pager"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"AWS command failed ({result.returncode}): {result.stderr.strip()}"
        )


def lambda_snapshot(function_name: str, qualifier: str | None = None) -> dict[str, Any]:
    args = [
        "lambda",
        "get-function",
        "--function-name",
        function_name,
        "--region",
        REGION,
        "--output",
        "json",
    ]
    if qualifier is not None:
        args.extend(["--qualifier", qualifier])
    return aws_json(*args)


def download_lambda(
    function_name: str, destination: Path, qualifier: str | None = None
) -> dict[str, Any]:
    response = lambda_snapshot(function_name, qualifier)
    with urllib.request.urlopen(response["Code"]["Location"], timeout=60) as remote:
        destination.write_bytes(remote.read())
    return response


def lambda_zip_sha256(path: Path) -> str:
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode()


def package_inventory(path: Path) -> dict[str, str]:
    inventory = {}
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith("/"):
                continue
            inventory[name] = sha256_bytes(archive.read(name))
    return inventory


def wait_lambda(function_name: str) -> None:
    aws_call(
        "lambda",
        "wait",
        "function-updated-v2",
        "--function-name",
        function_name,
        "--region",
        REGION,
    )


def list_esm(function_name: str) -> list[dict[str, Any]]:
    response = aws_json(
        "lambda",
        "list-event-source-mappings",
        "--function-name",
        function_name,
        "--region",
        REGION,
        "--output",
        "json",
    )
    return [
        {
            "uuid": item["UUID"],
            "state": item["State"],
            "event_source_arn": item.get("EventSourceArn"),
        }
        for item in response["EventSourceMappings"]
    ]


def assert_esm_state(
    function_name: str, expected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    current = list_esm(function_name)
    expected_by_uuid = {item["uuid"]: item for item in expected}
    current_by_uuid = {item["uuid"]: item for item in current}
    if current_by_uuid != expected_by_uuid:
        raise RuntimeError(f"ESM set changed concurrently for {function_name}")
    return current


def wait_esm(uuid: str, enabled: bool) -> None:
    expected = "Enabled" if enabled else "Disabled"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        value = aws_json(
            "lambda",
            "get-event-source-mapping",
            "--uuid",
            uuid,
            "--region",
            REGION,
            "--output",
            "json",
        )
        if value["State"] == expected:
            return
        if value["State"] in {"Failed", "Deleting"}:
            raise RuntimeError(f"ESM {uuid} entered {value['State']}")
        time.sleep(2)
    raise RuntimeError(f"ESM {uuid} did not reach {expected}")


def set_esm(uuid: str, enabled: bool) -> None:
    current = aws_json(
        "lambda",
        "get-event-source-mapping",
        "--uuid",
        uuid,
        "--region",
        REGION,
        "--output",
        "json",
    )["State"]
    expected = "Enabled" if enabled else "Disabled"
    if current == expected:
        return
    aws_call(
        "lambda",
        "update-event-source-mapping",
        "--uuid",
        uuid,
        "--enabled" if enabled else "--no-enabled",
        "--region",
        REGION,
    )
    wait_esm(uuid, enabled)


def backup_dir(run_dir: Path, operation_id: str) -> Path:
    return run_dir.resolve() / "backups" / operation_id


def metadata(
    target: Any,
    locator: Any,
    version: Any,
    backup: Any,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "target_sha256": sha256_bytes(canonical(target)),
        "locator_sha256": sha256_bytes(canonical(locator)),
        "version_sha256": sha256_bytes(canonical(version)),
        "backup_sha256": sha256_bytes(canonical(backup)),
    }


def lambda_backup_metadata(
    spec: dict[str, Any], directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    package = directory / "function.zip"
    return metadata(
        {
            "type": "lambda",
            "function": spec["function"],
            "alias": spec.get("alias"),
            "esm": spec.get("esm", False),
        },
        {"directory": str(directory)},
        {
            "revision_id": state["revision_id"],
            "code_sha256": state["code_sha256"],
            "variables": state.get("variables", {}),
            "alias": state.get("alias"),
            "esm": state.get("esm", []),
        },
        {
            "zip_sha256": sha256_file(package),
            "inventory_sha256": sha256_bytes(canonical(package_inventory(package))),
        },
    )


def backup_lambda(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state_path = directory / "state.json"
    if state_path.is_file():
        return verify_lambda_backup(spec, run_dir)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    package = directory / "function.zip"
    snapshot = download_lambda(spec["function"], package)
    config = snapshot["Configuration"]
    if lambda_zip_sha256(package) != config["CodeSha256"]:
        raise RuntimeError("Lambda package does not match its CodeSha256")
    state: dict[str, Any] = {
        "revision_id": config["RevisionId"],
        "code_sha256": config["CodeSha256"],
        "variables": config.get("Environment", {}).get("Variables", {}),
    }
    if spec.get("alias"):
        alias = aws_json(
            "lambda",
            "get-alias",
            "--function-name",
            spec["function"],
            "--name",
            spec["alias"],
            "--region",
            REGION,
            "--output",
            "json",
        )
        state["alias"] = {
            "name": alias["Name"],
            "function_version": alias["FunctionVersion"],
            "routing_config": alias.get("RoutingConfig", {}),
            "revision_id": alias["RevisionId"],
        }
    if spec.get("esm"):
        state["esm"] = list_esm(spec["function"])
    write_json(directory / "state.json", state)
    return lambda_backup_metadata(spec, directory, state)


def verify_lambda_backup(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    package = directory / "function.zip"
    if not package.is_file() or not zipfile.is_zipfile(package):
        raise RuntimeError("Lambda backup package is missing or invalid")
    if lambda_zip_sha256(package) != state["code_sha256"]:
        raise RuntimeError("Lambda backup package CodeSha256 changed")
    return lambda_backup_metadata(spec, directory, state)


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    for info in archive.infolist():
        name = PurePosixPath(info.filename)
        if name.is_absolute() or ".." in name.parts:
            raise RuntimeError(f"unsafe zip entry: {info.filename}")
        target = destination.joinpath(*name.parts)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))
        mode = (info.external_attr >> 16) & 0o777
        target.chmod(mode or 0o644)


def build_overlay(spec: dict[str, Any], current: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="claw-patch-v2-overlay-") as value:
        root = Path(value)
        with zipfile.ZipFile(current) as archive:
            safe_extract(archive, root)
        for name, item in spec["files"].items():
            destination = root.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = artifact(item["artifact"])
            shutil.copyfile(source, destination)
            destination.chmod(source.stat().st_mode & 0o777)
        python_files = [
            root.joinpath(*PurePosixPath(name).parts)
            for name in spec["files"]
            if name.endswith(".py")
        ]
        if python_files:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", *map(str, python_files)],
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError("overlay py_compile failed")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                name = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(name)
                info.date_time = (2020, 1, 1, 0, 0, 0)
                mode = path.stat().st_mode & 0o777
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def lambda_config(function_name: str) -> dict[str, Any]:
    return aws_json(
        "lambda",
        "get-function-configuration",
        "--function-name",
        function_name,
        "--region",
        REGION,
        "--output",
        "json",
    )


def lambda_config_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision_id": config["RevisionId"],
        "code_sha256": config["CodeSha256"],
        "variables": config.get("Environment", {}).get("Variables", {}),
    }


def same_lambda_config(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    revision: bool = True,
) -> bool:
    return (
        (not revision or left["revision_id"] == right["revision_id"])
        and left["code_sha256"] == right["code_sha256"]
        and left.get("variables", {}) == right.get("variables", {})
    )


def update_lambda_code(
    function_name: str, package: Path, expected_revision_id: str
) -> dict[str, Any]:
    aws_call(
        "lambda",
        "update-function-code",
        "--function-name",
        function_name,
        "--zip-file",
        f"fileb://{package}",
        "--revision-id",
        expected_revision_id,
        "--region",
        REGION,
    )
    wait_lambda(function_name)
    return lambda_config(function_name)


def update_lambda_environment(
    function_name: str,
    variables: dict[str, str],
    expected_revision_id: str,
    expected_code_sha256: str,
) -> dict[str, Any]:
    response = aws_json(
        "lambda",
        "update-function-configuration",
        "--function-name",
        function_name,
        "--revision-id",
        expected_revision_id,
        "--environment",
        json.dumps({"Variables": variables}, separators=(",", ":")),
        "--region",
        REGION,
        "--output",
        "json",
    )
    if response.get("Environment", {}).get("Variables", {}) != variables:
        raise RuntimeError("Lambda environment update response is incomplete")
    updated = wait_lambda_configuration(function_name)
    state = lambda_config_state(updated)
    if (
        state["code_sha256"] != expected_code_sha256
        or state["variables"] != variables
    ):
        raise RuntimeError("Lambda code or environment changed during update")
    return updated


def alias_state(function_name: str, alias_name: str) -> dict[str, Any]:
    alias = aws_json(
        "lambda",
        "get-alias",
        "--function-name",
        function_name,
        "--name",
        alias_name,
        "--region",
        REGION,
        "--output",
        "json",
    )
    return {
        "name": alias["Name"],
        "function_version": alias["FunctionVersion"],
        "routing_config": alias.get("RoutingConfig", {}),
        "revision_id": alias["RevisionId"],
    }


def same_alias(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["name"] == right["name"]
        and left["function_version"] == right["function_version"]
        and left.get("routing_config", {}) == right.get("routing_config", {})
    )


def update_alias(
    function_name: str,
    alias: dict[str, Any],
    expected_revision_id: str,
) -> dict[str, Any]:
    routing_config = alias.get("routing_config", {})
    if not routing_config:
        routing_config = {"AdditionalVersionWeights": {}}
    value = aws_json(
        "lambda",
        "update-alias",
        "--function-name",
        function_name,
        "--name",
        alias["name"],
        "--function-version",
        alias["function_version"],
        "--routing-config",
        json.dumps(routing_config, separators=(",", ":")),
        "--revision-id",
        expected_revision_id,
        "--region",
        REGION,
        "--output",
        "json",
    )
    return {
        "name": value["Name"],
        "function_version": value["FunctionVersion"],
        "routing_config": value.get("RoutingConfig", {}),
        "revision_id": value["RevisionId"],
    }


def publish_lambda_version(
    function_name: str,
    expected_revision_id: str,
    expected_code_sha256: str,
) -> str:
    value = aws_json(
        "lambda",
        "publish-version",
        "--function-name",
        function_name,
        "--revision-id",
        expected_revision_id,
        "--code-sha256",
        expected_code_sha256,
        "--region",
        REGION,
        "--output",
        "json",
    )
    if value["CodeSha256"] != expected_code_sha256:
        raise RuntimeError(f"published Lambda version changed for {function_name}")
    return value["Version"]


def lambda_versions(function_name: str) -> list[dict[str, Any]]:
    response = aws_json(
        "lambda",
        "list-versions-by-function",
        "--function-name",
        function_name,
        "--region",
        REGION,
        "--output",
        "json",
    )
    return response.get("Versions", [])


def matching_new_lambda_versions(
    versions: list[dict[str, Any]],
    versions_before: list[str],
    code_sha256: str,
    variables: dict[str, str],
) -> list[str]:
    before = set(versions_before)
    return [
        str(item["Version"])
        for item in versions
        if str(item.get("Version")) not in before
        and item.get("Version") != "$LATEST"
        and item.get("CodeSha256") == code_sha256
        and item.get("Environment", {}).get("Variables", {}) == variables
    ]


def build_lambda_target(
    spec: dict[str, Any],
    backed_up: dict[str, Any],
    directory: Path,
) -> Path:
    output = directory / "target.zip"
    if spec["mode"] == "package":
        shutil.copyfile(artifact(spec["target_package"]), output)
    elif spec["mode"] == "overlay":
        current = directory / "current.zip"
        snapshot = download_lambda(spec["function"], current)
        if (
            snapshot["Configuration"]["RevisionId"]
            != backed_up["revision_id"]
            or lambda_zip_sha256(current) != backed_up["code_sha256"]
        ):
            raise RuntimeError(
                f"Lambda {spec['function']} changed while building overlay"
            )
        build_overlay(spec, current, output)
    else:
        raise RuntimeError(f"unsupported Lambda mode: {spec['mode']}")
    return output


def apply_lambda(spec: dict[str, Any], run_dir: Path) -> None:
    function_name = spec["function"]
    backup_directory = backup_dir(run_dir, spec["id"])
    backed_up = load_json(backup_directory / "state.json")
    backed_up.setdefault("variables", {})
    applied_path = backup_directory / "applied.json"
    current = lambda_config_state(lambda_config(function_name))
    alias_before = None
    if spec.get("alias"):
        alias_before = alias_state(function_name, spec["alias"])
        if not applied_path.is_file() and alias_before != backed_up.get("alias"):
            raise RuntimeError(f"Lambda alias {spec['alias']} changed after backup")
    esm_before = list_esm(function_name) if spec.get("esm") else []
    if spec.get("esm"):
        assert_esm_state(function_name, backed_up.get("esm", []))

    target_values = resolve_lambda_env_targets(spec) if (
        spec.get("variables") or spec.get("secret_variables")
    ) else {}
    target_variables = {**backed_up["variables"], **target_values}

    if applied_path.is_file():
        applied_state = load_json(applied_path)
    else:
        if not same_lambda_config(current, backed_up):
            raise RuntimeError(f"Lambda {function_name} changed after backup")
        with tempfile.TemporaryDirectory(
            prefix="claw-patch-v2-lambda-"
        ) as value:
            output = build_lambda_target(
                spec,
                backed_up,
                Path(value),
            )
            applied_state = {
                "stage": "code-intent",
                "revision_id": backed_up["revision_id"],
                "code_sha256": lambda_zip_sha256(output),
                "variables": backed_up["variables"],
            }
            write_json(applied_path, applied_state)
            applied_config = update_lambda_code(
                function_name, output, backed_up["revision_id"]
            )
        current = lambda_config_state(applied_config)
        if (
            current["code_sha256"] != applied_state["code_sha256"]
            or current["variables"] != backed_up["variables"]
        ):
            raise RuntimeError("Lambda code update changed unexpected state")
        applied_state = {**current, "stage": "code-updated"}
        write_json(applied_path, applied_state)

    if applied_state["stage"] == "code-intent":
        if (
            current["code_sha256"] == applied_state["code_sha256"]
            and current["variables"] == backed_up["variables"]
        ):
            applied_state = {**current, "stage": "code-updated"}
            write_json(applied_path, applied_state)
        elif same_lambda_config(current, backed_up):
            with tempfile.TemporaryDirectory(
                prefix="claw-patch-v2-lambda-"
            ) as value:
                output = build_lambda_target(spec, backed_up, Path(value))
                if lambda_zip_sha256(output) != applied_state["code_sha256"]:
                    raise RuntimeError("Lambda target package changed during resume")
                updated = update_lambda_code(
                    function_name, output, backed_up["revision_id"]
                )
            current = lambda_config_state(updated)
            applied_state = {**current, "stage": "code-updated"}
            write_json(applied_path, applied_state)
        else:
            raise RuntimeError("Lambda code intent cannot be reconciled")

    if applied_state["stage"] == "code-updated":
        if not same_lambda_config(current, applied_state):
            raise RuntimeError("Lambda changed after code update")
        if target_values and current["variables"] != target_variables:
            applied_state = {
                **current,
                "stage": "environment-intent",
                "target_variables": target_variables,
            }
            write_json(applied_path, applied_state)
            updated = update_lambda_environment(
                function_name,
                target_variables,
                current["revision_id"],
                current["code_sha256"],
            )
            current = lambda_config_state(updated)
        applied_state = {**current, "stage": "environment-updated"}
        write_json(applied_path, applied_state)

    if applied_state["stage"] == "environment-intent":
        if (
            current["code_sha256"] == applied_state["code_sha256"]
            and current["variables"] == target_variables
        ):
            applied_state = {**current, "stage": "environment-updated"}
            write_json(applied_path, applied_state)
        elif same_lambda_config(current, applied_state):
            updated = update_lambda_environment(
                function_name,
                target_variables,
                current["revision_id"],
                current["code_sha256"],
            )
            current = lambda_config_state(updated)
            applied_state = {**current, "stage": "environment-updated"}
            write_json(applied_path, applied_state)
        else:
            raise RuntimeError("Lambda environment intent cannot be reconciled")

    alias_name = spec.get("alias")
    if alias_name:
        if applied_state["stage"] == "environment-updated":
            if spec.get("alias_target_version"):
                applied_state["published_version"] = str(
                    spec["alias_target_version"]
                )
                applied_state["stage"] = "published"
            elif spec.get("publish"):
                applied_state["versions_before"] = [
                    str(item["Version"])
                    for item in lambda_versions(function_name)
                ]
                applied_state["stage"] = "publish-intent"
            else:
                raise RuntimeError("alias operation has no target version policy")
            write_json(applied_path, applied_state)
        if applied_state["stage"] == "publish-intent":
            candidates = matching_new_lambda_versions(
                lambda_versions(function_name),
                applied_state["versions_before"],
                applied_state["code_sha256"],
                target_variables,
            )
            if len(candidates) > 1:
                raise RuntimeError("multiple Lambda versions appeared after publish")
            if candidates:
                version = candidates[0]
            else:
                if not same_lambda_config(current, applied_state):
                    raise RuntimeError("Lambda changed before version publish")
                version = publish_lambda_version(
                    function_name,
                    current["revision_id"],
                    current["code_sha256"],
                )
            post_publish = lambda_config_state(lambda_config(function_name))
            if (
                post_publish["code_sha256"] != applied_state["code_sha256"]
                or post_publish["variables"] != target_variables
            ):
                raise RuntimeError(
                    f"Lambda {function_name} changed after version publish"
                )
            current = post_publish
            applied_state = {
                **post_publish,
                "stage": "published",
                "published_version": version,
            }
            write_json(applied_path, applied_state)
        if applied_state["stage"] not in {"published", "complete"}:
            raise RuntimeError("Lambda alias cannot advance from current stage")
        version = applied_state["published_version"]
        target_alias = {
            "name": alias_name,
            "function_version": version,
            "routing_config": alias_before.get("routing_config", {}),
        }
        current_alias = alias_state(function_name, alias_name)
        if same_alias(current_alias, target_alias):
            applied_alias = current_alias
        elif current_alias == backed_up.get("alias"):
            applied_alias = update_alias(
                function_name,
                target_alias,
                current_alias["revision_id"],
            )
        else:
            raise RuntimeError(f"Lambda alias {alias_name} changed during apply")
        applied_state = {
            **applied_state,
            "stage": "complete",
            "alias": applied_alias,
        }
        write_json(applied_path, applied_state)
    elif applied_state["stage"] == "environment-updated":
        applied_state["stage"] = "complete"
        write_json(applied_path, applied_state)
    if spec.get("esm"):
        assert_esm_state(function_name, esm_before)


def rollback_lambda(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied = load_json(directory / "applied.json")
    state.setdefault("variables", {})
    applied.setdefault("variables", state["variables"])
    progress_path = directory / "rollback-progress.json"
    current = lambda_config_state(lambda_config(spec["function"]))
    esm_current = list_esm(spec["function"]) if spec.get("esm") else []
    if spec.get("esm"):
        assert_esm_state(spec["function"], state.get("esm", []))

    alias = state.get("alias")
    if alias:
        current_alias = alias_state(spec["function"], alias["name"])
        if not same_alias(current_alias, alias):
            applied_alias = applied.get("alias")
            if (
                applied_alias is None
                or not same_alias(current_alias, applied_alias)
                or current_alias["revision_id"] != applied_alias["revision_id"]
            ):
                raise RuntimeError(
                    f"Lambda alias {alias['name']} changed after apply"
                )
            write_json(
                progress_path,
                {"stage": "alias-intent", "alias": current_alias},
            )
            update_alias(
                spec["function"],
                alias,
                current_alias["revision_id"],
            )
        write_json(progress_path, {"stage": "alias-restored"})

    allowed_variables = [applied["variables"]]
    if isinstance(applied.get("target_variables"), dict):
        allowed_variables.append(applied["target_variables"])
    code_already_restored = current["code_sha256"] == state["code_sha256"]
    if not code_already_restored:
        receipt_gap = applied.get("stage") in {
            "code-intent",
            "environment-intent",
            "publish-intent",
        }
        semantic_target = (
            current["code_sha256"] == applied["code_sha256"]
            and current["variables"] in allowed_variables
        )
        if not semantic_target or (
            current["revision_id"] != applied["revision_id"]
            and not receipt_gap
        ):
            raise RuntimeError(f"Lambda {spec['function']} changed after apply")
        write_json(
            progress_path,
            {"stage": "code-intent", "revision_id": current["revision_id"]},
        )
        restored = update_lambda_code(
            spec["function"],
            directory / "function.zip",
            current["revision_id"],
        )
        current = lambda_config_state(restored)
        if (
            current["code_sha256"] != state["code_sha256"]
            or current["variables"] not in allowed_variables
        ):
            raise RuntimeError("Lambda code rollback changed unexpected state")
        write_json(
            progress_path,
            {"stage": "code-restored", "revision_id": current["revision_id"]},
        )
    elif (
        current["variables"] != state["variables"]
        and current["variables"] != applied["variables"]
    ):
        raise RuntimeError("Lambda environment changed during rollback")

    if current["variables"] != state["variables"]:
        if current["variables"] not in allowed_variables:
            raise RuntimeError("Lambda environment changed after patch")
        write_json(
            progress_path,
            {
                "stage": "environment-intent",
                "revision_id": current["revision_id"],
            },
        )
        restored = update_lambda_environment(
            spec["function"],
            state["variables"],
            current["revision_id"],
            state["code_sha256"],
        )
        current = lambda_config_state(restored)
    if (
        current["code_sha256"] != state["code_sha256"]
        or current["variables"] != state["variables"]
    ):
        raise RuntimeError("Lambda rollback did not restore code and environment")
    write_json(
        progress_path,
        {"stage": "complete", "revision_id": current["revision_id"]},
    )
    if spec.get("esm"):
        assert_esm_state(spec["function"], esm_current)


def rollback_verify_lambda(spec: dict[str, Any], run_dir: Path) -> bool:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    state.setdefault("variables", {})
    with tempfile.TemporaryDirectory(prefix="claw-patch-v2-rollback-") as value:
        current = Path(value) / "current.zip"
        download_lambda(spec["function"], current)
        if package_inventory(current) != package_inventory(directory / "function.zip"):
            return False
    if (
        lambda_config(spec["function"])
        .get("Environment", {})
        .get("Variables", {})
        != state["variables"]
    ):
        return False
    alias = state.get("alias")
    if alias:
        current_alias = aws_json(
            "lambda",
            "get-alias",
            "--function-name",
            spec["function"],
            "--name",
            alias["name"],
            "--region",
            REGION,
            "--output",
            "json",
        )
        if current_alias["FunctionVersion"] != alias["function_version"]:
            return False
        if current_alias.get("RoutingConfig", {}) != alias.get("routing_config", {}):
            return False
    if spec.get("esm"):
        expected_esm = {
            item["uuid"]: item["state"] for item in state.get("esm", [])
        }
        current_esm = {
            item["uuid"]: item["state"]
            for item in list_esm(spec["function"])
        }
        return current_esm == expected_esm
    return True


def s3_backup_metadata(
    spec: dict[str, Any], directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    backups = {
        item["key"]: sha256_file(directory / item["backup_name"])
        for item in state["objects"]
    }
    return metadata(
        {
            "type": "s3-bundle",
            "bucket": spec["bucket"],
            "keys": [item["key"] for item in spec["objects"]],
        },
        {"directory": str(directory)},
        {
            item["key"]: {
                "version_id": item.get("version_id"),
                "etag": item.get("etag"),
                "semantics_sha256": sha256_bytes(canonical(item["semantics"])),
            }
            for item in state["objects"]
        },
        backups,
    )


def s3_version_args(version_id: str | None) -> list[str]:
    return ["--version-id", version_id] if version_id else []


def s3_head(bucket: str, key: str, version_id: str | None = None) -> dict[str, Any]:
    return aws_json(
        "s3api",
        "head-object",
        "--bucket",
        bucket,
        "--key",
        key,
        *s3_version_args(version_id),
        "--region",
        REGION,
        "--output",
        "json",
    )


def s3_tags(bucket: str, key: str, version_id: str | None = None) -> list[dict[str, str]]:
    value = aws_json(
        "s3api",
        "get-object-tagging",
        "--bucket",
        bucket,
        "--key",
        key,
        *s3_version_args(version_id),
        "--region",
        REGION,
        "--output",
        "json",
    )
    return sorted(value.get("TagSet", []), key=lambda item: (item["Key"], item["Value"]))


def normalized_acl(value: dict[str, Any]) -> dict[str, Any]:
    owner = {
        key: item
        for key, item in value["Owner"].items()
        if key in {"ID", "DisplayName"}
    }
    grants = []
    for grant in value.get("Grants", []):
        grantee = {
            key: item
            for key, item in grant["Grantee"].items()
            if key in {"Type", "ID", "URI", "EmailAddress", "DisplayName"}
        }
        grants.append({"Grantee": grantee, "Permission": grant["Permission"]})
    grants.sort(key=lambda item: canonical(item))
    return {"Owner": owner, "Grants": grants}


def s3_acl(bucket: str, key: str, version_id: str | None = None) -> dict[str, Any]:
    value = aws_json(
        "s3api",
        "get-object-acl",
        "--bucket",
        bucket,
        "--key",
        key,
        *s3_version_args(version_id),
        "--region",
        REGION,
        "--output",
        "json",
    )
    return normalized_acl(value)


def s3_semantics(
    bucket: str,
    key: str,
    head: dict[str, Any],
    version_id: str | None = None,
) -> dict[str, Any]:
    fields = (
        "CacheControl",
        "ContentDisposition",
        "ContentEncoding",
        "ContentLanguage",
        "ContentType",
        "Expires",
        "Metadata",
        "ServerSideEncryption",
        "SSEKMSKeyId",
        "BucketKeyEnabled",
        "StorageClass",
        "WebsiteRedirectLocation",
        "ObjectLockMode",
        "ObjectLockRetainUntilDate",
        "ObjectLockLegalHoldStatus",
    )
    return {
        "headers": {field: head[field] for field in fields if field in head},
        "tags": s3_tags(bucket, key, version_id),
        "acl": s3_acl(bucket, key, version_id),
    }


def s3_put_args(semantics: dict[str, Any]) -> list[str]:
    headers = semantics["headers"]
    mapping = {
        "CacheControl": "--cache-control",
        "ContentDisposition": "--content-disposition",
        "ContentEncoding": "--content-encoding",
        "ContentLanguage": "--content-language",
        "ContentType": "--content-type",
        "Expires": "--expires",
        "ServerSideEncryption": "--server-side-encryption",
        "SSEKMSKeyId": "--ssekms-key-id",
        "StorageClass": "--storage-class",
        "WebsiteRedirectLocation": "--website-redirect-location",
        "ObjectLockMode": "--object-lock-mode",
        "ObjectLockRetainUntilDate": "--object-lock-retain-until-date",
        "ObjectLockLegalHoldStatus": "--object-lock-legal-hold-status",
    }
    result: list[str] = []
    for field, option in mapping.items():
        if field in headers:
            result.extend([option, str(headers[field])])
    if "Metadata" in headers:
        result.extend(
            ["--metadata", json.dumps(headers["Metadata"], separators=(",", ":"))]
        )
    if "BucketKeyEnabled" in headers:
        result.append(
            "--bucket-key-enabled"
            if headers["BucketKeyEnabled"]
            else "--no-bucket-key-enabled"
        )
    if semantics["tags"]:
        result.extend(
            [
                "--tagging",
                urllib.parse.urlencode(
                    [
                        (item["Key"], item["Value"])
                        for item in semantics["tags"]
                    ]
                ),
            ]
        )
    return result


def assert_default_private_acl(semantics: dict[str, Any]) -> None:
    acl = semantics["acl"]
    owner_id = acl["Owner"].get("ID")
    grants = acl["Grants"]
    if not owner_id or len(grants) != 1:
        raise RuntimeError("custom S3 ACL is not supported by this handler")
    grant = grants[0]
    if (
        grant["Permission"] != "FULL_CONTROL"
        or grant["Grantee"].get("Type") != "CanonicalUser"
        or grant["Grantee"].get("ID") != owner_id
    ):
        raise RuntimeError("custom S3 ACL is not supported by this handler")


def put_s3_object(
    bucket: str,
    key: str,
    source: Path,
    semantics: dict[str, Any],
    expected_etag: str,
) -> dict[str, Any]:
    assert_default_private_acl(semantics)
    response = aws_json(
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--body",
        str(source),
        "--if-match",
        expected_etag,
        *s3_put_args(semantics),
        "--region",
        REGION,
        "--output",
        "json",
    )
    version_id = response.get("VersionId")
    head = s3_head(bucket, key, version_id)
    current_semantics = s3_semantics(bucket, key, head, version_id)
    if current_semantics != semantics:
        raise RuntimeError(f"S3 object semantics changed for {key}")
    return {
        "etag": head["ETag"],
        "version_id": version_id,
        "semantics": current_semantics,
    }


def backup_s3(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state_path = directory / "state.json"
    if state_path.is_file():
        return verify_s3_backup(spec, run_dir)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    objects = []
    for index, item in enumerate(spec["objects"]):
        name = f"{index:02d}.bin"
        destination = directory / name
        head = s3_head(spec["bucket"], item["key"])
        version_id = head.get("VersionId")
        response = aws_json(
            "s3api",
            "get-object",
            "--bucket",
            spec["bucket"],
            "--key",
            item["key"],
            *s3_version_args(version_id),
            "--if-match",
            head["ETag"],
            "--region",
            REGION,
            str(destination),
        )
        if response.get("ETag") != head["ETag"]:
            raise RuntimeError(f"S3 object changed while backing up {item['key']}")
        objects.append(
            {
                "key": item["key"],
                "backup_name": name,
                "version_id": version_id,
                "etag": response.get("ETag"),
                "sha256": sha256_file(destination),
                "semantics": s3_semantics(
                    spec["bucket"], item["key"], head, version_id
                ),
            }
        )
    state = {"objects": objects}
    write_json(directory / "state.json", state)
    return s3_backup_metadata(spec, directory, state)


def verify_s3_backup(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    for item in state["objects"]:
        path = directory / item["backup_name"]
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise RuntimeError(f"invalid S3 backup for {item['key']}")
        assert_default_private_acl(item["semantics"])
    return s3_backup_metadata(spec, directory, state)


def s3_object_sha256(
    bucket: str,
    key: str,
    version_id: str | None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="claw-patch-v2-s3-hash-") as value:
        destination = Path(value) / "object"
        aws_json(
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            key,
            *s3_version_args(version_id),
            "--region",
            REGION,
            str(destination),
        )
        return sha256_file(destination)


def apply_s3(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    by_key = {item["key"]: item for item in state["objects"]}
    applied_path = directory / "applied.json"
    applied: dict[str, Any] = (
        load_json(applied_path) if applied_path.is_file() else {"objects": {}}
    )
    for item in spec["objects"]:
        source = artifact(item["target_artifact"])
        if sha256_file(source) != item["target_sha256"]:
            raise RuntimeError(f"target artifact hash mismatch for {item['key']}")
        backed_up = by_key[item["key"]]
        current = s3_head(spec["bucket"], item["key"])
        current_semantics = s3_semantics(
            spec["bucket"],
            item["key"],
            current,
            current.get("VersionId"),
        )
        applied_item = applied["objects"].get(item["key"])
        if applied_item is not None:
            if (
                current["ETag"] == applied_item["etag"]
                and current.get("VersionId") == applied_item.get("version_id")
                and current_semantics == applied_item["semantics"]
            ):
                continue
            raise RuntimeError(f"S3 object {item['key']} changed after apply")
        if (
            current["ETag"] != backed_up["etag"]
            or current.get("VersionId") != backed_up.get("version_id")
        ):
            if (
                current_semantics == backed_up["semantics"]
                and s3_object_sha256(
                    spec["bucket"],
                    item["key"],
                    current.get("VersionId"),
                )
                == item["target_sha256"]
            ):
                applied["objects"][item["key"]] = {
                    "etag": current["ETag"],
                    "version_id": current.get("VersionId"),
                    "semantics": current_semantics,
                }
                write_json(applied_path, applied)
                continue
            raise RuntimeError(f"S3 object {item['key']} changed after backup")
        if current_semantics != backed_up["semantics"]:
            raise RuntimeError(f"S3 object {item['key']} semantics changed after backup")
        applied["objects"][item["key"]] = put_s3_object(
            spec["bucket"],
            item["key"],
            source,
            backed_up["semantics"],
            backed_up["etag"],
        )
        write_json(applied_path, applied)


def rollback_s3(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied_path = directory / "applied.json"
    applied = load_json(applied_path) if applied_path.is_file() else {"objects": {}}
    rolled_back_path = directory / "rolled-back.json"
    rolled_back = (
        load_json(rolled_back_path) if rolled_back_path.is_file() else {"objects": {}}
    )
    for item in reversed(state["objects"]):
        current = s3_head(spec["bucket"], item["key"])
        current_semantics = s3_semantics(
            spec["bucket"],
            item["key"],
            current,
            current.get("VersionId"),
        )
        if (
            current["ETag"] == item["etag"]
            and current.get("VersionId") == item.get("version_id")
            and current_semantics == item["semantics"]
        ):
            continue
        restored_item = rolled_back["objects"].get(item["key"])
        if restored_item is not None and (
            current["ETag"] == restored_item["etag"]
            and current.get("VersionId") == restored_item.get("version_id")
            and current_semantics == restored_item["semantics"]
        ):
            continue
        if (
            current_semantics == item["semantics"]
            and s3_object_sha256(
                spec["bucket"],
                item["key"],
                current.get("VersionId"),
            )
            == item["sha256"]
        ):
            rolled_back["objects"][item["key"]] = {
                "etag": current["ETag"],
                "version_id": current.get("VersionId"),
                "semantics": current_semantics,
            }
            write_json(rolled_back_path, rolled_back)
            continue
        applied_item = applied["objects"].get(item["key"])
        if (
            applied_item is None
            or current["ETag"] != applied_item["etag"]
            or current.get("VersionId") != applied_item.get("version_id")
            or current_semantics != applied_item["semantics"]
        ):
            raise RuntimeError(f"S3 object {item['key']} changed after apply")
        rolled_back["objects"][item["key"]] = put_s3_object(
            spec["bucket"],
            item["key"],
            directory / item["backup_name"],
            item["semantics"],
            current["ETag"],
        )
        write_json(rolled_back_path, rolled_back)


def rollback_verify_s3(spec: dict[str, Any], run_dir: Path) -> bool:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    with tempfile.TemporaryDirectory(prefix="claw-patch-v2-s3-verify-") as value:
        for index, item in enumerate(state["objects"]):
            destination = Path(value) / str(index)
            aws_json(
                "s3api",
                "get-object",
                "--bucket",
                spec["bucket"],
                "--key",
                item["key"],
                "--region",
                REGION,
                str(destination),
            )
            if sha256_file(destination) != item["sha256"]:
                return False
            head = s3_head(spec["bucket"], item["key"])
            if (
                s3_semantics(
                    spec["bucket"],
                    item["key"],
                    head,
                    head.get("VersionId"),
                )
                != item["semantics"]
            ):
                return False
    return True


def asg_instances(asg_name: str) -> list[str]:
    response = aws_json(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        asg_name,
        "--region",
        REGION,
        "--output",
        "json",
    )
    groups = response["AutoScalingGroups"]
    if len(groups) != 1:
        raise RuntimeError(f"expected one ASG named {asg_name}")
    instances = [
        item["InstanceId"]
        for item in groups[0]["Instances"]
        if item["LifecycleState"] == "InService" and item["HealthStatus"] == "Healthy"
    ]
    if not instances:
        raise RuntimeError(f"ASG {asg_name} has no healthy instances")
    return sorted(instances)


def wait_ssm(command_id: str, instances: list[str]) -> dict[str, str]:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        response = aws_json(
            "ssm",
            "list-command-invocations",
            "--command-id",
            command_id,
            "--details",
            "--region",
            REGION,
            "--output",
            "json",
        )
        invocations = response["CommandInvocations"]
        if len(invocations) == len(instances) and all(
            item["Status"] in {"Success", "Failed", "TimedOut", "Cancelled"}
            for item in invocations
        ):
            outputs = {}
            for item in invocations:
                output = item["CommandPlugins"][0]["Output"]
                if item["Status"] != "Success":
                    raise RuntimeError(
                        f"SSM {command_id} failed on {item['InstanceId']}: {output}"
                    )
                outputs[item["InstanceId"]] = output
            return outputs
        time.sleep(2)
    reconciled = False
    try:
        aws_call(
            "ssm",
            "cancel-command",
            "--command-id",
            command_id,
            "--instance-ids",
            *instances,
            "--region",
            REGION,
        )
    finally:
        cancel_deadline = time.monotonic() + 60
        while time.monotonic() < cancel_deadline:
            response = aws_json(
                "ssm",
                "list-command-invocations",
                "--command-id",
                command_id,
                "--details",
                "--region",
                REGION,
                "--output",
                "json",
            )
            invocations = response["CommandInvocations"]
            if len(invocations) == len(instances) and all(
                item["Status"]
                in {"Success", "Failed", "TimedOut", "Cancelled", "Terminated"}
                for item in invocations
            ):
                reconciled = True
                break
            time.sleep(2)
    if not reconciled:
        raise SSMCommandUnknown(
            f"SSM command cancellation could not be reconciled: {command_id}"
        )
    raise RuntimeError(f"SSM command timed out: {command_id}")


def run_ssm(instances: list[str], commands: list[str], comment: str) -> dict[str, str]:
    response = aws_json(
        "ssm",
        "send-command",
        "--document-name",
        "AWS-RunShellScript",
        "--instance-ids",
        *instances,
        "--comment",
        comment,
        "--parameters",
        json.dumps({"commands": commands}, separators=(",", ":")),
        "--region",
        REGION,
        "--output",
        "json",
    )
    return wait_ssm(response["Command"]["CommandId"], instances)


def parse_hashes(output: str, paths: list[str]) -> dict[str, str]:
    lines = output.splitlines()
    result = {}
    for path in paths:
        line = next((value for value in lines if value.endswith(f"  {path}")), None)
        if line is None:
            raise RuntimeError(f"missing hash output for {path}")
        result[path] = line.split()[0]
    return result


def parse_file_metadata(output: str, paths: list[str]) -> dict[str, str]:
    lines = output.splitlines()
    result = {}
    for path in paths:
        suffix = f"  {path}"
        line = next(
            (value for value in lines if value.startswith("META ") and value.endswith(suffix)),
            None,
        )
        if line is None:
            raise RuntimeError(f"missing metadata output for {path}")
        result[path] = line.removeprefix("META ").removesuffix(suffix)
    return result


def host_backup_metadata(
    spec: dict[str, Any], directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    versions = {
        instance: {
            "hashes": data["original_hashes"],
            "metadata": data["original_metadata"],
        }
        for instance, data in state["instances"].items()
    }
    backups = {
        instance: {
            "hashes": data["backup_hashes"],
            "metadata": data["backup_metadata"],
        }
        for instance, data in state["instances"].items()
    }
    return metadata(
        {
            "type": "host-bundle",
            "asg": spec["asg"],
            "paths": sorted(item["destination"] for item in spec["files"]),
        },
        {
            "remote_backup_dir": state["remote_backup_dir"],
            "instances": sorted(state["instances"]),
            "local_state": str(directory / "state.json"),
        },
        versions,
        backups,
    )


def backup_host(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state_path = directory / "state.json"
    if state_path.is_file():
        return verify_host_backup(spec, run_dir)
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("partial host backup exists without state.json")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    token = sha256_bytes(f"{run_dir.resolve()}:{spec['id']}".encode())[:24]
    remote = f"/var/lib/claw-patch-v2/{token}"
    creating = f"{remote}.creating"
    instances = asg_instances(spec["asg"])
    paths = [item["destination"] for item in spec["files"]]
    commands = [
        "set -euo pipefail",
        f"test ! -e {shlex.quote(creating)}",
        f"if [ ! -d {shlex.quote(remote)} ]; then",
        f"install -d -m 0700 {shlex.quote(creating)}",
    ]
    for index, path in enumerate(paths):
        target = f"{creating}/{index:02d}.backup"
        commands.append(
            f"test -f {shlex.quote(path)} && "
            f"cp --preserve=mode,ownership,timestamps "
            f"{shlex.quote(path)} {shlex.quote(target)}"
        )
    commands.extend(
        [
            f"mv {shlex.quote(creating)} {shlex.quote(remote)}",
            "fi",
        ]
    )
    commands.append(
        "sha256sum "
        + " ".join(shlex.quote(path) for path in paths)
        + " "
        + " ".join(
            shlex.quote(f"{remote}/{index:02d}.backup")
            for index in range(len(paths))
        )
    )
    commands.append(
        "stat -c 'META %U:%G:%a  %n' "
        + " ".join(shlex.quote(path) for path in paths)
        + " "
        + " ".join(
            shlex.quote(f"{remote}/{index:02d}.backup")
            for index in range(len(paths))
        )
    )
    outputs = run_ssm(instances, commands, "claw-patch-v2 host backup")
    state: dict[str, Any] = {
        "remote_backup_dir": remote,
        "instances": {},
    }
    backup_paths = [
        f"{remote}/{index:02d}.backup" for index in range(len(paths))
    ]
    for instance, output in outputs.items():
        state["instances"][instance] = {
            "original_hashes": parse_hashes(output, paths),
            "backup_hashes": parse_hashes(output, backup_paths),
            "original_metadata": parse_file_metadata(output, paths),
            "backup_metadata": parse_file_metadata(output, backup_paths),
        }
    write_json(directory / "state.json", state)
    return host_backup_metadata(spec, directory, state)


def verify_host_backup(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    instances = sorted(state["instances"])
    remote = state["remote_backup_dir"]
    backup_paths = [
        f"{remote}/{index:02d}.backup" for index in range(len(spec["files"]))
    ]
    outputs = run_ssm(
        instances,
        [
            "set -euo pipefail",
            "sha256sum " + " ".join(map(shlex.quote, backup_paths)),
            "stat -c 'META %U:%G:%a  %n' "
            + " ".join(map(shlex.quote, backup_paths)),
        ],
        "claw-patch-v2 host backup verify",
    )
    for instance, output in outputs.items():
        observed = parse_hashes(output, backup_paths)
        if observed != state["instances"][instance]["backup_hashes"]:
            raise RuntimeError(f"host backup changed on {instance}")
        observed_metadata = parse_file_metadata(output, backup_paths)
        if observed_metadata != state["instances"][instance]["backup_metadata"]:
            raise RuntimeError(f"host backup metadata changed on {instance}")
    return host_backup_metadata(spec, directory, state)


def temporary_s3_prefix(spec: dict[str, Any], run_dir: Path) -> str:
    token = sha256_bytes(f"{run_dir.resolve()}:{spec['id']}".encode())[:24]
    return f"patch-staging/claw-patch-v2/{token}"


def host_services(spec: dict[str, Any]) -> list[str]:
    services = spec.get("services", ["host-agent.service"])
    if (
        not isinstance(services, list)
        or not all(
            isinstance(service, str)
            and service
            and "\x00" not in service
            and not service.startswith("-")
            for service in services
        )
    ):
        raise RuntimeError("host services must be a non-empty safe string array")
    return services


def service_commands(spec: dict[str, Any]) -> list[str]:
    commands = []
    for service in host_services(spec):
        quoted = shlex.quote(service)
        commands.extend(
            [
                f"systemctl restart -- {quoted}",
                f"systemctl is-active --quiet -- {quoted}",
            ]
        )
    return commands


def activation_command(spec: dict[str, Any]) -> str | None:
    activation = spec.get("activation")
    if activation is None:
        return None
    if not isinstance(activation, dict) or set(activation) != {
        "interpreter",
        "executable",
        "env",
        "argv",
    }:
        raise RuntimeError("host activation contract is invalid")
    if activation["interpreter"] != "bash":
        raise RuntimeError("host activation supports only the bash interpreter")
    executable = activation["executable"]
    path = PurePosixPath(executable) if isinstance(executable, str) else None
    if path is None or not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("host activation executable must be an absolute safe path")
    environment = activation["env"]
    if not isinstance(environment, dict) or not all(
        isinstance(key, str)
        and re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
        and isinstance(value, (str, int, float, bool))
        and "\x00" not in str(value)
        for key, value in environment.items()
    ):
        raise RuntimeError("host activation environment is invalid")
    argv = activation["argv"]
    if not isinstance(argv, list) or not all(
        isinstance(value, str) and "\x00" not in value for value in argv
    ):
        raise RuntimeError("host activation argv is invalid")
    assignments = " ".join(
        f"{key}={shlex.quote(str(value))}"
        for key, value in sorted(environment.items())
    )
    arguments = " ".join(shlex.quote(value) for value in argv)
    return (
        f"env {assignments} bash {shlex.quote(executable)}"
        + (f" {arguments}" if arguments else "")
    )


def require_versioned_bucket(bucket: str) -> None:
    value = aws_json(
        "s3api",
        "get-bucket-versioning",
        "--bucket",
        bucket,
        "--region",
        REGION,
        "--output",
        "json",
    )
    if value.get("Status") != "Enabled":
        raise RuntimeError("host staging bucket must have versioning enabled")


def delete_temp_objects(bucket: str, objects: list[dict[str, str | None]]) -> None:
    failures = []
    for item in objects:
        command = [
            "aws",
            "s3api",
            "delete-object",
            "--bucket",
            bucket,
            "--key",
            str(item["key"]),
        ]
        if item.get("version_id"):
            command.extend(["--version-id", str(item["version_id"])])
        command.extend(
            [
                "--region",
                REGION,
                "--no-cli-pager",
            ]
        )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            failures.append(str(item["key"]))
    if failures:
        raise RuntimeError(
            "failed to delete exact temporary objects: " + ", ".join(failures)
        )


def assert_host_snapshot(
    spec: dict[str, Any],
    state: dict[str, Any],
    *,
    allow_target: bool,
    allow_mixed: bool = False,
) -> dict[str, dict[str, str]]:
    instances = sorted(state["instances"])
    if asg_instances(spec["asg"]) != instances:
        raise RuntimeError(f"ASG {spec['asg']} instance set changed after backup")
    paths = [item["destination"] for item in spec["files"]]
    outputs = run_ssm(
        instances,
        [
            "set -euo pipefail",
            "sha256sum " + " ".join(map(shlex.quote, paths)),
            "stat -c 'META %U:%G:%a  %n' " + " ".join(map(shlex.quote, paths)),
        ],
        "claw-patch-v2 host concurrency gate",
    )
    result = {}
    target = {item["destination"]: item["target_sha256"] for item in spec["files"]}
    target_metadata = {
        item["destination"]: (
            f"{item['owner']}:{item['group']}:{item['mode'].lstrip('0')}"
        )
        for item in spec["files"]
    }
    for instance, output in outputs.items():
        hashes = parse_hashes(output, paths)
        metadata_value = parse_file_metadata(output, paths)
        original = state["instances"][instance]
        if allow_mixed:
            hashes_allowed = all(
                value in {original["original_hashes"][path], target[path]}
                for path, value in hashes.items()
            )
        else:
            allowed_hashes = [original["original_hashes"]]
            if allow_target:
                allowed_hashes.append(target)
            hashes_allowed = hashes in allowed_hashes
        if not hashes_allowed:
            raise RuntimeError(f"host files changed concurrently on {instance}")
        for path, value in hashes.items():
            expected_metadata = (
                original["original_metadata"][path]
                if value == original["original_hashes"][path]
                else target_metadata[path]
            )
            if metadata_value[path] != expected_metadata:
                raise RuntimeError(f"host file metadata changed on {instance}")
        result[instance] = hashes
    return result


def rollout_batches(
    instances: list[str],
    spec: dict[str, Any],
) -> list[list[str]]:
    if not instances:
        return []
    rollout = spec.get("rollout")
    if rollout is None:
        return [instances]
    if not isinstance(rollout, dict) or set(rollout) != {
        "strategy",
        "canary_instance_id",
        "max_concurrency",
        "bake_seconds",
    }:
        raise RuntimeError("host rollout contract is invalid")
    if rollout["strategy"] != "canary-waves":
        raise RuntimeError("host rollout strategy is unsupported")
    canary = rollout["canary_instance_id"]
    concurrency = rollout["max_concurrency"]
    bake_seconds = rollout["bake_seconds"]
    if (
        not isinstance(canary, str)
        or not canary
        or not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or concurrency < 1
        or not isinstance(bake_seconds, int)
        or isinstance(bake_seconds, bool)
        or not 0 <= bake_seconds <= 3600
    ):
        raise RuntimeError("host rollout values are invalid")
    remaining = [instance for instance in instances if instance != canary]
    batches = [[canary]] if canary in instances else []
    batches.extend(
        remaining[index : index + concurrency]
        for index in range(0, len(remaining), concurrency)
    )
    return batches


def enforce_canary_bake(directory: Path, rollout: dict[str, Any]) -> None:
    path = directory / "canary-bake.json"
    now = time.time()
    if path.is_file():
        progress = load_json(path)
        if (
            progress.get("canary_instance_id") != rollout["canary_instance_id"]
            or progress.get("bake_seconds") != rollout["bake_seconds"]
            or not isinstance(progress.get("target_observed_at_epoch"), (int, float))
        ):
            raise RuntimeError("host canary bake progress is invalid")
    else:
        progress = {
            "canary_instance_id": rollout["canary_instance_id"],
            "bake_seconds": rollout["bake_seconds"],
            "target_observed_at_epoch": now,
        }
        write_json(path, progress)
    if "bake_completed_at_epoch" in progress:
        return
    remaining = max(
        0.0,
        rollout["bake_seconds"]
        - (time.time() - progress["target_observed_at_epoch"]),
    )
    if remaining:
        time.sleep(remaining)
    progress["bake_completed_at_epoch"] = time.time()
    write_json(path, progress)


def apply_host(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    instances = sorted(state["instances"])
    rollout = spec.get("rollout")
    if rollout is not None:
        canary = rollout.get("canary_instance_id")
        if canary not in instances:
            raise RuntimeError("host rollout canary is not in the frozen instance set")
    observed = assert_host_snapshot(
        spec,
        state,
        allow_target=True,
        allow_mixed=True,
    )
    target_hashes = {
        item["destination"]: item["target_sha256"] for item in spec["files"]
    }
    pending = [
        instance
        for instance in instances
        if observed.get(instance) != target_hashes
    ]
    prefix = temporary_s3_prefix(spec, run_dir)
    temporary_objects: list[dict[str, str | None]] = []
    safe_to_cleanup = True
    try:
        require_versioned_bucket(spec["bucket"])
        for index, item in enumerate(spec["files"]):
            source = artifact(item["target_artifact"])
            if sha256_file(source) != item["target_sha256"]:
                raise RuntimeError(
                    f"host artifact hash mismatch: {item['target_artifact']}"
                )
            key = f"{prefix}/{index:02d}-{Path(item['destination']).name}"
            response = aws_json(
                "s3api",
                "put-object",
                "--bucket",
                spec["bucket"],
                "--key",
                key,
                "--body",
                str(source),
                "--region",
                REGION,
                "--output",
                "json",
            )
            temporary_objects.append(
                {
                    "key": key,
                    "version_id": response["VersionId"],
                    "etag": response["ETag"],
                }
            )
        stage = f"/var/tmp/claw-patch-v2-{prefix.rsplit('/', 1)[-1]}"
        remote_backup = state["remote_backup_dir"]
        commands = [
            "set -euo pipefail",
            f"install -d -m 0700 {shlex.quote(stage)}",
        ]
        staged = []
        for index, (item, temporary) in enumerate(
            zip(spec["files"], temporary_objects, strict=True)
        ):
            key = str(temporary["key"])
            path = f"{stage}/{index:02d}-{Path(item['destination']).name}"
            staged.append(path)
            commands.append(
                "aws s3api get-object "
                f"--bucket {shlex.quote(spec['bucket'])} "
                f"--key {shlex.quote(key)} "
                f"--version-id {shlex.quote(str(temporary['version_id']))} "
                f"--if-match {shlex.quote(str(temporary['etag']))} "
                f"--region {shlex.quote(REGION)} "
                f"{shlex.quote(path)} >/dev/null"
            )
            commands.append(
                f"test $(sha256sum {shlex.quote(path)} | awk '{{print $1}}') "
                f"= {shlex.quote(item['target_sha256'])}"
            )
        python_files = [
            staged[index]
            for index, item in enumerate(spec["files"])
            if item["destination"].endswith(".py")
        ]
        shell_files = [
            staged[index]
            for index, item in enumerate(spec["files"])
            if item["destination"].endswith(".sh")
        ]
        if python_files:
            commands.append("python3 -m py_compile " + " ".join(map(shlex.quote, python_files)))
        if shell_files:
            commands.append("bash -n " + " ".join(map(shlex.quote, shell_files)))
        restore_commands = []
        for index, item in enumerate(spec["files"]):
            restore_commands.append(
                f"cp --preserve=mode,ownership,timestamps "
                f"{shlex.quote(f'{remote_backup}/{index:02d}.backup')} "
                f"{shlex.quote(item['destination'])}"
            )
        activation = activation_command(spec)
        recovery_commands = [*restore_commands]
        if activation:
            recovery_commands.append(f"{activation} || true")
        recovery_commands.extend(
            f"systemctl restart -- {shlex.quote(service)} || true"
            for service in host_services(spec)
        )
        recovery_body = "; ".join(recovery_commands)
        commands.extend(
            [
                "changed=0",
                "rollback_patch() { "
                "rc=$?; "
                'if [ "${changed:-0}" = 1 ]; then '
                + recovery_body
                + "; fi; "
                + f"rm -rf -- {shlex.quote(stage)}; exit \"$rc\"; "
                "}",
                "trap rollback_patch ERR INT TERM",
                "changed=1",
            ]
        )
        for index, item in enumerate(spec["files"]):
            commands.append(
                f"install -o {shlex.quote(item['owner'])} "
                f"-g {shlex.quote(item['group'])} -m {shlex.quote(item['mode'])} "
                f"{shlex.quote(staged[index])} {shlex.quote(item['destination'])}"
            )
        if activation:
            commands.append(activation)
        commands.extend(service_commands(spec))
        if spec.get("metrics_required"):
            commands.extend(
                [
                    "metrics_ready=0",
                    (
                        "for attempt in $(seq 1 20); do "
                        "if curl -fsS --max-time 10 "
                        "http://127.0.0.1:8899/metrics "
                        "| grep -q '^openclaw_agent_build_info'; then "
                        "metrics_ready=1; break; fi; sleep 1; done"
                    ),
                    'test "$metrics_ready" = 1',
                ]
            )
        commands.extend(
            [
                "changed=0",
                "trap - ERR INT TERM",
                f"rm -rf -- {shlex.quote(stage)}",
            ]
        )
        try:
            safe_to_cleanup = False
            batches = rollout_batches(pending, spec)
            for index, batch in enumerate(batches):
                is_canary = (
                    rollout is not None
                    and batch == [rollout["canary_instance_id"]]
                )
                if rollout is not None and not is_canary:
                    enforce_canary_bake(directory, rollout)
                transaction_index = commands.index("changed=0")
                for instance in batch:
                    original = state["instances"][instance]
                    cas_commands = []
                    for item in spec["files"]:
                        path = item["destination"]
                        target_metadata = (
                            f"{item['owner']}:{item['group']}:"
                            f"{item['mode'].lstrip('0')}"
                        )
                        original_hash = original["original_hashes"][path]
                        original_metadata = original["original_metadata"][path]
                        cas_commands.append(
                            f"current_hash=$(sha256sum {shlex.quote(path)} "
                            "| awk '{{print $1}}'); "
                            f"current_meta=$(stat -c '%U:%G:%a' {shlex.quote(path)}); "
                            f"{{ test \"$current_hash\" = {shlex.quote(original_hash)} "
                            f"&& test \"$current_meta\" = "
                            f"{shlex.quote(original_metadata)}; }} || "
                            f"{{ test \"$current_hash\" = "
                            f"{shlex.quote(item['target_sha256'])} "
                            f"&& test \"$current_meta\" = "
                            f"{shlex.quote(target_metadata)}; }}"
                        )
                    instance_commands = [
                        *commands[:transaction_index],
                        *cas_commands,
                        *commands[transaction_index:],
                    ]
                    run_ssm(
                        [instance],
                        instance_commands,
                        "claw-patch-v2 host bundle apply",
                    )
                if is_canary and index + 1 < len(batches):
                    enforce_canary_bake(directory, rollout)
            safe_to_cleanup = True
        except SSMCommandUnknown:
            raise
        except Exception:
            rollback_host(spec, run_dir)
            if not rollback_verify_host(spec, run_dir):
                raise RuntimeError("host apply and automatic rollback both failed")
            safe_to_cleanup = True
            raise
        applied = {
            "instances": instances,
            "target_hashes": target_hashes,
        }
        write_json(directory / "applied.json", applied)
    finally:
        if temporary_objects and safe_to_cleanup:
            delete_temp_objects(spec["bucket"], temporary_objects)


def rollback_host(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    instances = sorted(state["instances"])
    assert_host_snapshot(spec, state, allow_target=True, allow_mixed=True)
    remote = state["remote_backup_dir"]
    commands = ["set -euo pipefail"]
    for index, item in enumerate(spec["files"]):
        source = f"{remote}/{index:02d}.backup"
        commands.append(
            f"test -f {shlex.quote(source)} && "
            f"cp --preserve=mode,ownership,timestamps "
            f"{shlex.quote(source)} {shlex.quote(item['destination'])}"
        )
    activation = activation_command(spec)
    if activation:
        commands.append(activation)
    commands.extend(service_commands(spec))
    run_ssm(instances, commands, "claw-patch-v2 host bundle rollback")


def rollback_verify_host(spec: dict[str, Any], run_dir: Path) -> bool:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    instances = sorted(state["instances"])
    paths = [item["destination"] for item in spec["files"]]
    outputs = run_ssm(
        instances,
        [
            "set -euo pipefail",
            *[
                f"systemctl is-active --quiet -- {shlex.quote(service)}"
                for service in host_services(spec)
            ],
            "sha256sum " + " ".join(map(shlex.quote, paths)),
            "stat -c 'META %U:%G:%a  %n' " + " ".join(map(shlex.quote, paths)),
        ],
        "claw-patch-v2 host rollback verify",
    )
    for instance, output in outputs.items():
        if parse_hashes(output, paths) != state["instances"][instance]["original_hashes"]:
            return False
        if (
            parse_file_metadata(output, paths)
            != state["instances"][instance]["original_metadata"]
        ):
            return False
    return True


def edge_install_line(spec: dict[str, Any]) -> str:
    logging = spec["logging"]
    values = {
        "ENGINE_REDIS_ENDPOINT": logging["engine_redis_endpoint"],
        "EDGE_LISTEN_PORT": str(logging.get("edge_listen_port", 8080)),
        "LOGGING_ENABLED": "true",
        "ASSETS_BUCKET": logging["assets_bucket"],
        "AWS_REGION": logging["region"],
        "FIREHOSE_DELIVERY_STREAM": logging["firehose_delivery_stream"],
    }
    if any(
        not isinstance(value, str) or not value or "\n" in value or "\x00" in value
        for value in values.values()
    ):
        raise RuntimeError("edge launch-template logging values are invalid")
    assignments = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in values.items()
    )
    return f"{assignments} bash /opt/openclaw-edge/install-edge.sh"


def is_edge_install_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    try:
        words = shlex.split(stripped, comments=True, posix=True)
    except ValueError as exc:
        raise RuntimeError("edge launch-template user data has invalid shell syntax") from exc
    if words[-2:] != ["bash", "/opt/openclaw-edge/install-edge.sh"]:
        return False
    return all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word) is not None
        for word in words[:-2]
    )


def render_edge_user_data(user_data: str, spec: dict[str, Any]) -> str:
    lines = user_data.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if is_edge_install_line(line)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "edge launch-template must contain exactly one install-edge invocation"
        )
    index = matches[0]
    original = lines[index]
    content = original.rstrip("\r\n")
    newline = original[len(content) :]
    indentation = content[: len(content) - len(content.lstrip())]
    lines[index] = indentation + edge_install_line(spec) + newline
    return "".join(lines)


def asg_launch_template(asg_name: str) -> dict[str, str]:
    response = aws_json(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        asg_name,
        "--region",
        REGION,
        "--output",
        "json",
    )
    groups = response["AutoScalingGroups"]
    if len(groups) != 1:
        raise RuntimeError(f"expected one ASG named {asg_name}")
    group = groups[0]
    if group.get("MixedInstancesPolicy"):
        raise RuntimeError("edge launch-template mutation does not support mixed policy")
    template = group.get("LaunchTemplate")
    if not isinstance(template, dict):
        raise TypeError("ASG does not have a direct launch-template pointer")
    template_id = template.get("LaunchTemplateId")
    version = template.get("Version")
    if (
        not isinstance(template_id, str)
        or not template_id
        or not isinstance(version, str)
        or not version.isdigit()
    ):
        raise RuntimeError("ASG launch-template pointer must use a numeric version")
    return {"LaunchTemplateId": template_id, "Version": version}


def launch_template_version(pointer: dict[str, str]) -> dict[str, Any]:
    response = aws_json(
        "ec2",
        "describe-launch-template-versions",
        "--launch-template-id",
        pointer["LaunchTemplateId"],
        "--versions",
        pointer["Version"],
        "--region",
        REGION,
        "--output",
        "json",
    )
    versions = response["LaunchTemplateVersions"]
    if len(versions) != 1:
        raise RuntimeError("expected exactly one launch-template version")
    return versions[0]


def launch_template_backup_metadata(
    spec: dict[str, Any], directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    return metadata(
        {"type": "asg-launch-template", "asg": spec["asg"]},
        {"directory": str(directory), "pointer": state["pointer"]},
        state["version"]["LaunchTemplateData"],
        {
            "version_number": state["version"]["VersionNumber"],
            "user_data_sha256": sha256_bytes(
                str(
                    state["version"]["LaunchTemplateData"].get("UserData", "")
                ).encode()
            ),
        },
    )


def backup_launch_template(
    spec: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state_path = directory / "state.json"
    if state_path.is_file():
        return verify_launch_template_backup(spec, run_dir)
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("partial launch-template backup exists without state.json")
    pointer = asg_launch_template(spec["asg"])
    version = launch_template_version(pointer)
    state = {"pointer": pointer, "version": version}
    write_json(state_path, state)
    return launch_template_backup_metadata(spec, directory, state)


def verify_launch_template_backup(
    spec: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    observed = launch_template_version(state["pointer"])
    if observed["LaunchTemplateData"] != state["version"]["LaunchTemplateData"]:
        raise RuntimeError("launch-template backup version changed")
    return launch_template_backup_metadata(spec, directory, state)


def apply_launch_template(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied_path = directory / "applied.json"
    if applied_path.is_file():
        applied = load_json(applied_path)
        if asg_launch_template(spec["asg"]) != applied["pointer"]:
            raise RuntimeError("ASG launch-template pointer drifted after apply")
        return
    original = state["pointer"]
    version = launch_template_version(original)
    encoded = version["LaunchTemplateData"].get("UserData")
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("edge launch-template has no user data")
    try:
        user_data = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("edge launch-template user data is not valid UTF-8") from exc
    rendered = render_edge_user_data(user_data, spec)
    target_user_data_sha256 = sha256_bytes(rendered.encode())
    target_user_data = base64.b64encode(rendered.encode()).decode()
    current_pointer = asg_launch_template(spec["asg"])
    if current_pointer != original:
        current_version = launch_template_version(current_pointer)
        current_user_data = current_version["LaunchTemplateData"].get("UserData", "")
        if (
            current_pointer["LaunchTemplateId"] == original["LaunchTemplateId"]
            and current_user_data == target_user_data
        ):
            write_json(
                applied_path,
                {
                    "pointer": current_pointer,
                    "created_version": current_pointer["Version"],
                    "target_user_data_sha256": target_user_data_sha256,
                },
            )
            return
        raise RuntimeError("ASG launch-template pointer changed after backup")
    if rendered == user_data:
        write_json(
            applied_path,
            {
                "pointer": original,
                "created_version": None,
                "target_user_data_sha256": target_user_data_sha256,
            },
        )
        return
    response = aws_json(
        "ec2",
        "create-launch-template-version",
        "--launch-template-id",
        original["LaunchTemplateId"],
        "--source-version",
        original["Version"],
        "--launch-template-data",
        json.dumps(
            {"UserData": base64.b64encode(rendered.encode()).decode()},
            separators=(",", ":"),
        ),
        "--version-description",
        f"claw-patch-v2:{spec['id']}",
        "--region",
        REGION,
        "--output",
        "json",
    )
    created = str(response["LaunchTemplateVersion"]["VersionNumber"])
    target = {
        "LaunchTemplateId": original["LaunchTemplateId"],
        "Version": created,
    }
    if asg_launch_template(spec["asg"]) != original:
        raise RuntimeError("ASG launch-template pointer changed before compare-and-swap")
    aws_call(
        "autoscaling",
        "update-auto-scaling-group",
        "--auto-scaling-group-name",
        spec["asg"],
        "--launch-template",
        f"LaunchTemplateId={original['LaunchTemplateId']},Version={created}",
        "--region",
        REGION,
    )
    if asg_launch_template(spec["asg"]) != target:
        raise RuntimeError("ASG launch-template pointer did not reach target")
    write_json(
        applied_path,
        {
            "pointer": target,
            "created_version": created,
            "target_user_data_sha256": target_user_data_sha256,
        },
    )


def rollback_launch_template(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied = load_json(directory / "applied.json")
    current = asg_launch_template(spec["asg"])
    original = state["pointer"]
    if current == original:
        return
    if current != applied["pointer"]:
        raise RuntimeError("ASG launch-template pointer changed after patch")
    aws_call(
        "autoscaling",
        "update-auto-scaling-group",
        "--auto-scaling-group-name",
        spec["asg"],
        "--launch-template",
        (
            f"LaunchTemplateId={original['LaunchTemplateId']},"
            f"Version={original['Version']}"
        ),
        "--region",
        REGION,
    )


def rollback_verify_launch_template(
    spec: dict[str, Any], run_dir: Path
) -> bool:
    state = load_json(backup_dir(run_dir, spec["id"]) / "state.json")
    return asg_launch_template(spec["asg"]) == state["pointer"]


def optional_aws_json(not_found: tuple[str, ...], *args: str) -> Any:
    try:
        return aws_json(*args)
    except RuntimeError as exc:
        if any(marker in str(exc) for marker in not_found):
            return None
        raise


def wait_lambda_configuration(function_name: str) -> dict[str, Any]:
    wait_lambda(function_name)
    config = lambda_config(function_name)
    if config.get("LastUpdateStatus") == "Failed":
        raise RuntimeError(
            f"Lambda configuration update failed for {function_name}: "
            f"{config.get('LastUpdateStatusReason', 'unknown reason')}"
        )
    return config


def secret_json(secret_id: str) -> dict[str, Any] | None:
    response = optional_aws_json(
        ("ResourceNotFoundException",),
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        secret_id,
        "--region",
        REGION,
        "--output",
        "json",
    )
    if response is None:
        return None
    value = response.get("SecretString")
    if not isinstance(value, str):
        raise TypeError(f"secret {secret_id} does not contain SecretString")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"secret {secret_id} is not JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"secret {secret_id} must contain a JSON object")
    return payload


def pagination_key(secret_id: str, json_key: str = "key") -> str:
    payload = secret_json(secret_id)
    if payload is None:
        raise RuntimeError(f"secret {secret_id} is absent")
    value = payload.get(json_key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"secret {secret_id} misses {json_key}")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise RuntimeError(f"secret {secret_id} has an invalid cursor key") from exc
    if len(decoded) != 32:
        raise RuntimeError(f"secret {secret_id} cursor key is not 32 bytes")
    return value


def resolve_lambda_env_targets(spec: dict[str, Any]) -> dict[str, str]:
    targets = dict(spec.get("variables", {}))
    for name, source in spec.get("secret_variables", {}).items():
        if set(source) - {"secret_id", "json_key"}:
            raise RuntimeError(f"lambda env secret source for {name} is invalid")
        targets[name] = pagination_key(
            source["secret_id"], source.get("json_key", "key")
        )
    if not targets or any(
        not isinstance(name, str)
        or not name
        or not isinstance(value, str)
        or "\x00" in value
        for name, value in targets.items()
    ):
        raise RuntimeError("lambda env target variables are invalid")
    return targets


def lambda_environment(function_name: str) -> dict[str, Any]:
    config = lambda_config(function_name)
    return {
        "revision_id": config["RevisionId"],
        "variables": config.get("Environment", {}).get("Variables", {}),
    }


def lambda_env_backup_metadata(
    spec: dict[str, Any], directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    return metadata(
        {"type": "lambda-env", "function": spec["function"]},
        {"directory": str(directory), "revision_id": state["revision_id"]},
        state["variables"],
        state,
    )


def backup_lambda_env(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state_path = directory / "state.json"
    if state_path.is_file():
        return verify_lambda_env_backup(spec, run_dir)
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("partial lambda-env backup exists without state.json")
    state = lambda_environment(spec["function"])
    write_json(state_path, state)
    return lambda_env_backup_metadata(spec, directory, state)


def verify_lambda_env_backup(
    spec: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    if not isinstance(state.get("variables"), dict) or not isinstance(
        state.get("revision_id"), str
    ):
        raise TypeError("lambda-env backup is invalid")
    return lambda_env_backup_metadata(spec, directory, state)


def apply_lambda_env(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied_path = directory / "applied.json"
    target_values = resolve_lambda_env_targets(spec)
    current = lambda_environment(spec["function"])
    if applied_path.is_file():
        applied = load_json(applied_path)
        if current != applied:
            raise RuntimeError("Lambda environment drifted after apply")
        return
    original = state["variables"]
    target = {**original, **target_values}
    if current["variables"] == target:
        write_json(applied_path, current)
        return
    if current != state:
        raise RuntimeError("Lambda environment changed after backup")
    response = aws_json(
        "lambda",
        "update-function-configuration",
        "--function-name",
        spec["function"],
        "--revision-id",
        state["revision_id"],
        "--environment",
        json.dumps({"Variables": target}, separators=(",", ":")),
        "--region",
        REGION,
        "--output",
        "json",
    )
    if response.get("Environment", {}).get("Variables", {}) != target:
        raise RuntimeError("Lambda environment update response is incomplete")
    updated = wait_lambda_configuration(spec["function"])
    applied = {
        "revision_id": updated["RevisionId"],
        "variables": updated.get("Environment", {}).get("Variables", {}),
    }
    if applied["variables"] != target:
        raise RuntimeError("Lambda environment did not reach target")
    write_json(applied_path, applied)


def rollback_lambda_env(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied = load_json(directory / "applied.json")
    current = lambda_environment(spec["function"])
    if current["variables"] == state["variables"]:
        return
    if current != applied:
        raise RuntimeError("Lambda environment changed after patch")
    response = aws_json(
        "lambda",
        "update-function-configuration",
        "--function-name",
        spec["function"],
        "--revision-id",
        current["revision_id"],
        "--environment",
        json.dumps({"Variables": state["variables"]}, separators=(",", ":")),
        "--region",
        REGION,
        "--output",
        "json",
    )
    if response.get("Environment", {}).get("Variables", {}) != state["variables"]:
        raise RuntimeError("Lambda environment rollback response is incomplete")
    restored = wait_lambda_configuration(spec["function"])
    if restored.get("Environment", {}).get("Variables", {}) != state["variables"]:
        raise RuntimeError("Lambda environment rollback did not restore backup")


def rollback_verify_lambda_env(spec: dict[str, Any], run_dir: Path) -> bool:
    state = load_json(backup_dir(run_dir, spec["id"]) / "state.json")
    return lambda_environment(spec["function"])["variables"] == state["variables"]


def table_description(table_name: str) -> dict[str, Any]:
    return aws_json(
        "dynamodb",
        "describe-table",
        "--table-name",
        table_name,
        "--region",
        REGION,
        "--output",
        "json",
    )["Table"]


def query_index_map(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["IndexName"]: item
        for item in table.get("GlobalSecondaryIndexes", [])
    }


def assert_query_index(
    table: dict[str, Any], index: dict[str, str]
) -> str | None:
    observed = query_index_map(table).get(index["name"])
    if observed is None:
        return None
    expected_key = [
        {"AttributeName": index["hash_key"], "KeyType": "HASH"}
    ]
    if (
        observed.get("KeySchema") != expected_key
        or observed.get("Projection") != {"ProjectionType": "ALL"}
    ):
        raise RuntimeError(f"tenant-query GSI {index['name']} has incompatible schema")
    definitions = {
        item["AttributeName"]: item["AttributeType"]
        for item in table.get("AttributeDefinitions", [])
    }
    if definitions.get(index["hash_key"]) != "S":
        raise RuntimeError(
            f"tenant-query GSI {index['name']} hash key is not String"
        )
    return observed.get("IndexStatus")


def wait_query_index(table_name: str, index: dict[str, str]) -> None:
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        table = table_description(table_name)
        status = assert_query_index(table, index)
        if table.get("TableStatus") == "ACTIVE" and status == "ACTIVE":
            return
        if status in {"DELETING"}:
            raise RuntimeError(
                f"tenant-query GSI {index['name']} entered {status}"
            )
        time.sleep(5)
    raise RuntimeError(f"tenant-query GSI {index['name']} did not become ACTIVE")


def ensure_query_indexes(spec: dict[str, Any]) -> None:
    for index in spec["indexes"]:
        table = table_description(spec["table"])
        status = assert_query_index(table, index)
        if status is not None:
            wait_query_index(spec["table"], index)
            continue
        if table.get("TableStatus") != "ACTIVE" or any(
            item.get("IndexStatus") != "ACTIVE"
            for item in table.get("GlobalSecondaryIndexes", [])
        ):
            raise RuntimeError("tenant table is not stable enough to add a GSI")
        aws_call(
            "dynamodb",
            "update-table",
            "--table-name",
            spec["table"],
            "--attribute-definitions",
            json.dumps(
                [
                    {
                        "AttributeName": index["hash_key"],
                        "AttributeType": "S",
                    }
                ],
                separators=(",", ":"),
            ),
            "--global-secondary-index-updates",
            json.dumps(
                [
                    {
                        "Create": {
                            "IndexName": index["name"],
                            "KeySchema": [
                                {
                                    "AttributeName": index["hash_key"],
                                    "KeyType": "HASH",
                                }
                            ],
                            "Projection": {"ProjectionType": "ALL"},
                        }
                    }
                ],
                separators=(",", ":"),
            ),
            "--region",
            REGION,
        )
        wait_query_index(spec["table"], index)


def scan_backfill_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    backfill = spec["backfill"]
    items: list[dict[str, Any]] = []
    start_key: dict[str, Any] | None = None
    while True:
        args = [
            "dynamodb",
            "scan",
            "--table-name",
            spec["table"],
            "--projection-expression",
            "#pk,#source,#target",
            "--expression-attribute-names",
            json.dumps(
                {
                    "#pk": backfill["partition_key"],
                    "#source": backfill["source"],
                    "#target": backfill["target"],
                },
                separators=(",", ":"),
            ),
            "--region",
            REGION,
            "--output",
            "json",
        ]
        if start_key is not None:
            args.extend(
                [
                    "--exclusive-start-key",
                    json.dumps(start_key, separators=(",", ":")),
                ]
            )
        response = aws_json(*args)
        items.extend(response.get("Items", []))
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return items


def ensure_query_backfill(spec: dict[str, Any]) -> None:
    backfill = spec["backfill"]
    for item in scan_backfill_items(spec):
        source = item.get(backfill["source"], {}).get("S")
        target = item.get(backfill["target"], {}).get("S")
        if not isinstance(source, str) or not source:
            continue
        if len(source.encode("utf-8")) > 256:
            continue
        if target == source:
            continue
        if target is not None:
            raise RuntimeError(
                f"tenant {item[backfill['partition_key']]} has conflicting "
                f"{backfill['target']}"
            )
        key = {backfill["partition_key"]: item[backfill["partition_key"]]}
        try:
            aws_call(
                "dynamodb",
                "update-item",
                "--table-name",
                spec["table"],
                "--key",
                json.dumps(key, separators=(",", ":")),
                "--update-expression",
                "SET #target = :value",
                "--condition-expression",
                "attribute_not_exists(#target) AND #source = :value",
                "--expression-attribute-names",
                json.dumps(
                    {
                        "#source": backfill["source"],
                        "#target": backfill["target"],
                    },
                    separators=(",", ":"),
                ),
                "--expression-attribute-values",
                json.dumps({":value": {"S": source}}, separators=(",", ":")),
                "--region",
                REGION,
            )
        except RuntimeError as exc:
            if "ConditionalCheckFailedException" not in str(exc):
                raise
            current = aws_json(
                "dynamodb",
                "get-item",
                "--table-name",
                spec["table"],
                "--key",
                json.dumps(key, separators=(",", ":")),
                "--consistent-read",
                "--region",
                REGION,
                "--output",
                "json",
            ).get("Item", {})
            if current.get(backfill["target"], {}).get("S") != source:
                raise RuntimeError(
                    f"tenant backfill raced with a conflicting update: {key}"
                ) from exc


def ensure_pagination_secret(spec: dict[str, Any]) -> None:
    current = secret_json(spec["secret"])
    if current is None:
        value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        aws_call(
            "secretsmanager",
            "create-secret",
            "--name",
            spec["secret"],
            "--description",
            "OpenClaw pagination AES-GCM cursor key",
            "--secret-string",
            json.dumps(
                {"purpose": "pagination-aes-gcm", "key": value},
                separators=(",", ":"),
            ),
            "--tags",
            "Key=ManagedBy,Value=claw-patch-v2",
            "--region",
            REGION,
        )
    pagination_key(spec["secret"])


def apply_tenant_query_foundation(spec: dict[str, Any]) -> None:
    ensure_pagination_secret(spec)
    ensure_query_indexes(spec)
    ensure_query_backfill(spec)


def normalize_policy(value: dict[str, Any]) -> dict[str, Any]:
    policy = json.loads(json.dumps(value))
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        for field in ("Action", "Resource"):
            item = statement.get(field)
            if isinstance(item, str):
                statement[field] = [item]
            if isinstance(statement.get(field), list):
                statement[field] = sorted(statement[field])
    policy["Statement"] = sorted(
        statements,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    return policy


def stats_role_policy(spec: dict[str, Any], table_arn: str) -> dict[str, Any]:
    account = table_arn.split(":")[4]
    region = table_arn.split(":")[3]
    tenants_arn = (
        f"arn:aws:dynamodb:{region}:{account}:table/{spec['tenants_table']}"
    )
    logs_arn = f"arn:aws:logs:{region}:{account}:*"
    bucket_arn = (
        f"arn:aws:s3:::{spec['assets_bucket']}/"
        f"{spec['rootfs_prefix'].rstrip('/')}/manifest.json"
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadTenants",
                "Effect": "Allow",
                "Action": ["dynamodb:DescribeTable", "dynamodb:Scan"],
                "Resource": [tenants_arn],
            },
            {
                "Sid": "WriteStats",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                "Resource": [table_arn],
            },
            {
                "Sid": "ReadRootfsManifest",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [bucket_arn],
            },
            {
                "Sid": "WriteLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": [logs_arn],
            },
        ],
    }


def iam_role(role_name: str) -> dict[str, Any] | None:
    response = optional_aws_json(
        ("NoSuchEntity",),
        "iam",
        "get-role",
        "--role-name",
        role_name,
        "--output",
        "json",
    )
    return None if response is None else response["Role"]


def iam_inline_policy(
    role_name: str, policy_name: str
) -> dict[str, Any] | None:
    response = optional_aws_json(
        ("NoSuchEntity",),
        "iam",
        "get-role-policy",
        "--role-name",
        role_name,
        "--policy-name",
        policy_name,
        "--output",
        "json",
    )
    return None if response is None else response["PolicyDocument"]


def wait_iam_role(role_name: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        role = iam_role(role_name)
        if role is not None:
            return role
        time.sleep(2)
    raise RuntimeError(f"IAM role {role_name} did not become readable")


def ensure_stats_table(spec: dict[str, Any]) -> dict[str, Any]:
    response = optional_aws_json(
        ("ResourceNotFoundException",),
        "dynamodb",
        "describe-table",
        "--table-name",
        spec["table"],
        "--region",
        REGION,
        "--output",
        "json",
    )
    if response is None:
        aws_call(
            "dynamodb",
            "create-table",
            "--table-name",
            spec["table"],
            "--attribute-definitions",
            '[{"AttributeName":"id","AttributeType":"S"}]',
            "--key-schema",
            '[{"AttributeName":"id","KeyType":"HASH"}]',
            "--billing-mode",
            "PAY_PER_REQUEST",
            "--tags",
            "Key=ManagedBy,Value=claw-patch-v2",
            "--region",
            REGION,
        )
        aws_call(
            "dynamodb",
            "wait",
            "table-exists",
            "--table-name",
            spec["table"],
            "--region",
            REGION,
        )
    table = table_description(spec["table"])
    if (
        table.get("TableStatus") != "ACTIVE"
        or table.get("KeySchema")
        != [{"AttributeName": "id", "KeyType": "HASH"}]
        or {
            item["AttributeName"]: item["AttributeType"]
            for item in table.get("AttributeDefinitions", [])
        }.get("id")
        != "S"
        or table.get("BillingModeSummary", {}).get("BillingMode")
        != "PAY_PER_REQUEST"
    ):
        raise RuntimeError("tenant-stats table has incompatible schema or billing")
    backups = aws_json(
        "dynamodb",
        "describe-continuous-backups",
        "--table-name",
        spec["table"],
        "--region",
        REGION,
        "--output",
        "json",
    )
    status = backups["ContinuousBackupsDescription"][
        "PointInTimeRecoveryDescription"
    ]["PointInTimeRecoveryStatus"]
    if status != "ENABLED":
        aws_call(
            "dynamodb",
            "update-continuous-backups",
            "--table-name",
            spec["table"],
            "--point-in-time-recovery-specification",
            "PointInTimeRecoveryEnabled=true",
            "--region",
            REGION,
        )
    return table


def ensure_stats_role(spec: dict[str, Any], table_arn: str) -> dict[str, Any]:
    role = iam_role(spec["role"])
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
    if role is None:
        aws_call(
            "iam",
            "create-role",
            "--role-name",
            spec["role"],
            "--assume-role-policy-document",
            json.dumps(trust, separators=(",", ":")),
            "--description",
            "OpenClaw tenant stats writer role",
            "--tags",
            "Key=ManagedBy,Value=claw-patch-v2",
        )
        role = wait_iam_role(spec["role"])
    policy_name = spec["role_policy_name"]
    expected = normalize_policy(stats_role_policy(spec, table_arn))
    current = iam_inline_policy(spec["role"], policy_name)
    if current is None:
        tags = {
            item["Key"]: item["Value"]
            for item in role.get("Tags", [])
        }
        if tags.get("ManagedBy") != "claw-patch-v2":
            raise RuntimeError(
                "refusing to attach policy to an unowned tenant-stats role"
            )
        aws_call(
            "iam",
            "put-role-policy",
            "--role-name",
            spec["role"],
            "--policy-name",
            policy_name,
            "--policy-document",
            json.dumps(expected, separators=(",", ":")),
        )
    elif normalize_policy(current) != expected:
        raise RuntimeError("tenant-stats writer role policy is incompatible")
    return role


def function_exists(function_name: str) -> dict[str, Any] | None:
    return optional_aws_json(
        ("ResourceNotFoundException",),
        "lambda",
        "get-function",
        "--function-name",
        function_name,
        "--region",
        REGION,
        "--output",
        "json",
    )


def stats_function_config_matches(
    config: dict[str, Any], spec: dict[str, Any], role_arn: str
) -> bool:
    expected_environment = {
        "TENANTS_TABLE": spec["tenants_table"],
        "TENANT_STATS_TABLE": spec["table"],
        "ASSETS_BUCKET": spec["assets_bucket"],
        "ROOTFS_PREFIX": spec["rootfs_prefix"],
        "STATS_SCAN_SEGMENTS": "8",
    }
    return (
        config.get("Runtime") == "python3.12"
        and config.get("Architectures") == ["arm64"]
        and config.get("Handler") == "handler.lambda_handler"
        and config.get("MemorySize") == 8192
        and config.get("Timeout") == 50
        and config.get("Role") == role_arn
        and config.get("Environment", {}).get("Variables", {})
        == expected_environment
    )


def lambda_handler_hash(function: dict[str, Any]) -> str:
    with urllib.request.urlopen(function["Code"]["Location"], timeout=60) as remote:
        payload = remote.read()
    code_sha256 = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    if code_sha256 != function["Configuration"]["CodeSha256"]:
        raise RuntimeError("tenant-stats Lambda changed while reading")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return sha256_bytes(archive.read("handler.py"))


def ensure_stats_function(
    spec: dict[str, Any], role_arn: str
) -> dict[str, Any]:
    function = function_exists(spec["function"])
    package = artifact(spec["target_package"])
    if sha256_file(package) != spec["target_package_sha256"]:
        raise RuntimeError("tenant-stats package artifact hash mismatch")
    if function is None:
        create_args = (
            "lambda",
            "create-function",
            "--function-name",
            spec["function"],
            "--runtime",
            "python3.12",
            "--architectures",
            "arm64",
            "--role",
            role_arn,
            "--handler",
            "handler.lambda_handler",
            "--zip-file",
            f"fileb://{package}",
            "--timeout",
            "50",
            "--memory-size",
            "8192",
            "--environment",
            json.dumps(
                {
                    "Variables": {
                        "TENANTS_TABLE": spec["tenants_table"],
                        "TENANT_STATS_TABLE": spec["table"],
                        "ASSETS_BUCKET": spec["assets_bucket"],
                        "ROOTFS_PREFIX": spec["rootfs_prefix"],
                        "STATS_SCAN_SEGMENTS": "8",
                    }
                },
                separators=(",", ":"),
            ),
            "--tags",
            "ManagedBy=claw-patch-v2",
            "--region",
            REGION,
        )
        for attempt in range(30):
            try:
                aws_call(*create_args)
                break
            except RuntimeError as exc:
                role_pending = (
                    "The role defined for the function cannot be assumed by Lambda."
                    in str(exc)
                )
                if not role_pending or attempt == 29:
                    raise
                time.sleep(2)
        wait_lambda(spec["function"])
        function = function_exists(spec["function"])
    if function is None:
        raise RuntimeError("tenant-stats Lambda is absent after create")
    if not stats_function_config_matches(
        function["Configuration"], spec, role_arn
    ):
        raise RuntimeError("tenant-stats Lambda configuration is incompatible")
    if lambda_handler_hash(function) != spec["target_handler_sha256"]:
        raise RuntimeError("tenant-stats Lambda handler is incompatible")
    concurrency = optional_aws_json(
        ("ResourceNotFoundException",),
        "lambda",
        "get-function-concurrency",
        "--function-name",
        spec["function"],
        "--region",
        REGION,
        "--output",
        "json",
    )
    reserved = (
        None
        if concurrency is None
        else concurrency.get("ReservedConcurrentExecutions")
    )
    if reserved is None:
        aws_call(
            "lambda",
            "put-function-concurrency",
            "--function-name",
            spec["function"],
            "--reserved-concurrent-executions",
            "1",
            "--region",
            REGION,
        )
    elif reserved != 1:
        raise RuntimeError("tenant-stats Lambda concurrency is incompatible")
    return function


def ensure_stats_schedule(spec: dict[str, Any], function_arn: str) -> None:
    rule = optional_aws_json(
        ("ResourceNotFoundException",),
        "events",
        "describe-rule",
        "--name",
        spec["schedule"],
        "--region",
        REGION,
        "--output",
        "json",
    )
    if rule is None:
        rule = aws_json(
            "events",
            "put-rule",
            "--name",
            spec["schedule"],
            "--schedule-expression",
            "rate(1 minute)",
            "--state",
            "ENABLED",
            "--description",
            "OpenClaw tenant statistics refresh",
            "--tags",
            "Key=ManagedBy,Value=claw-patch-v2",
            "--region",
            REGION,
            "--output",
            "json",
        )
        rule = {"Arn": rule["RuleArn"], "State": "ENABLED"}
    elif (
        rule.get("State") != "ENABLED"
        or rule.get("ScheduleExpression") != "rate(1 minute)"
    ):
        raise RuntimeError("tenant-stats schedule is incompatible")
    targets = aws_json(
        "events",
        "list-targets-by-rule",
        "--rule",
        spec["schedule"],
        "--region",
        REGION,
        "--output",
        "json",
    ).get("Targets", [])
    owned = next(
        (item for item in targets if item["Id"] == spec["schedule_target_id"]),
        None,
    )
    if owned is None:
        aws_call(
            "events",
            "put-targets",
            "--rule",
            spec["schedule"],
            "--targets",
            json.dumps(
                [{"Id": spec["schedule_target_id"], "Arn": function_arn}],
                separators=(",", ":"),
            ),
            "--region",
            REGION,
        )
    elif owned != {"Id": spec["schedule_target_id"], "Arn": function_arn}:
        raise RuntimeError("tenant-stats schedule target is incompatible")
    try:
        aws_call(
            "lambda",
            "add-permission",
            "--function-name",
            spec["function"],
            "--statement-id",
            spec["schedule_permission_id"],
            "--action",
            "lambda:InvokeFunction",
            "--principal",
            "events.amazonaws.com",
            "--source-arn",
            rule["Arn"],
            "--region",
            REGION,
        )
    except RuntimeError as exc:
        if "ResourceConflictException" not in str(exc):
            raise
    if not stats_schedule_permission_matches(
        spec["function"],
        spec["schedule_permission_id"],
        rule["Arn"],
    ):
        raise RuntimeError("tenant-stats schedule permission is incompatible")


def stats_schedule_permission_matches(
    function_name: str,
    statement_id: str,
    rule_arn: str,
) -> bool:
    response = optional_aws_json(
        ("ResourceNotFoundException",),
        "lambda",
        "get-policy",
        "--function-name",
        function_name,
        "--region",
        REGION,
        "--output",
        "json",
    )
    if response is None:
        return False
    try:
        policy = json.loads(response["Policy"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    statement = next(
        (
            item
            for item in policy.get("Statement", [])
            if item.get("Sid") == statement_id
        ),
        None,
    )
    return bool(
        statement
        and statement.get("Effect") == "Allow"
        and statement.get("Action") == "lambda:InvokeFunction"
        and statement.get("Principal") == {"Service": "events.amazonaws.com"}
        and statement.get("Condition", {})
        .get("ArnLike", {})
        .get("AWS:SourceArn")
        == rule_arn
    )


def invoke_stats_writer(spec: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="claw-patch-v2-stats-") as directory:
        output = Path(directory) / "response.json"
        response = aws_json(
            "lambda",
            "invoke",
            "--function-name",
            spec["function"],
            "--invocation-type",
            "RequestResponse",
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            "{}",
            "--region",
            REGION,
            str(output),
        )
        if response.get("FunctionError"):
            detail = output.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"tenant-stats initial invoke failed: {detail}")


def apply_tenant_stats_foundation(spec: dict[str, Any]) -> None:
    table = ensure_stats_table(spec)
    role = ensure_stats_role(spec, table["TableArn"])
    function = ensure_stats_function(spec, role["Arn"])
    ensure_stats_schedule(spec, function["Configuration"]["FunctionArn"])
    invoke_stats_writer(spec)


def function_role(function_name: str) -> tuple[str, str]:
    role_arn = lambda_config(function_name)["Role"]
    role_name = role_arn.rsplit("/", 1)[-1]
    if not role_name:
        raise RuntimeError(f"Lambda {function_name} has an invalid role ARN")
    return role_arn, role_name


def iam_policy_backup_metadata(
    spec: dict[str, Any], directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    return metadata(
        {
            "type": "iam-inline-policy",
            "function": spec["function"],
            "policy_name": spec["policy_name"],
        },
        {
            "directory": str(directory),
            "role_arn": state["role_arn"],
        },
        state["policy"],
        state,
    )


def backup_iam_policy(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state_path = directory / "state.json"
    if state_path.is_file():
        return verify_iam_policy_backup(spec, run_dir)
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("partial IAM policy backup exists without state.json")
    role_arn, role_name = function_role(spec["function"])
    state = {
        "role_arn": role_arn,
        "role_name": role_name,
        "policy": iam_inline_policy(role_name, spec["policy_name"]),
    }
    write_json(state_path, state)
    return iam_policy_backup_metadata(spec, directory, state)


def verify_iam_policy_backup(
    spec: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    if function_role(spec["function"])[0] != state["role_arn"]:
        raise RuntimeError("API Lambda role changed after IAM backup")
    return iam_policy_backup_metadata(spec, directory, state)


def current_iam_policy(spec: dict[str, Any], state: dict[str, Any]) -> Any:
    role_arn, role_name = function_role(spec["function"])
    if role_arn != state["role_arn"] or role_name != state["role_name"]:
        raise RuntimeError("API Lambda role changed during IAM mutation")
    return iam_inline_policy(role_name, spec["policy_name"])


def apply_iam_policy(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied_path = directory / "applied.json"
    target = normalize_policy(spec["policy_document"])
    current = current_iam_policy(spec, state)
    if applied_path.is_file():
        if current is None or normalize_policy(current) != target:
            raise RuntimeError("API IAM policy drifted after apply")
        return
    if current is not None and normalize_policy(current) == target:
        write_json(applied_path, {"policy": target})
        return
    original = state["policy"]
    if (current is None) != (original is None) or (
        current is not None
        and normalize_policy(current) != normalize_policy(original)
    ):
        raise RuntimeError("API IAM policy changed after backup")
    aws_call(
        "iam",
        "put-role-policy",
        "--role-name",
        state["role_name"],
        "--policy-name",
        spec["policy_name"],
        "--policy-document",
        json.dumps(target, separators=(",", ":")),
    )
    observed = current_iam_policy(spec, state)
    if observed is None or normalize_policy(observed) != target:
        raise RuntimeError("API IAM policy did not reach target")
    write_json(applied_path, {"policy": target})


def rollback_iam_policy(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    target = normalize_policy(
        load_json(directory / "applied.json")["policy"]
    )
    current = current_iam_policy(spec, state)
    original = state["policy"]
    if (current is None and original is None) or (
        current is not None
        and original is not None
        and normalize_policy(current) == normalize_policy(original)
    ):
        return
    if current is None or normalize_policy(current) != target:
        raise RuntimeError("API IAM policy changed after patch")
    if original is None:
        aws_call(
            "iam",
            "delete-role-policy",
            "--role-name",
            state["role_name"],
            "--policy-name",
            spec["policy_name"],
        )
    else:
        aws_call(
            "iam",
            "put-role-policy",
            "--role-name",
            state["role_name"],
            "--policy-name",
            spec["policy_name"],
            "--policy-document",
            json.dumps(normalize_policy(original), separators=(",", ":")),
        )


def rollback_verify_iam_policy(spec: dict[str, Any], run_dir: Path) -> bool:
    state = load_json(backup_dir(run_dir, spec["id"]) / "state.json")
    current = current_iam_policy(spec, state)
    original = state["policy"]
    if current is None or original is None:
        return current is None and original is None
    return normalize_policy(current) == normalize_policy(original)


def api_resources(api_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    position: str | None = None
    while True:
        args = [
            "apigateway",
            "get-resources",
            "--rest-api-id",
            api_id,
            "--limit",
            "500",
            "--region",
            REGION,
            "--output",
            "json",
        ]
        if position is not None:
            args.extend(["--position", position])
        response = aws_json(*args)
        items.extend(response.get("items", []))
        position = response.get("position")
        if not position:
            return items


def api_resource(api_id: str, path: str) -> dict[str, Any] | None:
    matches = [item for item in api_resources(api_id) if item.get("path") == path]
    if len(matches) > 1:
        raise RuntimeError(f"REST API has duplicate resources for {path}")
    return matches[0] if matches else None


def api_method(api_id: str, resource_id: str, method: str) -> dict[str, Any] | None:
    return optional_aws_json(
        ("NotFoundException",),
        "apigateway",
        "get-method",
        "--rest-api-id",
        api_id,
        "--resource-id",
        resource_id,
        "--http-method",
        method,
        "--region",
        REGION,
        "--output",
        "json",
    )


def api_integration(
    api_id: str, resource_id: str, method: str
) -> dict[str, Any] | None:
    return optional_aws_json(
        ("NotFoundException",),
        "apigateway",
        "get-integration",
        "--rest-api-id",
        api_id,
        "--resource-id",
        resource_id,
        "--http-method",
        method,
        "--region",
        REGION,
        "--output",
        "json",
    )


def method_template(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "authorizationType": value["authorizationType"],
        "apiKeyRequired": value.get("apiKeyRequired", False),
    }
    for field in (
        "authorizerId",
        "authorizationScopes",
        "operationName",
        "requestParameters",
        "requestValidatorId",
    ):
        if field in value:
            result[field] = value[field]
    return result


def integration_template(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "type": value["type"],
        "integrationHttpMethod": value.get("httpMethod"),
    }
    for field in (
        "uri",
        "connectionType",
        "connectionId",
        "credentials",
        "requestParameters",
        "requestTemplates",
        "passthroughBehavior",
        "cacheNamespace",
        "cacheKeyParameters",
        "contentHandling",
        "timeoutInMillis",
        "tlsConfig",
    ):
        if field in value:
            result[field] = value[field]
    return {key: value for key, value in result.items() if value is not None}


def api_route_snapshot(spec: dict[str, Any]) -> dict[str, Any] | None:
    resource = api_resource(spec["api_id"], spec["target_path"])
    if resource is None:
        return None
    method = api_method(spec["api_id"], resource["id"], "GET")
    integration = api_integration(spec["api_id"], resource["id"], "GET")
    options_method = api_method(spec["api_id"], resource["id"], "OPTIONS")
    options_integration = api_integration(
        spec["api_id"], resource["id"], "OPTIONS"
    )
    return {
        "resource_id": resource["id"],
        "method": method,
        "integration": integration,
        "options_method": options_method,
        "options_integration": options_integration,
    }


def cors_route_matches(snapshot: dict[str, Any]) -> bool:
    method = snapshot["options_method"]
    integration = snapshot["options_integration"]
    method_response = (
        method.get("methodResponses", {}).get("204") if method is not None else None
    )
    integration_response = (
        integration.get("integrationResponses", {}).get("204")
        if integration is not None
        else None
    )
    return (
        method is not None
        and method.get("authorizationType") == "NONE"
        and method.get("apiKeyRequired", False) is False
        and integration is not None
        and integration.get("type") == "MOCK"
        and method_response is not None
        and method_response.get("responseParameters")
        == {
            "method.response.header.Access-Control-Allow-Headers": True,
            "method.response.header.Access-Control-Allow-Methods": True,
            "method.response.header.Access-Control-Allow-Origin": True,
        }
        and integration_response is not None
        and integration_response.get("responseParameters")
        == {
            "method.response.header.Access-Control-Allow-Headers": (
                "'Content-Type,x-api-key,Authorization'"
            ),
            "method.response.header.Access-Control-Allow-Methods": (
                "'OPTIONS,GET,PUT,POST,DELETE,PATCH,HEAD'"
            ),
            "method.response.header.Access-Control-Allow-Origin": "'*'",
        }
    )


def api_route_partial_matches(
    snapshot: dict[str, Any],
    method: dict[str, Any],
    integration: dict[str, Any],
) -> bool:
    get_method = snapshot["method"]
    get_integration = snapshot["integration"]
    options_method = snapshot["options_method"]
    options_integration = snapshot["options_integration"]
    method_response = (
        options_method.get("methodResponses", {}).get("204")
        if options_method is not None
        else None
    )
    integration_response = (
        options_integration.get("integrationResponses", {}).get("204")
        if options_integration is not None
        else None
    )
    return (
        (get_method is None or method_template(get_method) == method)
        and (
            get_integration is None
            or integration_template(get_integration) == integration
        )
        and (
            options_method is None
            or (
                options_method.get("authorizationType") == "NONE"
                and options_method.get("apiKeyRequired", False) is False
                and (
                    method_response is None
                    or method_response.get("responseParameters")
                    == {
                        "method.response.header.Access-Control-Allow-Headers": True,
                        "method.response.header.Access-Control-Allow-Methods": True,
                        "method.response.header.Access-Control-Allow-Origin": True,
                    }
                )
            )
        )
        and (
            options_integration is None
            or (
                options_integration.get("type") == "MOCK"
                and (
                    integration_response is None
                    or integration_response.get("responseParameters")
                    == {
                        "method.response.header.Access-Control-Allow-Headers": (
                            "'Content-Type,x-api-key,Authorization'"
                        ),
                        "method.response.header.Access-Control-Allow-Methods": (
                            "'OPTIONS,GET,PUT,POST,DELETE,PATCH,HEAD'"
                        ),
                        "method.response.header.Access-Control-Allow-Origin": "'*'",
                    }
                )
            )
        )
    )


def api_route_matches(
    snapshot: dict[str, Any] | None,
    method: dict[str, Any],
    integration: dict[str, Any],
) -> bool:
    return (
        snapshot is not None
        and snapshot["method"] is not None
        and method_template(snapshot["method"]) == method
        and snapshot["integration"] is not None
        and integration_template(snapshot["integration"]) == integration
        and cors_route_matches(snapshot)
    )


def api_stage(api_id: str, stage: str) -> dict[str, Any]:
    return aws_json(
        "apigateway",
        "get-stage",
        "--rest-api-id",
        api_id,
        "--stage-name",
        stage,
        "--region",
        REGION,
        "--output",
        "json",
    )


def api_route_backup_metadata(
    spec: dict[str, Any], directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    return metadata(
        {
            "type": "api-route",
            "api_id": spec["api_id"],
            "target_path": spec["target_path"],
        },
        {
            "directory": str(directory),
            "stage": spec["stage"],
            "deployment_id": state["deployment_id"],
        },
        {
            "method": state["method"],
            "integration": state["integration"],
        },
        state,
    )


def backup_api_route(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state_path = directory / "state.json"
    if state_path.is_file():
        return verify_api_route_backup(spec, run_dir)
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError("partial API route backup exists without state.json")
    if api_route_snapshot(spec) is not None:
        raise RuntimeError("target API route existed before backup")
    source = api_resource(spec["api_id"], spec["source_path"])
    if source is None:
        raise RuntimeError(f"source API route is absent: {spec['source_path']}")
    method = api_method(spec["api_id"], source["id"], "GET")
    integration = api_integration(spec["api_id"], source["id"], "GET")
    if method is None or integration is None:
        raise RuntimeError("source API route is incomplete")
    stage = api_stage(spec["api_id"], spec["stage"])
    if not stage.get("deploymentId"):
        raise RuntimeError("API stage has no deployment")
    state = {
        "deployment_id": stage["deploymentId"],
        "method": method_template(method),
        "integration": integration_template(integration),
    }
    write_json(state_path, state)
    return api_route_backup_metadata(spec, directory, state)


def verify_api_route_backup(
    spec: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    if not state.get("deployment_id"):
        raise RuntimeError("API route backup has no deployment")
    return api_route_backup_metadata(spec, directory, state)


def put_api_route(spec: dict[str, Any], resource_id: str, state: dict[str, Any]) -> None:
    method = {
        "restApiId": spec["api_id"],
        "resourceId": resource_id,
        "httpMethod": "GET",
        **state["method"],
    }
    existing_method = api_method(spec["api_id"], resource_id, "GET")
    if existing_method is None:
        aws_call(
            "apigateway",
            "put-method",
            "--cli-input-json",
            json.dumps(method, separators=(",", ":")),
            "--region",
            REGION,
        )
    elif method_template(existing_method) != state["method"]:
        raise RuntimeError("partial API route GET method drifted")
    integration = {
        "restApiId": spec["api_id"],
        "resourceId": resource_id,
        "httpMethod": "GET",
        **state["integration"],
    }
    existing_integration = api_integration(spec["api_id"], resource_id, "GET")
    if existing_integration is None:
        aws_call(
            "apigateway",
            "put-integration",
            "--cli-input-json",
            json.dumps(integration, separators=(",", ":")),
            "--region",
            REGION,
        )
    elif integration_template(existing_integration) != state["integration"]:
        raise RuntimeError("partial API route GET integration drifted")

    options_method_input = {
        "restApiId": spec["api_id"],
        "resourceId": resource_id,
        "httpMethod": "OPTIONS",
        "authorizationType": "NONE",
        "apiKeyRequired": False,
    }
    options_method = api_method(spec["api_id"], resource_id, "OPTIONS")
    if options_method is None:
        aws_call(
            "apigateway",
            "put-method",
            "--cli-input-json",
            json.dumps(options_method_input, separators=(",", ":")),
            "--region",
            REGION,
        )
    elif (
        options_method.get("authorizationType") != "NONE"
        or options_method.get("apiKeyRequired", False) is not False
    ):
        raise RuntimeError("partial API route OPTIONS method drifted")

    options_integration_input = {
        "restApiId": spec["api_id"],
        "resourceId": resource_id,
        "httpMethod": "OPTIONS",
        "type": "MOCK",
        "requestTemplates": {"application/json": "{ statusCode: 200 }"},
        "passthroughBehavior": "WHEN_NO_MATCH",
        "timeoutInMillis": 29000,
    }
    options_integration = api_integration(spec["api_id"], resource_id, "OPTIONS")
    if options_integration is None:
        aws_call(
            "apigateway",
            "put-integration",
            "--cli-input-json",
            json.dumps(options_integration_input, separators=(",", ":")),
            "--region",
            REGION,
        )
    elif options_integration.get("type") != "MOCK":
        raise RuntimeError("partial API route OPTIONS integration drifted")

    method_response = (
        options_method.get("methodResponses", {}).get("204")
        if options_method is not None
        else None
    )
    method_response_parameters = {
        "method.response.header.Access-Control-Allow-Headers": True,
        "method.response.header.Access-Control-Allow-Methods": True,
        "method.response.header.Access-Control-Allow-Origin": True,
    }
    if method_response is None:
        aws_call(
            "apigateway",
            "put-method-response",
            "--cli-input-json",
            json.dumps(
                {
                    "restApiId": spec["api_id"],
                    "resourceId": resource_id,
                    "httpMethod": "OPTIONS",
                    "statusCode": "204",
                    "responseParameters": method_response_parameters,
                },
                separators=(",", ":"),
            ),
            "--region",
            REGION,
        )
    elif method_response.get("responseParameters") != method_response_parameters:
        raise RuntimeError("partial API route method response drifted")

    integration_response = (
        options_integration.get("integrationResponses", {}).get("204")
        if options_integration is not None
        else None
    )
    integration_response_parameters = {
        "method.response.header.Access-Control-Allow-Headers": (
            "'Content-Type,x-api-key,Authorization'"
        ),
        "method.response.header.Access-Control-Allow-Methods": (
            "'OPTIONS,GET,PUT,POST,DELETE,PATCH,HEAD'"
        ),
        "method.response.header.Access-Control-Allow-Origin": "'*'",
    }
    if integration_response is None:
        aws_call(
            "apigateway",
            "put-integration-response",
            "--cli-input-json",
            json.dumps(
                {
                    "restApiId": spec["api_id"],
                    "resourceId": resource_id,
                    "httpMethod": "OPTIONS",
                    "statusCode": "204",
                    "responseParameters": integration_response_parameters,
                },
                separators=(",", ":"),
            ),
            "--region",
            REGION,
        )
    elif (
        integration_response.get("responseParameters")
        != integration_response_parameters
    ):
        raise RuntimeError("partial API route integration response drifted")


def apply_api_route(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    progress_path = directory / "progress.json"
    applied_path = directory / "applied.json"
    progress = load_json(progress_path) if progress_path.is_file() else {}
    if applied_path.is_file():
        applied = load_json(applied_path)
        if api_stage(spec["api_id"], spec["stage"]).get("deploymentId") != applied[
            "deployment_id"
        ]:
            raise RuntimeError("API stage drifted after route apply")
        if not api_route_matches(
            api_route_snapshot(spec), state["method"], state["integration"]
        ):
            raise RuntimeError("API route drifted after apply")
        return
    stage_deployment = api_stage(spec["api_id"], spec["stage"]).get("deploymentId")
    expected_stage = progress.get("deployment_id", state["deployment_id"])
    if stage_deployment not in {state["deployment_id"], expected_stage}:
        raise RuntimeError("API stage changed after route backup")
    resource = api_resource(spec["api_id"], spec["target_path"])
    if resource is None:
        root = api_resource(spec["api_id"], "/")
        if root is None:
            raise RuntimeError("REST API root resource is absent")
        response = aws_json(
            "apigateway",
            "create-resource",
            "--rest-api-id",
            spec["api_id"],
            "--parent-id",
            root["id"],
            "--path-part",
            spec["target_path"].strip("/"),
            "--region",
            REGION,
            "--output",
            "json",
        )
        resource = response
        progress["resource_id"] = resource["id"]
        write_json(progress_path, progress)
    elif progress.get("resource_id") not in {None, resource["id"]}:
        raise RuntimeError("target API resource changed during apply")
    else:
        progress["resource_id"] = resource["id"]
        write_json(progress_path, progress)
    snapshot = api_route_snapshot(spec)
    if snapshot is None or not api_route_partial_matches(
        snapshot, state["method"], state["integration"]
    ):
        raise RuntimeError("target API resource changed during partial apply")
    if not api_route_matches(snapshot, state["method"], state["integration"]):
        put_api_route(spec, resource["id"], state)
    if not api_route_matches(
        api_route_snapshot(spec), state["method"], state["integration"]
    ):
        raise RuntimeError("API route configuration did not reach target")
    if "deployment_id" not in progress:
        deployment = aws_json(
            "apigateway",
            "create-deployment",
            "--rest-api-id",
            spec["api_id"],
            "--description",
            spec["deployment_description"],
            "--region",
            REGION,
            "--output",
            "json",
        )
        progress["deployment_id"] = deployment["id"]
        write_json(progress_path, progress)
    current = api_stage(spec["api_id"], spec["stage"]).get("deploymentId")
    if current == state["deployment_id"]:
        aws_call(
            "apigateway",
            "update-stage",
            "--rest-api-id",
            spec["api_id"],
            "--stage-name",
            spec["stage"],
            "--patch-operations",
            (
                "op=replace,path=/deploymentId,"
                f"value={progress['deployment_id']}"
            ),
            "--region",
            REGION,
        )
    elif current != progress["deployment_id"]:
        raise RuntimeError("API stage changed before route compare-and-swap")
    if api_stage(spec["api_id"], spec["stage"]).get("deploymentId") != progress[
        "deployment_id"
    ]:
        raise RuntimeError("API stage did not reach target deployment")
    write_json(
        applied_path,
        {
            "deployment_id": progress["deployment_id"],
            "resource_id": progress["resource_id"],
        },
    )


def ddb_ttl_description(table: str) -> dict[str, Any]:
    return aws_json(
        "dynamodb",
        "describe-time-to-live",
        "--table-name",
        table,
        "--region",
        REGION,
        "--output",
        "json",
    )["TimeToLiveDescription"]


def apply_ddb_ttl_disable(spec: dict[str, Any]) -> None:
    deadline = time.monotonic() + spec.get("timeout_seconds", 300)
    while time.monotonic() < deadline:
        description = ddb_ttl_description(spec["table"])
        status = description.get("TimeToLiveStatus")
        if status == "DISABLED":
            return
        if status == "DISABLING":
            time.sleep(5)
            continue
        if status != "ENABLED":
            raise RuntimeError(f"DynamoDB TTL entered unsupported state {status}")
        attribute = description.get("AttributeName")
        if not isinstance(attribute, str) or not attribute:
            raise RuntimeError("enabled DynamoDB TTL has no attribute name")
        aws_call(
            "dynamodb",
            "update-time-to-live",
            "--table-name",
            spec["table"],
            "--time-to-live-specification",
            json.dumps(
                {"Enabled": False, "AttributeName": attribute},
                separators=(",", ":"),
            ),
            "--region",
            REGION,
        )
    raise RuntimeError("DynamoDB TTL did not become DISABLED")


def rollback_api_route(spec: dict[str, Any], run_dir: Path) -> None:
    directory = backup_dir(run_dir, spec["id"])
    state = load_json(directory / "state.json")
    applied_path = directory / "applied.json"
    progress_path = directory / "progress.json"
    applied = (
        load_json(applied_path)
        if applied_path.is_file()
        else load_json(progress_path)
        if progress_path.is_file()
        else {}
    )
    current = api_stage(spec["api_id"], spec["stage"]).get("deploymentId")
    patch_deployment = applied.get("deployment_id")
    if patch_deployment is not None and current == patch_deployment:
        aws_call(
            "apigateway",
            "update-stage",
            "--rest-api-id",
            spec["api_id"],
            "--stage-name",
            spec["stage"],
            "--patch-operations",
            (
                "op=replace,path=/deploymentId,"
                f"value={state['deployment_id']}"
            ),
            "--region",
            REGION,
        )
    elif current != state["deployment_id"]:
        raise RuntimeError("API stage changed after route patch")
    resource = api_resource(spec["api_id"], spec["target_path"])
    if resource is None:
        return
    if resource["id"] != applied.get("resource_id"):
        raise RuntimeError("API route changed after patch")
    snapshot = api_route_snapshot(spec)
    if snapshot is None or not api_route_partial_matches(
        snapshot, state["method"], state["integration"]
    ):
        raise RuntimeError("partial API route changed after patch")
    aws_call(
        "apigateway",
        "delete-resource",
        "--rest-api-id",
        spec["api_id"],
        "--resource-id",
        resource["id"],
        "--region",
        REGION,
    )


def rollback_verify_api_route(spec: dict[str, Any], run_dir: Path) -> bool:
    state = load_json(backup_dir(run_dir, spec["id"]) / "state.json")
    return (
        api_stage(spec["api_id"], spec["stage"]).get("deploymentId")
        == state["deployment_id"]
        and api_resource(spec["api_id"], spec["target_path"]) is None
    )


def backup(kind: str, spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    if kind == "lambda":
        return backup_lambda(spec, run_dir)
    if kind == "s3":
        return backup_s3(spec, run_dir)
    if kind == "host":
        return backup_host(spec, run_dir)
    if kind == "launch-template":
        return backup_launch_template(spec, run_dir)
    if kind == "lambda-env":
        return backup_lambda_env(spec, run_dir)
    if kind == "iam-policy":
        return backup_iam_policy(spec, run_dir)
    if kind == "api-route":
        return backup_api_route(spec, run_dir)
    raise RuntimeError(f"unsupported backup kind: {kind}")


def backup_verify(kind: str, spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    if kind == "lambda":
        return verify_lambda_backup(spec, run_dir)
    if kind == "s3":
        return verify_s3_backup(spec, run_dir)
    if kind == "host":
        return verify_host_backup(spec, run_dir)
    if kind == "launch-template":
        return verify_launch_template_backup(spec, run_dir)
    if kind == "lambda-env":
        return verify_lambda_env_backup(spec, run_dir)
    if kind == "iam-policy":
        return verify_iam_policy_backup(spec, run_dir)
    if kind == "api-route":
        return verify_api_route_backup(spec, run_dir)
    raise RuntimeError(f"unsupported backup verify kind: {kind}")


def apply(kind: str, spec: dict[str, Any], run_dir: Path) -> None:
    if kind == "lambda":
        apply_lambda(spec, run_dir)
        return
    if kind == "s3":
        apply_s3(spec, run_dir)
        return
    if kind == "host":
        apply_host(spec, run_dir)
        return
    if kind == "launch-template":
        apply_launch_template(spec, run_dir)
        return
    if kind == "lambda-env":
        apply_lambda_env(spec, run_dir)
        return
    if kind == "tenant-query-foundation":
        apply_tenant_query_foundation(spec)
        return
    if kind == "tenant-stats-foundation":
        apply_tenant_stats_foundation(spec)
        return
    if kind == "iam-policy":
        apply_iam_policy(spec, run_dir)
        return
    if kind == "api-route":
        apply_api_route(spec, run_dir)
        return
    if kind == "ddb-ttl-disable":
        apply_ddb_ttl_disable(spec)
        return
    raise RuntimeError(f"unsupported apply kind: {kind}")


def rollback(kind: str, spec: dict[str, Any], run_dir: Path) -> None:
    if kind == "lambda":
        rollback_lambda(spec, run_dir)
        return
    if kind == "s3":
        rollback_s3(spec, run_dir)
        return
    if kind == "host":
        rollback_host(spec, run_dir)
        return
    if kind == "launch-template":
        rollback_launch_template(spec, run_dir)
        return
    if kind == "lambda-env":
        rollback_lambda_env(spec, run_dir)
        return
    if kind == "iam-policy":
        rollback_iam_policy(spec, run_dir)
        return
    if kind == "api-route":
        rollback_api_route(spec, run_dir)
        return
    raise RuntimeError(f"unsupported rollback kind: {kind}")


def rollback_verify(kind: str, spec: dict[str, Any], run_dir: Path) -> bool:
    if kind == "lambda":
        return rollback_verify_lambda(spec, run_dir)
    if kind == "s3":
        return rollback_verify_s3(spec, run_dir)
    if kind == "host":
        return rollback_verify_host(spec, run_dir)
    if kind == "launch-template":
        return rollback_verify_launch_template(spec, run_dir)
    if kind == "lambda-env":
        return rollback_verify_lambda_env(spec, run_dir)
    if kind == "iam-policy":
        return rollback_verify_iam_policy(spec, run_dir)
    if kind == "api-route":
        return rollback_verify_api_route(spec, run_dir)
    raise RuntimeError(f"unsupported rollback verify kind: {kind}")


def assert_locator(kind: str, locator: str, spec: dict[str, Any]) -> None:
    field = {
        "lambda": "function",
        "s3": "bucket",
        "host": "asg",
        "launch-template": "asg",
        "lambda-env": "function",
        "iam-policy": "function",
        "api-route": "api_id",
        "ddb-ttl-disable": "table",
        "tenant-query-foundation": "table",
        "tenant-stats-foundation": "table",
    }.get(kind)
    if field is None or spec.get(field) != locator:
        raise RuntimeError(f"{kind} locator does not match the immutable spec")


def main(argv: list[str]) -> int:
    try:
        if not REGION:
            raise RuntimeError("AWS_REGION is required")
        action = argv[1]
        kind = argv[2]
        locator = argv[3]
        spec = load_json(Path(argv[4]).resolve(strict=True))
        run_dir = Path(argv[5]).resolve()
        assert_locator(kind, locator, spec)
        if action == "backup":
            print(json.dumps(backup(kind, spec, run_dir), sort_keys=True))
            return 0
        if action == "backup-verify":
            print(json.dumps(backup_verify(kind, spec, run_dir), sort_keys=True))
            return 0
        if action == "apply":
            for declared_artifact in argv[6:]:
                artifact(declared_artifact)
            apply(kind, spec, run_dir)
            return 0
        if action == "rollback":
            rollback(kind, spec, run_dir)
            return 0
        if action == "rollback-verify":
            return 0 if rollback_verify(kind, spec, run_dir) else 1
        raise RuntimeError(f"unknown action: {action}")
    except Exception as error:  # noqa: BLE001 - command boundary normalizes failures
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
