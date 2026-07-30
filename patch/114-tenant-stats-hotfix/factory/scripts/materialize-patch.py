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
from urllib.parse import urlsplit


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
    if (
        override is not None
        and isinstance(discovered, str)
        and discovered
        and override != discovered
    ):
        fail(
            "OC_CONTROL_PLANE_STAGE disagrees with the stage confirmed by discovery"
        )
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


def backend_manifest(
    account: str,
    region: str,
    inputs: dict[str, object],
) -> dict[str, object]:
    value = common_manifest("114-tenant-stats-table")
    value.update(
        {
            "fixes": [
                {
                    "id": "F-tenant-stats-backend",
                    "summary": (
                        "Create the statistics table, writer Lambda, least-privilege "
                        "role, initial snapshot, and one-minute schedule"
                    ),
                    "paths": [
                        "deploy/stacks/storage.py",
                        "deploy/stacks/lambdas.py",
                        "deploy/lambda/tenant_stats/handler.py",
                    ],
                    "applies_when": "always",
                    "params_changed": [],
                    "verification_ids": ["V-tenant-stats-backend"],
                }
            ],
            "verifications": [
                {
                    "id": "V-tenant-stats-backend",
                    "fix_id": "F-tenant-stats-backend",
                    "phase": "A-readonly",
                    "action": "generated verify.sh",
                    "observable": (
                        "DynamoDB schema/PITR, writer code/config/IAM, EventBridge "
                        "target, Lambda permission, and a current snapshot"
                    ),
                    "pass_when": "all backend resources match and current snapshot exists",
                    "fail_when": "any resource is absent, unreadable, or differs",
                    "timeout_s": 600,
                    "cleanup": None,
                }
            ],
            "rollback_notice": {
                "behavior": (
                    "Rollback disables the patch-owned EventBridge schedule and "
                    "removes its Lambda invoke permission."
                ),
                "retained_resources": [
                    "DynamoDB table and its data",
                    "tenant statistics writer Lambda",
                    "writer IAM role and inline policy",
                ],
                "reason": (
                    "Deleting stateful or reusable backend resources automatically "
                    "could destroy data or hide recovery evidence."
                ),
            },
            "tenant_stats_backends": [
                {
                    "target_account": account,
                    "target_region": region,
                    "marker": f"ocpatch:114-tenant-stats-table:{PATCH_SHA}",
                    "table": {
                        "name": TABLE,
                        "partition_key": {"name": "id", "type": "S"},
                        "billing_mode": "PAY_PER_REQUEST",
                        "pitr": True,
                    },
                    "writer": {
                        "source_path": "deploy/lambda/tenant_stats/handler.py",
                        "function_name": "openclaw-tenant-stats-writer",
                        "role_name": "openclaw-tenant-stats-writer-role",
                        "policy_name": "openclaw-tenant-stats-writer",
                        "runtime": "python3.12",
                        "architecture": "arm64",
                        "handler": "handler.lambda_handler",
                        "timeout": 50,
                        "memory_size": 8192,
                        "reserved_concurrency": 1,
                        "environment": {
                            "TENANTS_TABLE": inputs["tenants_table"],
                            "TENANT_STATS_TABLE": TABLE,
                            "ASSETS_BUCKET": inputs["assets_bucket"],
                            "ROOTFS_PREFIX": inputs["rootfs_prefix"],
                            "STATS_SCAN_SEGMENTS": "8",
                        },
                    },
                    "schedule": {
                        "rule_name": "openclaw-tenant-stats-schedule",
                        "expression": "rate(1 minute)",
                        "target_id": "TenantStatsWriter",
                        "permission_statement_id": "ocpatch-tenant-stats-schedule",
                    },
                    "cfn_follow_up": (
                        "Enable tenant_stats.enabled and import the retained table "
                        "before the next CloudFormation deployment."
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
    reference: dict[str, object],
    headers_sha256: str,
    configured_url: str,
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
                    "invoke_url": configured_url,
                    "path": "/tenants-stats",
                    "method": "GET",
                    "target_function": function_name,
                    "target_qualifier": qualifier,
                    "authorization_type": reference["authorization_type"],
                    "api_key_required": reference["api_key_required"],
                    "authorizer_id": reference.get("authorizer_id"),
                    "authorizer_name": reference.get("authorizer_name"),
                    "authorization_scopes": reference.get(
                        "authorization_scopes", []
                    ),
                    "auth_reference_path": reference["path"],
                    "auth_reference_method": reference["method"],
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
                    "probe": {
                        "method": "GET",
                        "headers_file_sha256": headers_sha256,
                        "body": None,
                        "expected_status": 200,
                        "expected_body_fields": {
                            "business": {},
                            "snapshot_stale": False,
                        },
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
    config_facts_path: Path,
    lambda_template_path: Path,
    output: Path,
    stage_override: str | None,
) -> None:
    environment = load_object(environment_path, "environment")
    config_facts = load_object(config_facts_path, "customer config facts")
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
    if control_plane.get("entrypoint_kind") != "explicit-rest-resources":
        fail(
            "the operator-confirmed entry point is not an explicit-resource REST "
            "API; ANY /{proxy+} is not an accepted target for this patch"
        )
    configured_url = control_plane.get("configured_client_url")
    if not isinstance(configured_url, str):
        fail("control-plane API lacks the operator-confirmed HTTPS client URL")
    parsed_url = urlsplit(configured_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        fail("operator-confirmed client URL must be a plain HTTPS base URL")
    reference = control_plane.get("reference_method")
    if not isinstance(reference, dict):
        fail("control-plane API has no exact /tenants GET reference method")
    if reference.get("path") != "/tenants" or reference.get("method") != "GET":
        fail("the auth reference must be exact GET /tenants")
    auth_type = reference.get("authorization_type")
    if auth_type not in {"NONE", "CUSTOM", "COGNITO_USER_POOLS"}:
        fail(
            f"reference authorization {auth_type!r} has no authenticated HTTP "
            "probe implementation in this factory"
        )
    if auth_type in {"CUSTOM", "COGNITO_USER_POOLS"} and not (
        isinstance(reference.get("authorizer_id"), str)
        and isinstance(reference.get("authorizer_name"), str)
    ):
        fail("the protected reference method's authorizer is unresolved")
    headers_sha256 = control_plane.get("probe_headers_sha256")
    if not isinstance(headers_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", headers_sha256
    ):
        fail("authenticated probe headers file was not hash-bound")
    probes = control_plane.get("probe_results")
    if not isinstance(probes, list) or not all(
        isinstance(item, dict)
        and item.get("path") in {"/tenants", "/hosts"}
        and isinstance(item.get("status"), int)
        and 200 <= item["status"] < 300
        for item in probes
    ) or {item["path"] for item in probes} != {"/tenants", "/hosts"}:
        fail("authenticated GET /tenants and GET /hosts probes did not both pass")

    config_sha = config_facts.get("config_sha256")
    api_mode = config_facts.get("api_mode")
    if not isinstance(config_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", config_sha):
        fail("customer config facts have no valid source hash")
    if api_mode not in {"edge", "private", "both"}:
        fail("customer config facts have no valid api_mode")

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
    expected_function_ref = f":function:{function_name}:{qualifier}/invocations"
    if expected_function_ref not in str(reference.get("integration_uri") or ""):
        fail(
            "the exact GET /tenants reference method does not integrate the "
            f"confirmed serving Lambda {function_name}:{qualifier}"
        )

    inputs = environment.get("tenant_stats_inputs")
    if not isinstance(inputs, dict):
        fail("environment.tenant_stats_inputs is missing")
    for key in ("tenants_table", "assets_bucket", "rootfs_prefix"):
        if not isinstance(inputs.get(key), str) or not inputs[key]:
            fail(f"tenant-stats writer input {key} is unresolved")

    target_confirmation = {
        "customer_config_sha256": config_sha,
        "configured_api_mode": api_mode,
        "confirmed_api_id": api_id,
        "confirmed_stage": stage,
        "confirmed_client_url": configured_url,
        "entrypoint_kind": "explicit-rest-resources",
        "proxy_resources_are_not_targets": True,
        "reference_authorization_type": auth_type,
        "reference_authorizer_name": reference.get("authorizer_name"),
        "reference_api_key_required": reference.get("api_key_required") is True,
        "reference_authorization_scopes": reference.get(
            "authorization_scopes", []
        ),
        "authenticated_probe_headers_sha256": headers_sha256,
        "authenticated_probe_results": probes,
    }

    lambda_manifest = copy.deepcopy(
        load_object(lambda_template_path, "Lambda manifest template")
    )
    functions = lambda_manifest.get("lambda_functions")
    if not isinstance(functions, list) or len(functions) != 1:
        fail("Lambda manifest template must declare exactly one function")
    functions[0]["function_name"] = function_name
    functions[0]["alias"] = qualifier
    functions[0]["target_account"] = account
    functions[0]["target_region"] = region
    lambda_manifest["kit_files"] = {}
    lambda_manifest["target_confirmation"] = copy.deepcopy(target_confirmation)

    if output.exists() or output.is_symlink():
        fail(f"output already exists: {output}")
    output.mkdir(parents=True)
    backend = backend_manifest(account, region, inputs)
    backend["target_confirmation"] = copy.deepcopy(target_confirmation)
    write_json(output / "114-tenant-stats-table" / "manifest.json", backend)
    write_json(output / "114-api-lambda" / "manifest.json", lambda_manifest)
    route = route_manifest(
        account,
        region,
        api_id,
        stage,
        function_name,
        qualifier,
        reference,
        headers_sha256,
        configured_url,
    )
    route["target_confirmation"] = copy.deepcopy(target_confirmation)
    write_json(
        output / "114-tenants-stats-route" / "manifest.json",
        route,
    )
    print(
        "MATERIALIZED_PATCH_SET "
        f"account={account} region={region} api={api_id} stage={stage} "
        f"lambda={function_name}:{qualifier}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("environment", type=Path)
    parser.add_argument("config_facts", type=Path)
    parser.add_argument("lambda_template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--stage")
    arguments = parser.parse_args()
    materialize(
        arguments.environment,
        arguments.config_facts,
        arguments.lambda_template,
        arguments.output,
        arguments.stage,
    )


if __name__ == "__main__":
    main()
