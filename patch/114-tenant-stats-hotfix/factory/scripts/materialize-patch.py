#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bind the public patch templates to one discovered target environment."""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path


BASE_SHA = "a547dc74fe25ea0219c804933c5a7da8af1e3b39"
PATCH_SHA = "f8b9e14e5f456a24dc8fc597528a7b1b1540a9f3"
TABLE = "openclaw-tenant-stats"


def fail(message: str) -> None:
    raise SystemExit(f"MATERIALIZE_FAILED: {message}")


def load_object(path: Path, description: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        fail(f"{description} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read {description}: {exc}")
    if not isinstance(value, dict):
        fail(f"{description} must be a JSON object")
    return value


def one_stage(environment: dict[str, object], override: str | None) -> str:
    control_plane = environment.get("control_plane_api")
    if not isinstance(control_plane, dict):
        fail("environment.control_plane_api is missing")
    stages = control_plane.get("deployed_stages")
    available = []
    if isinstance(stages, list):
        available = [
            item["stage"]
            for item in stages
            if isinstance(item, dict)
            and isinstance(item.get("stage"), str)
            and item["stage"]
        ]
    discovered = control_plane.get("stage")
    selected = override or (discovered if isinstance(discovered, str) else None)
    if selected is None:
        if len(available) != 1:
            fail(
                "the live API stage is ambiguous; set OC_CONTROL_PLANE_STAGE "
                "to one of: " + ", ".join(available)
            )
        selected = available[0]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", selected):
        fail(f"invalid API Gateway stage: {selected!r}")
    if available and selected not in available:
        fail(f"stage {selected!r} is not deployed on the confirmed API")
    return selected


def common_manifest(identifier: str) -> dict[str, object]:
    return {
        "id": identifier,
        "base_sha": BASE_SHA,
        "patch_sha": PATCH_SHA,
        "status": "READY",
        "excludes": [],
        "cloudformation": {
            "status": "NOT_APPLICABLE",
            "reason": (
                "This is a target-bound hot-patch operation. It does not run a "
                "CloudFormation deployment."
            ),
            "provenance": None,
            "stacks": [],
        },
        "kit_files": {},
        "paths": {},
    }


def table_manifest(account: str, region: str) -> dict[str, object]:
    value = common_manifest("114-tenant-stats-table")
    value.update(
        {
            "fixes": [
                {
                    "id": "F-tenant-stats-table",
                    "summary": "Create the tenant statistics table with PITR enabled",
                    "paths": ["deploy/stacks/storage.py"],
                    "applies_when": "always",
                    "params_changed": [],
                    "verification_ids": ["V-tenant-stats-table"],
                }
            ],
            "verifications": [
                {
                    "id": "V-tenant-stats-table",
                    "fix_id": "F-tenant-stats-table",
                    "phase": "A-readonly",
                    "action": "generated verify.sh",
                    "observable": "DynamoDB table schema, billing mode, status, and PITR",
                    "pass_when": "table is ACTIVE with the declared schema and PITR enabled",
                    "fail_when": "the table is absent, unreadable, or differs",
                    "timeout_s": 600,
                    "cleanup": None,
                }
            ],
            "ddb_tables": [
                {
                    "table": TABLE,
                    "target_account": account,
                    "target_region": region,
                    "partition_key": {"name": "id", "type": "S"},
                    "billing_mode": "PAY_PER_REQUEST",
                    "pitr": True,
                    "cfn_follow_up": (
                        f"import {TABLE} into the storage stack before the next "
                        "CloudFormation deployment"
                    ),
                }
            ],
        }
    )
    return value


def route_manifest(
    account: str,
    region: str,
    api_id: str,
    stage: str,
    function_name: str,
    qualifier: str,
) -> dict[str, object]:
    value = common_manifest("114-tenants-stats-route")
    value.update(
        {
            "fixes": [
                {
                    "id": "F-tenants-stats-route",
                    "summary": "Add GET /tenants-stats to the confirmed control-plane API",
                    "paths": ["deploy/stacks/lambdas.py"],
                    "applies_when": "always",
                    "params_changed": [],
                    "verification_ids": ["V-tenants-stats-route"],
                }
            ],
            "verifications": [
                {
                    "id": "V-tenants-stats-route",
                    "fix_id": "F-tenants-stats-route",
                    "phase": "A-readonly",
                    "action": "generated verify.sh",
                    "observable": "deployed OpenAPI route and Lambda integration",
                    "pass_when": "the live stage exports the declared method and integration",
                    "fail_when": "the deployed route is absent, unreadable, or differs",
                    "timeout_s": 300,
                    "cleanup": None,
                }
            ],
            "api_routes": [
                {
                    "api_id": api_id,
                    "target_account": account,
                    "target_region": region,
                    "stage": stage,
                    "path": "/tenants-stats",
                    "method": "GET",
                    "target_function": function_name,
                    "target_qualifier": qualifier,
                    "authorization_type": "NONE",
                    "api_key_required": True,
                    "cors": {
                        "allow_origin": "*",
                        "allow_headers": [
                            "Content-Type",
                            "x-api-key",
                            "Authorization",
                        ],
                        "allow_methods": [
                            "OPTIONS",
                            "GET",
                            "PUT",
                            "POST",
                            "DELETE",
                            "PATCH",
                            "HEAD",
                        ],
                    },
                    "kind": "lambda-proxy-route",
                }
            ],
        }
    )
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=False)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def materialize(
    environment_path: Path,
    lambda_template_path: Path,
    output: Path,
    stage_override: str | None,
) -> None:
    environment = load_object(environment_path, "environment")
    account = str(environment.get("account", ""))
    region = environment.get("region")
    if not re.fullmatch(r"[0-9]{12}", account):
        fail("environment.account must be a 12-digit AWS account id")
    if not isinstance(region, str) or not re.fullmatch(
        r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+", region
    ):
        fail("environment.region is invalid")

    control_plane = environment.get("control_plane_api")
    if not isinstance(control_plane, dict) or control_plane.get("confirmed") is not True:
        fail("control-plane API was not machine-confirmed")
    api_id = control_plane.get("id")
    if not isinstance(api_id, str) or not re.fullmatch(r"[a-z0-9]{10}", api_id):
        fail("confirmed control-plane API id is invalid")
    stage = one_stage(environment, stage_override)

    lambda_link = environment.get("lambda_link")
    if not isinstance(lambda_link, dict):
        fail("environment.lambda_link is missing")
    function_name = lambda_link.get("function")
    qualifier = lambda_link.get("serving_qualifier")
    if not isinstance(function_name, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", function_name
    ):
        fail("serving Lambda function is unresolved")
    if not isinstance(qualifier, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", qualifier
    ):
        fail("serving Lambda qualifier is unresolved")
    aliases = lambda_link.get("aliases")
    if not isinstance(aliases, list) or not any(
        isinstance(item, dict) and item.get("alias") == qualifier for item in aliases
    ):
        fail(f"serving qualifier {qualifier!r} is not a live Lambda alias")

    lambda_manifest = copy.deepcopy(
        load_object(lambda_template_path, "Lambda manifest template")
    )
    functions = lambda_manifest.get("lambda_functions")
    if not isinstance(functions, list) or len(functions) != 1:
        fail("Lambda manifest template must declare exactly one function")
    functions[0]["function_name"] = function_name
    functions[0]["alias"] = qualifier
    lambda_manifest["kit_files"] = {}

    if output.exists() or output.is_symlink():
        fail(f"output already exists: {output}")
    output.mkdir(parents=True)
    write_json(output / "114-tenant-stats-table" / "manifest.json", table_manifest(account, region))
    write_json(output / "114-api-lambda" / "manifest.json", lambda_manifest)
    write_json(
        output / "114-tenants-stats-route" / "manifest.json",
        route_manifest(account, region, api_id, stage, function_name, qualifier),
    )
    print(
        "MATERIALIZED_PATCH_SET "
        f"account={account} region={region} api={api_id} stage={stage} "
        f"lambda={function_name}:{qualifier}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("environment", type=Path)
    parser.add_argument("lambda_template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--stage")
    arguments = parser.parse_args()
    materialize(
        arguments.environment,
        arguments.lambda_template,
        arguments.output,
        arguments.stage,
    )


if __name__ == "__main__":
    main()
