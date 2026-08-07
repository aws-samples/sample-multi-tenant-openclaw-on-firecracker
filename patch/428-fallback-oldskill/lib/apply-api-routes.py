#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Plan/apply/verify/finalize/rollback exact API Gateway REST route resources."""

# EXCEEDS: file 1215 lines > 800-line hard cap. Split into plan/apply/rollback/lock
# submodules is a separate refactor MR (do not mix into the #375 functional fix).

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

CONFIRM = "APPLY"
DEPLOYMENT_DESCRIPTION = "claw patch exact API routes"


def deployment_description(run_id: str) -> str:
    # Stamp each apply's create-deployment with its own run id so crash-resume
    # can recover ONLY the deployment this run created. A fixed description
    # cannot distinguish our orphan from another host's or stage's deployment,
    # and a local lease does not serialize other machines or operators.
    return f"{DEPLOYMENT_DESCRIPTION} run={run_id}"


METHOD_KEYS = (
    "authorizationType",
    "apiKeyRequired",
    "authorizerId",
    "authorizationScopes",
    "requestParameters",
    "requestModels",
    "requestValidatorId",
    "operationName",
)
INTEGRATION_KEYS = (
    "type",
    "httpMethod",
    "uri",
    "credentials",
    "connectionType",
    "connectionId",
    "contentHandling",
    "passthroughBehavior",
    "requestParameters",
    "requestTemplates",
    "cacheNamespace",
    "cacheKeyParameters",
    "timeoutInMillis",
    "tlsConfig",
)
SAFE_PART = re.compile(r"^(?:[A-Za-z0-9._-]+|\{[A-Za-z0-9._-]+\+?\})$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
STACK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
LOGICAL_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,254}$")
HTTP_METHOD = re.compile(r"^[A-Z]+$")


def fail(message: str, code: int = 2) -> None:
    print(f"FATAL: {message}", file=sys.stderr)
    raise SystemExit(code)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compact(value: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value}


def valid_path(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or value.endswith("/")
        or "//" in value
    ):
        return False
    parts = value[1:].split("/")
    if not all(SAFE_PART.fullmatch(part) and part not in {".", ".."} for part in parts):
        return False
    return all(
        not part.endswith("+}") or index == len(parts) - 1
        for index, part in enumerate(parts)
    )


def parent_path(path: str) -> str:
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def path_part(path: str) -> str:
    return path.rsplit("/", 1)[1]


def validate_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        fail("route spec must be an object with version=1")
    target = value.get("target")
    if (
        not isinstance(target, dict)
        or set(target)
        != {
            "stack",
            "rest_api_logical_id",
            "stage",
            "template_method_logical_id",
        }
        or not isinstance(target.get("stack"), str)
        or not STACK_NAME.fullmatch(target["stack"])
        or not isinstance(target.get("rest_api_logical_id"), str)
        or not LOGICAL_ID.fullmatch(target["rest_api_logical_id"])
        or not isinstance(target.get("template_method_logical_id"), str)
        or not LOGICAL_ID.fullmatch(target["template_method_logical_id"])
        or not isinstance(target.get("stage"), str)
        or not SAFE_ID.fullmatch(target["stage"])
    ):
        fail(
            "route spec requires target.stack, target.rest_api_logical_id, "
            "target.template_method_logical_id, and target.stage"
        )
    template = value.get("template")
    routes = value.get("routes")
    if (
        not isinstance(template, dict)
        or not valid_path(template.get("path"))
        or not isinstance(template.get("method"), str)
        or not HTTP_METHOD.fullmatch(template["method"])
        or template["method"] == "OPTIONS"
        or not isinstance(routes, list)
        or not routes
    ):
        fail("route spec requires a valid template path/method and non-empty routes")

    seen_paths: set[str] = set()
    for route in routes:
        if not isinstance(route, dict) or not valid_path(route.get("path")):
            fail("each route requires a safe path")
        path = route["path"]
        if path in seen_paths:
            fail(
                f"route path {path!r} is declared more than once; group methods by path"
            )
        seen_paths.add(path)
        resource_change = route.get("resource_change")
        if resource_change not in {"A", "M", "D", "NONE"}:
            fail(f"{path}: resource_change must be A/M/D/NONE")
        source_path = route.get("source_path")
        if resource_change == "M":
            if (
                not valid_path(source_path)
                or source_path == path
                or parent_path(source_path) != parent_path(path)
            ):
                fail(f"{path}: resource M requires a distinct same-parent source_path")
        elif source_path is not None:
            fail(f"{path}: source_path is only valid for resource_change=M")

        methods = route.get("methods")
        if not isinstance(methods, list):
            fail(f"{path}: methods must be an array")
        seen_methods: set[str] = set()
        for method in methods:
            if (
                not isinstance(method, dict)
                or method.get("change") not in {"A", "M", "D"}
                or not isinstance(method.get("method"), str)
                or not HTTP_METHOD.fullmatch(method["method"])
                or method["method"] == "OPTIONS"
                or method["method"] in seen_methods
            ):
                fail(f"{path}: each method needs a unique non-OPTIONS method and A/M/D")
            seen_methods.add(method["method"])

        cors = route.get("cors")
        if not isinstance(cors, dict) or cors.get("change") not in {
            "A",
            "M",
            "D",
            "NONE",
        }:
            fail(f"{path}: cors.change must be A/M/D/NONE")
        if cors["change"] in {"A", "M"}:
            if (
                not isinstance(cors.get("allow_origin"), str)
                or not cors["allow_origin"]
                or not isinstance(cors.get("allow_headers"), list)
                or not cors["allow_headers"]
                or not all(
                    isinstance(item, str) and item for item in cors["allow_headers"]
                )
                or not isinstance(cors.get("allow_methods"), list)
                or not cors["allow_methods"]
                or not all(
                    isinstance(item, str) and HTTP_METHOD.fullmatch(item)
                    for item in cors["allow_methods"]
                )
                or "OPTIONS" not in cors["allow_methods"]
                or len(set(cors["allow_headers"])) != len(cors["allow_headers"])
                or len(set(cors["allow_methods"])) != len(cors["allow_methods"])
            ):
                fail(f"{path}: CORS A/M requires explicit origin, headers, and methods")
        elif set(cors) != {"change"}:
            fail(f"{path}: CORS D/NONE must not carry an unused configuration")

        if resource_change == "A" and any(item["change"] != "A" for item in methods):
            fail(f"{path}: a new resource may only add methods")
        if resource_change == "D":
            if any(item["change"] != "D" for item in methods) or cors["change"] not in {
                "D",
                "NONE",
            }:
                fail(
                    f"{path}: deleting a resource requires deleting every declared method/CORS"
                )
    return value


def verify_target_binding(
    target: dict[str, str], api: str, stage: str, region: str
) -> None:
    if target["stage"] != stage:
        fail(
            f"spec target stage {target['stage']!r} does not match "
            f"command stage {stage!r}"
        )
    result = subprocess.run(
        [
            "aws",
            "cloudformation",
            "describe-stack-resource",
            "--stack-name",
            target["stack"],
            "--logical-resource-id",
            target["rest_api_logical_id"],
            "--region",
            region,
            "--output",
            "json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail(
            "cannot bind command API to CloudFormation target: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        detail = json.loads(result.stdout).get("StackResourceDetail", {})
    except json.JSONDecodeError as exc:
        fail(f"CloudFormation target lookup returned invalid JSON: {exc}")
    if (
        detail.get("LogicalResourceId") != target["rest_api_logical_id"]
        or detail.get("ResourceType") != "AWS::ApiGateway::RestApi"
    ):
        fail("CloudFormation target is not the declared API Gateway RestApi")
    physical_id = detail.get("PhysicalResourceId")
    if physical_id != api:
        fail(
            f"CloudFormation target {target['stack']}/"
            f"{target['rest_api_logical_id']} resolves to API {physical_id!r}, "
            f"not command API {api!r}"
        )


class Gateway:
    def __init__(self, api: str, stage: str, region: str):
        self.api = api
        self.stage = stage
        self.region = region

    def call(self, action: str, *args: str, optional: bool = False) -> Any:
        result = subprocess.run(
            [
                "aws",
                "apigateway",
                action,
                "--rest-api-id",
                self.api,
                *args,
                "--region",
                self.region,
                "--output",
                "json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            if optional and "NotFoundException" in result.stderr:
                return None
            fail(
                f"aws apigateway {action} failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            fail(f"aws apigateway {action} returned invalid JSON: {exc}")

    def resources(self) -> list[dict[str, Any]]:
        result = self.call("get-resources", "--limit", "500")
        items = result.get("items")
        if not isinstance(items, list):
            fail("get-resources returned no items array")
        return items

    def resource(self, path: str) -> dict[str, Any] | None:
        matches = [item for item in self.resources() if item.get("path") == path]
        if len(matches) > 1:
            fail(f"API contains duplicate resource path {path}")
        return matches[0] if matches else None

    def deployments(self) -> list[dict[str, Any]]:
        result = self.call("get-deployments", "--limit", "500")
        items = result.get("items")
        if not isinstance(items, list):
            fail("get-deployments returned no items array")
        return items

    def method_bundle(self, resource_id: str, method: str) -> dict[str, Any] | None:
        method_value = self.call(
            "get-method",
            "--resource-id",
            resource_id,
            "--http-method",
            method,
            optional=True,
        )
        if method_value is None:
            return None
        integration = self.call(
            "get-integration",
            "--resource-id",
            resource_id,
            "--http-method",
            method,
            optional=True,
        )
        return {"method": method_value, "integration": integration}

    def delete_method(
        self, resource_id: str, method: str, optional: bool = False
    ) -> None:
        if optional and self.method_bundle(resource_id, method) is None:
            return
        self.call(
            "delete-method",
            "--resource-id",
            resource_id,
            "--http-method",
            method,
        )

    def put_bundle(
        self,
        resource_id: str,
        method: str,
        bundle: dict[str, Any],
        replace: bool,
        record_mutation: Any | None = None,
    ) -> None:
        def mutate(label: str, expected: dict[str, Any] | None, action: Any) -> None:
            expected_copy = json.loads(json.dumps(expected))
            if record_mutation is None:
                action()
            else:
                record_mutation(label, expected_copy, action)

        if replace:
            mutate(
                "delete-method",
                None,
                lambda: self.delete_method(resource_id, method, optional=True),
            )
        method_value = bundle["method"]
        method_input = {
            "restApiId": self.api,
            "resourceId": resource_id,
            "httpMethod": method,
            **compact(method_value, METHOD_KEYS),
        }
        expected_method = compact(method_value, METHOD_KEYS)
        expected: dict[str, Any] = {
            "method": expected_method,
            "integration": None,
        }
        mutate(
            "put-method",
            expected,
            lambda: self.call(
                "put-method", "--cli-input-json", canonical(method_input)
            ),
        )
        for status, response in sorted(method_value.get("methodResponses", {}).items()):
            response_input = {
                "restApiId": self.api,
                "resourceId": resource_id,
                "httpMethod": method,
                "statusCode": status,
                **compact(response, ("responseParameters", "responseModels")),
            }
            expected_method.setdefault("methodResponses", {})[status] = {
                "statusCode": status,
                **compact(response, ("responseParameters", "responseModels")),
            }
            mutate(
                f"put-method-response:{status}",
                expected,
                lambda response_input=response_input: self.call(
                    "put-method-response",
                    "--cli-input-json",
                    canonical(response_input),
                ),
            )

        integration = bundle["integration"]
        integration_input = {
            "restApiId": self.api,
            "resourceId": resource_id,
            "httpMethod": method,
            "type": integration["type"],
            **compact(integration, INTEGRATION_KEYS[2:]),
        }
        if "httpMethod" in integration:
            integration_input["integrationHttpMethod"] = integration["httpMethod"]
        expected["integration"] = compact(integration, INTEGRATION_KEYS)
        mutate(
            "put-integration",
            expected,
            lambda: self.call(
                "put-integration", "--cli-input-json", canonical(integration_input)
            ),
        )
        for status, response in sorted(
            integration.get("integrationResponses", {}).items()
        ):
            response_input = {
                "restApiId": self.api,
                "resourceId": resource_id,
                "httpMethod": method,
                "statusCode": status,
                **compact(
                    response,
                    (
                        "selectionPattern",
                        "responseParameters",
                        "responseTemplates",
                        "contentHandling",
                    ),
                ),
            }
            expected["integration"].setdefault("integrationResponses", {})[status] = {
                "statusCode": status,
                **compact(
                    response,
                    (
                        "selectionPattern",
                        "responseParameters",
                        "responseTemplates",
                        "contentHandling",
                    ),
                ),
            }
            mutate(
                f"put-integration-response:{status}",
                expected,
                lambda response_input=response_input: self.call(
                    "put-integration-response",
                    "--cli-input-json",
                    canonical(response_input),
                ),
            )

    def ensure_path(
        self,
        path: str,
        state: dict[str, Any],
        persist: Any,
        *,
        record_created: bool = True,
    ) -> dict[str, Any]:
        root = self.resource("/")
        if root is None:
            fail("API root resource is absent")
        parent_id = root["id"]
        current = ""
        for part in path[1:].split("/"):
            current += f"/{part}"
            resource = self.resource(current)
            if resource is None:
                # Journal the intent to create BEFORE the AWS call. A crash
                # between create-resource and persist would otherwise leave an
                # orphan resource that rollback never sees. The id is filled in
                # once AWS returns; rollback reconciles a null id by path.
                record = None
                if record_created:
                    record = {"path": current, "id": None}
                    state["created_resources"].append(record)
                    persist()
                resource = self.call(
                    "create-resource",
                    "--parent-id",
                    parent_id,
                    "--path-part",
                    part,
                )
                if not resource.get("id"):
                    fail(f"create-resource returned no id for {current}")
                if record is not None:
                    record["id"] = resource["id"]
                    persist()
            parent_id = resource["id"]
        result = self.resource(path)
        if result is None:
            fail(f"failed to create resource path {path}")
        return result

    def stage_deployment(self) -> str:
        value = self.call("get-stage", "--stage-name", self.stage).get("deploymentId")
        if not isinstance(value, str) or not value:
            fail(f"stage {self.stage!r} has no deployment")
        return value


def normalize_bundle(bundle: dict[str, Any] | None) -> Any:
    if bundle is None:
        return None
    method = compact(bundle["method"], METHOD_KEYS + ("methodResponses",))
    integration_value = bundle.get("integration")
    integration = (
        compact(
            integration_value,
            INTEGRATION_KEYS + ("integrationResponses",),
        )
        if isinstance(integration_value, dict)
        else None
    )
    if integration is not None and integration.get("type") == "MOCK":
        # API Gateway materializes these service-owned defaults on every MOCK.
        for key in ("cacheNamespace", "cacheKeyParameters", "timeoutInMillis"):
            integration.pop(key, None)
    return {"method": method, "integration": integration}


def desired_method_bundle(template: dict[str, Any], method: str) -> dict[str, Any]:
    value = json.loads(json.dumps(template))
    value["method"].pop("methodResponses", None)
    value["integration"].pop("integrationResponses", None)
    return value


def desired_cors_bundle(cors: dict[str, Any]) -> dict[str, Any]:
    status = "204"
    allow_headers = ",".join(cors["allow_headers"])
    allow_methods = ",".join(cors["allow_methods"])
    return {
        "method": {
            "authorizationType": "NONE",
            "apiKeyRequired": False,
            "methodResponses": {
                status: {
                    "statusCode": status,
                    "responseParameters": {
                        "method.response.header.Access-Control-Allow-Headers": True,
                        "method.response.header.Access-Control-Allow-Methods": True,
                        "method.response.header.Access-Control-Allow-Origin": True,
                    },
                }
            },
        },
        "integration": {
            "type": "MOCK",
            "requestTemplates": {"application/json": '{"statusCode": 204}'},
            "passthroughBehavior": "WHEN_NO_MATCH",
            "integrationResponses": {
                status: {
                    "statusCode": status,
                    "responseParameters": {
                        "method.response.header.Access-Control-Allow-Headers": f"'{allow_headers}'",
                        "method.response.header.Access-Control-Allow-Methods": f"'{allow_methods}'",
                        "method.response.header.Access-Control-Allow-Origin": (
                            f"'{cors['allow_origin']}'"
                        ),
                    },
                }
            },
        },
    }


def gate(message: str) -> None:
    print(f"\n>>> {message}")
    answer = input(f"    type '{CONFIRM}' to proceed (else abort): ")
    if answer != CONFIRM:
        print("aborted")
        raise SystemExit(3)


def state_path(api: str, stage: str) -> Path:
    root = Path(
        os.environ.get("OC_APPLY_API_STATE_DIR", "~/.oc-apply-api-routes")
    ).expanduser()
    return root / f"{api}.{stage}.json"


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def lease_path(state_file: Path) -> Path:
    return state_file.with_name(state_file.name + ".lock")


def acquire_lease(state_file: Path) -> int:
    """Atomically claim an exclusive api/stage lease, failing closed if held.

    A plain exists()-then-mutate window lets two concurrent applies both pass
    the state check, both clear the interactive gate, then race their AWS
    mutations and clobber each other's state file. O_CREAT|O_EXCL makes the
    claim atomic so exactly one apply/rollback owns the api/stage at a time.
    """
    # ponytail: file lease, fail-closed. A SIGKILL leaks the lock and blocks
    # further apply/rollback until it is removed by hand -- deliberately the
    # safe direction; add PID-liveness takeover only if crash frequency hurts.
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.parent.chmod(0o700)
    try:
        return os.open(
            lease_path(state_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        fail(f"another apply/rollback holds the lease: {lease_path(state_file)}")


def release_lease(state_file: Path, fd: int) -> None:
    os.close(fd)
    lease_path(state_file).unlink(missing_ok=True)


def journaled_mutation(
    journal: dict[str, Any],
    persist: Any,
    label: str,
    expected_after: dict[str, Any] | None,
    action: Any,
) -> None:
    before = json.loads(json.dumps(journal["current"]))
    after = normalize_bundle(expected_after)
    step = {
        "label": label,
        "before": before,
        "after": json.loads(json.dumps(after)),
        "status": "started",
    }
    journal["status"] = "started"
    journal["steps"].append(step)
    persist()
    action()
    step["status"] = "completed"
    journal["current"] = json.loads(json.dumps(after))
    persist()


def load_state(
    path: Path, api: str, stage: str, region: str, spec_sha: str
) -> dict[str, Any]:
    if not path.is_file():
        fail(f"apply state is missing: {path}")
    value = json.loads(path.read_text())
    expected = {
        "api": api,
        "stage": stage,
        "region": region,
        "spec_sha256": spec_sha,
    }
    if any(value.get(key) != item for key, item in expected.items()):
        fail("apply state belongs to a different API, stage, region, or route spec")
    template = value.get("template")
    if (
        not isinstance(template, dict)
        or not isinstance(template.get("method"), dict)
        or not isinstance(template.get("integration"), dict)
    ):
        fail("apply state has no valid apply-time template snapshot")
    return value


def route_lookup_path(route: dict[str, Any]) -> str:
    return route.get("source_path", route["path"])


def template_bundle(gateway: Gateway, spec: dict[str, Any]) -> dict[str, Any]:
    template_resource = gateway.resource(spec["template"]["path"])
    if template_resource is None:
        fail(f"template route {spec['template']['path']!r} is absent")
    template = gateway.method_bundle(
        template_resource["id"], spec["template"]["method"]
    )
    if (
        template is None
        or template["integration"].get("type") != "AWS_PROXY"
        or not template["integration"].get("httpMethod")
        or not template["integration"].get("uri")
    ):
        fail("template method must have a complete AWS_PROXY integration")
    return template


def preflight(
    gateway: Gateway, spec: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    template = template_bundle(gateway, spec)

    snapshots = []
    all_resources = gateway.resources()
    for route in spec["routes"]:
        path = route["path"]
        lookup = route_lookup_path(route)
        resource = next(
            (item for item in all_resources if item.get("path") == lookup), None
        )
        destination = next(
            (item for item in all_resources if item.get("path") == path), None
        )
        change = route["resource_change"]
        if change == "A" and destination is not None:
            fail(f"{path}: resource A requires an absent target")
        if change in {"M", "D", "NONE"} and resource is None:
            fail(f"{lookup}: resource {change} requires an existing target")
        if change == "M" and destination is not None:
            fail(f"{path}: resource M destination already exists")
        resource_id = resource.get("id") if resource else None

        method_snapshots = {}
        for method in route["methods"]:
            before = (
                gateway.method_bundle(resource_id, method["method"])
                if resource_id
                else None
            )
            if method["change"] == "A" and before is not None:
                fail(f"{lookup}: method {method['method']} A already exists")
            if method["change"] in {"M", "D"} and before is None:
                fail(
                    f"{lookup}: method {method['method']} {method['change']} is absent"
                )
            method_snapshots[method["method"]] = before

        cors_before = (
            gateway.method_bundle(resource_id, "OPTIONS") if resource_id else None
        )
        cors_change = route["cors"]["change"]
        if cors_change == "A" and cors_before is not None:
            fail(f"{lookup}: CORS A already exists")
        if cors_change in {"M", "D"} and cors_before is None:
            fail(f"{lookup}: CORS {cors_change} is absent")

        if change == "D":
            child_prefix = f"{lookup}/"
            children = [
                item.get("path")
                for item in all_resources
                if isinstance(item.get("path"), str)
                and item["path"].startswith(child_prefix)
            ]
            if children:
                fail(f"{lookup}: resource D has child resources: {children}")
            declared = {item["method"] for item in route["methods"]}
            if cors_change == "D":
                declared.add("OPTIONS")
            actual = set(resource.get("resourceMethods", {}))
            if actual != declared:
                fail(
                    f"{lookup}: resource D methods {sorted(actual)} do not exactly "
                    f"match declared deletes {sorted(declared)}"
                )
        snapshots.append(
            {
                "route": route,
                "lookup_path": lookup,
                "resource_id": resource_id,
                "parent_id": resource.get("parentId") if resource else None,
                "methods_before": method_snapshots,
                "cors_before": cors_before,
                "journal": {
                    "resource": "pending",
                    "methods": {
                        method["method"]: {
                            "status": "pending",
                            "current": normalize_bundle(
                                method_snapshots[method["method"]]
                            ),
                            "steps": [],
                        }
                        for method in route["methods"]
                    },
                    "cors": {
                        "status": "pending",
                        "current": normalize_bundle(cors_before),
                        "steps": [],
                    },
                },
            }
        )
    return template, snapshots


def print_plan(
    gateway: Gateway,
    spec: dict[str, Any],
    template: dict[str, Any],
    snapshots: list[dict[str, Any]],
) -> None:
    print(f"API={gateway.api} stage={gateway.stage} region={gateway.region}")
    print(f"template: {spec['template']['method']} {spec['template']['path']}")
    print(canonical(normalize_bundle(template)))
    for snapshot in snapshots:
        route = snapshot["route"]
        methods = (
            ",".join(f"{item['change']}:{item['method']}" for item in route["methods"])
            or "-"
        )
        print(
            f"  resource={route['resource_change']} {snapshot['lookup_path']}"
            f"->{route['path']} methods={methods} cors={route['cors']['change']}"
        )


def apply_routes(
    gateway: Gateway,
    spec: dict[str, Any],
    template: dict[str, Any],
    state: dict[str, Any],
    state_file: Path,
) -> None:
    def persist() -> None:
        write_state(state_file, state)

    def recorder(journal: dict[str, Any]) -> Any:
        return lambda label, expected, action: journaled_mutation(
            journal,
            persist,
            label,
            expected,
            action,
        )

    for snapshot in state["routes"]:
        route = snapshot["route"]
        change = route["resource_change"]
        if change != "NONE":
            snapshot["journal"]["resource"] = "started"
            persist()
        if change == "A":
            resource = gateway.ensure_path(route["path"], state, persist)
        else:
            resource = gateway.resource(snapshot["lookup_path"])
            if resource is None:
                fail(f"{snapshot['lookup_path']}: resource disappeared before apply")
        if change == "M":
            resource = gateway.call(
                "update-resource",
                "--resource-id",
                resource["id"],
                "--patch-operations",
                f"op=replace,path=/pathPart,value={path_part(route['path'])}",
            )
        if change in {"A", "M"}:
            snapshot["journal"]["resource"] = "completed"
        snapshot["applied_resource_id"] = resource["id"]
        persist()

        for method in route["methods"]:
            method_journal = snapshot["journal"]["methods"][method["method"]]
            method_journal["status"] = "started"
            persist()
            if method["change"] in {"A", "M"}:
                gateway.put_bundle(
                    resource["id"],
                    method["method"],
                    desired_method_bundle(template, method["method"]),
                    replace=method["change"] == "M",
                    record_mutation=recorder(method_journal),
                )
            else:
                journaled_mutation(
                    method_journal,
                    persist,
                    "delete-method",
                    None,
                    lambda: gateway.delete_method(resource["id"], method["method"]),
                )
            method_journal["status"] = "completed"
            persist()

        cors_change = route["cors"]["change"]
        cors_journal = snapshot["journal"]["cors"]
        if cors_change in {"A", "M"}:
            cors_journal["status"] = "started"
            persist()
            gateway.put_bundle(
                resource["id"],
                "OPTIONS",
                desired_cors_bundle(route["cors"]),
                replace=cors_change == "M",
                record_mutation=recorder(cors_journal),
            )
            cors_journal["status"] = "completed"
        elif cors_change == "D":
            cors_journal["status"] = "started"
            persist()
            journaled_mutation(
                cors_journal,
                persist,
                "delete-method",
                None,
                lambda: gateway.delete_method(resource["id"], "OPTIONS"),
            )
            cors_journal["status"] = "completed"
        persist()
        if change == "D":
            gateway.call("delete-resource", "--resource-id", resource["id"])
            snapshot["journal"]["resource"] = "completed"
            persist()


def verify_route_resources(
    gateway: Gateway,
    spec: dict[str, Any],
    template: dict[str, Any],
) -> None:
    for route in spec["routes"]:
        resource = gateway.resource(route["path"])
        if route["resource_change"] == "D":
            if resource is not None:
                fail(f"{route['path']}: deleted resource still exists")
            print(f"PASS: deleted resource {route['path']}")
            continue
        if resource is None:
            fail(f"{route['path']}: expected resource is absent")
        if route["resource_change"] == "M" and gateway.resource(route["source_path"]):
            fail(f"{route['source_path']}: renamed source still exists")
        if route["resource_change"] == "A":
            expected_methods = {
                method["method"]
                for method in route["methods"]
                if method["change"] != "D"
            }
            if route["cors"]["change"] in {"A", "M"}:
                expected_methods.add("OPTIONS")
            actual_methods = set(resource.get("resourceMethods", {}))
            if actual_methods != expected_methods:
                fail(
                    f"{route['path']}: new resource methods drifted "
                    f"from {sorted(expected_methods)} to {sorted(actual_methods)}"
                )
        for method in route["methods"]:
            actual = gateway.method_bundle(resource["id"], method["method"])
            if method["change"] == "D":
                if actual is not None:
                    fail(
                        f"{route['path']}: deleted method {method['method']} still exists"
                    )
                continue
            expected = desired_method_bundle(template, method["method"])
            if normalize_bundle(actual) != normalize_bundle(expected):
                fail(f"{method['method']} {route['path']} differs from template")
            print(f"PASS: {method['method']} {route['path']}")
        cors_change = route["cors"]["change"]
        cors_actual = gateway.method_bundle(resource["id"], "OPTIONS")
        if cors_change == "D":
            if cors_actual is not None:
                fail(f"OPTIONS {route['path']} still exists")
        elif cors_change in {"A", "M"}:
            expected_cors = desired_cors_bundle(route["cors"])
            if normalize_bundle(cors_actual) != normalize_bundle(expected_cors):
                fail(f"OPTIONS {route['path']} CORS config differs from spec")
            print(f"PASS: OPTIONS {route['path']} exact CORS headers")


def verify_routes(
    gateway: Gateway,
    spec: dict[str, Any],
    template: dict[str, Any],
    state: dict[str, Any],
) -> None:
    verify_route_resources(gateway, spec, template)
    expected_deployment = state.get("new_deployment")
    current_deployment = gateway.stage_deployment()
    if not expected_deployment or current_deployment != expected_deployment:
        fail(f"stage deployment={current_deployment} expected={expected_deployment}")
    print(f"PASS: stage {gateway.stage!r} uses deployment {current_deployment}")


def finalize_routes(gateway: Gateway, state: dict[str, Any], state_file: Path) -> None:
    if state.get("rolled_back"):
        fail("cannot finalize a rolled-back route apply")
    deployment = state.get("new_deployment")
    previous = state.get("previous_deployment")
    if (
        not isinstance(deployment, str)
        or not deployment
        or not isinstance(previous, str)
        or not previous
        or deployment == previous
    ):
        fail("state has no distinct previous/new deployment to finalize")
    verify_routes(
        gateway,
        {"routes": [item["route"] for item in state["routes"]]},
        state["template"],
        state,
    )

    state["finalize_pending"] = True
    write_state(state_file, state)
    deployment_ids = {
        item.get("id")
        for item in gateway.deployments()
        if isinstance(item, dict)
    }
    if previous in deployment_ids:
        gateway.call(
            "delete-deployment",
            "--deployment-id",
            previous,
            optional=True,
        )
    remaining = {
        item.get("id")
        for item in gateway.deployments()
        if isinstance(item, dict)
    }
    if previous in remaining:
        fail(f"previous deployment {previous!r} still exists after finalize")
    if deployment not in remaining:
        fail(f"active deployment {deployment!r} disappeared during finalize")

    state["finalize_pending"] = False
    state["finalized"] = True
    write_state(state_file, state)
    state_file.unlink()
    print(
        f"PASS: finalized stage {gateway.stage!r}; "
        f"deleted previous deployment {previous}"
    )


def rollback_routes(gateway: Gateway, state: dict[str, Any], state_file: Path) -> None:
    def persist() -> None:
        write_state(state_file, state)

    deployment = state.get("new_deployment")
    previous = state["previous_deployment"]
    current = gateway.stage_deployment() if deployment else previous
    if deployment:
        if current not in {deployment, previous}:
            fail(f"stage drifted to {current}; refusing to overwrite it")
    elif state.get("deployment_pending"):
        # Resume after a crash between create-deployment and the new_deployment
        # back-fill. update-stage runs only after that back-fill, so the stage
        # still uses its prior deployment; any deployment carrying THIS run's
        # description that the stage is NOT using is the orphan we created.
        # Match on the per-run id, not the fixed description: a local lease
        # cannot serialize other machines or stages, so a fixed description
        # could match a deployment another publish created. If the state file
        # predates run ids we cannot prove ownership, so fail closed.
        run_id = state.get("run_id")
        if not run_id:
            fail(
                "deployment_pending state lacks a run_id; cannot prove which "
                "deployment this interrupted run created. Confirm the orphan "
                "deployment, delete it manually, then re-run."
            )
        stage_current = gateway.stage_deployment()
        for orphan in gateway.deployments():
            if (
                orphan.get("description") == deployment_description(run_id)
                and orphan.get("id") != stage_current
            ):
                gateway.call(
                    "delete-deployment",
                    "--deployment-id",
                    orphan["id"],
                    optional=True,
                )
        state["deployment_pending"] = False
        persist()
    if state.get("routes_applied") and not state.get("rollback_started"):
        verify_route_resources(
            gateway,
            {"routes": [item["route"] for item in state["routes"]]},
            state["template"],
        )
    state["rollback_started"] = True
    persist()
    if deployment and current == deployment:
        gateway.call(
            "update-stage",
            "--stage-name",
            gateway.stage,
            "--patch-operations",
            f"op=replace,path=/deploymentId,value={previous}",
        )

    def restore_bundle(
        resource_id: str,
        method: str,
        before: dict[str, Any] | None,
        journal: dict[str, Any],
        label: str,
    ) -> None:
        if journal["status"] == "pending":
            return
        current_bundle = gateway.method_bundle(resource_id, method)
        current_normalized = normalize_bundle(current_bundle)
        before_normalized = normalize_bundle(before)
        if current_normalized == before_normalized:
            return
        steps = journal.get("steps", [])
        if not steps or canonical(current_normalized) not in {
            canonical(steps[-1]["before"]),
            canonical(steps[-1]["after"]),
        }:
            fail(f"{label}: drifted after partial apply; refusing to overwrite it")
        journal["current"] = json.loads(json.dumps(current_normalized))
        persist()

        def record(
            mutation_label: str,
            expected: dict[str, Any] | None,
            action: Any,
        ) -> None:
            journaled_mutation(
                journal,
                persist,
                f"rollback:{mutation_label}",
                expected,
                action,
            )

        if before is None:
            journaled_mutation(
                journal,
                persist,
                "rollback:delete-method",
                None,
                lambda: gateway.delete_method(resource_id, method, optional=True),
            )
        else:
            gateway.put_bundle(
                resource_id,
                method,
                before,
                replace=True,
                record_mutation=record,
            )
        journal["status"] = "restored"
        persist()

    for snapshot in reversed(state["routes"]):
        route = snapshot["route"]
        change = route["resource_change"]
        if change == "A":
            resource = gateway.resource(route["path"])
            if resource is None:
                continue
            applied_resource_id = snapshot.get("applied_resource_id")
            if (
                applied_resource_id is not None
                and resource.get("id") != applied_resource_id
            ):
                fail(
                    f"{route['path']}: resource identity drifted after partial "
                    "apply; refusing to overwrite it"
                )
        if change == "D":
            resource = gateway.resource(snapshot["lookup_path"])
            recreate_intended = "rollback_resource_id" in snapshot
            if resource is None:
                parent = gateway.resource(parent_path(snapshot["lookup_path"]))
                if parent is None or parent.get("id") != snapshot["parent_id"]:
                    fail(
                        f"{snapshot['lookup_path']}: parent resource drifted; "
                        "refusing to recreate the deleted resource"
                    )
                # Journal the intent to recreate BEFORE the AWS call, mirroring
                # ensure_path. A crash between create-resource and the id
                # back-fill would otherwise leave a resource whose id we never
                # recorded, and the resume branch below would treat our own
                # resource as external drift and deadlock the rollback.
                snapshot["rollback_resource_id"] = None
                persist()
                resource = gateway.call(
                    "create-resource",
                    "--parent-id",
                    parent["id"],
                    "--path-part",
                    path_part(snapshot["lookup_path"]),
                )
                snapshot["rollback_resource_id"] = resource.get("id")
                persist()
            elif resource.get("id") == snapshot.get("rollback_resource_id"):
                pass
            elif recreate_intended and snapshot.get("rollback_resource_id") is None:
                # Resume after a crash between create-resource and the id
                # back-fill. A resource exists at this path but its id was never
                # recorded, so we cannot prove it is the one WE created rather
                # than a concurrent deployment's. Adopting it would let rollback
                # write the restored methods onto a foreign resource, so fail
                # closed and require a human to confirm ownership (record the id
                # in rollback_resource_id) or delete the orphan, then re-run.
                fail(
                    f"{snapshot['lookup_path']}: an interrupted rollback recreate "
                    "left a resource whose id was never recorded; refusing to "
                    "adopt it by path because ownership cannot be proven. Confirm "
                    "the resource, set rollback_resource_id, or delete the orphan, "
                    "then re-run."
                )
            elif (
                snapshot["journal"]["resource"] == "completed"
                or resource.get("id") != snapshot["resource_id"]
            ):
                fail(
                    f"{snapshot['lookup_path']}: resource reappeared after delete; "
                    "refusing to overwrite it"
                )
        elif change == "M":
            source = gateway.resource(route["source_path"])
            destination = gateway.resource(route["path"])
            if (
                destination is not None
                and destination.get("id") == snapshot["resource_id"]
                and source is None
            ):
                resource = gateway.call(
                    "update-resource",
                    "--resource-id",
                    destination["id"],
                    "--patch-operations",
                    "op=replace,path=/pathPart,"
                    f"value={path_part(route['source_path'])}",
                )
            elif (
                source is not None
                and source.get("id") == snapshot["resource_id"]
                and destination is None
            ):
                # Apply failed before this rename. Restore snapshots in place.
                resource = source
            else:
                fail(
                    f"{route['source_path']}->{route['path']}: rollback cannot "
                    "disambiguate source/destination drift"
                )
        else:
            resource = gateway.resource(snapshot["lookup_path"])
            if resource is None:
                fail(f"{snapshot['lookup_path']}: resource absent during rollback")
        for method_spec in route["methods"]:
            method = method_spec["method"]
            restore_bundle(
                resource["id"],
                method,
                snapshot["methods_before"][method],
                snapshot["journal"]["methods"][method],
                f"{method} {route['path']}",
            )
        restore_bundle(
            resource["id"],
            "OPTIONS",
            snapshot["cors_before"],
            snapshot["journal"]["cors"],
            f"OPTIONS {route['path']}",
        )

    for created in reversed(state["created_resources"]):
        resource = gateway.resource(created["path"])
        if resource is None:
            continue
        live_id = resource.get("id")
        if created["id"] is None:
            # The crash landed between journaling the create intent and filling
            # in the AWS id, so a resource exists at this path but we never
            # recorded its id. We cannot prove it is the one WE created rather
            # than a concurrent deployment's, and deleting it would destroy a
            # foreign resource. Mirror the recreate resume branch above: fail
            # closed and require a human to confirm ownership (record the id in
            # created_resources) or delete the orphan, then re-run.
            fail(
                f"{created['path']}: an interrupted apply left a created "
                "resource whose id was never recorded; refusing to delete it "
                "by path because ownership cannot be proven. Confirm the "
                "resource, set its id in created_resources, or delete the "
                "orphan, then re-run."
            )
        if live_id != created["id"]:
            continue
        children = [
            item.get("path")
            for item in gateway.resources()
            if item.get("parentId") == live_id
        ]
        if resource.get("resourceMethods") or children:
            fail(
                f"{created['path']}: created resource gained undeclared "
                f"methods or children {children}; "
                "refusing to delete it"
            )
        gateway.call("delete-resource", "--resource-id", live_id, optional=True)
    if deployment:
        gateway.call("delete-deployment", "--deployment-id", deployment, optional=True)
    state["rolled_back"] = True
    persist()


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "validate-spec":
        spec_path = Path(sys.argv[2])
        if not spec_path.is_file():
            fail(f"spec not found: {spec_path}")
        validate_spec(json.loads(spec_path.read_bytes()))
        print(f"PASS: valid API route spec {spec_path}")
        return
    if len(sys.argv) != 6:
        fail(
            "usage: apply-api-routes.sh validate-spec <spec.json>\n"
            "       apply-api-routes.sh plan|apply|verify|finalize|rollback "
            "<spec.json> <rest-api-id> <stage> <region>"
        )
    command, spec_name, api, stage, region = sys.argv[1:]
    if command not in {"plan", "apply", "verify", "finalize", "rollback"}:
        fail(f"unknown command {command!r}")
    if not SAFE_ID.fullmatch(api) or not SAFE_ID.fullmatch(stage):
        fail("API id and stage may contain only letters, digits, _ and -")
    spec_path = Path(spec_name)
    if not spec_path.is_file():
        fail(f"spec not found: {spec_path}")
    spec_data = spec_path.read_bytes()
    spec = validate_spec(json.loads(spec_data))
    spec_sha = hashlib.sha256(spec_data).hexdigest()
    verify_target_binding(spec["target"], api, stage, region)
    gateway = Gateway(api, stage, region)
    state_file = state_path(api, stage)

    if command == "plan":
        template, snapshots = preflight(gateway, spec)
        print_plan(gateway, spec, template, snapshots)
        return
    if command == "apply":
        lease = acquire_lease(state_file)
        try:
            if state_file.exists():
                fail(f"state already exists at {state_file}")
            template, snapshots = preflight(gateway, spec)
            print_plan(gateway, spec, template, snapshots)
            gate(
                f"apply exact route lifecycle to API {api!r} and deploy stage {stage!r}"
            )
            template, snapshots = preflight(gateway, spec)
            state = {
                "api": api,
                "stage": stage,
                "region": region,
                "spec_sha256": spec_sha,
                "run_id": uuid.uuid4().hex,
                "previous_deployment": gateway.stage_deployment(),
                "new_deployment": None,
                "template": template,
                "created_resources": [],
                "routes": snapshots,
                "routes_applied": False,
                "rolled_back": False,
            }
            write_state(state_file, state)
            apply_routes(gateway, spec, template, state, state_file)
            state["routes_applied"] = True
            write_state(state_file, state)
            # Journal the create-deployment intent BEFORE the call: a crash
            # between the call and the id back-fill would otherwise leave an
            # orphan deployment that rollback cannot see (new_deployment stays
            # None). rollback reconciles the orphan by this flag + description.
            state["deployment_pending"] = True
            write_state(state_file, state)
            deployment = gateway.call(
                "create-deployment",
                "--description",
                deployment_description(state["run_id"]),
            ).get("id")
            if not isinstance(deployment, str) or not deployment:
                fail("create-deployment returned no id; run rollback")
            state["new_deployment"] = deployment
            state["deployment_pending"] = False
            write_state(state_file, state)
            # Re-read the live stage right before repointing it. The local lease
            # only serializes this tool, so a concurrent publish (another host,
            # a CDK run) could have moved the stage since preflight. If it no
            # longer points at the deployment we recorded, repointing would
            # silently overwrite that publish; abort so rollback restores the
            # deployment we captured instead of clobbering a foreign one.
            live_stage = gateway.stage_deployment()
            if live_stage != state["previous_deployment"]:
                fail(
                    f"stage {stage!r} drifted from {state['previous_deployment']!r} "
                    f"to {live_stage!r} since preflight; refusing to overwrite a "
                    "concurrent deployment. Run rollback, then re-run."
                )
            gateway.call(
                "update-stage",
                "--stage-name",
                stage,
                "--patch-operations",
                f"op=replace,path=/deploymentId,value={deployment}",
            )
            verify_routes(gateway, spec, template, state)
        finally:
            release_lease(state_file, lease)
        return

    if command == "verify":
        state = load_state(state_file, api, stage, region, spec_sha)
        verify_routes(gateway, spec, state["template"], state)
        return

    lease = acquire_lease(state_file)
    try:
        state = load_state(state_file, api, stage, region, spec_sha)
        if command == "finalize":
            gate(
                f"finalize API {api!r} stage {stage!r} and delete its "
                "previous deployment"
            )
            finalize_routes(gateway, state, state_file)
            return
        if state.get("finalize_pending"):
            fail(
                "finalize has started; rollback is no longer safe. "
                "Resume finalize instead."
            )
        if state.get("finalized"):
            fail("state is already finalized; rollback is no longer available")
        if state.get("rolled_back"):
            fail("state is already rolled back")
        gate(f"restore stage {stage!r} and every state-captured route resource")
        rollback_routes(gateway, state, state_file)
    finally:
        release_lease(state_file, lease)


if __name__ == "__main__":
    main()
