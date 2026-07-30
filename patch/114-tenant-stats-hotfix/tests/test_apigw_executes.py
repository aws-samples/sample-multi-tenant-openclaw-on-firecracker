# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RUN the generated API Gateway route scripts against a stub AWS, end to end.

Why: an independent reviewer scored this lane 2.3/10 because a line-continuation backslash had lost
its newline (so `put-method` got a blank argument and the CLI answered "Unknown options"), and
`classify_aws_error` was referenced but never defined (exit 255). `bash -n` passed both — syntax is
fine, the ARGUMENTS are not. So these tests EXECUTE the scripts against a stub `aws`/`curl` that
rejects any blank or space-prefixed argument and records every call.
"""

import importlib.util
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

import pytest


PATCH_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compile_apigw", PATCH_ROOT / "factory" / "scripts" / "_compile_apigw.py"
)
compiler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compiler)

linux_only = pytest.mark.skipif(
    platform.system() != "Linux", reason="the recipes need bash 4+ and GNU coreutils"
)

ACCOUNT = "111111111111"
REGION = "ap-southeast-1"
API = "abcdefghij"
STAGE = "v1"
PATH = "/tenants-stats"
FN = "openclaw-api"
CORS_ORIGIN = "*"
CORS_HEADERS = ["Content-Type", "x-api-key", "Authorization"]
CORS_METHODS = ["OPTIONS", "GET", "PUT", "POST", "DELETE", "PATCH", "HEAD"]

STUB_AWS = r"""#!/usr/bin/env python3
import copy
import json
import os
import re
import sys

argv = sys.argv[1:]
state_path = os.environ["STUB_STATE"]
state = json.load(open(state_path))
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("\t".join(argv) + "\n")


def has(*words):
    return all(word in argv for word in words)


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def save():
    json.dump(state, open(state_path, "w"))


def die(message, code=254):
    save()
    sys.stderr.write(message + "\n")
    raise SystemExit(code)


def fail_once(name):
    failures = state.setdefault("fail_once", {})
    message = failures.pop(name, None)
    if message:
        die(message)


def resource_by_id(resource_id):
    return next(
        (item for item in state["resources"]["items"] if item["id"] == resource_id),
        None,
    )


def method_for(resource_id, method):
    resource = resource_by_id(resource_id)
    return (resource or {}).get("resourceMethods", {}).get(method.upper())


def build_snapshot():
    paths = {}
    summary = {}
    schemes = {}
    authorizers = {
        item["id"]: item for item in state.get("authorizers", {}).get("items", [])
    }
    for resource in state["resources"]["items"]:
        path = resource["path"]
        for method_name, method in resource.get("resourceMethods", {}).items():
            method_name = method_name.upper()
            operation = {}
            security = []
            if method.get("operationName"):
                operation["operationId"] = method["operationName"]
            parameters = []
            for name, required in (method.get("requestParameters") or {}).items():
                _, _, location, parameter_name = name.split(".", 3)
                parameters.append(
                    {
                        "name": parameter_name,
                        "in": "query" if location == "querystring" else location,
                        "required": bool(required),
                        "schema": {"type": "string"},
                    }
                )
            if parameters:
                operation["parameters"] = parameters
            responses = {}
            for status, response in (method.get("methodResponses") or {}).items():
                headers = {}
                for name in (response.get("responseParameters") or {}):
                    headers[name.rsplit(".", 1)[-1]] = {"schema": {"type": "string"}}
                responses[status] = {
                    "description": status + " response",
                    "headers": headers,
                    "content": {},
                }
            if responses:
                operation["responses"] = responses
            integration = copy.deepcopy(method.get("methodIntegration") or {})
            if integration:
                responses = integration.pop("integrationResponses", None)
                if responses is not None:
                    exported_responses = {}
                    for status, response in responses.items():
                        response = copy.deepcopy(response)
                        selector = response.pop("selectionPattern", None) or "default"
                        response.setdefault("statusCode", status)
                        exported_responses[selector] = response
                    integration["responses"] = exported_responses
                operation["x-amazon-apigateway-integration"] = integration
            authorizer_id = method.get("authorizerId")
            if authorizer_id:
                authorizer = authorizers[authorizer_id]
                scheme_name = authorizer["name"]
                detail = {
                    key: value
                    for key, value in authorizer.items()
                    if key
                    in {
                        "authType",
                        "authorizerCredentials",
                        "authorizerResultTtlInSeconds",
                        "authorizerUri",
                        "identitySource",
                        "identityValidationExpression",
                        "providerARNs",
                        "type",
                    }
                }
                schemes[scheme_name] = {
                    "type": "apiKey",
                    "name": "Authorization",
                    "in": "header",
                    "x-amazon-apigateway-authorizer": detail,
                }
                security.append(
                    {scheme_name: sorted(method.get("authorizationScopes") or [])}
                )
            if method.get("apiKeyRequired", False):
                schemes["oc_api_key"] = {
                    "type": "apiKey",
                    "name": "x-api-key",
                    "in": "header",
                }
                security.append({"oc_api_key": []})
            if security:
                operation["security"] = security
            paths.setdefault(path, {})[method_name.lower()] = operation
            summary.setdefault(path, {})[method_name] = {
                "authorizationType": method.get("authorizationType", "NONE"),
                "apiKeyRequired": bool(method.get("apiKeyRequired", False)),
            }
    document = {
        "openapi": "3.0.1",
        "info": {"title": state["rest_api"]["name"], "version": "stub"},
        "paths": paths,
        "x-amazon-apigateway-api-key-source": state["rest_api"]["apiKeySource"],
        "x-amazon-apigateway-security-policy": state["rest_api"]["securityPolicy"],
    }
    if schemes:
        document["components"] = {"securitySchemes": schemes}
    return {"doc": document, "summary": {"apiSummary": summary}}


# The defect this suite exists to catch: a dropped continuation newline turns the next line into a
# single blank argument. The real CLI rejects it; the stub must be at least as strict.
for argument in argv:
    if argument.strip() == "" or argument != argument.lstrip():
        die("Unknown options: %r" % argument, 252)

if has("sts", "get-caller-identity"):
    print(state["account"])
elif has("apigateway", "get-stage"):
    stage_name = opt("--stage-name")
    stage = state["stages"].get(stage_name)
    if stage is None:
        die("An error occurred (NotFoundException): stage")
    if (
        state.pop("move_formal_stage_before_switch_once", False)
        and stage_name == state["formal_stage"]
        and len(state["deployments"]) > 1
    ):
        state["deployments"]["foreign"] = copy.deepcopy(state["deployments"]["dep0"])
        state["stages"][stage_name]["deploymentId"] = "foreign"
        stage = state["stages"][stage_name]
        save()
    query = opt("--query")
    if query == "canarySettings.percentTraffic":
        value = (stage.get("canarySettings") or {}).get("percentTraffic")
        print("None" if value is None else value)
    elif query == "deploymentId":
        print(stage["deploymentId"])
    else:
        print(json.dumps(stage))
elif has("apigateway", "get-export"):
    fail_once("get-export")
    if opt("--parameters") != '{"extensions":"integrations,authorizers,apigateway"}':
        die("get-export must include integrations, authorizers, and apigateway")
    stage_name = opt("--stage-name")
    stage = state["stages"].get(stage_name)
    if stage is None:
        die("An error occurred (NotFoundException): stage")
    deployment_id = stage["deploymentId"]
    if (
        state.pop("fail_formal_export_after_switch_once", False)
        and stage_name == state["formal_stage"]
        and deployment_id != "dep0"
    ):
        die("An error occurred (ServiceUnavailableException)")
    document = copy.deepcopy(state["deployments"][deployment_id]["doc"])
    if (
        state.get("corrupt_formal_export_after_switch", False)
        and stage_name == state["formal_stage"]
        and deployment_id != "dep0"
    ):
        document["x-amazon-apigateway-api-key-source"] = "AUTHORIZER"
    document["servers"] = [
        {
            "url": "https://%s.execute-api.%s.amazonaws.com/%s"
            % (state["api_id"], state["region"], stage_name)
        }
    ]
    json.dump(document, open(argv[-1], "w"))
elif has("apigateway", "get-deployment"):
    fail_once("get-deployment")
    deployment_id = opt("--deployment-id")
    deployment = state["deployments"].get(deployment_id)
    if deployment is None:
        die("An error occurred (NotFoundException): deployment")
    response = copy.deepcopy(deployment["summary"])
    response.update(
        {
            "id": deployment_id,
            "description": deployment.get("description", ""),
            "createdDate": deployment.get("createdDate", 0),
        }
    )
    print(json.dumps(response))
elif has("apigateway", "get-authorizers"):
    if opt("--limit") != "500":
        die("stub requires --limit 500")
    fail_once("get-authorizers")
    print(json.dumps(state.get("authorizers", {"items": []})))
elif has("apigateway", "get-resources"):
    if opt("--limit") != "500":
        die("stub requires --limit 500")
    if (
        state.pop("fail_live_read_after_create_once", False)
        and len(state["deployments"]) > 1
    ):
        die("socket closed unexpectedly")
    fail_once("get-resources")
    query = opt("--query")
    if query:
        match = re.search(r"path=='([^']*)'", query)
        path = match.group(1)
        resource = next(
            (item for item in state["resources"]["items"] if item["path"] == path),
            None,
        )
        print(resource["id"] if resource else "None")
    else:
        print(json.dumps(state["resources"]))
elif has("apigateway", "get-rest-api"):
    fail_once("get-rest-api")
    print(json.dumps(state["rest_api"]))
elif has("apigateway", "get-models"):
    if opt("--limit") != "500":
        die("stub requires --limit 500")
    fail_once("get-models")
    print(json.dumps(state.get("models", {"items": []})))
elif has("apigateway", "get-request-validators"):
    if opt("--limit") != "500":
        die("stub requires --limit 500")
    fail_once("get-request-validators")
    print(json.dumps(state.get("request_validators", {"items": []})))
elif has("apigateway", "get-gateway-responses"):
    if opt("--limit") != "500":
        die("stub requires --limit 500")
    fail_once("get-gateway-responses")
    print(json.dumps(state.get("gateway_responses", {"items": []})))
elif has("apigateway", "get-documentation-parts"):
    if opt("--limit") != "500":
        die("stub requires --limit 500")
    fail_once("get-documentation-parts")
    print(json.dumps(state.get("documentation_parts", {"items": []})))
elif has("apigateway", "get-documentation-versions"):
    if opt("--limit") != "500":
        die("stub requires --limit 500")
    fail_once("get-documentation-versions")
    print(json.dumps(state.get("documentation_versions", {"items": []})))
elif has("apigateway", "get-integration"):
    fail_once("get-integration")
    method = method_for(opt("--resource-id"), opt("--http-method"))
    integration = (method or {}).get("methodIntegration")
    if not integration:
        die("An error occurred (NotFoundException): integration")
    if opt("--query") == "uri":
        print(integration.get("uri", "None"))
    else:
        print(json.dumps(integration))
elif has("apigateway", "get-integration-response"):
    fail_once("get-integration-response")
    method = method_for(opt("--resource-id"), opt("--http-method"))
    integration = (method or {}).get("methodIntegration") or {}
    response = (integration.get("integrationResponses") or {}).get(opt("--status-code"))
    if response is None:
        die("An error occurred (NotFoundException): integration response")
    print(json.dumps(response))
elif has("apigateway", "get-method"):
    fail_once("get-method")
    method = method_for(opt("--resource-id"), opt("--http-method"))
    if method is None:
        die("An error occurred (NotFoundException): method")
    print(json.dumps(method))
elif has("apigateway", "get-method-response"):
    fail_once("get-method-response")
    method = method_for(opt("--resource-id"), opt("--http-method"))
    response = ((method or {}).get("methodResponses") or {}).get(opt("--status-code"))
    if response is None:
        die("An error occurred (NotFoundException): method response")
    print(json.dumps(response))
elif has("apigateway", "create-resource"):
    fail_once("create-resource")
    parent = resource_by_id(opt("--parent-id"))
    part = opt("--path-part")
    path = parent["path"].rstrip("/") + "/" + part
    state["res_seq"] = state.get("res_seq", 0) + 1
    resource_id = "res%d" % state["res_seq"]
    state["resources"]["items"].append(
        {"id": resource_id, "path": path, "resourceMethods": {}}
    )
    save()
    print(resource_id)
elif has("apigateway", "put-method"):
    fail_once("put-method")
    resource = resource_by_id(opt("--resource-id"))
    method_name = opt("--http-method").upper()
    if state.get("method_conflict") or method_name in resource["resourceMethods"]:
        die("An error occurred (ConflictException): Method already exists")
    key_flags = [
        flag
        for flag in ("--api-key-required", "--no-api-key-required")
        if flag in argv
    ]
    if len(key_flags) != 1:
        die("put-method requires exactly one API-key flag")
    resource["resourceMethods"][method_name] = {
        "authorizationType": opt("--authorization-type"),
        "apiKeyRequired": key_flags[0] == "--api-key-required",
    }
    if "--authorizer-id" in argv:
        resource["resourceMethods"][method_name]["authorizerId"] = opt(
            "--authorizer-id"
        )
    if "--authorization-scopes" in argv:
        start = argv.index("--authorization-scopes") + 1
        scopes = []
        while start < len(argv) and not argv[start].startswith("--"):
            scopes.append(argv[start])
            start += 1
        resource["resourceMethods"][method_name]["authorizationScopes"] = scopes
    save()
    print("{}")
elif has("apigateway", "put-integration"):
    fail_once("put-integration")
    method = method_for(opt("--resource-id"), opt("--http-method"))
    if method is None:
        die("An error occurred (NotFoundException): method")
    integration = {"type": opt("--type")}
    if "--integration-http-method" in argv:
        integration["httpMethod"] = opt("--integration-http-method")
    if "--uri" in argv:
        integration["uri"] = opt("--uri")
    if "--passthrough-behavior" in argv:
        integration["passthroughBehavior"] = opt("--passthrough-behavior")
    if "--request-templates" in argv:
        integration["requestTemplates"] = json.loads(opt("--request-templates"))
    method["methodIntegration"] = integration
    save()
    print("{}")
elif has("apigateway", "put-method-response"):
    fail_once("put-method-response")
    method = method_for(opt("--resource-id"), opt("--http-method"))
    if method is None:
        die("An error occurred (NotFoundException): method")
    status = opt("--status-code")
    responses = method.setdefault("methodResponses", {})
    if status in responses:
        die("An error occurred (ConflictException): Method response already exists")
    responses[status] = {
        "statusCode": status,
        "responseParameters": json.loads(opt("--response-parameters") or "{}"),
    }
    save()
    print("{}")
elif has("apigateway", "put-integration-response"):
    fail_once("put-integration-response")
    method = method_for(opt("--resource-id"), opt("--http-method"))
    integration = (method or {}).get("methodIntegration")
    if not integration:
        die("An error occurred (NotFoundException): integration")
    status = opt("--status-code")
    responses = integration.setdefault("integrationResponses", {})
    if status in responses:
        die("An error occurred (ConflictException): Integration response already exists")
    response = {
        "statusCode": status,
        "responseParameters": json.loads(opt("--response-parameters") or "{}"),
    }
    if "--selection-pattern" in argv:
        response["selectionPattern"] = opt("--selection-pattern")
    responses[status] = response
    save()
    print("{}")
elif has("apigateway", "create-deployment"):
    fail_once("create-deployment")
    if "--stage-name" in argv or "--stage-description" in argv:
        die("create-deployment must not bind any stage", 251)
    state["deploy_seq"] = state.get("deploy_seq", 0) + 1
    deployment_id = "dep%d" % state["deploy_seq"]
    state["deployments"][deployment_id] = build_snapshot()
    state["deployments"][deployment_id]["description"] = opt("--description") or ""
    if state.pop("mutate_after_create_once", False):
        state["resources"]["items"].append(
            {
                "id": "concurrent",
                "path": "/concurrent-write",
                "resourceMethods": {
                    "DELETE": {
                        "authorizationType": "NONE",
                        "apiKeyRequired": False,
                    }
                },
            }
        )
    save()
    print(deployment_id)
elif has("apigateway", "create-stage"):
    die("create-stage is forbidden in the generated lane", 251)
elif has("apigateway", "delete-stage"):
    die("delete-stage is forbidden in the generated lane", 251)
elif has("apigateway", "update-stage"):
    fail_once("update-stage")
    stage_name = opt("--stage-name")
    if stage_name not in state["stages"]:
        die("An error occurred (NotFoundException): stage")
    operation = opt("--patch-operations")
    deployment_id = operation.split("value=", 1)[1]
    if deployment_id not in state["deployments"]:
        die("An error occurred (BadRequestException): deployment")
    state["stages"][stage_name]["deploymentId"] = deployment_id
    save()
    print("{}")
elif has("lambda", "add-permission"):
    fail_once("add-permission")
    policy = state.get("policy")
    sid = opt("--statement-id")
    existing = (policy or {}).get("Statement", [])
    if state.get("perm_conflict") or any(item.get("Sid") == sid for item in existing):
        die("An error occurred (ResourceConflictException)")
    function_name = opt("--function-name")
    statement = {
        "Sid": sid,
        "Effect": "Allow",
        "Action": opt("--action"),
        "Principal": {"Service": opt("--principal")},
        "Resource": (
            "arn:aws:lambda:%s:%s:function:%s"
            % (state["region"], state["account"], function_name)
        ),
        "Condition": {"ArnLike": {"AWS:SourceArn": opt("--source-arn")}},
    }
    policy = state.get("policy") or {"Version": "2012-10-17", "Statement": []}
    state["policy"] = policy
    policy["Statement"].append(statement)
    save()
    print("{}")
elif has("lambda", "get-policy"):
    fail_once("get-policy")
    policy = state.get("policy")
    if policy is None:
        die("An error occurred (ResourceNotFoundException): policy")
    print(json.dumps(policy))
else:
    die("stub: unhandled %s" % " ".join(argv), 3)
"""

STUB_CURL = r"""#!/usr/bin/env python3
import json, os, sys

state = json.load(open(os.environ["STUB_STATE"]))
# -w '%{http_code}' means the body goes to -o <file> and the status prints to stdout.
out = None
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("curl\t" + "\t".join(argv) + "\n")
if "-o" in argv:
    out = argv[argv.index("-o") + 1]
formal = state["stages"][state["formal_stage"]]
deployment = state["deployments"][formal["deploymentId"]]
route = (deployment["doc"].get("paths") or {}).get(state["route_path"]) or {}
if state.get("force_http_response") or state["route_method"].lower() in route:
    body, code = state.get("backend_body", '{"ok":true}'), state.get("backend_code", "200")
else:
    body, code = '{"message":"Missing Authentication Token"}', "403"
if out:
    open(out, "w").write(body)
sys.stdout.write(code)
"""


def _kit(tmp_path, **overrides):
    kit = tmp_path / "kit"
    kit.mkdir(exist_ok=True)
    spec = {
        "api_id": API,
        "target_account": ACCOUNT,
        "target_region": REGION,
        "stage": STAGE,
        "path": PATH,
        "method": "GET",
        "target_function": FN,
        "target_qualifier": "live",
        "invoke_url": f"https://{API}.execute-api.{REGION}.amazonaws.com/{STAGE}",
        "authorization_type": "NONE",
        "api_key_required": True,
        "cors": {
            "allow_origin": CORS_ORIGIN,
            "allow_headers": CORS_HEADERS,
            "allow_methods": CORS_METHODS,
        },
        "kind": "lambda-proxy-route",
    }
    spec.update(overrides)
    (kit / "manifest.json").write_text(
        json.dumps(
            {
                "id": "114-tenants-stats-route",
                "base_sha": "a" * 40,
                "patch_sha": "f" * 40,
                "status": "READY",
                "kit_files": {},
                "paths": {},
                "api_routes": [spec],
            }
        )
    )
    result = compiler.compile_apigw_kit(str(kit))
    (kit / "REVIEW.json").write_text(
        json.dumps({"kit_fingerprint": "a" * 64})
    )
    return kit, result


def _want_uri():
    return (
        f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FN}:live/invocations"
    )


def _env(tmp_path, **state_overrides):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "aws").write_text(STUB_AWS)
    (bindir / "aws").chmod(0o755)
    (bindir / "curl").write_text(STUB_CURL)
    (bindir / "curl").chmod(0o755)
    baseline = {
        "doc": {
            "openapi": "3.0.1",
            "info": {"title": "openclaw-orchestrator", "version": "stub"},
            "paths": {"/": {"get": {}}},
            "x-amazon-apigateway-api-key-source": "HEADER",
            "x-amazon-apigateway-security-policy": "TLS_1_0",
        },
        "summary": {
            "apiSummary": {
                "/": {"GET": {"authorizationType": "NONE", "apiKeyRequired": False}}
            }
        },
        "description": "baseline",
    }
    canary_percent = state_overrides.pop("canary_percent", None)
    state = {
        "account": ACCOUNT,
        "region": REGION,
        "api_id": API,
        "formal_stage": STAGE,
        "stages": {
            STAGE: {
                "deploymentId": "dep0",
                "description": "formal",
            }
        },
        "deployments": {"dep0": baseline},
        "authorizers": {"items": []},
        "rest_api": {
            "id": API,
            "name": "openclaw-orchestrator",
            "apiKeySource": "HEADER",
            "endpointConfiguration": {"types": ["EDGE"], "ipAddressType": "ipv4"},
            "disableExecuteApiEndpoint": False,
            "securityPolicy": "TLS_1_0",
        },
        "models": {"items": []},
        "request_validators": {"items": []},
        "gateway_responses": {"items": []},
        "documentation_parts": {"items": []},
        "documentation_versions": {"items": []},
        "resources": {
            "items": [
                {
                    "id": "root",
                    "path": "/",
                    "resourceMethods": {
                        "GET": {
                            "authorizationType": "NONE",
                            "apiKeyRequired": False,
                        }
                    },
                },
            ]
        },
        "route_path": PATH,
        "route_method": "GET",
        "policy": None,
        "res_seq": 0,
        "deploy_seq": 0,
    }
    state.update(state_overrides)
    authorizer_items = state.get("authorizers", {}).get("items", [])
    if authorizer_items:
        schemes = {}
        for authorizer in authorizer_items:
            detail = {
                key: value
                for key, value in authorizer.items()
                if key
                in {
                    "authType",
                    "authorizerCredentials",
                    "authorizerResultTtlInSeconds",
                    "authorizerUri",
                    "identitySource",
                    "identityValidationExpression",
                    "providerARNs",
                    "type",
                }
            }
            schemes[authorizer["name"]] = {
                "type": "apiKey",
                "name": "Authorization",
                "in": "header",
                "x-amazon-apigateway-authorizer": detail,
            }
        baseline["doc"]["components"] = {"securitySchemes": schemes}
    if canary_percent is not None:
        state["stages"][STAGE]["canarySettings"] = {
            "percentTraffic": canary_percent
        }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "STUB_LOG": str(tmp_path / "calls.log"),
            "STUB_STATE": str(state_file),
            "OC_PATCH_REGION": REGION,
            "OC_PATCH_STATE_ROOT": str(tmp_path / "st"),
            "OC_PATCH_APIGW_TIMEOUT": "8",
        }
    )
    return env, tmp_path / "calls.log", state_file


def _run(kit, result, stage, env, args=()):
    path = kit / "lib" / "compiled" / result["resource_id"] / f"{stage}.sh"
    return subprocess.run(
        ["bash", str(path), *args], capture_output=True, text=True, env=env
    )


def _calls(log):
    return log.read_text().splitlines() if log.exists() else []


def _read_state(path):
    return json.loads(path.read_text())


def _write_state(path, state):
    path.write_text(json.dumps(state))


def _formal_deployment(state):
    return state["stages"][STAGE]["deploymentId"]


def _root_resources(method):
    return {
        "items": [
            {
                "id": "root",
                "path": "/",
                "resourceMethods": {"GET": method} if method is not None else {},
            }
        ]
    }


def _exact_policy(result, source_arn=None):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": f"ocpatch-{result['resource_id']}",
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Principal": {"Service": "apigateway.amazonaws.com"},
                "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FN}:live",
                "Condition": {
                    "ArnLike": {
                        "AWS:SourceArn": source_arn
                        or (
                            f"arn:aws:execute-api:{REGION}:{ACCOUNT}:"
                            f"{API}/{STAGE}/GET{PATH}"
                        )
                    }
                },
            }
        ],
    }


def test_generated_http_header_validator_is_valid_python(tmp_path):
    headers_file = tmp_path / "probe-headers.json"
    headers_file.write_text(json.dumps({"X-Probe": "route-kit"}))
    kit, result = _kit(
        tmp_path,
        probe={
            "method": "GET",
            "headers_file_sha256": hashlib.sha256(headers_file.read_bytes()).hexdigest(),
            "body": None,
            "expected_status": 200,
            "expected_body_fields": {"ok": True},
        },
    )
    apply = (
        kit / "lib" / "compiled" / result["resource_id"] / "apply.sh"
    ).read_text()
    marker = 'python3 - "$headers_file" > "$normalized_headers.tmp" <<\'PYEOF\'\n'
    validator = apply.split(marker, 1)[1].split("\nPYEOF", 1)[0]
    compile(validator, "<generated-http-header-validator>", "exec")
    assert 'for ch in "\\r\\n\\t"' in validator


@linux_only
def test_no_call_receives_a_blank_argument(tmp_path):
    """The 2.3/10 defect, asserted directly: a lost continuation newline makes a blank argument, and
    classify_aws_error being undefined made every failure exit 255."""
    kit, result = _kit(tmp_path)
    run = _run(kit, result, "apply", env=_env(tmp_path)[0])
    assert "Unknown options" not in run.stderr, run.stderr
    assert run.returncode not in (252, 255), run.stdout + run.stderr


@linux_only
def test_missing_review_receipt_fails_44_before_aws(tmp_path):
    kit, result = _kit(tmp_path)
    review = kit / "REVIEW.json"
    review.rename(kit / "REVIEW.json.missing")
    env, log, _ = _env(tmp_path)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 44, run.stdout + run.stderr
    assert "final REVIEW.json is missing" in run.stderr
    assert not _calls(log)


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"kit_fingerprint": "A" * 64},
        {"kit_fingerprint": "a" * 63},
        {"kit_fingerprint": 7},
    ],
)
@linux_only
def test_invalid_review_fingerprint_fails_44_before_aws(tmp_path, receipt):
    kit, result = _kit(tmp_path)
    (kit / "REVIEW.json").write_text(json.dumps(receipt))
    env, log, _ = _env(tmp_path)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 44, run.stdout + run.stderr
    assert "valid final kit_fingerprint" in run.stderr
    assert not _calls(log)


@linux_only
def test_apply_creates_the_route_deploys_and_goes_live(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(tmp_path)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 0, run.stdout + run.stderr
    calls = _calls(log)
    assert any("put-method" in c for c in calls)
    assert any("put-integration" in c for c in calls)
    assert any("add-permission" in c for c in calls), (
        "the route 500s without invoke permission"
    )
    assert any("create-deployment" in c for c in calls), (
        "config alone does not change behavior"
    )
    create = next(c for c in calls if "create-deployment" in c)
    assert "--stage-name" not in create
    assert "--stage-description" not in create
    assert not any("create-stage" in call for call in calls)
    assert not any("delete-stage" in call for call in calls)
    assert any("update-stage" in c for c in calls), "the stage switch is a separate step"
    create_index = next(
        index for index, call in enumerate(calls) if "create-deployment" in call
    )
    switch = next(index for index, call in enumerate(calls) if "update-stage" in call)
    live_reads = [
        index
        for index, call in enumerate(calls)
        if "apigateway\tget-resources" in call and "--query" not in call
    ]
    assert any(index < create_index for index in live_reads)
    assert any(create_index < index < switch for index in live_reads)
    assert any(
        index > switch
        and "get-export" in call
        and f"--stage-name\t{STAGE}" in call
        for index, call in enumerate(calls)
    )
    get_method_call = next(
        call for call in calls if "put-method" in call and "\tGET\t" in call
    )
    options_method_call = next(
        call for call in calls if "put-method" in call and "\tOPTIONS\t" in call
    )
    assert "--api-key-required" in get_method_call
    assert "--no-api-key-required" in options_method_call
    assert any("put-method-response" in c and "\tOPTIONS\t" in c for c in calls)
    assert any("put-integration-response" in c and "\tOPTIONS\t" in c for c in calls)

    state = _read_state(state_path)
    resource = next(item for item in state["resources"]["items"] if item["path"] == PATH)
    get = resource["resourceMethods"]["GET"]
    assert get["authorizationType"] == "NONE"
    assert get["apiKeyRequired"] is True
    assert get["methodIntegration"] == {
        "type": "AWS_PROXY",
        "httpMethod": "POST",
        "uri": _want_uri(),
    }
    options = resource["resourceMethods"]["OPTIONS"]
    assert options["authorizationType"] == "NONE"
    assert options["apiKeyRequired"] is False
    integration = options["methodIntegration"]
    assert integration["type"] == "MOCK"
    assert integration["passthroughBehavior"] == "NEVER"
    assert integration["requestTemplates"] == {
        "application/json": '{"statusCode": 200}'
    }
    assert set(options["methodResponses"]["200"]["responseParameters"]) == {
        "method.response.header.Access-Control-Allow-Headers",
        "method.response.header.Access-Control-Allow-Methods",
        "method.response.header.Access-Control-Allow-Origin",
    }
    response = integration["integrationResponses"]["200"]
    assert "selectionPattern" not in response
    assert response["responseParameters"] == {
        "method.response.header.Access-Control-Allow-Headers": (
            f"'{','.join(CORS_HEADERS)}'"
        ),
        "method.response.header.Access-Control-Allow-Methods": (
            f"'{','.join(CORS_METHODS)}'"
        ),
        "method.response.header.Access-Control-Allow-Origin": f"'{CORS_ORIGIN}'",
    }
    assert _formal_deployment(state) == "dep1"
    assert set(state["stages"]) == {STAGE}, "the lane must never create a temp stage"
    assert "APPLIED" in run.stdout


@linux_only
def test_cognito_scopes_are_distinct_cli_values(tmp_path):
    scopes = ["tenant.read", "tenant.audit"]
    expected_scopes = sorted(scopes)
    authorizer = {
        "id": "auth1",
        "name": "tenant-pool",
        "type": "COGNITO_USER_POOLS",
        "providerARNs": [
            f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/pool1"
        ],
    }
    kit, result = _kit(
        tmp_path,
        authorization_type="COGNITO_USER_POOLS",
        authorizer_id="auth1",
        authorizer_name="tenant-pool",
        authorization_scopes=scopes,
    )
    env, log, state_path = _env(
        tmp_path, authorizers={"items": [authorizer]}
    )
    run = _run(kit, result, "apply", env)
    assert run.returncode == 0, run.stdout + run.stderr
    call = next(
        item
        for item in _calls(log)
        if "\tapigateway\tput-method\t" in f"\t{item}\t" and "\tGET\t" in item
    )
    argv = call.split("\t")
    start = argv.index("--authorization-scopes") + 1
    assert argv[start : start + len(scopes)] == expected_scopes
    resource = next(
        item
        for item in _read_state(state_path)["resources"]["items"]
        if item["path"] == PATH
    )
    assert resource["resourceMethods"]["GET"]["authorizationScopes"] == expected_scopes


@linux_only
def test_pending_change_is_rejected_before_any_write(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(
        tmp_path,
        resources={
            "items": [
                {"id": "root", "path": "/", "resourceMethods": {"GET": {}}},
                {
                    "id": "other",
                    "path": "/somebody-elses-route",
                    "resourceMethods": {"POST": {}},
                },
            ]
        },
    )
    run = _run(kit, result, "apply", env)
    assert run.returncode == 47, run.stdout + run.stderr
    assert "mutable API differs from the formal deployment" in run.stderr
    calls = _calls(log)
    writes = (
        "create-resource",
        "put-method",
        "put-integration",
        "put-method-response",
        "put-integration-response",
        "add-permission",
        "create-deployment",
        "create-stage",
        "update-stage",
        "delete-stage",
    )
    assert not any(any(write in call for write in writes) for call in calls)
    state = _read_state(state_path)
    assert _formal_deployment(state) == "dep0"
    assert set(state["stages"]) == {STAGE}


@pytest.mark.parametrize(
    "method",
    [
        {
            "authorizationType": "NONE",
            "apiKeyRequired": False,
            "methodIntegration": {
                "type": "HTTP",
                "httpMethod": "GET",
                "uri": "https://somebody-else.example",
            },
        },
        {"authorizationType": "AWS_IAM", "apiKeyRequired": False},
        None,
    ],
    ids=["integration-changed", "auth-changed", "method-deleted"],
)
@linux_only
def test_apply_refuses_structural_third_party_changes(tmp_path, method):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(tmp_path, resources=_root_resources(method))
    run = _run(kit, result, "apply", env)
    assert run.returncode == 47, run.stdout + run.stderr
    assert not any(
        write in call
        for call in _calls(log)
        for write in (
            "create-resource",
            "put-method",
            "put-integration",
            "add-permission",
            "create-deployment",
            "update-stage",
        )
    )
    assert _formal_deployment(_read_state(state_path)) == "dep0"


@linux_only
def test_api_key_source_drift_is_rejected_before_formal_switch(tmp_path):
    kit, result = _kit(tmp_path)
    rest_api = {
        "id": API,
        "name": "openclaw-orchestrator",
        "apiKeySource": "AUTHORIZER",
        "endpointConfiguration": {"types": ["EDGE"], "ipAddressType": "ipv4"},
        "disableExecuteApiEndpoint": False,
        "securityPolicy": "TLS_1_0",
    }
    env, log, state_path = _env(tmp_path, rest_api=rest_api)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 47, run.stdout + run.stderr
    assert "restApi" in run.stderr
    assert not any("update-stage" in call for call in _calls(log))
    state = _read_state(state_path)
    assert _formal_deployment(state) == "dep0"
    assert set(state["stages"]) == {STAGE}


@linux_only
def test_create_pre_post_concurrent_change_refuses_the_stage_switch(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(tmp_path, mutate_after_create_once=True)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 47, run.stdout + run.stderr
    assert "differs from the pre-create projection" in run.stderr
    calls = _calls(log)
    assert not any("update-stage" in call for call in calls)
    state = _read_state(state_path)
    assert _formal_deployment(state) == "dep0"
    assert set(state["deployments"]) == {"dep0", "dep1"}
    assert set(state["stages"]) == {STAGE}


@linux_only
def test_formal_export_read_failure_rolls_back_only_the_stage_pointer(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(
        tmp_path, fail_formal_export_after_switch_once=True
    )
    run = _run(kit, result, "apply", env)
    assert run.returncode == 41, run.stdout + run.stderr
    state = _read_state(state_path)
    assert _formal_deployment(state) == "dep0"
    assert any(item["path"] == PATH for item in state["resources"]["items"])
    assert state["policy"] is not None
    calls = _calls(log)
    assert len([call for call in calls if "update-stage" in call]) == 2
    assert not any("create-stage" in call or "delete-stage" in call for call in calls)


@linux_only
def test_formal_export_semantic_mismatch_rolls_back_the_stage_pointer(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(
        tmp_path, corrupt_formal_export_after_switch=True
    )
    run = _run(kit, result, "apply", env)
    assert run.returncode == 47, run.stdout + run.stderr
    assert "deployed projection differs from pre-create projection" in run.stderr
    state = _read_state(state_path)
    assert _formal_deployment(state) == "dep0"
    assert any(item["path"] == PATH for item in state["resources"]["items"])
    assert state["policy"] is not None
    assert len([call for call in _calls(log) if "update-stage" in call]) == 2


@linux_only
def test_a_conflicting_method_not_ours_is_refused(tmp_path):
    """put-method conflicts and this patch has no recorded resource id, so it is someone else's
    method and the run refuses rather than repointing their traffic."""
    kit, result = _kit(tmp_path)
    env, log, _ = _env(tmp_path, method_conflict=True, method_auth="AWS_IAM")
    run = _run(kit, result, "apply", env)
    assert run.returncode == 49, run.stdout + run.stderr
    assert "repoints their traffic" in run.stderr
    assert not any("create-deployment" in c for c in _calls(log))


@linux_only
def test_a_foreign_method_conflict_never_becomes_owned_on_retry(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, _ = _env(tmp_path, method_conflict=True)
    first = _run(kit, result, "apply", env)
    second = _run(kit, result, "apply", env)
    assert first.returncode == 49, first.stdout + first.stderr
    assert second.returncode == 49, second.stdout + second.stderr
    assert not any("put-integration" in call for call in _calls(log))


@linux_only
def test_active_stage_canary_is_refused(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, _ = _env(tmp_path, canary_percent=10)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 47, run.stdout + run.stderr
    assert "canary" in run.stderr
    assert not any("put-method" in call for call in _calls(log))


@linux_only
def test_permission_conflict_requires_the_exact_statement(tmp_path):
    kit, result = _kit(tmp_path)
    wrong = _exact_policy(
        result,
        source_arn=f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{API}/other/GET{PATH}",
    )
    env, log, _ = _env(tmp_path, perm_conflict=True, policy=wrong)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 49, run.stdout + run.stderr
    assert "does not grant" in run.stderr
    assert not any("update-stage" in call for call in _calls(log))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("An error occurred (ThrottlingException)", 41),
        ("An error occurred (AccessDeniedException)", 49),
        ("socket closed unexpectedly", 46),
    ],
)
@linux_only
def test_policy_read_failures_keep_the_shared_exit_classes(
    tmp_path, message, expected
):
    kit, result = _kit(tmp_path)
    env, _, _ = _env(
        tmp_path,
        perm_conflict=True,
        policy=_exact_policy(result),
        fail_once={"get-policy": message},
    )
    run = _run(kit, result, "apply", env)
    assert run.returncode == expected, run.stdout + run.stderr


@linux_only
def test_verify_checks_the_deployed_integration_not_the_config(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, _ = _env(tmp_path)
    assert _run(kit, result, "apply", env).returncode == 0
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "VERIFIED" in verify.stdout


@linux_only
def test_backend_500_is_observation_not_a_route_lane_failure(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, _ = _env(tmp_path, backend_code="500", backend_body='{"error":"ddb"}')
    apply = _run(kit, result, "apply", env)
    verify = _run(kit, result, "verify", env)
    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "status=500" in apply.stdout


@linux_only
def test_real_http_probe_passes_and_asserts_request_and_response(tmp_path):
    headers_file = tmp_path / "probe-headers.json"
    headers_file.write_text(
        json.dumps({"Content-Type": "application/json", "X-Probe": "route-kit"})
    )
    probe = {
        "method": "POST",
        "headers_file_sha256": hashlib.sha256(headers_file.read_bytes()).hexdigest(),
        "body": {"probe": True},
        "expected_status": 201,
        "expected_body_fields": {"ok": True, "result": {"kind": "route"}},
    }
    kit, result = _kit(tmp_path, probe=probe)
    env, log, _ = _env(
        tmp_path,
        backend_code="201",
        backend_body='{"ok":true,"result":{"kind":"route","id":"p1"}}',
    )
    env["OC_PATCH_HTTP_HEADERS_FILE"] = str(headers_file)
    apply = _run(kit, result, "apply", env)
    verify = _run(kit, result, "verify", env)
    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "HTTP_PROBE_VERIFIED method=POST status=201" in apply.stdout
    curl_calls = [call for call in _calls(log) if call.startswith("curl\t")]
    assert any("--request\tPOST" in call for call in curl_calls)
    assert any("--header\tX-Probe: route-kit" in call for call in curl_calls)
    assert any("--data-binary\t@" in call for call in curl_calls)


@linux_only
def test_missing_http_headers_rolls_back_the_stage_switch(tmp_path):
    headers_file = tmp_path / "probe-headers.json"
    headers_file.write_text(json.dumps({"X-Probe": "route-kit"}))
    probe = {
        "method": "GET",
        "headers_file_sha256": hashlib.sha256(headers_file.read_bytes()).hexdigest(),
        "body": None,
        "expected_status": 200,
        "expected_body_fields": {"ok": True},
    }
    kit, result = _kit(tmp_path, probe=probe)
    env, _, state_path = _env(tmp_path)
    env.pop("OC_PATCH_HTTP_HEADERS_FILE", None)
    apply = _run(kit, result, "apply", env)
    assert apply.returncode == 43, apply.stdout + apply.stderr
    assert "OC_PATCH_HTTP_HEADERS_FILE is required" in apply.stderr
    assert "ROLLED_BACK_FAILED_SWITCH" in apply.stderr
    assert _formal_deployment(_read_state(state_path)) == "dep0"


@pytest.mark.parametrize(
    ("backend_code", "backend_body", "message"),
    [
        ("500", '{"ok":true,"result":{"kind":"route"}}', "expected HTTP 201"),
        ("201", '{"ok":false,"result":{"kind":"route"}}', "body field"),
    ],
)
@linux_only
def test_real_http_probe_failure_exits_43(
    tmp_path, backend_code, backend_body, message
):
    headers_file = tmp_path / "probe-headers.json"
    headers_file.write_text(json.dumps({"X-Probe": "route-kit"}))
    probe = {
        "method": "POST",
        "headers_file_sha256": hashlib.sha256(headers_file.read_bytes()).hexdigest(),
        "body": {"probe": True},
        "expected_status": 201,
        "expected_body_fields": {"ok": True},
    }
    kit, result = _kit(tmp_path, probe=probe)
    env, _, _ = _env(
        tmp_path, backend_code=backend_code, backend_body=backend_body
    )
    env["OC_PATCH_HTTP_HEADERS_FILE"] = str(headers_file)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 43, run.stdout + run.stderr
    assert message in run.stderr


@linux_only
def test_catch_all_http_200_cannot_hide_a_missing_deployed_route(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path, force_http_response=True, backend_code="200")
    assert _run(kit, result, "apply", env).returncode == 0
    state = _read_state(state_path)
    deployment = state["deployments"][_formal_deployment(state)]
    deployment["doc"]["paths"].pop(PATH)
    deployment["summary"]["apiSummary"].pop(PATH)
    _write_state(state_path, state)
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 40, verify.stdout + verify.stderr
    assert "no GET /tenants-stats" in verify.stderr


@linux_only
def test_verify_fails_when_the_deployed_integration_points_elsewhere(tmp_path):
    """The current config can be correct while the DEPLOYED stage serves a different integration;
    verify reads the deployed definition, so it catches that."""
    kit, result = _kit(tmp_path)
    env, _, state = _env(tmp_path)
    assert _run(kit, result, "apply", env).returncode == 0
    payload = json.loads(state.read_text())
    # Deployed stage now serves the route pointing at a DIFFERENT function.
    deployment = payload["deployments"][_formal_deployment(payload)]
    deployment["doc"]["paths"][PATH]["get"][
        "x-amazon-apigateway-integration"
    ]["uri"] = "arn:aws:apigateway:x:lambda:path/.../function:someone-else/invocations"
    state.write_text(json.dumps(payload))
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 40, verify.stdout + verify.stderr
    assert "wrong integration, auth, or API-key requirement" in verify.stderr


@linux_only
def test_verify_rejects_wrong_deployed_api_key_requirement(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    assert _run(kit, result, "apply", env).returncode == 0
    state = _read_state(state_path)
    deployment = state["deployments"][_formal_deployment(state)]
    deployment["summary"]["apiSummary"][PATH]["GET"]["apiKeyRequired"] = False
    _write_state(state_path, state)
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 47, verify.stdout + verify.stderr
    assert "API-key security disagrees" in verify.stderr


@linux_only
def test_verify_rejects_wrong_deployed_options_cors(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    assert _run(kit, result, "apply", env).returncode == 0
    state = _read_state(state_path)
    deployment = state["deployments"][_formal_deployment(state)]
    options = deployment["doc"]["paths"][PATH]["options"]
    response = options["x-amazon-apigateway-integration"]["responses"]["default"]
    response["responseParameters"][
        "method.response.header.Access-Control-Allow-Origin"
    ] = "'https://wrong.example'"
    _write_state(state_path, state)
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 40, verify.stdout + verify.stderr
    assert f"deployed OPTIONS {PATH} has the wrong CORS contract" in verify.stderr


@linux_only
def test_verify_fails_when_stage_no_longer_points_at_recorded_deployment(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    applied = _run(kit, result, "apply", env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    state = _read_state(state_path)
    recorded = _formal_deployment(state)
    state["deployments"]["dep2"] = json.loads(
        json.dumps(state["deployments"][recorded])
    )
    state["stages"][STAGE]["deploymentId"] = "dep2"
    _write_state(state_path, state)
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 40, verify.stdout + verify.stderr
    assert "must still point at recorded deployment" in verify.stderr
    assert "dep2" in verify.stderr and recorded in verify.stderr


@linux_only
def test_crash_after_deployment_id_is_recorded_reuses_the_same_deployment(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(tmp_path, fail_live_read_after_create_once=True)
    first = _run(kit, result, "apply", env)
    assert first.returncode == 46, first.stdout + first.stderr
    state = _read_state(state_path)
    assert _formal_deployment(state) == "dep0"
    assert set(state["deployments"]) == {"dep0", "dep1"}
    assert set(state["stages"]) == {STAGE}
    deployment_files = list(tmp_path.glob("st/**/deployment_id"))
    assert len(deployment_files) == 1
    assert deployment_files[0].read_text() == "dep1"

    second = _run(kit, result, "apply", env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "RESUME deployment=dep1" in second.stdout
    creates = [call for call in _calls(log) if "create-deployment" in call]
    assert len(creates) == 1


@linux_only
def test_crash_after_stage_switch_resumes_without_a_second_deployment(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(tmp_path)
    first = _run(kit, result, "apply", env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _formal_deployment(_read_state(state_path)) == "dep1"
    applied = next(tmp_path.glob("st/**/applied"))
    applied.rename(applied.with_name("crash-before-applied-marker"))

    second = _run(kit, result, "apply", env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "RESUME stage v1 already points at deployment dep1" in second.stdout
    creates = [call for call in _calls(log) if "create-deployment" in call]
    assert len(creates) == 1


@linux_only
def test_verify_reads_deployed_auth_not_mutable_method_auth(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    assert _run(kit, result, "apply", env).returncode == 0
    state = _read_state(state_path)
    resource = next(item for item in state["resources"]["items"] if item["path"] == PATH)
    resource["resourceMethods"]["GET"]["authorizationType"] = "AWS_IAM"
    _write_state(state_path, state)
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 0, verify.stdout + verify.stderr


@linux_only
def test_verify_rejects_wrong_deployed_auth_even_if_mutable_auth_is_right(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    assert _run(kit, result, "apply", env).returncode == 0
    state = _read_state(state_path)
    deployment = state["deployments"][_formal_deployment(state)]
    deployment["summary"]["apiSummary"][PATH]["GET"][
        "authorizationType"
    ] = "AWS_IAM"
    _write_state(state_path, state)
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 47, verify.stdout + verify.stderr
    assert "AWS_IAM security" in verify.stderr


@linux_only
def test_completed_rerun_reobserves_the_permission(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    assert _run(kit, result, "apply", env).returncode == 0
    state = _read_state(state_path)
    state["policy"] = None
    _write_state(state_path, state)
    rerun = _run(kit, result, "apply", env)
    assert rerun.returncode == 40, rerun.stdout + rerun.stderr
    assert "no exact" in rerun.stderr


@linux_only
def test_apply_does_not_depend_on_an_environment_only_lock(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    run = _run(kit, result, "apply", env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert _formal_deployment(_read_state(state_path)) == "dep1"


@linux_only
def test_rollback_does_not_depend_on_an_environment_only_lock(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, state_path = _env(tmp_path)
    applied = _run(kit, result, "apply", env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    rollback = _run(kit, result, "rollback", env)
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    state = _read_state(state_path)
    assert _formal_deployment(state) == "dep0"
    assert any(item["path"] == PATH for item in state["resources"]["items"])
    assert state["policy"] is not None


@linux_only
def test_switch_and_rollback_reread_the_formal_stage_around_each_write(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(tmp_path)
    apply = _run(kit, result, "apply", env)
    rollback = _run(kit, result, "rollback", env)
    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    calls = _calls(log)
    writes = [
        index for index, call in enumerate(calls) if "\tapigateway\tupdate-stage\t" in f"\t{call}\t"
    ]
    assert len(writes) == 2
    for index in writes:
        before = calls[index - 1]
        after = calls[index + 1]
        assert "apigateway\tget-stage" in before and f"--stage-name\t{STAGE}" in before
        assert "apigateway\tget-stage" in after and f"--stage-name\t{STAGE}" in after
    assert "value=dep1" in calls[writes[0]]
    assert "value=dep0" in calls[writes[1]]
    assert _formal_deployment(_read_state(state_path)) == "dep0"
    assert list(tmp_path.glob("st/**/recycle/applied.*"))


@linux_only
def test_completed_second_apply_skips_without_a_new_deployment(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, _ = _env(tmp_path)
    first = _run(kit, result, "apply", env)
    second = _run(kit, result, "apply", env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "SKIP GET /tenants-stats remains deployed" in second.stdout
    creates = [call for call in _calls(log) if "create-deployment" in call]
    assert len(creates) == 1


@linux_only
def test_review_fingerprint_gets_a_distinct_state_directory(tmp_path):
    kit, result = _kit(tmp_path)
    env, _, _ = _env(tmp_path)
    first = _run(kit, result, "apply", env)
    assert first.returncode == 0, first.stdout + first.stderr
    first_state = (
        tmp_path
        / "st"
        / ACCOUNT
        / REGION
        / "114-tenants-stats-route"
        / ("f" * 40)
        / ("a" * 64)
        / result["resource_id"]
    )
    assert (first_state / "applied").exists()

    (kit / "REVIEW.json").write_text(
        json.dumps({"kit_fingerprint": "b" * 64})
    )
    verify = _run(kit, result, "verify", env)
    assert verify.returncode == 44, verify.stdout + verify.stderr
    second_state = (
        tmp_path
        / "st"
        / ACCOUNT
        / REGION
        / "114-tenants-stats-route"
        / ("f" * 40)
        / ("b" * 64)
        / result["resource_id"]
    )
    assert second_state != first_state
    assert second_state.exists()
    assert not (second_state / "applied").exists()


@linux_only
def test_third_party_stage_move_before_switch_is_refused(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state_path = _env(
        tmp_path, move_formal_stage_before_switch_once=True
    )
    run = _run(kit, result, "apply", env)
    assert run.returncode == 40, run.stdout + run.stderr
    assert "third-party stage move" in run.stderr
    state = _read_state(state_path)
    assert _formal_deployment(state) == "foreign"
    assert len([call for call in _calls(log) if "create-deployment" in call]) == 1
    assert not any("update-stage" in call for call in _calls(log))
    assert set(state["stages"]) == {STAGE}


@linux_only
def test_rollback_dry_run_needs_no_credentials(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, _ = _env(tmp_path)
    del env["OC_PATCH_REGION"]
    run = _run(kit, result, "rollback", env, args=("--dry-run",))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "does NOT delete the route configuration" in run.stdout
    assert not _calls(log), "the dry run must not call AWS"


@linux_only
def test_the_wrong_account_is_refused_before_any_write(tmp_path):
    kit, result = _kit(tmp_path)
    env, log, state = _env(tmp_path)
    payload = json.loads(state.read_text())
    payload["account"] = "999988887777"
    state.write_text(json.dumps(payload))
    run = _run(kit, result, "apply", env)
    assert run.returncode == 3, run.stdout + run.stderr
    assert "Refusing to touch the wrong account" in run.stderr
    for c in _calls(log):
        assert "create-deployment" not in c and "put-method" not in c
