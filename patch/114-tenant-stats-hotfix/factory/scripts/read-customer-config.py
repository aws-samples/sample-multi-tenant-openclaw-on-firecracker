#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Extract non-secret patch routing facts from a customer config.yml."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml


def fail(message: str) -> None:
    raise SystemExit(f"CONFIG_CHECK_FAILED: {message}")


def read_config(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        fail(f"config must be a regular file: {path}")
    raw = path.read_bytes()
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        fail(f"invalid YAML: {exc}")
    if not isinstance(value, dict):
        fail("config root must be an object")

    api = value.get("api") or {}
    if not isinstance(api, dict):
        fail("api must be an object")
    explicit_mode = str(api.get("mode") or "").strip().lower()
    legacy_private = api.get("private_api_enabled")
    if explicit_mode:
        if explicit_mode not in {"edge", "private", "both"}:
            fail("api.mode must be edge, private, or both")
        mode = explicit_mode
    elif legacy_private is True:
        mode = "both"
    else:
        mode = "edge"

    tenant_stats = value.get("tenant_stats") or {}
    if not isinstance(tenant_stats, dict):
        fail("tenant_stats must be an object")
    enabled = tenant_stats.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        fail("tenant_stats.enabled must be a boolean when present")

    return {
        "schema_version": 1,
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "api_mode": mode,
        "api_mode_source": "api.mode" if explicit_mode else "legacy/default",
        "tenant_stats_enabled": enabled,
        "note": (
            "These are routing hints only. The operator-confirmed client URL and "
            "authenticated live probes select the REST API that may be patched."
        ),
    }


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            trash = Path.home() / "Documents" / "trashllm" / "oc-patch-temp"
            trash.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, trash / f"{temporary.name}.{os.getpid()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    write_json(arguments.output, read_config(arguments.config))


if __name__ == "__main__":
    main()
