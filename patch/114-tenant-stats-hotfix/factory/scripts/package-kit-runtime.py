#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Copy the fixed execution runtime into a generated patch kit."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


RUNTIME_FILES = (
    "patch-set.sh",
    "autopatch.sh",
    "preflight-once.sh",
    "patch-plan.sh",
    "interview-once.py",
    "_lanes.sh",
    "review-kit.py",
    "find-unqualified-routes.py",
    "discover-env.sh",
)
RUNTIME_PREFIX = "runtime/scripts/"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path, description: str) -> tuple[bytes, int]:
    if path.is_symlink():
        raise ValueError(f"symlink is not allowed for {description}: {path}")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise ValueError(f"missing {description}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{description} is not a regular file: {path}")
    return path.read_bytes(), stat.S_IMODE(metadata.st_mode)


def _load_sources(script_dir: Path) -> dict[str, tuple[bytes, int]]:
    return {
        name: _read_regular(script_dir / name, "runtime source")
        for name in RUNTIME_FILES
    }


def _load_manifest(kit: Path) -> tuple[Path, dict[str, object], int]:
    manifest_path = kit / "manifest.json"
    data, mode = _read_regular(manifest_path, "kit manifest")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid kit manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("kit manifest must be a JSON object")
    inventory = manifest.get("kit_files", {})
    if not isinstance(inventory, dict):
        raise ValueError("manifest.kit_files must be a JSON object")
    return manifest_path, manifest, mode


def _check_directory(path: Path, description: str) -> bool:
    if path.is_symlink():
        raise ValueError(f"symlink is not allowed for {description}: {path}")
    if not path.exists():
        return False
    if not path.is_dir():
        raise ValueError(f"{description} is not a directory: {path}")
    return True


def _declared_hash(
    inventory: dict[str, object], relative: str
) -> str | None:
    if relative not in inventory:
        return None
    declaration = inventory[relative]
    if not isinstance(declaration, dict):
        raise ValueError(f"manifest.kit_files[{relative!r}] must be an object")
    digest = declaration.get("sha256")
    if not isinstance(digest, str):
        raise ValueError(
            f"manifest.kit_files[{relative!r}].sha256 must be a string"
        )
    return digest


def _validate_existing_runtime(
    kit: Path,
    sources: dict[str, tuple[bytes, int]],
    old_inventory: dict[str, object],
) -> dict[str, tuple[bytes, int] | None]:
    runtime_dir = kit / "runtime"
    scripts_dir = runtime_dir / "scripts"
    runtime_exists = _check_directory(runtime_dir, "kit runtime directory")
    scripts_exists = False
    if runtime_exists:
        scripts_exists = _check_directory(scripts_dir, "kit runtime scripts directory")

    expected = set(RUNTIME_FILES)
    expected_relative = {f"{RUNTIME_PREFIX}{name}" for name in RUNTIME_FILES}
    for relative in old_inventory:
        if relative.startswith(RUNTIME_PREFIX) and relative not in expected_relative:
            raise ValueError(
                f"non-tool runtime artifact declared in manifest: {relative}"
            )

    existing: dict[str, tuple[bytes, int] | None] = {
        name: None for name in RUNTIME_FILES
    }
    if scripts_exists:
        for path in sorted(scripts_dir.iterdir(), key=lambda item: item.name):
            if path.is_symlink():
                raise ValueError(
                    f"symlink is not allowed in kit runtime scripts: {path}"
                )
            if path.name not in expected or not path.is_file():
                raise ValueError(f"non-tool runtime artifact already exists: {path}")
        for name in RUNTIME_FILES:
            target = scripts_dir / name
            if target.exists():
                existing[name] = _read_regular(target, "runtime target")

    for name, current in existing.items():
        relative = f"{RUNTIME_PREFIX}{name}"
        declared = _declared_hash(old_inventory, relative)
        if current is None:
            if declared is not None:
                raise ValueError(
                    f"manifest declares missing runtime target: {relative}"
                )
            continue
        current_digest = _sha(current[0])
        desired_digest = _sha(sources[name][0])
        if declared is not None and current_digest != declared:
            raise ValueError(
                f"runtime target hash mismatch for {relative}: "
                f"actual {current_digest}, declared {declared}"
            )
        if declared is None and current_digest != desired_digest:
            raise ValueError(
                f"runtime target is not owned by package-kit-runtime: {relative}"
            )
    return existing


def _inventory_lib(kit: Path) -> dict[str, dict[str, str]]:
    lib_dir = kit / "lib"
    if not _check_directory(lib_dir, "kit lib directory"):
        return {}

    inventory: dict[str, dict[str, str]] = {}
    for root, dirs, files in os.walk(lib_dir, followlinks=False):
        dirs.sort()
        files.sort()
        root_path = Path(root)
        for name in dirs:
            directory = root_path / name
            if directory.is_symlink():
                raise ValueError(f"symlink is not allowed in kit lib/: {directory}")
            if not directory.is_dir():
                raise ValueError(f"non-directory entry in kit lib/: {directory}")
        for name in files:
            path = root_path / name
            data, _ = _read_regular(path, "kit lib file")
            relative = path.relative_to(kit).as_posix()
            inventory[relative] = {"sha256": _sha(data)}
    return inventory


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
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
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            trash = Path.home() / "Documents" / "trashllm" / "oc-patch-temp"
            try:
                trash.mkdir(parents=True, exist_ok=True)
                os.replace(
                    temporary,
                    trash / f"{temporary.name}.{os.getpid()}",
                )
            except OSError:
                # Preserve the failed temporary file in place if trash is unavailable.
                pass


def package(kit: Path, script_dir: Path) -> None:
    sources = _load_sources(script_dir)
    manifest_path, manifest, manifest_mode = _load_manifest(kit)
    old_inventory = manifest.get("kit_files", {})
    assert isinstance(old_inventory, dict)
    existing = _validate_existing_runtime(kit, sources, old_inventory)

    inventory = _inventory_lib(kit)
    for name in RUNTIME_FILES:
        inventory[f"{RUNTIME_PREFIX}{name}"] = {"sha256": _sha(sources[name][0])}
    inventory = dict(sorted(inventory.items()))
    manifest["kit_files"] = inventory
    manifest_bytes = (
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    scripts_dir = kit / "runtime" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        source_data, source_mode = sources[name]
        current = existing[name]
        if current is None or current != (source_data, source_mode):
            _atomic_write(scripts_dir / name, source_data, source_mode)
    if manifest_path.read_bytes() != manifest_bytes:
        _atomic_write(manifest_path, manifest_bytes, manifest_mode)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: package-kit-runtime.py <kit>", file=sys.stderr)
        return 2
    kit_argument = Path(argv[1])
    if kit_argument.is_symlink():
        print(
            f"PACKAGE_KIT_RUNTIME_FAILED: symlink is not allowed for kit: "
            f"{kit_argument}",
            file=sys.stderr,
        )
        return 2
    try:
        kit = kit_argument.resolve(strict=True)
        if not kit.is_dir():
            raise ValueError(f"kit is not a directory: {kit}")
        package(kit, Path(__file__).resolve().parent)
    except (OSError, ValueError) as exc:
        print(f"PACKAGE_KIT_RUNTIME_FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"PACKAGED_KIT_RUNTIME files={len(RUNTIME_FILES)} kit={kit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
