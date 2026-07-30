#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bind an independent Claude verdict to the exact generated patch kit.

The compiler, reviewer, and live runner are separate gates. This tool connects the
second gate to the third without making the compiler judge its own output:

  review-kit.py prepare <kit> <material>
  review-kit.sh <kit> <rubric-file>
  review-kit.py check <kit>

`review-kit.sh` is the supported process entry point. The private `_seal` command
binds the retained Claude result to the kit and is intentionally omitted from the
public usage text. The receipt is deliberately labeled unsigned: it catches stale
or accidentally edited bytes, but it is not a cryptographic authorization boundary
against someone who controls the local filesystem.

The fingerprint excludes run-time artifacts and the review receipt itself. It covers
manifest.json and every generated/shipped file, so changing one byte after review
invalidates the receipt before preflight can write to AWS.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath


EXCLUDED_FILES = {
    "PLAN.json",
    "DECISION.json",
    "REVIEW.json",
    "CLAUDE-REVIEW.txt",
}
REVIEWER = "Claude Code safe-mode review-kit.sh"
VERDICT_FILENAME = "CLAUDE-REVIEW.txt"
VERDICT_RE = re.compile(
    r"^KIT_REVIEW_VERDICT: PASS SCORE=([0-9]+(?:\.[0-9]+)?) "
    r"BLOCKERS=([0-9]+) FINGERPRINT=([0-9a-f]{64})$"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generated_files(kit: Path) -> list[Path]:
    files = []
    for root, dirs, names in os.walk(kit):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            if name in EXCLUDED_FILES or name.endswith(".pyc"):
                continue
            path = Path(root) / name
            if path.is_symlink():
                raise ValueError(f"symlink is not allowed in a reviewed kit: {path}")
            if path.is_file():
                files.append(path)
    return sorted(files, key=lambda path: path.relative_to(kit).as_posix())


def kit_fingerprint(kit: Path) -> str:
    entries = [
        (path.relative_to(kit).as_posix(), _sha(path.read_bytes()))
        for path in generated_files(kit)
    ]
    return _sha(json.dumps(entries, separators=(",", ":")).encode())


def lambda_source_artifacts(kit: Path) -> dict[str, tuple[str, bytes]]:
    """Map overlay-relative source paths to the separately shipped source artifact."""
    try:
        manifest = json.loads((kit / "manifest.json").read_text(encoding="utf-8"))
        functions = manifest.get("lambda_functions") or []
        if len(functions) != 1:
            return {}
        package_root = functions[0].get("package_root")
        paths = manifest.get("paths")
        if not isinstance(package_root, str) or not isinstance(paths, dict):
            return {}
        prefix = package_root.rstrip("/") + "/"
        artifacts = {}
        for source_path, declaration in paths.items():
            if not isinstance(source_path, str) or not source_path.startswith(prefix):
                continue
            if not isinstance(declaration, dict):
                continue
            artifact = declaration.get("artifact")
            if not isinstance(artifact, str):
                continue
            artifact_path = kit / artifact
            if artifact_path.is_symlink() or not artifact_path.is_file():
                continue
            artifacts[source_path[len(prefix) :]] = (
                artifact,
                artifact_path.read_bytes(),
            )
        return artifacts
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def review_view(
    relative: str,
    data: bytes,
    source_artifacts: dict[str, tuple[str, bytes]] | None = None,
) -> bytes:
    if relative.endswith("/payload/overlay.json"):
        try:
            payload = json.loads(data)
            sources = payload["sources"]
            base_hashes = payload["base_hashes"]
            patch_hashes = payload["patch_hashes"]
            if not all(
                isinstance(item, dict)
                for item in (sources, base_hashes, patch_hashes)
            ):
                raise ValueError("overlay maps must be JSON objects")

            summary = {
                "overlay_payload": True,
                "original_bytes": len(data),
                "base_hashes": base_hashes,
                "patch_hashes": patch_hashes,
                "source_count": len(sources),
            }
            chunks = [
                (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
            ]
            import base64

            for source_path, encoded in sorted(sources.items()):
                parsed = PurePosixPath(source_path)
                if (
                    not source_path
                    or parsed.is_absolute()
                    or ".." in parsed.parts
                    or source_path not in patch_hashes
                ):
                    raise ValueError(f"unsafe or unbound source path: {source_path!r}")
                decoded = base64.b64decode(encoded, validate=True)
                digest = _sha(decoded)
                if patch_hashes[source_path] != digest:
                    raise ValueError(
                        f"source hash mismatch for {source_path}: "
                        f"{digest} != {patch_hashes[source_path]}"
                    )
                decoded.decode("utf-8")
                chunks.append(
                    (
                        f"\n######## OVERLAY SOURCE {source_path} "
                        f"sha256={digest} ########\n"
                    ).encode()
                )
                artifact = (source_artifacts or {}).get(source_path)
                if artifact is not None:
                    artifact_path, artifact_bytes = artifact
                    if artifact_bytes != decoded:
                        raise ValueError(
                            "overlay source differs from separately shipped artifact: "
                            f"{source_path} != {artifact_path}"
                        )
                    chunks.append(
                        (
                            "SOURCE_BYTES_IDENTICAL_TO_KIT_ARTIFACT "
                            f"{artifact_path}\n"
                        ).encode()
                    )
                else:
                    chunks.append(decoded)
                    if not decoded.endswith(b"\n"):
                        chunks.append(b"\n")
            return b"".join(chunks)
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError(
                f"Lambda overlay must be fully reviewable: {relative}: {exc}"
            ) from exc
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "binary content cannot be independently reviewed from a hash alone: "
            f"{relative}"
        ) from exc
    return data


def parse_verdict(text: str) -> dict[str, object]:
    lines = text.splitlines()
    if not lines:
        raise ValueError("Claude verdict is empty")
    match = VERDICT_RE.fullmatch(lines[-1])
    if not match:
        raise ValueError(
            "physical final line must be KIT_REVIEW_VERDICT: PASS "
            "SCORE=<0-10> BLOCKERS=0 FINGERPRINT=<64 hex>"
        )
    score = float(match.group(1))
    blockers = int(match.group(2))
    actual_blockers = sum(line.startswith("BLOCKER:") for line in lines)
    if not 0 <= score <= 10:
        raise ValueError(f"score {score} is outside 0-10")
    if blockers != actual_blockers:
        raise ValueError(
            f"declared BLOCKERS={blockers}, found {actual_blockers} BLOCKER lines"
        )
    if score < 6.5 or blockers != 0:
        raise ValueError(
            f"review did not pass: score={score}, blockers={blockers}; need >=6.5 and 0"
        )
    return {
        "score": score,
        "blockers": blockers,
        "fingerprint": match.group(3),
    }


def review_material(kit: Path) -> bytes:
    fingerprint = kit_fingerprint(kit)
    source_artifacts = lambda_source_artifacts(kit)
    handle = io.BytesIO()
    try:
        handle.write(
            (
                "INDEPENDENT PATCH KIT REVIEW\n"
                f"KIT_FINGERPRINT={fingerprint}\n\n"
                "Review the final generated manifest and scripts as production hot-patch "
                "artifacts. A blocker is any path that can report success while the intended "
                "live effect is absent, overwrite third-party work, break rollback or "
                "idempotency, weaken auth, or give the unattended driver a misleading exit "
                "code. Every blocker must be one line beginning BLOCKER:. The physical final "
                "line must be exactly:\n"
                "KIT_REVIEW_VERDICT: PASS SCORE=<0-10> BLOCKERS=<integer> "
                f"FINGERPRINT={fingerprint}\n\n"
            ).encode()
        )
        for path in generated_files(kit):
            relative = path.relative_to(kit).as_posix()
            data = path.read_bytes()
            handle.write(f"######## {relative} sha256={_sha(data)} ########\n".encode())
            view = review_view(relative, data, source_artifacts)
            handle.write(view)
            if not view.endswith(b"\n"):
                handle.write(b"\n")
        return handle.getvalue()
    finally:
        handle.close()


def prepare(kit: Path, output: Path) -> None:
    output.write_bytes(review_material(kit))


def seal(kit: Path, material_path: Path, verdict_path: Path) -> None:
    material_bytes = material_path.read_bytes()
    material_text = material_bytes.decode("utf-8")
    fingerprint = kit_fingerprint(kit)
    expected_material = review_material(kit)
    if material_bytes != expected_material:
        raise ValueError(
            "review material is not the complete canonical view of the current kit"
        )
    expected_header = f"KIT_FINGERPRINT={fingerprint}"
    if expected_header not in material_text.splitlines()[:4]:
        raise ValueError(
            "review material does not bind the current kit fingerprint"
        )
    verdict_bytes = verdict_path.read_bytes()
    verdict_text = verdict_bytes.decode("utf-8")
    parsed = parse_verdict(verdict_text)
    if parsed["fingerprint"] != fingerprint:
        raise ValueError(
            "Claude reviewed fingerprint "
            f"{parsed['fingerprint']}, current kit is {fingerprint}"
        )
    receipt = {
        "schema_version": 1,
        "receipt_type": "unsigned_process_receipt",
        "reviewer": REVIEWER,
        "verdict": "PASS",
        "score": parsed["score"],
        "blockers": 0,
        "kit_fingerprint": fingerprint,
        "review_material_sha256": _sha(material_bytes),
        "verdict_sha256": _sha(verdict_bytes),
    }
    verdict_out = kit / VERDICT_FILENAME
    receipt_out = kit / "REVIEW.json"
    verdict_tmp = kit / f".{VERDICT_FILENAME}.tmp"
    receipt_tmp = kit / ".REVIEW.json.tmp"
    verdict_tmp.write_bytes(verdict_bytes)
    receipt_tmp.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    os.replace(verdict_tmp, verdict_out)
    os.replace(receipt_tmp, receipt_out)


def check(kit: Path) -> None:
    receipt_path = kit / "REVIEW.json"
    verdict_path = kit / VERDICT_FILENAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    verdict_bytes = verdict_path.read_bytes()
    parsed = parse_verdict(verdict_bytes.decode("utf-8"))
    fingerprint = kit_fingerprint(kit)
    material_hash = _sha(review_material(kit))
    expected = {
        "schema_version": 1,
        "receipt_type": "unsigned_process_receipt",
        "reviewer": REVIEWER,
        "verdict": "PASS",
        "score": parsed["score"],
        "blockers": 0,
        "kit_fingerprint": fingerprint,
        "review_material_sha256": material_hash,
        "verdict_sha256": _sha(verdict_bytes),
    }
    recorded_material_hash = receipt.get("review_material_sha256")
    if not isinstance(recorded_material_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", recorded_material_hash
    ):
        raise ValueError("REVIEW.json has no valid review material hash")
    if receipt != expected:
        raise ValueError("REVIEW.json does not match the verdict or current kit")
    if parsed["fingerprint"] != fingerprint:
        raise ValueError(
            f"review is stale: reviewed {parsed['fingerprint']}, current kit {fingerprint}"
        )
    print(
        f"CLAUDE_REVIEW_OK score={parsed['score']} blockers=0 "
        f"fingerprint={fingerprint}"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: review-kit.py prepare <kit> <material> | "
            "check <kit> | fingerprint <kit>",
            file=sys.stderr,
        )
        return 2
    command = argv[1]
    kit = Path(argv[2]).resolve()
    try:
        if command == "prepare" and len(argv) == 4:
            prepare(kit, Path(argv[3]).resolve())
        elif command == "_seal" and len(argv) == 5:
            if os.environ.get("OC_PATCH_REVIEW_WRAPPER") != "claude":
                raise ValueError("_seal is private; use review-kit.sh")
            seal(
                kit,
                Path(argv[3]).resolve(),
                Path(argv[4]).resolve(),
            )
        elif command == "check" and len(argv) == 3:
            check(kit)
        elif command == "fingerprint" and len(argv) == 3:
            print(kit_fingerprint(kit))
        else:
            raise ValueError("invalid command arguments")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"REVIEW_GATE_FAILED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
