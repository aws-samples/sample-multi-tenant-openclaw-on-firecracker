#!/usr/bin/env python3
"""Command-line entry point for claw-patch-v2."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _patch_model import (
    PatchError,
    load_catalog,
    load_json,
    validate_artifacts,
    validate_environment,
    validate_manifest,
)
from _patch_runtime import (
    acknowledge_manual,
    approve_plan,
    create_plan,
    execute_plan,
    rollback_plan,
    verify_plan,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = SKILL_DIR / "references" / "feature-catalog.json"


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True))


def _validate(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = args.manifest.resolve(strict=True)
    manifest = load_json(manifest_path)
    catalog = load_catalog(args.catalog.resolve(strict=True))
    model = validate_manifest(manifest, catalog)
    validate_artifacts(manifest, manifest_path.parent)
    result: dict[str, Any] = {
        "valid": True,
        "manifest": str(manifest_path),
        "enabled_features": sorted(model["enabled_features"]),
        "operation_count": len(model["operations"]),
        "acceptance_count": len(model["acceptance"]),
    }
    if args.environment is not None:
        environment = load_json(args.environment.resolve(strict=True))
        validate_environment(environment)
        result["target"] = {
            "account": environment["account"],
            "region": environment["region"],
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="patchctl.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate manifest closure")
    validate.add_argument("--manifest", type=_path, required=True)
    validate.add_argument("--catalog", type=_path, default=DEFAULT_CATALOG)
    validate.add_argument("--environment", type=_path)

    plan = subparsers.add_parser("plan", help="create an immutable read-only plan")
    plan.add_argument("--manifest", type=_path, required=True)
    plan.add_argument("--environment", type=_path, required=True)
    plan.add_argument("--catalog", type=_path, default=DEFAULT_CATALOG)
    plan.add_argument("--run-dir", type=_path, required=True)

    approve = subparsers.add_parser("approve", help="approve one exact plan")
    approve.add_argument("--run-dir", type=_path, required=True)
    approve.add_argument("--yes", action="store_true")
    approve.add_argument("--allow-irreversible", action="store_true")

    for name in ("apply", "resume", "verify", "rollback"):
        command = subparsers.add_parser(name)
        command.add_argument("--run-dir", type=_path, required=True)

    acknowledge = subparsers.add_parser(
        "ack-manual",
        help="bind redacted evidence to a completed manual operation",
    )
    acknowledge.add_argument("--run-dir", type=_path, required=True)
    acknowledge.add_argument("--operation", required=True)
    acknowledge.add_argument("--evidence", type=_path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            result = _validate(args)
        elif args.command == "plan":
            plan = create_plan(
                args.manifest,
                args.environment,
                args.catalog,
                args.run_dir,
            )
            result = {
                "plan": str((args.run_dir / "PLAN.json").resolve()),
                "plan_fingerprint": plan["plan_fingerprint"],
                "target": plan["target"],
                "operations": [
                    {
                        "id": operation["id"],
                        "state": operation["observed_state"],
                        "execution": operation["execution"],
                        "rollback": operation["rollback"],
                    }
                    for operation in plan["operations"]
                ],
            }
        elif args.command == "approve":
            if not args.yes:
                raise PatchError("approve requires --yes")
            result = approve_plan(
                args.run_dir,
                allow_irreversible=args.allow_irreversible,
            )
        elif args.command in {"apply", "resume"}:
            result = execute_plan(args.run_dir)
        elif args.command == "ack-manual":
            result = acknowledge_manual(
                args.run_dir,
                args.operation,
                args.evidence,
            )
        elif args.command == "verify":
            result = verify_plan(args.run_dir)
        elif args.command == "rollback":
            result = rollback_plan(args.run_dir)
        else:
            raise AssertionError(f"unhandled command: {args.command}")
    except (OSError, PatchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
