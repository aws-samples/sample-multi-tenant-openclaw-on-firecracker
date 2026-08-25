#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Mirror the latest S3 shared-skill object versions onto one host."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import time

import boto3


def _safe_relative_key(key: str, prefix: str) -> Path | None:
    if not key.startswith(prefix) or key.endswith("/"):
        return None
    relative = PurePosixPath(key[len(prefix) :])
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe S3 skill key: {key!r}")
    return Path(*relative.parts)


def _latest_objects(s3, bucket: str, prefix: str) -> dict[str, dict]:
    """Return the latest non-deleted version for every object below prefix."""
    desired: dict[str, dict] = {}
    deleted: set[str] = set()
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for marker in page.get("DeleteMarkers", []):
            if marker.get("IsLatest"):
                deleted.add(marker["Key"])
        for version in page.get("Versions", []):
            if version.get("IsLatest"):
                desired[version["Key"]] = {
                    "version_id": str(version.get("VersionId") or ""),
                    "etag": str(version.get("ETag") or "").strip('"'),
                    "size": int(version.get("Size") or 0),
                }
    for key in deleted:
        desired.pop(key, None)
    return desired


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _write_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _download_version(s3, bucket: str, key: str, version: dict, target: Path) -> str:
    kwargs = {
        "Bucket": bucket,
        "Key": key,
        "ChecksumMode": "ENABLED",
    }
    if version["version_id"]:
        kwargs["VersionId"] = version["version_id"]
    response = s3.get_object(**kwargs)
    returned_version = str(response.get("VersionId") or "")
    if returned_version and returned_version != version["version_id"]:
        raise RuntimeError(
            f"S3 returned VersionId {returned_version!r} for requested "
            f"{version['version_id']!r}: {key}"
        )
    returned_etag = str(response.get("ETag") or "").strip('"')
    if version["etag"] and returned_etag != version["etag"]:
        raise RuntimeError(f"S3 ETag changed while downloading {key}")

    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with target.open("xb") as stream:
        body = response["Body"]
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            stream.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(target, 0o644)

    expected_size = int(response.get("ContentLength", version["size"]))
    if size != expected_size or size != version["size"]:
        raise RuntimeError(
            f"S3 size mismatch for {key}: expected={version['size']} downloaded={size}"
        )
    sha256 = digest.hexdigest()
    metadata_sha = str((response.get("Metadata") or {}).get("sha256") or "").lower()
    if metadata_sha and metadata_sha != sha256:
        raise RuntimeError(f"S3 sha256 metadata mismatch for {key}")
    checksum_sha = str(response.get("ChecksumSHA256") or "")
    if checksum_sha and response.get("ChecksumType") != "COMPOSITE":
        actual_b64 = base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
        if checksum_sha != actual_b64:
            raise RuntimeError(f"S3 ChecksumSHA256 mismatch for {key}")
    return sha256


def sync_shared_skills(
    s3,
    bucket: str,
    region: str,
    destination: Path,
    state_path: Path,
    prefix: str = "skills/",
) -> dict:
    del region  # region belongs to client construction; kept in the public contract.
    desired = _latest_objects(s3, bucket, prefix)
    old_state = _read_state(state_path).get("objects") or {}
    applied_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changed = 0
    unchanged = 0
    desired_paths: set[str] = set()
    changed_paths: set[str] = set()
    new_state: dict[str, dict] = {}

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".shared-skills-stage.", dir=destination.parent
    ) as stage_name:
        stage = Path(stage_name)
        for key, version in sorted(desired.items()):
            relative = _safe_relative_key(key, prefix)
            if relative is None:
                continue
            relative_key = relative.as_posix()
            desired_paths.add(relative_key)
            current = destination / relative
            staged = stage / relative
            previous = old_state.get(key) or {}
            same_version = (
                previous.get("version_id") == version["version_id"]
                and previous.get("etag") == version["etag"]
                and current.is_file()
            )
            if same_version and previous.get("sha256") == _sha256(current):
                checksum = previous["sha256"]
                unchanged += 1
            else:
                checksum = _download_version(s3, bucket, key, version, staged)
                changed_paths.add(relative_key)
                changed += 1
            new_state[key] = {
                "version_id": version["version_id"],
                "etag": version["etag"],
                "sha256": checksum,
                "applied_at": applied_at,
            }

        # No host-visible mutation happens until every requested object is downloaded
        # and validated. A crash during this commit is repaired on the next run because
        # the state file is written last.
        destination.mkdir(parents=True, exist_ok=True)
        for relative_key in sorted(changed_paths):
            relative = Path(*PurePosixPath(relative_key).parts)
            source = stage / relative
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)

        deleted = 0
        for path in sorted(destination.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                relative_key = path.relative_to(destination).as_posix()
                if relative_key not in desired_paths:
                    path.unlink()
                    deleted += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    _write_state(
        state_path,
        {
            "bucket": bucket,
            "prefix": prefix,
            "applied_at": applied_at,
            "objects": new_state,
        },
    )
    return {
        "changed": changed,
        "unchanged": unchanged,
        "deleted": deleted,
        "objects": len(new_state),
        "applied_at": applied_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--destination", default="/data/shared-skills")
    parser.add_argument(
        "--state", default="/var/lib/openclaw/shared-skills-sync-state.json"
    )
    parser.add_argument(
        "--lock", default="/run/lock/openclaw-shared-skills-sync.lock"
    )
    args = parser.parse_args()

    lock_path = Path(args.lock)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        s3 = boto3.client("s3", region_name=args.region)
        result = sync_shared_skills(
            s3,
            args.bucket,
            args.region,
            Path(args.destination),
            Path(args.state),
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
