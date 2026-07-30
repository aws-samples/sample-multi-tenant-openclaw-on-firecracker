#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render the fixed operator documents that travel with every compiled kit."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


TEMPLATES = {
    "CLAUDE.md": "patch-claude-template.md",
    "APPLY-INSTRUCTIONS.md": "compiled-apply-instructions-template.md",
}


def lane(manifest: dict[str, object]) -> str:
    fields = (
        ("lambda_functions", "lambda"),
        ("ddb_settings", "ddb"),
        ("ddb_tables", "ddb-create"),
        ("api_routes", "api-gateway"),
        ("tenant_stats_backends", "tenant-stats-backend"),
    )
    selected = [
        name
        for field, name in fields
        if isinstance(manifest.get(field), list) and manifest[field]
    ]
    if len(selected) > 1:
        raise ValueError("one kit may declare only one typed lane")
    return selected[0] if selected else "host-config"


def atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"document target is a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            trash = Path.home() / "Documents" / "trashllm" / "oc-patch-temp"
            trash.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, trash / f"{temporary.name}.{os.getpid()}")


def package(kit: Path, references: Path) -> None:
    manifest_path = kit / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"invalid manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    kit_id = manifest.get("id")
    if not isinstance(kit_id, str) or not kit_id:
        raise ValueError("manifest.id must be a non-empty string")
    selected_lane = lane(manifest)
    for output_name, template_name in TEMPLATES.items():
        template_path = references / template_name
        if template_path.is_symlink() or not template_path.is_file():
            raise ValueError(f"invalid document template: {template_path}")
        rendered = (
            template_path.read_text(encoding="utf-8")
            .replace("{{KIT_ID}}", kit_id)
            .replace("{{LANE}}", selected_lane)
        )
        if "{{" in rendered or "}}" in rendered:
            raise ValueError(f"unresolved document placeholder in {template_name}")
        atomic_write(kit / output_name, rendered.encode("utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: package-kit-docs.py <kit>", file=sys.stderr)
        return 2
    try:
        kit = Path(argv[1]).resolve(strict=True)
        if not kit.is_dir():
            raise ValueError(f"kit is not a directory: {kit}")
        package(kit, Path(__file__).resolve().parents[1] / "references")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"PACKAGE_KIT_DOCS_FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"PACKAGED_KIT_DOCS kit={kit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
