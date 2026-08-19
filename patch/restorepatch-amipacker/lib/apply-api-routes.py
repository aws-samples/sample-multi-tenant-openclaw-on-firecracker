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
import tempfile
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

    def deployed_operations(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Path -> METHOD -> the operation the stage's ACTIVE deployment actually serves.

        A Resource/Method exists on the REST API the moment it is created, but a stage serves the
        snapshot its deployment captured. So the resource tree alone cannot answer "is this route
        live" -- an operator can create every method and still serve 403/404 until a deployment
        happens, and a state file that finalize deleted cannot answer it either. `get-export` is
        stage-scoped, so it reads exactly what the active deployment serves.

        Exported WITH `extensions=integrations`, so the answer is not just the method NAMES: it
        carries the deployed `security` (api-key requirement), the integration type/uri, and the
        CORS response parameters. Comparing only names would let a newer deployment serve the right
        verbs with the wrong auth or integration and still pass.
        """
        with tempfile.TemporaryDirectory(prefix="oc-oas-") as tmp:
            out = Path(tmp) / "stage.oas.json"
            result = subprocess.run(
                [
                    "aws", "apigateway", "get-export",
                    "--rest-api-id", self.api,
                    "--stage-name", self.stage,
                    "--export-type", "oas30",
                    "--parameters", "extensions=integrations",
                    "--region", self.region,
                    str(out),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                fail(
                    "aws apigateway get-export failed (cannot prove what the active deployment "
                    f"serves): {result.stderr.strip() or result.stdout.strip()}"
                )
            try:
                document = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                fail(f"stage export is not readable JSON: {exc}")
        served: dict[str, dict[str, dict[str, Any]]] = {}
        for path_value, operations in (document.get("paths") or {}).items():
            if not isinstance(path_value, str) or not isinstance(operations, dict):
                continue
            served[path_value] = {
                method.upper(): operation
                for method, operation in operations.items()
                if isinstance(method, str) and isinstance(operation, dict)
            }
        return served

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
    if integration is not None:
        # cacheNamespace defaults to the resource's OWN id, for every integration type -- not
        # just MOCK. A method cloned from the template therefore never carries the template
        # resource's namespace, so comparing it reported drift on every already-present route
        # and the idempotent ALREADY path could never trigger on a real API.
        integration.pop("cacheNamespace", None)
    if integration is not None and integration.get("type") == "MOCK":
        # API Gateway materializes these service-owned defaults on every MOCK. requestTemplates
        # is CDK construct boilerplate ({ statusCode: 200 } vs {"statusCode": 204}) that never
        # reaches the client; the browser-visible Access-Control-Allow-* values live in
        # integrationResponses/methodResponses and are still compared.
        for key in ("cacheKeyParameters", "timeoutInMillis", "requestTemplates"):
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


def gate(message: str, assume_yes: bool = False) -> None:
    print(f"\n>>> {message}")
    if assume_yes:
        print("    gate approved non-interactively via --yes")
        return
    try:
        answer = input(f"    type '{CONFIRM}' to proceed (else abort): ")
    except EOFError:
        answer = ""
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
            # An environment that applied an earlier revision of this kit already carries that
            # revision's routes. Converge instead of failing the whole run, but only when this
            # route is in its exact declared end state AND already served; any drift still
            # falls through to the failure below rather than being silently overwritten.
            if route_already_satisfied(gateway, spec, route, template):
                print(f"ALREADY: {path} is present in its declared end state; skipping")
                continue
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


def _deployed_auth(operation: dict[str, Any]) -> bool:
    """Does the deployed operation require an API key? Mirrors ApiKeyRequired."""
    for requirement in operation.get("security") or []:
        if isinstance(requirement, dict) and "api_key" in requirement:
            return True
    return False


def _deployed_authorizers(operation: dict[str, Any]) -> list[str]:
    """Security scheme names the deployed operation requires beyond the plain API key.

    A stage export represents an authorizer as an extra security requirement (and an
    `x-amazon-apigateway-authorizer` scheme). Ignoring them would let a deployment that puts a
    Cognito/IAM/Lambda authorizer in front of these routes pass as equivalent to the template.
    """
    names: list[str] = []
    for requirement in operation.get("security") or []:
        if not isinstance(requirement, dict):
            continue
        names.extend(name for name in requirement if name != "api_key")
    auth = operation.get("x-amazon-apigateway-auth")
    if isinstance(auth, dict):
        # AWS documents `type: NONE` as a valid explicit value, so presence alone is not an
        # authorizer -- compare the type.
        if str(auth.get("type", "")).upper() not in {"", "NONE"}:
            names.append(f"x-amazon-apigateway-auth:{auth.get('type')}")
    elif auth:
        names.append("x-amazon-apigateway-auth")
    return sorted(set(names))


# `responses` is the integration response mapping; the CORS preflight comparison handles it
# explicitly (status codes and the Access-Control-Allow-* values), so it is excluded from the
# generic field diff rather than compared twice in a shape the two sides express differently.
# `responseTransferMode` is deliberately NOT excluded: both `get-integration` and the stage
# export report it, and BUFFERED vs STREAM is a materially different runtime behaviour.
_EXPORT_ONLY_INTEGRATION_FIELDS = frozenset({"responses"})
def _integration_view(integration: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize an integration for comparison, whichever side it came from.

    The export lower-cases `type` and `passthroughBehavior` (`aws_proxy`, `when_no_match`) where the
    REST API reports them upper-case, and upper-cases nothing else, so those enums are folded.
    Export-only annotations are dropped because neither side can agree on them.
    """
    view = {
        key: value
        for key, value in (integration or {}).items()
        if key not in _EXPORT_ONLY_INTEGRATION_FIELDS
    }
    if "type" in view:
        view["type"] = str(view["type"]).lower()
    if "passthroughBehavior" in view:
        view["passthroughBehavior"] = str(view["passthroughBehavior"]).lower()
    if "httpMethod" in view:
        view["httpMethod"] = str(view["httpMethod"]).upper()
    # cacheKeyParameters is semantically a set; the two sources may order it differently, so
    # ordering alone must not read as a difference.
    if isinstance(view.get("cacheKeyParameters"), list):
        view["cacheKeyParameters"] = sorted(view["cacheKeyParameters"])
    return view


def _deployed_integration(operation: dict[str, Any]) -> dict[str, Any]:
    return _integration_view(operation.get("x-amazon-apigateway-integration"))


def _desired_integration(bundle: dict[str, Any]) -> dict[str, Any]:
    return _integration_view((bundle or {}).get("integration"))


def _integration_differences(deployed: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    """Field names where the ACTIVE deployment's integration disagrees with the template.

    Compares every field BOTH sides carry -- so a whitelist cannot leave a field unchecked -- and
    additionally flags a sensitive field the deployment carries but the template does not. Fields
    only the template has are not compared, because a stage export legitimately omits optional
    settings and requiring them would false-fail every time.
    """
    # An operation the export reports with NO integration at all yields an empty view; comparing
    # only common fields would then find nothing and accept it. Treat presence mismatch first.
    if bool(deployed) != bool(desired):
        return [
            "integration absent from the active deployment"
            if desired
            else "integration present on the active deployment but not in the template"
        ]
    # A MOCK preflight's request template body is CDK construct boilerplate that never reaches
    # the client, so it is not part of the CORS contract (same ruling the kit validator applies).
    # Only drop it when BOTH sides are MOCK; on a proxy integration requestTemplates is real
    # configuration and stays compared.
    if (
        str(deployed.get("type") or "").upper() == "MOCK"
        and str(desired.get("type") or "").upper() == "MOCK"
    ):
        deployed = {k: v for k, v in deployed.items() if k != "requestTemplates"}
        desired = {k: v for k, v in desired.items() if k != "requestTemplates"}
    differing = [
        field
        for field in sorted(set(deployed) & set(desired))
        if deployed[field] != desired[field]
    ]
    # Any field the DEPLOYED integration carries that the template does not is extra behaviour the
    # template never asked for (a request-parameter mapping, credentials, a VPC link, ...). Flag all
    # of them, not just a sensitive subset: a whitelist is exactly what let deployed-only settings
    # through. Empty/false values are ignored because they carry no behaviour, and export-only
    # annotations were already dropped by _integration_view.
    differing.extend(
        f"{field} (present only on the deployed integration)"
        for field in sorted(set(deployed) - set(desired))
        if deployed.get(field)
    )
    return differing


def _cors_response_list(
    responses: dict[str, Any] | None, *, key_is_selector: bool
) -> list[str]:
    """One canonical entry per integration response: (selector, statusCode, ACAO headers).

    A LIST, not a dict keyed by status: keying by statusCode silently overwrites a duplicate status,
    so a preflight mapping the same status twice with different headers compared equal.

    The SELECTOR differs by source and must be read from the right place:
      * in the OpenAPI export, `x-amazon-apigateway-integration.responses` is keyed BY the selection
        pattern (`default`, or a regex) -- so the key is the selector;
      * in this kit's desired bundle, `integrationResponses` is keyed by status and an explicit
        `selectionPattern` is optional, defaulting to `default`.
    Reading `.selectionPattern` off the export (which never carries it there) discarded the selector
    entirely, so swapping `default` for a narrow regex compared equal.
    """
    entries: list[str] = []
    for key, response in (responses or {}).items():
        if not isinstance(response, dict):
            continue
        selector = key if key_is_selector else (response.get("selectionPattern") or "default")
        # Compare the WHOLE response, not only the Access-Control-Allow-* values: a response
        # template, contentHandling, or any other field on the deployed preflight is behaviour the
        # spec never asked for. `responseParameters` header names are reduced to the header itself
        # so the two sources' `method.response.header.` prefixes do not read as a difference, and
        # empty/None fields are dropped so `{}` on one side and absent on the other are equal.
        body = {
            field: value
            for field, value in response.items()
            if field not in ("selectionPattern", "responseParameters", "statusCode") and value
        }
        headers = {
            name.rsplit(".", 1)[-1]: value
            for name, value in (response.get("responseParameters") or {}).items()
        }
        entries.append(
            canonical(
                {
                    "selector": str(selector),
                    "statusCode": str(response.get("statusCode")),
                    "responseParameters": headers,
                    "rest": body,
                }
            )
        )
    return sorted(entries)


def _deployed_cors_responses(operation: dict[str, Any]) -> list[str]:
    integration = operation.get("x-amazon-apigateway-integration") or {}
    return _cors_response_list(integration.get("responses"), key_is_selector=True)


def _desired_cors_responses(bundle: dict[str, Any]) -> list[str]:
    integration = (bundle or {}).get("integration") or {}
    return _cors_response_list(
        integration.get("integrationResponses"), key_is_selector=False
    )




def _absolute_request_differences(
    operation: dict[str, Any], template_bundle: dict[str, Any] | None
) -> list[str]:
    """Compare a deployed operation against the APPLY-TIME template bundle, not the deployed one.

    The clone-vs-deployed-template comparison is RELATIVE: if the deployed template method and the
    new routes drifted together (both carrying the same request validator, say), they still match
    each other and the check passes. The bundle this kit captured at apply time is the absolute
    reference, so the request-gating parts are compared against it as well.

    Only gating properties are compared, because the two shapes differ: the bundle is
    `get-method`-shaped (`method.requestValidatorId`, `method.requestParameters`) while the export is
    OpenAPI-shaped (`x-amazon-apigateway-request-validator`, `parameters`).
    """
    problems: list[str] = []
    method = (template_bundle or {}).get("method") or {}
    if not method:
        return problems
    wants_validator = bool(method.get("requestValidatorId"))
    has_validator = bool(operation.get("x-amazon-apigateway-request-validator"))
    if wants_validator != has_validator:
        problems.append(
            "request-validator presence differs from the apply-time template "
            f"(deployed={has_validator} template={wants_validator})"
        )
    # Compare LOCATION-QUALIFIED names ("header:X-Tenant", "query:X-Tenant"), not bare names: a
    # required header replaced by a same-named query parameter is a different contract, and bare
    # names made the two indistinguishable.
    #
    # `method.request.path.*` is excluded on BOTH sides. Path parameters follow from the path
    # template, not from this spec, and the deployed side already excludes `in: path` -- including
    # them on the template side alone false-failed every target operation whenever the template
    # method had a required path parameter.
    location_of = {"header": "header", "querystring": "query"}
    # Split only the fixed `method.request.<location>.` prefix: API Gateway allows a parameter name
    # to contain periods (e.g. `method.request.header.X.Tenant`), so splitting on every period and
    # keeping one segment truncated the name and made distinct parameters compare equal.
    wanted = sorted(
        f"{location_of[parts[2]]}:{parts[3]}"
        for parts, required in (
            (name.split(".", 3), required)
            for name, required in (method.get("requestParameters") or {}).items()
        )
        if required and len(parts) == 4 and parts[0] == "method" and parts[1] == "request"
        and parts[2] in location_of
    )
    deployed_location = {"header": "header", "query": "query"}
    got = sorted(
        f"{deployed_location[str(parameter.get('in'))]}:{parameter.get('name')}"
        for parameter in (operation.get("parameters") or [])
        if isinstance(parameter, dict)
        and str(parameter.get("in")) in deployed_location
        and parameter.get("required")
    )
    if wanted != got:
        problems.append(
            "required header/query request parameters differ from the apply-time template "
            f"(deployed={got} template={wanted})"
        )
    return problems


def _request_contract(operation: dict[str, Any]) -> str:
    """The request contract a deployed operation enforces, ignoring path parameters.

    Path parameters legitimately differ between the template method and a new route (different path
    templates), so they are excluded; a header/query parameter, a request body, or a model is NOT
    allowed to differ, because that would make the new route stricter or looser than the template it
    claims to clone.
    """
    parameters = sorted(
        canonical(
            {
                key: value
                for key, value in parameter.items()
                # `in` is kept, so a required header swapped for a same-named query parameter is a
                # different contract rather than an identical one.
                if key in ("name", "in", "required", "schema")
            }
        )
        for parameter in (operation.get("parameters") or [])
        if isinstance(parameter, dict) and parameter.get("in") != "path"
    )
    return canonical(
        {
            "parameters": parameters,
            "requestBody": operation.get("requestBody"),
            "security": operation.get("security"),
            # A request validator rejects calls before they reach the integration, so it is part of
            # the contract the route claims to clone.
            "requestValidator": operation.get("x-amazon-apigateway-request-validator"),
            # The response contract is part of what a caller sees, so a new route that declares
            # different responses than the template it clones is a difference too.
            "responses": operation.get("responses"),
        }
    )


def unserved_routes(
    gateway: Gateway, spec: dict[str, Any], template: dict[str, Any] | None = None
) -> list[str]:
    """What the stage's ACTIVE deployment gets wrong about this spec. Empty list == correct.

    Checks required PRESENCE (additive/modified routes) and required ABSENCE (a deleted route, a
    deleted method, and the source path of a rename). Absence matters: a destructive spec whose
    delete was never deployed would otherwise "verify" while the stage still serves the old route.

    When the apply-time template bundle is supplied, also compares the DEPLOYED configuration --
    api-key requirement and integration type/uri -- not just the method name, so a newer deployment
    serving the right verbs with the wrong auth or integration is caught.
    """
    served = gateway.deployed_operations()
    # The template method AS DEPLOYED is the reference the spec says these routes clone. Comparing
    # against it (rather than only the live REST API bundle) catches a deployment whose new routes
    # enforce a different request contract than the template.
    template_target = (spec.get("template") or {}) if isinstance(spec, dict) else {}
    template_operation = served.get(str(template_target.get("path")), {}).get(
        str(template_target.get("method", "")).upper()
    )
    problems: list[str] = []
    for route in spec["routes"]:
        path_value = route["path"]
        change = route["resource_change"]
        operations = served.get(path_value, {})

        if change == "D":
            still = sorted(operations)
            if still:
                problems.append(
                    f"{path_value} (deleted route STILL served: {', '.join(still)})"
                )
            continue

        if change == "M" and route.get("source_path"):
            stale = sorted(served.get(route["source_path"], {}))
            if stale:
                problems.append(
                    f"{route['source_path']} (renamed source STILL served: {', '.join(stale)})"
                )

        required = {
            method["method"].upper()
            for method in route["methods"]
            if method["change"] != "D"
        }
        removed = {
            method["method"].upper()
            for method in route["methods"]
            if method["change"] == "D"
        }
        cors_change = route.get("cors", {}).get("change")
        if cors_change in {"A", "M"}:
            required.add("OPTIONS")
        elif cors_change == "D":
            removed.add("OPTIONS")

        absent = sorted(required - set(operations))
        if absent:
            problems.append(f"{path_value} (not served: {', '.join(absent)})")
        if change == "A":
            # The kit created this resource, so the active deployment must serve exactly what the
            # spec declared. An extra method here is a route nobody asked for, reachable by callers.
            extra = sorted(set(operations) - required)
            if extra:
                problems.append(
                    f"{path_value} (active deployment serves undeclared method(s): "
                    f"{', '.join(extra)})"
                )
        lingering = sorted(removed & set(operations))
        if lingering:
            problems.append(
                f"{path_value} (deleted method STILL served: {', '.join(lingering)})"
            )

        if template is None:
            continue
        for method in route["methods"]:
            name = method["method"].upper()
            if method["change"] == "D" or name not in operations:
                continue
            desired = desired_method_bundle(template, name)
            want_key = bool((desired.get("method") or {}).get("apiKeyRequired"))
            if _deployed_auth(operations[name]) != want_key:
                problems.append(
                    f"{path_value} {name} (deployed api-key requirement differs from template)"
                )
            # An authorizer in front of these routes is a behaviour change the template does not
            # ask for. The template method is authorizationType NONE + api key; anything else the
            # export reports is flagged rather than ignored.
            extra_auth = _deployed_authorizers(operations[name])
            wants_none = str((desired.get("method") or {}).get("authorizationType", "")).upper() in {
                "",
                "NONE",
            }
            if extra_auth and wants_none:
                problems.append(
                    f"{path_value} {name} (deployed operation requires unexpected authorizer(s): "
                    f"{', '.join(extra_auth)})"
                )
            elif not wants_none and not extra_auth:
                problems.append(
                    f"{path_value} {name} (template requires an authorizer but the deployed "
                    "operation has none)"
                )
            if template_operation is None:
                # Without the template operation in the SAME export there is nothing to clone
                # against, so the clone claim is unprovable. Fail closed rather than skip.
                problems.append(
                    f"{path_value} {name} (the spec's template method "
                    f"{template_target.get('method')} {template_target.get('path')} is not served "
                    "by the active deployment, so the request contract cannot be proven)"
                )
            elif _request_contract(operations[name]) != _request_contract(template_operation):
                problems.append(
                    f"{path_value} {name} (deployed request contract differs from the template "
                    "method's: header/query parameters, request body, security, or responses)"
                )
            # Absolute check: coordinated drift of BOTH the deployed template and its clones would
            # satisfy the relative comparison above, so also compare against the bundle captured at
            # apply time.
            for issue in _absolute_request_differences(operations[name], template):
                problems.append(f"{path_value} {name} ({issue})")
            differing = _integration_differences(
                _deployed_integration(operations[name]), _desired_integration(desired)
            )
            if differing:
                problems.append(
                    f"{path_value} {name} (deployed integration differs from template: "
                    f"{', '.join(differing)})"
                )
        if cors_change in {"A", "M"} and "OPTIONS" in operations:
            deployed_options = operations["OPTIONS"]
            expected = desired_cors_bundle(route["cors"])
            if _deployed_auth(deployed_options):
                problems.append(f"{path_value} OPTIONS (deployed preflight requires an api key)")
            if _deployed_authorizers(deployed_options):
                problems.append(
                    f"{path_value} OPTIONS (deployed preflight requires an authorizer)"
                )
            preflight_differences = _integration_differences(
                _deployed_integration(deployed_options), _desired_integration(expected)
            )
            if preflight_differences:
                problems.append(
                    f"{path_value} OPTIONS (deployed preflight integration differs from spec: "
                    f"{', '.join(preflight_differences)})"
                )
            # The spec's preflight declares no request parameters, no request body and no request
            # validator. A deployed preflight that adds any of them can REJECT the browser's
            # preflight, so require them to be absent.
            preflight_params = [
                parameter
                for parameter in (deployed_options.get("parameters") or [])
                if isinstance(parameter, dict) and parameter.get("in") != "path"
            ]
            if preflight_params:
                problems.append(
                    f"{path_value} OPTIONS (deployed preflight requires undeclared request "
                    f"parameter(s): {sorted(str(item.get('name')) for item in preflight_params)})"
                )
            if deployed_options.get("requestBody"):
                problems.append(
                    f"{path_value} OPTIONS (deployed preflight declares an undeclared request body)"
                )
            if deployed_options.get("x-amazon-apigateway-request-validator"):
                problems.append(
                    f"{path_value} OPTIONS (deployed preflight has an undeclared request validator)"
                )
            # The preflight's METHOD response contract (which statuses it declares, which headers
            # each may return, and any response content/model) is what a browser actually sees
            # alongside the integration mapping. `description` is AWS-generated boilerplate and
            # empty `content` is the no-model case, so both are normalized away.
            def _method_response_view(body: Any) -> dict[str, Any]:
                body = body or {}
                view: dict[str, Any] = {"headers": sorted(body.get("headers") or {})}
                content = body.get("content") or {}
                if content:
                    view["content"] = content
                return view

            deployed_method_responses = {
                str(status): _method_response_view(body)
                for status, body in (deployed_options.get("responses") or {}).items()
            }
            expected_method_responses = {
                str((body or {}).get("statusCode")): {
                    "headers": sorted(
                        name.rsplit(".", 1)[-1]
                        for name in ((body or {}).get("responseParameters") or {})
                    )
                }
                for body in ((expected.get("method") or {}).get("methodResponses") or {}).values()
            }
            if deployed_method_responses != expected_method_responses:
                problems.append(
                    f"{path_value} OPTIONS (deployed preflight method responses differ from spec: "
                    f"deployed={deployed_method_responses} spec={expected_method_responses})"
                )
            # Per-status CORS comparison: which status the preflight answers AND the exact
            # Access-Control-Allow-* values it returns for it.
            deployed_cors = _deployed_cors_responses(deployed_options)
            wanted_cors = _desired_cors_responses(expected)
            if deployed_cors != wanted_cors:
                only_deployed = [item for item in deployed_cors if item not in wanted_cors]
                only_wanted = [item for item in wanted_cors if item not in deployed_cors]
                problems.append(
                    f"{path_value} OPTIONS (deployed CORS responses differ from spec; "
                    f"deployed-only={only_deployed or '[]'} spec-only={only_wanted or '[]'})"
                )
    return problems


def route_already_satisfied(
    gateway: Gateway,
    spec: dict[str, Any],
    route: dict[str, Any],
    template: dict[str, Any],
) -> bool:
    """Read-only: is this ONE declared route already in its exact desired end state?

    Split out of routes_already_satisfied so a PARTIALLY applied environment converges. A
    customer who applied an earlier revision of this kit already carries that revision's
    routes; a later revision declares those plus new ones. A whole-spec decision cannot
    express that, so the per-route preflight used to die on the first already-present route
    and the new routes never landed.

    Same strictness as the whole-spec check: purely additive, the resource exists with
    exactly the declared methods, every method bundle equals the template, the OPTIONS CORS
    equals the spec, and the stage's ACTIVE deployment already serves it. Any drift returns
    False, so the caller still fails loud instead of silently overwriting it.
    """
    if route["resource_change"] != "A" or route["cors"]["change"] != "A":
        return False
    if any(method["change"] != "A" for method in route["methods"]):
        return False
    resource = gateway.resource(route["path"])
    if resource is None:
        return False
    expected_methods = {method["method"] for method in route["methods"]}
    expected_methods.add("OPTIONS")
    if set(resource.get("resourceMethods", {})) != expected_methods:
        return False
    for method in route["methods"]:
        actual = gateway.method_bundle(resource["id"], method["method"])
        if normalize_bundle(actual) != normalize_bundle(
            desired_method_bundle(template, method["method"])
        ):
            return False
    cors_actual = gateway.method_bundle(resource["id"], "OPTIONS")
    if normalize_bundle(cors_actual) != normalize_bundle(
        desired_cors_bundle(route["cors"])
    ):
        return False
    # The stage serves its deployment's snapshot, so a correct-looking resource tree can still
    # be unserved. Scope the served check to this one route.
    single = dict(spec)
    single["routes"] = [route]
    return not unserved_routes(gateway, single, template)


def routes_already_satisfied(gateway: Gateway, spec: dict[str, Any]) -> bool:
    """Read-only: is every declared route already present in its exact desired end state?

    Returns True only when the spec is purely additive (every resource_change and method/CORS
    change is A) and each target resource already exists with exactly the declared methods,
    each method bundle equals the template, and the OPTIONS CORS equals the spec. This makes a
    re-apply (or an environment that already carries these routes from an earlier revision) a
    safe ALREADY no-op, matching the rest of the kit's idempotent reconcile model. Any absence
    or drift returns False, so the normal apply path still runs and still fails loud on drift
    rather than silently overwriting it.
    """
    for route in spec["routes"]:
        if route["resource_change"] != "A":
            return False
        if route["cors"]["change"] != "A":
            return False
        if any(method["change"] != "A" for method in route["methods"]):
            return False
    template = template_bundle(gateway, spec)
    # Per-route, same strictness. Each check already requires the ACTIVE deployment to serve
    # that route, so a correct-looking resource tree that is unserved still returns False.
    return all(
        route_already_satisfied(gateway, spec, route, template)
        for route in spec["routes"]
    )


def assert_routes_served(
    gateway: Gateway, spec: dict[str, Any], template: dict[str, Any] | None = None
) -> None:
    missing = unserved_routes(gateway, spec, template)
    if missing:
        fail(
            "the stage's ACTIVE deployment does not serve: "
            + "; ".join(missing)
            + " -- create a deployment for this stage, then re-verify"
        )
    print(
        f"PASS: stage {gateway.stage!r} active deployment serves every declared route"
    )


def verify_routes(
    gateway: Gateway,
    spec: dict[str, Any],
    template: dict[str, Any],
    state: dict[str, Any],
) -> None:
    verify_route_resources(gateway, spec, template)
    if state.get("deployment_deferred"):
        # This run deliberately did not create a deployment: the caller's own apply-api owns the
        # single deployment for this stage (one transaction, one repoint). So the binding to prove
        # is not "the stage uses MY deployment" but "whatever deployment the stage uses serves
        # these routes" -- which is the stronger statement anyway.
        print(
            f"NOTE: deployment deferred to the caller; asserting the stage's active "
            f"deployment serves the routes instead of a deployment id"
        )
        assert_routes_served(gateway, spec, template)
        return
    expected_deployment = state.get("new_deployment")
    current_deployment = gateway.stage_deployment()
    if not expected_deployment:
        fail("state records no deployment for this apply")
    if current_deployment == expected_deployment:
        print(f"PASS: stage {gateway.stage!r} uses deployment {current_deployment}")
    else:
        # A later deployment superseded ours. That is normal (an operator redeployed, or another
        # tool in the same rollout owns the stage) and it is NOT a failure by itself -- what must
        # hold is that whatever the stage serves still carries these routes. Requiring id equality
        # made a second verify of an already-correct environment fail, which is not idempotent.
        print(
            f"NOTE: stage {gateway.stage!r} now uses deployment {current_deployment}, not the "
            f"{expected_deployment} this run created; asserting the routes are still served"
        )
    assert_routes_served(gateway, spec, template)


def finalize_routes(
    gateway: Gateway,
    state: dict[str, Any],
    state_file: Path,
    spec: dict[str, Any] | None = None,
) -> None:
    if state.get("rolled_back"):
        fail("cannot finalize a rolled-back route apply")
    if state.get("deployment_deferred"):
        # This run created no deployment, so it has no replaced deployment to release. The caller
        # that owns the single deployment finalizes it. Still prove the routes are served before
        # dropping the state, so finalize can never be the step that hides an unserved route.
        verify_route_resources(
            gateway,
            {
                "routes": [item["route"] for item in state["routes"]],
                "template": (spec or {}).get("template"),
            },
            state["template"],
        )
        assert_routes_served(
            gateway,
            {
                "routes": [item["route"] for item in state["routes"]],
                "template": (spec or {}).get("template"),
            },
            state["template"],
        )
        state["finalized"] = True
        write_state(state_file, state)
        state_file.unlink()
        print(
            "PASS: nothing to finalize (deployment was deferred to the caller); "
            "routes verified as served"
        )
        return
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
    # Finalize's job is to release the REPLACED deployment. Deleting the one the stage is actually
    # using would break the stage, so that is the hard refusal; a stage that has since moved to a
    # newer deployment is fine as long as the routes are still served.
    if gateway.stage_deployment() == previous:
        fail(
            f"stage {gateway.stage!r} still uses {previous}, the deployment finalize would "
            "delete; re-point the stage first"
        )
    verify_route_resources(
        gateway,
        {
                "routes": [item["route"] for item in state["routes"]],
                "template": (spec or {}).get("template"),
            },
        state["template"],
    )
    assert_routes_served(
        gateway,
        {
                "routes": [item["route"] for item in state["routes"]],
                "template": (spec or {}).get("template"),
            },
        state["template"],
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
        # rollback has no spec in scope; verify_route_resources needs only the routes.
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
    arguments = sys.argv[1:]
    if arguments.count("--yes") > 1:
        fail("--yes may be specified only once")
    assume_yes = "--yes" in arguments
    if assume_yes:
        arguments.remove("--yes")
    if len(arguments) == 2 and arguments[0] == "validate-spec":
        spec_path = Path(arguments[1])
        if not spec_path.is_file():
            fail(f"spec not found: {spec_path}")
        validate_spec(json.loads(spec_path.read_bytes()))
        print(f"PASS: valid API route spec {spec_path}")
        return
    if len(arguments) != 5:
        fail(
            "usage: apply-api-routes.sh validate-spec <spec.json>\n"
            "       apply-api-routes.sh plan|apply|verify|finalize|rollback "
            "<spec.json> <rest-api-id> <stage> <region> [--yes]"
        )
    command, spec_name, api, stage, region = arguments
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
    # Single deployment transaction: when the restorepatch driver's apply-api is going to create
    # the one deployment for this stage, this tool must NOT create a competing one.
    defer_deployment = os.environ.get("OC_ROUTE_DEFER_DEPLOYMENT") == "1"

    if command == "plan":
        template, snapshots = preflight(gateway, spec)
        print_plan(gateway, spec, template, snapshots)
        return
    if command == "apply":
        # Idempotent no-op: if the routes are already in their exact desired end state AND served
        # by the stage's active deployment, a re-run converges without touching AWS. Drift falls
        # through to preflight, which fails loud rather than silently overwriting.
        #
        # This must hold whether or not a state file survives. A leftover state file means an
        # earlier run of THIS spec is still awaiting finalize (or was interrupted) -- it does not
        # mean the routes need applying again, and refusing here is exactly the non-idempotent
        # behaviour that made a repeat apply fail on an already-correct environment. So report
        # ALREADY and point at the step that is actually outstanding.
        # Hold the per-API/stage lease BEFORE the ALREADY decision. Reading live state outside the
        # lease lets a concurrent finalize or rollback change it between the read and this exit.
        lease = acquire_lease(state_file)
        try:
            if routes_already_satisfied(gateway, spec):
                print(
                    "ALREADY: declared routes already present, and served by the active deployment"
                )
                if state_file.exists():
                    # A surviving state file means an earlier run of THIS spec is awaiting finalize
                    # (or was interrupted); it does not mean route work is outstanding. Refusing
                    # here is exactly the non-idempotent behaviour a repeat apply hit. Load it so a
                    # corrupt/foreign state still fails closed rather than being ignored.
                    load_state(state_file, api, stage, region, spec_sha)
                    print(
                        f"NOTE: an unfinalized apply state remains at {state_file}; "
                        "run finalize (or rollback) to close it -- no route work is outstanding"
                    )
                return
            if state_file.exists():
                fail(f"state already exists at {state_file}")
            template, snapshots = preflight(gateway, spec)
            print_plan(gateway, spec, template, snapshots)
            gate(
                f"apply exact route lifecycle to API {api!r} and deploy stage {stage!r}",
                assume_yes,
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
            if defer_deployment:
                # Single deployment transaction: the caller (the restorepatch driver's apply-api)
                # creates ONE deployment for this stage and repoints it once. If this tool also
                # created one, the stage would end on ours while the caller's verify/finalize
                # still expected theirs -- a guaranteed failure AFTER the routes were installed.
                state["deployment_deferred"] = True
                write_state(state_file, state)
                print(
                    "routes applied; deployment deferred to the caller "
                    "(OC_ROUTE_DEFER_DEPLOYMENT=1). The routes are NOT served until that "
                    "deployment happens; run verify afterwards."
                )
                return
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
        # After an idempotent ALREADY apply (or a finalized run) there is no state file. The
        # routes are still the thing to prove, so verify their live presence/shape directly.
        if not state_file.exists():
            if routes_already_satisfied(gateway, spec):
                live_template = template_bundle(gateway, spec)
                verify_route_resources(gateway, spec, live_template)
                assert_routes_served(gateway, spec, live_template)
                print("PASS: declared routes already present (no pending apply state)")
                return
            fail(f"apply state is missing and routes are not present: {state_file}")
        state = load_state(state_file, api, stage, region, spec_sha)
        verify_routes(gateway, spec, state["template"], state)
        return

    if command == "finalize" and not state_file.exists():
        # Nothing to finalize: no replaced deployment is pending. Idempotent no-op only when the
        # routes are already in place; otherwise something is wrong and we must not claim success.
        if routes_already_satisfied(gateway, spec):
            print("ALREADY: no pending deployment to finalize; routes already present")
            return
        fail(f"finalize has no apply state and routes are not present: {state_file}")

    lease = acquire_lease(state_file)
    try:
        state = load_state(state_file, api, stage, region, spec_sha)
        if command == "finalize":
            gate(
                f"finalize API {api!r} stage {stage!r} and delete its "
                "previous deployment",
                assume_yes,
            )
            finalize_routes(gateway, state, state_file, spec)
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
        gate(
            f"restore stage {stage!r} and every state-captured route resource",
            assume_yes,
        )
        rollback_routes(gateway, state, state_file)
    finally:
        release_lease(state_file, lease)


if __name__ == "__main__":
    main()
