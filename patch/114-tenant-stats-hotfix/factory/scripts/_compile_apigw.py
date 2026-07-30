#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compile an API Gateway REST route into Bash apply/verify/rollback.

The generated lane creates an unattached Deployment, then moves only the formal
Stage pointer. It never creates a temporary Stage. The mutable API and every
deployed export are normalized by the same shipped apigw-snapshot.py bytes.
"""

import base64
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path


SUPPORTED_KINDS = {"lambda-proxy-route"}
PROBE_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
_PROBE_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_PROBE_KEYS = {
    "method",
    "headers",
    "body",
    "expected_status",
    "expected_body_fields",
}


_PROJECTION_DIFF_PY = r"""
import copy
import json
import re
import sys


def fail(message):
    print("UNPROVABLE: " + message, file=sys.stderr)
    raise SystemExit(47)


def load(path, label):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print("%s parse: %s" % (label, exc), file=sys.stderr)
        raise SystemExit(46)
    if not isinstance(value, dict):
        print("%s must be an object" % label, file=sys.stderr)
        raise SystemExit(46)
    return value


def changed_keys(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return []
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def compare_exact(left, right):
    if left == right:
        return
    keys = changed_keys(left, right)
    fail("full API projections differ; changed top-level fields: %s" % (keys or ["<value>"]))


def path_parameters(path):
    values = [value.removesuffix("+") for value in re.findall(r"\{([^{}]+)\}", path)]
    if len(values) != len(set(values)):
        fail("target path repeats a path parameter: %s" % path)
    return sorted(values)


def prefixes(path):
    parts = [part for part in path.split("/") if part]
    current = ""
    result = []
    for part in parts:
        current += "/" + part
        result.append(current)
    return result


def compare_route_closure(baseline, candidate, path, method, expected):
    method = method.upper()
    if set(expected) != {method, "OPTIONS"}:
        fail("expected target contract must contain exactly %s and OPTIONS" % method)

    baseline_outer = copy.deepcopy(baseline)
    candidate_outer = copy.deepcopy(candidate)
    baseline_paths = baseline_outer.pop("paths", {})
    candidate_paths = candidate_outer.pop("paths", {})
    if baseline_outer != candidate_outer:
        fail(
            "projection differs outside paths; changed top-level fields: %s"
            % changed_keys(baseline_outer, candidate_outer)
        )
    if not isinstance(baseline_paths, dict) or not isinstance(candidate_paths, dict):
        fail("projection paths must be objects")

    allowed_prefixes = set(prefixes(path))
    for candidate_path in sorted(set(baseline_paths) | set(candidate_paths)):
        if candidate_path == path:
            continue
        before = baseline_paths.get(candidate_path)
        after = candidate_paths.get(candidate_path)
        if before is None and candidate_path in allowed_prefixes:
            expected_resource = {
                "pathParameters": path_parameters(candidate_path),
                "methods": {},
            }
            if after != expected_resource:
                fail(
                    "new path resource %s is not an empty required prefix"
                    % candidate_path
                )
            continue
        if before != after:
            fail("path %s changed outside the target route closure" % candidate_path)

    before_target = baseline_paths.get(path)
    after_target = candidate_paths.get(path)
    if after_target is None or not isinstance(after_target, dict):
        fail("target path %s is missing" % path)
    if before_target is None:
        before_target = {"pathParameters": path_parameters(path), "methods": {}}
    if not isinstance(before_target, dict):
        fail("baseline target path %s is invalid" % path)
    before_methods = before_target.get("methods", {})
    after_methods = after_target.get("methods", {})
    if not isinstance(before_methods, dict) or not isinstance(after_methods, dict):
        fail("target methods must be objects")
    if method in before_methods or "OPTIONS" in before_methods:
        fail("baseline already owns the target business method or OPTIONS")

    wanted_methods = copy.deepcopy(before_methods)
    wanted_methods.update(expected)
    if after_target.get("pathParameters") != before_target.get("pathParameters"):
        fail("target path parameters changed outside the allowed new-path closure")
    if after_methods != wanted_methods:
        fail("target route contract is not exact")
    if set(after_target) != {"pathParameters", "methods"}:
        fail("target path projection has unknown fields")


def review_fingerprint(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle).get("kit_fingerprint")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        print("REVIEW.json is unreadable: %s" % exc, file=sys.stderr)
        raise SystemExit(44)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        print("REVIEW.json has no valid final kit_fingerprint", file=sys.stderr)
        raise SystemExit(44)
    print(value)


def main(argv):
    if len(argv) == 3 and argv[1] == "review-fingerprint":
        review_fingerprint(argv[2])
        return 0
    if len(argv) < 4:
        print(
            "usage: projection-diff.py exact LEFT RIGHT | "
            "route-closure BASELINE CANDIDATE PATH METHOD EXPECTED_JSON",
            file=sys.stderr,
        )
        return 46
    mode = argv[1]
    left = load(argv[2], "left projection")
    right = load(argv[3], "right projection")
    if mode == "exact":
        if len(argv) != 4:
            return 46
        compare_exact(left, right)
        return 0
    if mode != "route-closure" or len(argv) != 7:
        return 46
    try:
        expected = json.loads(argv[6])
    except json.JSONDecodeError as exc:
        print("expected route parse: %s" % exc, file=sys.stderr)
        return 46
    if not isinstance(expected, dict):
        return 46
    compare_route_closure(left, right, argv[4], argv[5], expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
"""


def _q(value):
    return shlex.quote(str(value))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def apigw_recipe_id(api_id, path, method):
    digest = hashlib.sha256(f"{api_id}:{path}:{method}".encode()).hexdigest()[:10]
    return f"apigw-{digest}"


def _normalized_integration(
    *,
    integration_type,
    http_method=None,
    uri=None,
    request_templates=None,
    passthrough="WHEN_NO_MATCH",
    responses=None,
):
    return {
        "type": integration_type,
        "httpMethod": http_method,
        "uri": uri,
        "credentials": None,
        "cacheNamespace": "$resource",
        "cacheKeyParameters": [],
        "connectionType": "INTERNET",
        "connectionId": None,
        "integrationTarget": None,
        "requestParameters": {},
        "requestTemplates": request_templates or {},
        "passthroughBehavior": passthrough,
        "contentHandling": None,
        "timeoutInMillis": 29000,
        "responseTransferMode": "BUFFERED",
        "tlsConfig": {"insecureSkipVerification": False},
        "integrationResponses": responses or {},
    }


def expected_route_methods(spec):
    method = str(spec["method"]).upper()
    qualifier = spec.get("target_qualifier")
    function_arn = (
        f"arn:aws:lambda:{spec['target_region']}:{spec['target_account']}:"
        f"function:{spec['target_function']}"
    )
    if qualifier:
        function_arn += f":{qualifier}"
    uri = (
        f"arn:aws:apigateway:{spec['target_region']}:lambda:path/2015-03-31/"
        f"functions/{function_arn}/invocations"
    )
    cors = spec["cors"]
    headers = ",".join(cors["allow_headers"])
    methods = ",".join(cors["allow_methods"])
    origin = cors["allow_origin"]
    response_parameters = {
        "method.response.header.Access-Control-Allow-Headers": f"'{headers}'",
        "method.response.header.Access-Control-Allow-Methods": f"'{methods}'",
        "method.response.header.Access-Control-Allow-Origin": f"'{origin}'",
    }
    base_method = {
        "authorizationType": spec.get("authorization_type") or "NONE",
        "apiKeyRequired": bool(spec["api_key_required"]),
        "authorizer": None,
        "authorizationScopes": [],
        "operationName": None,
        "requestParameters": {},
        "requestModels": {},
        "requestValidator": None,
        "methodResponses": {},
        "integration": _normalized_integration(
            integration_type="AWS_PROXY",
            http_method="POST",
            uri=uri,
        ),
    }
    options_method = {
        "authorizationType": "NONE",
        "apiKeyRequired": False,
        "authorizer": None,
        "authorizationScopes": [],
        "operationName": None,
        "requestParameters": {},
        "requestModels": {},
        "requestValidator": None,
        "methodResponses": {
            "200": {
                "responseParameters": {
                    "method.response.header.Access-Control-Allow-Headers": True,
                    "method.response.header.Access-Control-Allow-Methods": True,
                    "method.response.header.Access-Control-Allow-Origin": True,
                },
                "responseModels": {},
            }
        },
        "integration": _normalized_integration(
            integration_type="MOCK",
            request_templates={"application/json": '{"statusCode": 200}'},
            passthrough="NEVER",
            responses={
                "default": {
                    "statusCode": "200",
                    "responseParameters": response_parameters,
                    "responseTemplates": {},
                    "contentHandling": None,
                }
            },
        ),
    }
    return {method: base_method, "OPTIONS": options_method}


def _validate_probe(spec):
    if "probe" not in spec:
        return
    probe = spec["probe"]
    if not isinstance(probe, dict):
        raise SystemExit("api_routes[0].probe must be an object")
    missing = _PROBE_KEYS - set(probe)
    extra = set(probe) - _PROBE_KEYS
    if missing or extra:
        raise SystemExit(
            "api_routes[0].probe must declare exactly "
            f"{sorted(_PROBE_KEYS)} (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    probe_method = probe["method"]
    if not isinstance(probe_method, str) or probe_method not in PROBE_METHODS:
        raise SystemExit(
            "api_routes[0].probe.method must be one of "
            f"{sorted(PROBE_METHODS)}"
        )
    headers = probe["headers"]
    if not isinstance(headers, dict):
        raise SystemExit("api_routes[0].probe.headers must be an object")
    for name, value in headers.items():
        if not isinstance(name, str) or not _PROBE_HEADER_NAME_RE.fullmatch(name):
            raise SystemExit(
                f"api_routes[0].probe header name {name!r} is not a valid HTTP token"
            )
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise SystemExit(
                f"api_routes[0].probe header {name!r} must be a single-line string"
            )
    status = probe["expected_status"]
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
    ):
        raise SystemExit(
            "api_routes[0].probe.expected_status must be an integer from 100 through 599"
        )
    if not isinstance(probe["expected_body_fields"], dict):
        raise SystemExit(
            "api_routes[0].probe.expected_body_fields must be an object"
        )
    for label in ("body", "expected_body_fields"):
        try:
            json.dumps(probe[label], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SystemExit(
                f"api_routes[0].probe.{label} must be valid JSON: {exc}"
            ) from exc


def _common(manifest, spec):
    api_key_required = bool(spec["api_key_required"])
    api_key_flag = "--api-key-required" if api_key_required else "--no-api-key-required"
    cors = spec["cors"]
    cors_headers = ",".join(cors["allow_headers"])
    cors_methods = ",".join(cors["allow_methods"])
    cors_origin = cors["allow_origin"]
    cors_method_responses = json.dumps(
        {
            "method.response.header.Access-Control-Allow-Headers": True,
            "method.response.header.Access-Control-Allow-Methods": True,
            "method.response.header.Access-Control-Allow-Origin": True,
        },
        separators=(",", ":"),
    )
    cors_integration_responses = json.dumps(
        {
            "method.response.header.Access-Control-Allow-Headers": f"'{cors_headers}'",
            "method.response.header.Access-Control-Allow-Methods": f"'{cors_methods}'",
            "method.response.header.Access-Control-Allow-Origin": f"'{cors_origin}'",
        },
        separators=(",", ":"),
    )
    cors_request_templates = json.dumps(
        {"application/json": '{"statusCode": 200}'}, separators=(",", ":")
    )
    expected_route = json.dumps(
        expected_route_methods(spec), separators=(",", ":"), sort_keys=True
    )
    probe = spec.get("probe")
    if probe is not None:
        probe_enabled = "true"
        probe_method = probe["method"].upper()
        probe_headers = " ".join(
            _q(f"{name}: {value}") for name, value in probe["headers"].items()
        )
        probe_has_body = str(probe["body"] is not None).lower()
        probe_body_b64 = base64.b64encode(
            json.dumps(probe["body"], separators=(",", ":")).encode()
        ).decode()
        probe_expected_status = str(probe["expected_status"])
        probe_expected_fields_b64 = base64.b64encode(
            json.dumps(
                probe["expected_body_fields"], separators=(",", ":")
            ).encode()
        ).decode()
    else:
        probe_enabled = "false"
        probe_method = ""
        probe_headers = ""
        probe_has_body = "false"
        probe_body_b64 = ""
        probe_expected_status = ""
        probe_expected_fields_b64 = ""
    deployment_owner = (
        f"ocpatch:{manifest['id']}:{manifest['patch_sha']}:"
        f"{apigw_recipe_id(spec['api_id'], spec['path'], spec['method'])}"
    )
    return f"""\
ARTIFACT_ID={_q(manifest["id"])}
CONTENT_VERSION={_q(manifest["patch_sha"])}
API_ID={_q(spec["api_id"])}
TARGET_ACCOUNT={_q(spec["target_account"])}
TARGET_REGION={_q(spec["target_region"])}
STAGE={_q(spec["stage"])}
ROUTE_PATH={_q(spec["path"])}
METHOD={_q(str(spec["method"]).upper())}
TARGET_FUNCTION={_q(spec["target_function"])}
TARGET_QUALIFIER={_q(spec.get("target_qualifier") or "")}
AUTHORIZATION={_q(spec.get("authorization_type") or "NONE")}
API_KEY_REQUIRED={_q(str(api_key_required).lower())}
API_KEY_FLAG={_q(api_key_flag)}
CORS_ALLOW_ORIGIN={_q(cors_origin)}
CORS_ALLOW_HEADERS={_q(cors_headers)}
CORS_ALLOW_METHODS={_q(cors_methods)}
CORS_METHOD_RESPONSES={_q(cors_method_responses)}
CORS_INTEGRATION_RESPONSES={_q(cors_integration_responses)}
CORS_REQUEST_TEMPLATES={_q(cors_request_templates)}
EXPECTED_ROUTE_JSON={_q(expected_route)}
RESOURCE_ID={_q(apigw_recipe_id(spec["api_id"], spec["path"], spec["method"]))}
DEPLOYMENT_OWNER={_q(deployment_owner)}
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
PROJECTION_DIFF_HELPER="$SCRIPT_DIR/projection-diff.py"
APIGW_SNAPSHOT_HELPER="$SCRIPT_DIR/apigw-snapshot.py"
REVIEW_RECEIPT="$SCRIPT_DIR/../../../REVIEW.json"
PROBE_ENABLED={probe_enabled}
PROBE_METHOD={_q(probe_method)}
PROBE_HEADERS=({probe_headers})
PROBE_HAS_BODY={probe_has_body}
PROBE_BODY_B64={_q(probe_body_b64)}
PROBE_EXPECTED_STATUS={_q(probe_expected_status)}
PROBE_EXPECTED_FIELDS_B64={_q(probe_expected_fields_b64)}

REGION="${{OC_PATCH_REGION:?OC_PATCH_REGION required}}"
[[ -f "$PROJECTION_DIFF_HELPER" && -f "$APIGW_SNAPSHOT_HELPER" \\
    && -f "$REVIEW_RECEIPT" ]] || {{
  echo "FATAL: compiled helper or final REVIEW.json is missing" >&2
  exit 44
}}
KIT_FINGERPRINT="$(python3 "$PROJECTION_DIFF_HELPER" review-fingerprint \\
  "$REVIEW_RECEIPT")" || {{
  rc=$?
  exit "$rc"
}}
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
LIVE_ACCOUNT="$(aws sts get-caller-identity --region "$REGION" --query Account --output text \\
  2>/dev/null || true)"
[[ "$LIVE_ACCOUNT" =~ ^[0-9]{{12}}$ ]] || {{
  echo "FATAL: cannot read the live account from STS - refusing to act blind" >&2
  exit 3
}}
[[ "$LIVE_ACCOUNT" == "$TARGET_ACCOUNT" ]] || {{
  echo "FATAL: this kit targets account $TARGET_ACCOUNT but the credentials are for" >&2
  echo "       $LIVE_ACCOUNT. Refusing to touch the wrong account." >&2
  exit 3
}}
[[ "$REGION" == "$TARGET_REGION" ]] || {{
  echo "FATAL: this kit targets $TARGET_REGION but OC_PATCH_REGION is $REGION." >&2
  exit 3
}}
ACCOUNT_ID="$LIVE_ACCOUNT"
STATE_ROOT="${{OC_PATCH_STATE_ROOT:-${{HOME:-/tmp}}/.oc-patch-apigw}}"
STATE_DIR="${{STATE_ROOT}}/${{ACCOUNT_ID}}/${{REGION}}/${{ARTIFACT_ID}}/${{CONTENT_VERSION}}/${{KIT_FINGERPRINT}}/${{RESOURCE_ID}}"
umask 077
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
PERM_SID="ocpatch-${{RESOURCE_ID}}"
PERM_FN="${{TARGET_FUNCTION}}${{TARGET_QUALIFIER:+:$TARGET_QUALIFIER}}"
PERM_PATH="$(printf '%s' "$ROUTE_PATH" | sed -E 's/\\{{[^{{}}]+\\}}/*/g')"
PERM_ARN="arn:aws:execute-api:${{REGION}}:${{ACCOUNT_ID}}:${{API_ID}}/${{STAGE}}/${{METHOD}}${{PERM_PATH}}"
PERM_RESOURCE_ARN="arn:aws:lambda:${{REGION}}:${{ACCOUNT_ID}}:function:${{PERM_FN}}"
DEPLOY_VISIBLE_TIMEOUT="${{OC_PATCH_APIGW_TIMEOUT:-120}}"
READ_FAILED_RC=46

integration_uri() {{
  local fn_arn="arn:aws:lambda:${{REGION}}:${{ACCOUNT_ID}}:function:${{TARGET_FUNCTION}}"
  [[ -n "$TARGET_QUALIFIER" ]] && fn_arn="${{fn_arn}}:${{TARGET_QUALIFIER}}"
  printf 'arn:aws:apigateway:%s:lambda:path/2015-03-31/functions/%s/invocations' \\
    "$REGION" "$fn_arn"
}}

classify_aws_error() {{
  local text="$1"
  if printf '%s' "$text" | grep -qE 'TooManyRequests|ThrottlingException|LimitExceeded|ServiceUnavailable|InternalServerError|InternalFailure|RequestTimeout'; then
    printf '41'
  elif printf '%s' "$text" | grep -qE 'AccessDenied|UnauthorizedException|AuthFailure|InvalidClientTokenId|BadRequestException|ValidationException|ConflictException'; then
    printf '49'
  else
    printf '46'
  fi
}}

read_named_stage_snapshot() {{
  local stage_name="$1" out parsed rc=0
  if ! out="$(aws apigateway get-stage --region "$REGION" --rest-api-id "$API_ID" \\
      --stage-name "$stage_name" --output json 2>&1)"; then
    if printf '%s' "$out" | grep -q 'NotFoundException'; then
      SNAPSHOT_DEPLOYMENT="NOT_FOUND"
      SNAPSHOT_CANARY="NONE"
      SNAPSHOT_DESCRIPTION=""
      return 0
    fi
    printf '%s\\n' "$out" >&2
    return "$(classify_aws_error "$out")"
  fi
  parsed="$(python3 - "$out" <<'PYEOF' || rc=$?
import json
import sys

try:
    stage = json.loads(sys.argv[1])
except Exception as exc:
    print("stage parse failed: %s" % exc, file=sys.stderr)
    raise SystemExit(46)
print(stage.get("deploymentId") or "NONE")
canary = (stage.get("canarySettings") or {{}}).get("percentTraffic")
print("NONE" if canary in (None, 0, 0.0) else canary)
print(stage.get("description") or "")
PYEOF
  )"
  rc="${{rc:-0}}"
  [[ "$rc" -eq 0 ]] || return "$rc"
  SNAPSHOT_DEPLOYMENT="${{parsed%%$'\\n'*}}"
  parsed="${{parsed#*$'\\n'}}"
  SNAPSHOT_CANARY="${{parsed%%$'\\n'*}}"
  SNAPSHOT_DESCRIPTION="${{parsed#*$'\\n'}}"
}}

read_stage_snapshot() {{
  read_named_stage_snapshot "$STAGE" || return $?
  STAGE_DEPLOYMENT="$SNAPSHOT_DEPLOYMENT"
  STAGE_CANARY="$SNAPSHOT_CANARY"
}}

read_stage_deployment() {{
  read_stage_snapshot || {{
    rc=$?
    echo "FATAL: cannot read stage $STAGE of $API_ID - refusing to guess at the rollback target" >&2
    return "$rc"
  }}
}}

assert_no_canary() {{
  read_stage_snapshot || return $?
  [[ "$STAGE_CANARY" == "NONE" ]] && return 0
  echo "FATAL: stage $STAGE sends $STAGE_CANARY percent of traffic to a canary deployment." >&2
  echo "       One export cannot prove both traffic paths." >&2
  return 47
}}

run_snapshot_helper() {{
  local rc=0
  python3 "$APIGW_SNAPSHOT_HELPER" "$@" || rc=$?
  rc="${{rc:-0}}"
  case "$rc" in
    0) return 0 ;;
    2) return 47 ;;
    *) return 46 ;;
  esac
}}

export_stage_projection() {{
  local stage_name="$1" prefix="$2" expected="${{3:-}}"
  local export_file="$STATE_DIR/${{prefix}}-export.json"
  local summary_file="$STATE_DIR/${{prefix}}-summary.json"
  local projection_file="$STATE_DIR/${{prefix}}-projection.json"
  local out dep canary description
  read_named_stage_snapshot "$stage_name" || return $?
  dep="$SNAPSHOT_DEPLOYMENT"
  canary="$SNAPSHOT_CANARY"
  description="$SNAPSHOT_DESCRIPTION"
  [[ "$dep" != "NONE" && "$dep" != "NOT_FOUND" ]] || return 44
  [[ "$canary" == "NONE" ]] || {{
    echo "FATAL: stage $stage_name has canary traffic; one export cannot prove both paths" >&2
    return 47
  }}
  [[ -z "$expected" || "$dep" == "$expected" ]] || {{
    echo "DRIFT: stage $stage_name is on deployment $dep, expected $expected" >&2
    return 40
  }}
  if ! out="$(aws apigateway get-export --region "$REGION" --rest-api-id "$API_ID" \\
      --stage-name "$stage_name" --export-type oas30 \\
      --parameters '{{"extensions":"integrations,authorizers,apigateway"}}' \\
      "$export_file.tmp" 2>&1)"; then
    printf '%s\\n' "$out" >&2
    return "$(classify_aws_error "$out")"
  fi
  if ! out="$(aws apigateway get-deployment --region "$REGION" --rest-api-id "$API_ID" \\
      --deployment-id "$dep" --embed apisummary --output json 2>&1)"; then
    printf '%s\\n' "$out" >&2
    return "$(classify_aws_error "$out")"
  fi
  printf '%s' "$out" > "$summary_file.tmp"
  read_named_stage_snapshot "$stage_name" || return $?
  if [[ "$SNAPSHOT_DEPLOYMENT" != "$dep" || "$SNAPSHOT_CANARY" != "$canary" \\
      || "$SNAPSHOT_DESCRIPTION" != "$description" ]]; then
    echo "FATAL: stage $stage_name changed while its immutable snapshot was being read" >&2
    return 41
  fi
  mv -f "$export_file.tmp" "$export_file"
  mv -f "$summary_file.tmp" "$summary_file"
  run_snapshot_helper from-export "$export_file" "$summary_file" \\
    "$projection_file.tmp" || return $?
  mv -f "$projection_file.tmp" "$projection_file"
  EXPORTED_DEPLOYMENT="$dep"
  EXPORTED_PROJECTION="$projection_file"
}}

proof_read_json() {{
  local destination="$1" out
  shift
  if ! out="$(aws "$@" 2>&1)"; then
    printf '%s\\n' "$out" >&2
    echo "FATAL: projection proof could not read AWS state" >&2
    return 46
  fi
  printf '%s' "$out" > "$destination.tmp"
  mv -f "$destination.tmp" "$destination"
}}

capture_live_projection() {{
  local prefix="$1" base="$STATE_DIR/$1"
  proof_read_json "$base-resources.json" apigateway get-resources --region "$REGION" \\
    --rest-api-id "$API_ID" --embed methods --limit 500 --output json || return 46
  proof_read_json "$base-authorizers.json" apigateway get-authorizers --region "$REGION" \\
    --rest-api-id "$API_ID" --limit 500 --output json || return 46
  proof_read_json "$base-rest-api.json" apigateway get-rest-api --region "$REGION" \\
    --rest-api-id "$API_ID" --output json || return 46
  proof_read_json "$base-models.json" apigateway get-models --region "$REGION" \\
    --rest-api-id "$API_ID" --limit 500 --output json || return 46
  proof_read_json "$base-validators.json" apigateway get-request-validators \\
    --region "$REGION" --rest-api-id "$API_ID" --limit 500 --output json || return 46
  proof_read_json "$base-gateway-responses.json" apigateway get-gateway-responses \\
    --region "$REGION" --rest-api-id "$API_ID" --limit 500 --output json || return 46
  run_snapshot_helper from-live \\
    "$base-resources.json" "$base-authorizers.json" "$base-rest-api.json" \\
    "$base-models.json" "$base-validators.json" "$base-gateway-responses.json" \\
    "$base-projection.json.tmp" || return $?
  mv -f "$base-projection.json.tmp" "$base-projection.json"
  LIVE_PROJECTION="$base-projection.json"
}}

compare_projection_exact() {{
  local left="$1" right="$2" label="$3" out rc=0
  out="$(python3 "$PROJECTION_DIFF_HELPER" exact "$left" "$right" 2>&1)" || rc=$?
  rc="${{rc:-0}}"
  if [[ "$rc" -ne 0 ]]; then
    printf '%s\\n' "$out" >&2
    echo "FATAL: $label" >&2
    [[ "$rc" -eq 47 ]] && return 47
    return 46
  fi
}}

validate_projection_target_closure() {{
  local candidate="$1" label="$2" out rc=0
  out="$(python3 "$PROJECTION_DIFF_HELPER" route-closure \\
      "$STATE_DIR/baseline-projection.json" "$candidate" \\
      "$ROUTE_PATH" "$METHOD" "$EXPECTED_ROUTE_JSON" 2>&1)" || rc=$?
  rc="${{rc:-0}}"
  if [[ "$rc" -ne 0 ]]; then
    printf '%s\\n' "$out" >&2
    echo "FATAL: $label" >&2
    [[ "$rc" -eq 47 ]] && return 47
    return 46
  fi
}}

assert_live_matches_formal_baseline() {{
  export_stage_projection "$STAGE" formal-prewrite "$BASELINE_DEPLOYMENT" || return $?
  capture_live_projection "live-prewrite" || return $?
  compare_projection_exact "$STATE_DIR/formal-prewrite-projection.json" \\
    "$STATE_DIR/live-prewrite-projection.json" \\
    "mutable API differs from the formal deployment before the first write" || return $?
  echo "PENDING_CHECK_OK formal_deployment=$BASELINE_DEPLOYMENT"
}}

assert_live_is_target_closure() {{
  local prefix="$1"
  capture_live_projection "$prefix" || return $?
  validate_projection_target_closure "$STATE_DIR/$prefix-projection.json" \\
    "mutable API is not baseline + the exact target route closure" || return $?
  echo "ROUTE_CLOSURE_OK phase=$prefix"
}}

capture_baseline_snapshot() {{
  local expected=""
  if [[ -f "$STATE_DIR/baseline_deployment" ]]; then
    expected="$(cat "$STATE_DIR/baseline_deployment")"
    if [[ -f "$STATE_DIR/baseline-projection.json" ]]; then
      BASELINE_DEPLOYMENT="$expected"
      return 0
    fi
  fi
  export_stage_projection "$STAGE" baseline "$expected" || return $?
  BASELINE_DEPLOYMENT="$EXPORTED_DEPLOYMENT"
  printf '%s' "$BASELINE_DEPLOYMENT" > "$STATE_DIR/baseline_deployment.tmp"
  mv -f "$STATE_DIR/baseline_deployment.tmp" "$STATE_DIR/baseline_deployment"
}}

read_resource_id() {{
  local out
  if ! out="$(aws apigateway get-resources --region "$REGION" --rest-api-id "$API_ID" \\
      --limit 500 --query "items[?path=='$1'].id | [0]" --output text 2>&1)"; then
    printf '%s\\n' "$out" >&2
    return "$(classify_aws_error "$out")"
  fi
  if [[ -n "$out" && "$out" != "None" ]]; then
    RESOURCE_LOOKUP="$out"
  else
    RESOURCE_LOOKUP=""
  fi
}}

read_integration_uri() {{
  local out
  if out="$(aws apigateway get-integration --region "$REGION" --rest-api-id "$API_ID" \\
      --resource-id "$1" --http-method "$METHOD" --query uri --output text 2>&1)"; then
    INTEGRATION_URI="$out"
    return 0
  fi
  if printf '%s' "$out" | grep -q 'NotFoundException'; then
    INTEGRATION_URI="NONE"
    return 0
  fi
  printf '%s\\n' "$out" >&2
  return "$(classify_aws_error "$out")"
}}

current_method_matches() {{
  local out rc=0
  if ! out="$(aws apigateway get-method --region "$REGION" --rest-api-id "$API_ID" \\
      --resource-id "$1" --http-method "$METHOD" --output json 2>&1)"; then
    printf '%s\\n' "$out" >&2
    return "$(classify_aws_error "$out")"
  fi
  python3 - "$AUTHORIZATION" "$API_KEY_REQUIRED" "$out" <<'PYEOF' || rc=$?
import json
import sys

want_auth, want_key, raw = sys.argv[1:]
try:
    method = json.loads(raw)
except Exception as exc:
    print("method parse failed: %s" % exc, file=sys.stderr)
    raise SystemExit(46)
if (
    method.get("authorizationType", "NONE") != want_auth
    or bool(method.get("apiKeyRequired", False)) != (want_key == "true")
):
    raise SystemExit(40)
PYEOF
  rc="${{rc:-0}}"
  return "$rc"
}}

current_cors_matches() {{
  local require_complete="${{1:-no}}" method_json integration_json rc=0
  if ! method_json="$(aws apigateway get-method --region "$REGION" --rest-api-id "$API_ID" \\
      --resource-id "$RES" --http-method OPTIONS --output json 2>&1)"; then
    printf '%s\\n' "$method_json" >&2
    return "$(classify_aws_error "$method_json")"
  fi
  if ! integration_json="$(aws apigateway get-integration --region "$REGION" \\
      --rest-api-id "$API_ID" --resource-id "$RES" --http-method OPTIONS \\
      --output json 2>&1)"; then
    if printf '%s' "$integration_json" | grep -q 'NotFoundException'; then
      integration_json='{{}}'
    else
      printf '%s\\n' "$integration_json" >&2
      return "$(classify_aws_error "$integration_json")"
    fi
  fi
  python3 - "$require_complete" "$CORS_ALLOW_ORIGIN" "$CORS_ALLOW_HEADERS" \\
      "$CORS_ALLOW_METHODS" "$method_json" "$integration_json" <<'PYEOF' || rc=$?
import json
import sys

complete, origin, headers, methods, method_raw, integration_raw = sys.argv[1:]
try:
    method = json.loads(method_raw)
    integration = json.loads(integration_raw)
except Exception as exc:
    print("CORS state parse failed: %s" % exc, file=sys.stderr)
    raise SystemExit(46)
if (
    method.get("authorizationType", "NONE") != "NONE"
    or bool(method.get("apiKeyRequired", False))
):
    raise SystemExit(40)
expected_method = {{
    "method.response.header.Access-Control-Allow-Headers": True,
    "method.response.header.Access-Control-Allow-Methods": True,
    "method.response.header.Access-Control-Allow-Origin": True,
}}
expected_integration = {{
    "method.response.header.Access-Control-Allow-Headers": "'" + headers + "'",
    "method.response.header.Access-Control-Allow-Methods": "'" + methods + "'",
    "method.response.header.Access-Control-Allow-Origin": "'" + origin + "'",
}}
method_responses = method.get("methodResponses") or {{}}
integration_responses = integration.get("integrationResponses") or {{}}
if method_responses:
    actual = (method_responses.get("200") or {{}}).get("responseParameters") or {{}}
    if actual != expected_method:
        raise SystemExit(40)
if integration:
    if (
        str(integration.get("type", "")).upper() != "MOCK"
        or str(integration.get("passthroughBehavior", "")).upper() != "NEVER"
        or (integration.get("requestTemplates") or {{}}).get("application/json")
        != '{{"statusCode": 200}}'
    ):
        raise SystemExit(40)
if integration_responses:
    response = integration_responses.get("200") or integration_responses.get("default") or {{}}
    if (
        str(response.get("statusCode", "")) != "200"
        or (response.get("responseParameters") or {{}}) != expected_integration
    ):
        raise SystemExit(40)
if complete == "yes" and (
    not integration or "200" not in method_responses or not integration_responses
):
    raise SystemExit(40)
PYEOF
  rc="${{rc:-0}}"
  return "$rc"
}}

projection_route_matches() {{
  local projection="$1" rc=0
  python3 - "$projection" "$ROUTE_PATH" "$METHOD" "$EXPECTED_ROUTE_JSON" <<'PYEOF' || rc=$?
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        projection = json.load(handle)
    expected = json.loads(sys.argv[4])
except Exception as exc:
    print("deployed route parse failed: %s" % exc, file=sys.stderr)
    raise SystemExit(46)
path, method = sys.argv[2], sys.argv[3].upper()
actual_methods = ((projection.get("paths") or {{}}).get(path) or {{}}).get("methods") or {{}}
business = actual_methods.get(method)
if business is None:
    raise SystemExit(4)
if business != expected.get(method):
    raise SystemExit(5)
if actual_methods.get("OPTIONS") != expected.get("OPTIONS"):
    raise SystemExit(6)
PYEOF
  rc="${{rc:-0}}"
  return "$rc"
}}

deployed_route_matches() {{
  local stage_name="${{1:-$STAGE}}" expected="${{2:-}}" prefix rc
  prefix="route-${{stage_name}}"
  export_stage_projection "$stage_name" "$prefix" "$expected" || return $?
  projection_route_matches "$STATE_DIR/$prefix-projection.json" || rc=$?
  rc="${{rc:-0}}"
  return "$rc"
}}

require_deployed_route() {{
  local expected="${{1:-}}" rc
  if deployed_route_matches "$STAGE" "$expected"; then
    return 0
  else
    rc=$?
  fi
  case "$rc" in
    4) echo "DRIFT: deployed stage has no $METHOD $ROUTE_PATH" >&2; return 40 ;;
    5) echo "DRIFT: deployed $METHOD $ROUTE_PATH has the wrong integration, auth, or API-key requirement" >&2; return 40 ;;
    6) echo "DRIFT: deployed OPTIONS $ROUTE_PATH has the wrong CORS contract" >&2; return 40 ;;
    *) return "$rc" ;;
  esac
}}

policy_grants_invoke() {{
  local out rc
  if ! out="$(aws lambda get-policy --region "$REGION" --function-name "$PERM_FN" \\
      --query Policy --output text 2>&1)"; then
    if printf '%s' "$out" | grep -q 'ResourceNotFoundException'; then
      return 1
    fi
    printf '%s\\n' "$out" >&2
    return "$(classify_aws_error "$out")"
  fi
  python3 - "$PERM_SID" "$PERM_ARN" "$PERM_RESOURCE_ARN" "$out" <<'PYEOF' || rc=$?
import json
import sys

sid, source_arn, resource_arn, raw = sys.argv[1:]
try:
    policy = json.loads(raw)
except Exception as exc:
    print("Lambda policy parse failed: %s" % exc, file=sys.stderr)
    raise SystemExit(46)
statements = policy.get("Statement") or []
if isinstance(statements, dict):
    statements = [statements]
for statement in statements:
    if statement.get("Sid") != sid:
        continue
    action = statement.get("Action") or []
    if isinstance(action, str):
        action = [action]
    principal = statement.get("Principal") or {{}}
    service = principal.get("Service") if isinstance(principal, dict) else None
    if isinstance(service, str):
        service = [service]
    condition = statement.get("Condition") or {{}}
    arn_like = condition.get("ArnLike") or condition.get("ArnEquals") or {{}}
    source = arn_like.get("AWS:SourceArn") or arn_like.get("aws:SourceArn")
    resources = statement.get("Resource") or []
    if isinstance(resources, str):
        resources = [resources]
    if (
        statement.get("Effect") == "Allow"
        and "lambda:InvokeFunction" in action
        and "apigateway.amazonaws.com" in (service or [])
        and source == source_arn
        and resource_arn in resources
    ):
        raise SystemExit(0)
raise SystemExit(1)
PYEOF
  rc="${{rc:-0}}"
  return "$rc"
}}

require_invoke_policy() {{
  local rc
  if policy_grants_invoke; then
    return 0
  else
    rc=$?
  fi
  if [[ "$rc" -eq 1 ]]; then
    echo "DRIFT: Lambda policy has no exact $PERM_SID grant for $PERM_ARN" >&2
    return 40
  fi
  return "$rc"
}}

read_deployment_owner() {{
  local deployment_id="$1" out rc=0
  if ! out="$(aws apigateway get-deployment --region "$REGION" --rest-api-id "$API_ID" \\
      --deployment-id "$deployment_id" --output json 2>&1)"; then
    if printf '%s' "$out" | grep -q 'NotFoundException'; then
      return 44
    fi
    printf '%s\\n' "$out" >&2
    return "$(classify_aws_error "$out")"
  fi
  DEPLOYMENT_DESCRIPTION="$(python3 - "$out" <<'PYEOF' || rc=$?
import json
import sys
try:
    value = json.loads(sys.argv[1])
except Exception as exc:
    print("deployment parse failed: %s" % exc, file=sys.stderr)
    raise SystemExit(46)
print(value.get("description") or "")
PYEOF
  )"
  rc="${{rc:-0}}"
  return "$rc"
}}

validate_recorded_deployment() {{
  local deployment_id="$1"
  [[ -f "$STATE_DIR/pre-create-projection.json" ]] || {{
    echo "FATAL: recorded deployment has no pre-create projection anchor" >&2
    return 44
  }}
  validate_projection_target_closure "$STATE_DIR/pre-create-projection.json" \\
    "recorded pre-create projection is not the exact target closure" || return $?
  read_deployment_owner "$deployment_id" || return $?
  [[ "$DEPLOYMENT_DESCRIPTION" == "$DEPLOYMENT_OWNER" ]] || {{
    echo "FATAL: recorded deployment $deployment_id is not owned by this kit" >&2
    return 47
  }}
}}

recycle_applied_marker() {{
  [[ -f "$STATE_DIR/applied" ]] || return 0
  mkdir -p "$STATE_DIR/recycle"
  mv -f "$STATE_DIR/applied" "$STATE_DIR/recycle/applied.$(date +%s).$$"
}}

INVOKE_URL="${{OC_PATCH_APIGW_URL:-https://${{API_ID}}.execute-api.${{REGION}}.amazonaws.com/${{STAGE}}}}"

observe_http() {{
  local code rc body_file="$STATE_DIR/http-observation.body"
  code="$(curl -s -o "$body_file" -w '%{{http_code}}' --max-time 15 \\
    "${{INVOKE_URL}}${{ROUTE_PATH}}" 2>/dev/null)" && rc=0 || rc=$?
  echo "HTTP_OBSERVATION_ONLY curl_rc=$rc status=${{code:-000}} claim=definition-only"
  return 0
}}

run_http_probe() {{
  local code rc=0 header
  local request_file="$STATE_DIR/http-probe.request"
  local body_file="$STATE_DIR/http-probe.body"
  local -a curl_args
  curl_args=(curl -sS -o "$body_file" -w '%{{http_code}}' --max-time 15 \\
    --request "$PROBE_METHOD")
  for header in "${{PROBE_HEADERS[@]-}}"; do
    [[ -n "$header" ]] || continue
    curl_args+=(--header "$header")
  done
  if [[ "$PROBE_HAS_BODY" == "true" ]]; then
    if ! python3 - "$PROBE_BODY_B64" "$request_file" <<'PYEOF'
import base64
import sys
try:
    body = base64.b64decode(sys.argv[1], validate=True)
    with open(sys.argv[2], "wb") as handle:
        handle.write(body)
except Exception as exc:
    print("HTTP probe request body decode failed: %s" % exc, file=sys.stderr)
    raise SystemExit(43)
PYEOF
    then
      return 43
    fi
    curl_args+=(--data-binary "@$request_file")
  fi
  if code="$("${{curl_args[@]}}" "${{INVOKE_URL}}${{ROUTE_PATH}}")"; then
    :
  else
    rc=$?
    echo "HTTP probe transport failed: curl_rc=$rc" >&2
    return 43
  fi
  python3 - "$code" "$PROBE_EXPECTED_STATUS" "$body_file" \\
      "$PROBE_EXPECTED_FIELDS_B64" <<'PYEOF' || rc=$?
import base64
import json
import sys

actual_status, expected_status, body_path, expected_fields_raw = sys.argv[1:]
if actual_status != expected_status:
    print(
        "HTTP probe expected HTTP %s, got %s" % (expected_status, actual_status),
        file=sys.stderr,
    )
    raise SystemExit(43)
try:
    with open(body_path) as handle:
        actual = json.load(handle)
    expected = json.loads(base64.b64decode(expected_fields_raw, validate=True))
except Exception as exc:
    print("HTTP probe response parse failed: %s" % exc, file=sys.stderr)
    raise SystemExit(43)

def require_fields(value, wanted, path="$"):
    if isinstance(wanted, dict):
        if not isinstance(value, dict):
            print("HTTP probe body field %s is not an object" % path, file=sys.stderr)
            raise SystemExit(43)
        for key, expected_value in wanted.items():
            child = path + "." + key
            if key not in value:
                print("HTTP probe body field %s is missing" % child, file=sys.stderr)
                raise SystemExit(43)
            require_fields(value[key], expected_value, child)
    elif value != wanted:
        print(
            "HTTP probe body field %s expected %r, got %r"
            % (path, wanted, value),
            file=sys.stderr,
        )
        raise SystemExit(43)

require_fields(actual, expected)
PYEOF
  rc="${{rc:-0}}"
  [[ "$rc" -eq 0 ]] || return 43
  echo "HTTP_PROBE_VERIFIED method=$PROBE_METHOD status=$code"
}}

verify_http_contract() {{
  if [[ "$PROBE_ENABLED" == "true" ]]; then
    run_http_probe
  else
    observe_http
  fi
}}
"""


def _apply(common):
    return f"""#!/usr/bin/env bash
set -euo pipefail
{common}

rollback_failed_switch() {{
  local failed_rc="$1" out rb_rc
  read_stage_deployment || return $?
  if [[ "$STAGE_DEPLOYMENT" != "$NEW_DEP" ]]; then
    echo "DRIFT: post-switch verification failed and stage moved to $STAGE_DEPLOYMENT;" >&2
    echo "       refusing to overwrite a third-party stage move" >&2
    return 40
  fi
  if ! out="$(aws apigateway update-stage --region "$REGION" --rest-api-id "$API_ID" \\
      --stage-name "$STAGE" \\
      --patch-operations "op=replace,path=/deploymentId,value=$BASELINE_DEPLOYMENT" \\
      2>&1 >/dev/null)"; then
    printf '%s\\n' "$out" >&2
    rb_rc="$(classify_aws_error "$out")"
    echo "FATAL: post-switch verification failed and automatic stage rollback also failed" >&2
    return "$rb_rc"
  fi
  read_stage_deployment || return $?
  [[ "$STAGE_DEPLOYMENT" == "$BASELINE_DEPLOYMENT" ]] || {{
    echo "FATAL: failed deployment validation and stage did not return to baseline" >&2
    return 43
  }}
  echo "ROLLED_BACK_FAILED_SWITCH $STAGE -> $BASELINE_DEPLOYMENT; rc=$failed_rc" >&2
  echo "NOTE route configuration and Lambda permission are intentionally retained" >&2
  return 0
}}

if capture_baseline_snapshot; then
  :
else
  rc=$?
  if [[ "$rc" -eq 44 ]]; then
    echo "FATAL: stage $STAGE on $API_ID has no deployment to use as a rollback anchor" >&2
  fi
  exit "$rc"
fi
base_dep="$BASELINE_DEPLOYMENT"
echo "BASELINE stage=$STAGE deployment=$base_dep"

assert_no_canary || exit $?
read_stage_deployment || exit $?
recorded_dep=""
[[ ! -f "$STATE_DIR/deployment_id" ]] || recorded_dep="$(cat "$STATE_DIR/deployment_id")"

if [[ -f "$STATE_DIR/applied" ]]; then
  [[ -n "$recorded_dep" && -f "$STATE_DIR/pre-create-projection.json" ]] || {{
    echo "FATAL: applied marker has no recorded deployment/projection anchor" >&2
    exit 44
  }}
  want_dep="$(cat "$STATE_DIR/applied")"
  [[ "$want_dep" == "$recorded_dep" ]] || {{
    echo "DRIFT: applied deployment $want_dep disagrees with recorded $recorded_dep" >&2
    exit 40
  }}
  [[ "$STAGE_DEPLOYMENT" == "$recorded_dep" ]] || {{
    echo "DRIFT: stage $STAGE must still point at recorded deployment $recorded_dep;" >&2
    echo "       it now points at $STAGE_DEPLOYMENT" >&2
    exit 40
  }}
  validate_recorded_deployment "$recorded_dep" || exit $?
  export_stage_projection "$STAGE" completed "$recorded_dep" || exit $?
  compare_projection_exact "$STATE_DIR/pre-create-projection.json" \\
    "$STATE_DIR/completed-projection.json" \\
    "recorded deployment no longer exports the full pre-create projection" || exit $?
  validate_projection_target_closure "$STATE_DIR/completed-projection.json" \\
    "recorded deployment no longer has the exact target closure" || exit $?
  projection_route_matches "$STATE_DIR/completed-projection.json" || {{
    rc=$?
    echo "DRIFT: deployed route contract is no longer exact" >&2
    [[ "$rc" -eq 46 ]] && exit 46
    exit 40
  }}
  require_invoke_policy || exit $?
  verify_http_contract || exit $?
  echo "SKIP $METHOD $ROUTE_PATH remains deployed with the exact integration and invoke grant"
  exit 0
fi

if [[ -n "$recorded_dep" ]]; then
  validate_recorded_deployment "$recorded_dep" || exit $?
  [[ "$STAGE_DEPLOYMENT" == "$base_dep" || "$STAGE_DEPLOYMENT" == "$recorded_dep" ]] || {{
    echo "DRIFT: stage $STAGE is on $STAGE_DEPLOYMENT, not baseline $base_dep or this" >&2
    echo "       kit's deployment $recorded_dep. Refusing a third-party stage move." >&2
    exit 40
  }}
  NEW_DEP="$recorded_dep"
  echo "RESUME deployment=$NEW_DEP"
  require_invoke_policy || exit $?
  if [[ "$STAGE_DEPLOYMENT" == "$base_dep" ]]; then
    capture_live_projection "post-create" || exit $?
    compare_projection_exact "$STATE_DIR/pre-create-projection.json" \\
      "$STATE_DIR/post-create-projection.json" \\
      "mutable API differs from the pre-create projection after create-deployment" || exit $?
    echo "LIVE_PROJECTION_STABLE deployment=$NEW_DEP"
  fi
else
  [[ "$STAGE_DEPLOYMENT" == "$base_dep" ]] || {{
    echo "DRIFT: stage $STAGE moved away from baseline $base_dep before configuration" >&2
    exit 40
  }}
  if [[ ! -f "$STATE_DIR/configuration_started" ]]; then
    assert_live_matches_formal_baseline || exit $?
    printf '%s' "$base_dep" > "$STATE_DIR/configuration_started.tmp"
    mv -f "$STATE_DIR/configuration_started.tmp" "$STATE_DIR/configuration_started"
  fi

  read_resource_id "$ROUTE_PATH" || exit $?
  existing="$RESOURCE_LOOKUP"
  if [[ -n "$existing" ]]; then
    read_integration_uri "$existing" || exit $?
    cur="$INTEGRATION_URI"
    if [[ -n "$cur" && "$cur" != "None" && "$cur" != "NONE" ]]; then
      owned="$(cat "$STATE_DIR/method_owned" 2>/dev/null || true)"
      if [[ "$cur" == "$(integration_uri)" && "$owned" == "$existing" ]]; then
        echo "RESUME $METHOD $ROUTE_PATH was created by an earlier run of this patch"
      else
        echo "DRIFT: $METHOD $ROUTE_PATH already exists and integrates $cur" >&2
        echo "       This patch did not create it; repointing it would move someone else's traffic." >&2
        exit 40
      fi
    fi
  fi

  read_resource_id "/" || exit $?
  parent="$RESOURCE_LOOKUP"
  [[ -n "$parent" ]] || {{
    echo "FATAL: cannot find the root resource of $API_ID" >&2
    exit 3
  }}
  accum=""
  IFS='/' read -r -a segments <<< "${{ROUTE_PATH#/}}"
  for seg in "${{segments[@]}}"; do
    [[ -n "$seg" ]] || continue
    accum="${{accum}}/${{seg}}"
    read_resource_id "$accum" || exit $?
    found="$RESOURCE_LOOKUP"
    if [[ -z "$found" ]]; then
      if ! found="$(aws apigateway create-resource --region "$REGION" \\
          --rest-api-id "$API_ID" --parent-id "$parent" --path-part "$seg" \\
          --query id --output text 2>&1)"; then
        printf '%s\\n' "$found" >&2
        echo "FATAL: could not create the resource segment '$seg' of $ROUTE_PATH" >&2
        exit "$(classify_aws_error "$found")"
      fi
      echo "CREATED resource $accum ($found)"
    fi
    parent="$found"
  done
  PRIOR_RES="$(cat "$STATE_DIR/method_owned" 2>/dev/null || true)"
  RES="$parent"

  if method_out="$(aws apigateway put-method --region "$REGION" --rest-api-id "$API_ID" \\
      --resource-id "$RES" --http-method "$METHOD" --authorization-type "$AUTHORIZATION" \\
      "$API_KEY_FLAG" 2>&1)"; then
    printf '%s' "$RES" > "$STATE_DIR/method_owned.tmp"
    mv -f "$STATE_DIR/method_owned.tmp" "$STATE_DIR/method_owned"
    printf '%s' "$RES" > "$STATE_DIR/resource_id"
    echo "CREATED method $METHOD $ROUTE_PATH (authorization=$AUTHORIZATION)"
  elif printf '%s' "$method_out" | grep -q 'ConflictException'; then
    if [[ -n "$PRIOR_RES" && "$PRIOR_RES" == "$RES" ]]; then
      current_method_matches "$RES" || {{
        rc=$?
        echo "DRIFT: patch-owned method no longer has the declared auth/API-key contract" >&2
        exit "$rc"
      }}
      echo "RESUME method $METHOD $ROUTE_PATH was created by an earlier run of this patch"
    else
      echo "FATAL: $METHOD $ROUTE_PATH already exists and this patch has no successful" >&2
      echo "       put-method ownership marker. Attaching an integration to someone else's" >&2
      echo "       method repoints their traffic. Resolve by hand." >&2
      exit 49
    fi
  else
    printf '%s\\n' "$method_out" >&2
    echo "FATAL: could not put the method $METHOD $ROUTE_PATH" >&2
    exit "$(classify_aws_error "$method_out")"
  fi

  if ! int_out="$(aws apigateway put-integration --region "$REGION" \\
      --rest-api-id "$API_ID" --resource-id "$RES" --http-method "$METHOD" \\
      --type AWS_PROXY --integration-http-method POST --uri "$(integration_uri)" \\
      2>&1 >/dev/null)"; then
    printf '%s\\n' "$int_out" >&2
    echo "FATAL: could not put the integration for $METHOD $ROUTE_PATH" >&2
    exit "$(classify_aws_error "$int_out")"
  fi
  echo "CONFIGURED $METHOD $ROUTE_PATH -> $TARGET_FUNCTION${{TARGET_QUALIFIER:+:$TARGET_QUALIFIER}}"

  PRIOR_OPTIONS_RES="$(cat "$STATE_DIR/options_owned" 2>/dev/null || true)"
  if options_out="$(aws apigateway put-method --region "$REGION" \\
      --rest-api-id "$API_ID" --resource-id "$RES" --http-method OPTIONS \\
      --authorization-type NONE --no-api-key-required 2>&1)"; then
    printf '%s' "$RES" > "$STATE_DIR/options_owned.tmp"
    mv -f "$STATE_DIR/options_owned.tmp" "$STATE_DIR/options_owned"
    echo "CREATED method OPTIONS $ROUTE_PATH (authorization=NONE api_key_required=false)"
  elif printf '%s' "$options_out" | grep -q 'ConflictException'; then
    if [[ -z "$PRIOR_OPTIONS_RES" || "$PRIOR_OPTIONS_RES" != "$RES" ]]; then
      echo "FATAL: OPTIONS $ROUTE_PATH exists without this patch's ownership marker." >&2
      echo "       Refusing to overwrite someone else's CORS contract." >&2
      exit 49
    fi
    current_cors_matches no || {{
      rc=$?
      echo "DRIFT: patch-owned OPTIONS $ROUTE_PATH no longer matches this kit" >&2
      exit "$rc"
    }}
    echo "RESUME method OPTIONS $ROUTE_PATH was created by an earlier run"
  else
    printf '%s\\n' "$options_out" >&2
    echo "FATAL: could not put OPTIONS $ROUTE_PATH" >&2
    exit "$(classify_aws_error "$options_out")"
  fi

  if ! cors_int_out="$(aws apigateway put-integration --region "$REGION" \\
      --rest-api-id "$API_ID" --resource-id "$RES" --http-method OPTIONS --type MOCK \\
      --passthrough-behavior NEVER --request-templates "$CORS_REQUEST_TEMPLATES" 2>&1)"; then
    printf '%s\\n' "$cors_int_out" >&2
    echo "FATAL: could not configure the OPTIONS mock integration" >&2
    exit "$(classify_aws_error "$cors_int_out")"
  fi
  if ! cors_method_out="$(aws apigateway put-method-response --region "$REGION" \\
      --rest-api-id "$API_ID" --resource-id "$RES" --http-method OPTIONS \\
      --status-code 200 --response-parameters "$CORS_METHOD_RESPONSES" 2>&1)"; then
    if ! printf '%s' "$cors_method_out" | grep -q 'ConflictException'; then
      printf '%s\\n' "$cors_method_out" >&2
      echo "FATAL: could not configure the OPTIONS method response" >&2
      exit "$(classify_aws_error "$cors_method_out")"
    fi
    current_cors_matches no || exit $?
  fi
  if ! cors_response_out="$(aws apigateway put-integration-response --region "$REGION" \\
      --rest-api-id "$API_ID" --resource-id "$RES" --http-method OPTIONS \\
      --status-code 200 --response-parameters "$CORS_INTEGRATION_RESPONSES" 2>&1)"; then
    if ! printf '%s' "$cors_response_out" | grep -q 'ConflictException'; then
      printf '%s\\n' "$cors_response_out" >&2
      echo "FATAL: could not configure the OPTIONS integration response" >&2
      exit "$(classify_aws_error "$cors_response_out")"
    fi
  fi
  current_cors_matches yes || {{
    rc=$?
    echo "DRIFT: OPTIONS $ROUTE_PATH is incomplete or does not match the declared CORS contract" >&2
    exit "$rc"
  }}
  echo "CONFIGURED OPTIONS $ROUTE_PATH CORS origin=$CORS_ALLOW_ORIGIN"

  assert_live_is_target_closure postwrite || exit $?

  if perm_out="$(aws lambda add-permission --region "$REGION" --function-name "$PERM_FN" \\
      --statement-id "$PERM_SID" --action lambda:InvokeFunction \\
      --principal apigateway.amazonaws.com --source-arn "$PERM_ARN" 2>&1)"; then
    echo "GRANTED invoke permission $PERM_SID -> $PERM_ARN"
  elif printf '%s' "$perm_out" | grep -q 'ResourceConflictException'; then
    if policy_grants_invoke; then
      echo "SKIP invoke permission $PERM_SID already grants $PERM_ARN"
    else
      policy_rc=$?
      [[ "$policy_rc" -eq 1 ]] || exit "$policy_rc"
      echo "FATAL: statement $PERM_SID exists but does not grant $PERM_ARN, so the route would" >&2
      echo "       return 500. Remove the stale statement and rerun:" >&2
      echo "         aws lambda remove-permission --region $REGION \\\\" >&2
      echo "           --function-name $PERM_FN --statement-id $PERM_SID" >&2
      exit 49
    fi
  else
    printf '%s\\n' "$perm_out" >&2
    echo "FATAL: could not grant API Gateway permission to invoke $PERM_FN - the route would" >&2
    echo "       return 500. Nothing was deployed." >&2
    exit "$(classify_aws_error "$perm_out")"
  fi
  require_invoke_policy || exit $?

  assert_live_is_target_closure pre-create || exit $?
  if ! deploy_out="$(aws apigateway create-deployment --region "$REGION" \\
      --rest-api-id "$API_ID" --description "$DEPLOYMENT_OWNER" \\
      --query id --output text 2>&1)"; then
    printf '%s\\n' "$deploy_out" >&2
    echo "FATAL: create-deployment failed. The route CONFIGURATION exists but the formal" >&2
    echo "       stage was not switched, so live traffic is unchanged." >&2
    exit "$(classify_aws_error "$deploy_out")"
  fi
  NEW_DEP="$deploy_out"
  [[ -n "$NEW_DEP" && "$NEW_DEP" != "None" ]] || exit 46
  printf '%s' "$NEW_DEP" > "$STATE_DIR/deployment_id.tmp"
  mv -f "$STATE_DIR/deployment_id.tmp" "$STATE_DIR/deployment_id"
  echo "CREATED unattached deployment=$NEW_DEP"

  capture_live_projection "post-create" || exit $?
  compare_projection_exact "$STATE_DIR/pre-create-projection.json" \\
    "$STATE_DIR/post-create-projection.json" \\
    "mutable API differs from the pre-create projection after create-deployment" || exit $?
  # API Gateway has no mutable-RestApi revision or create-deployment CAS. The A/B reads detect
  # observed changes around creation, but cannot exclude a write-and-revert between reads or a
  # write after B. This is detection, not CAS; post-switch export is the final snapshot proof.
  echo "LIVE_PROJECTION_STABLE deployment=$NEW_DEP"
fi

read_stage_deployment || exit $?
[[ "$STAGE_CANARY" == "NONE" ]] || {{
  echo "FATAL: formal stage $STAGE has canary traffic" >&2
  exit 47
}}
now_dep="$STAGE_DEPLOYMENT"
if [[ "$now_dep" == "$NEW_DEP" ]]; then
  echo "RESUME stage $STAGE already points at deployment $NEW_DEP"
elif [[ "$now_dep" == "$base_dep" ]]; then
  read_stage_deployment || exit $?
  [[ "$STAGE_DEPLOYMENT" == "$base_dep" ]] || {{
    echo "DRIFT: formal stage moved before switch; refusing to overwrite it" >&2
    exit 40
  }}
  if ! stage_out="$(aws apigateway update-stage --region "$REGION" \\
      --rest-api-id "$API_ID" --stage-name "$STAGE" \\
      --patch-operations "op=replace,path=/deploymentId,value=$NEW_DEP" \\
      2>&1 >/dev/null)"; then
    printf '%s\\n' "$stage_out" >&2
    echo "FATAL: deployment $NEW_DEP exists, but stage $STAGE was not switched" >&2
    exit "$(classify_aws_error "$stage_out")"
  fi
  read_stage_deployment || exit $?
  [[ "$STAGE_DEPLOYMENT" == "$NEW_DEP" ]] || {{
    echo "DRIFT: formal stage is $STAGE_DEPLOYMENT after switch, expected $NEW_DEP" >&2
    exit 40
  }}
  echo "SWITCHED stage=$STAGE deployment=$NEW_DEP"
else
  echo "DRIFT: stage $STAGE is on $now_dep, not baseline $base_dep or this patch's" >&2
  echo "       deployment $NEW_DEP. Refusing a third-party stage move." >&2
  exit 40
fi

deadline=$(( $(date +%s) + DEPLOY_VISIBLE_TIMEOUT ))
post_rc=47
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  if export_stage_projection "$STAGE" deployed "$NEW_DEP"; then
    if compare_projection_exact "$STATE_DIR/pre-create-projection.json" \\
        "$STATE_DIR/deployed-projection.json" \\
        "deployed projection differs from pre-create projection"; then
      if validate_projection_target_closure "$STATE_DIR/deployed-projection.json" \\
          "deployed projection does not contain the exact target route closure" \\
          && projection_route_matches "$STATE_DIR/deployed-projection.json"; then
        post_rc=0
        break
      else
        post_rc=$?
      fi
    else
      post_rc=$?
    fi
  else
    post_rc=$?
    break
  fi
  [[ "$post_rc" -eq 47 ]] || break
  sleep 1
done
if [[ "$post_rc" -ne 0 ]]; then
  rollback_failed_switch "$post_rc" || {{
    rb_rc=$?
    [[ "$rb_rc" -eq 40 ]] && exit 40
    exit "$rb_rc"
  }}
  echo "FATAL: formal stage export did not verify the recorded deployment" >&2
  exit "$post_rc"
fi

require_invoke_policy || {{
  post_rc=$?
  rollback_failed_switch "$post_rc" || exit $?
  exit "$post_rc"
}}
verify_http_contract || {{
  post_rc=$?
  rollback_failed_switch "$post_rc" || exit $?
  exit "$post_rc"
}}
printf '%s' "$NEW_DEP" > "$STATE_DIR/applied.tmp"
mv -f "$STATE_DIR/applied.tmp" "$STATE_DIR/applied"
if [[ "$PROBE_ENABLED" == "true" ]]; then
  echo "APPLIED $METHOD $ROUTE_PATH definition and declared HTTP probe are verified"
else
  echo "APPLIED_DEFINITION_VERIFIED $METHOD $ROUTE_PATH on deployed stage $STAGE (no HTTP probe declared)"
fi
"""


def _verify(common):
    return f"""#!/usr/bin/env bash
set -euo pipefail
{common}

# OC_STAGE_BODY_BEGIN
# Read-only. Verification binds the formal stage, the recorded Deployment id, and the complete
# normalized projection captured immediately before create-deployment.
[[ -f "$STATE_DIR/applied" && -f "$STATE_DIR/deployment_id" \\
    && -f "$STATE_DIR/pre-create-projection.json" \\
    && -f "$STATE_DIR/baseline-projection.json" ]] || {{
  echo "FATAL: no patch-owned deployment/projection anchor for $METHOD $ROUTE_PATH" >&2
  exit 44
}}
want_dep="$(cat "$STATE_DIR/applied")"
recorded_dep="$(cat "$STATE_DIR/deployment_id")"
[[ "$want_dep" == "$recorded_dep" ]] || {{
  echo "DRIFT: applied deployment $want_dep disagrees with recorded $recorded_dep" >&2
  exit 40
}}
validate_recorded_deployment "$recorded_dep" || exit $?
assert_no_canary || exit $?
read_stage_deployment || exit $?
[[ "$STAGE_DEPLOYMENT" == "$recorded_dep" ]] || {{
  echo "DRIFT: stage $STAGE must still point at recorded deployment $recorded_dep;" >&2
  echo "       it now points at $STAGE_DEPLOYMENT" >&2
  exit 40
}}
export_stage_projection "$STAGE" verify "$recorded_dep" || exit $?
projection_route_matches "$STATE_DIR/verify-projection.json" || {{
  rc=$?
  case "$rc" in
    4) echo "DRIFT: deployed stage has no $METHOD $ROUTE_PATH" >&2 ;;
    5) echo "DRIFT: deployed $METHOD $ROUTE_PATH has the wrong integration, auth, or API-key requirement" >&2 ;;
    6) echo "DRIFT: deployed OPTIONS $ROUTE_PATH has the wrong CORS contract" >&2 ;;
    *) exit "$rc" ;;
  esac
  exit 40
}}
if compare_projection_exact "$STATE_DIR/pre-create-projection.json" \\
    "$STATE_DIR/verify-projection.json" \\
    "formal stage export differs from the recorded full projection"; then
  :
else
  rc=$?
  [[ "$rc" -eq 47 ]] && exit 40
  exit "$rc"
fi
validate_projection_target_closure "$STATE_DIR/verify-projection.json" \\
  "formal stage export no longer has the exact target route closure" || exit $?
require_deployed_route "$recorded_dep" || exit $?
require_invoke_policy || exit $?
verify_http_contract || exit $?
if [[ "$PROBE_ENABLED" == "true" ]]; then
  echo "VERIFIED deployed definition and declared HTTP probe for $METHOD $ROUTE_PATH"
else
  echo "DEFINITION_VERIFIED deployed $METHOD $ROUTE_PATH (no HTTP probe declared)"
fi
"""


def _rollback(common):
    return f"""#!/usr/bin/env bash
set -uo pipefail
if [[ "${{1:-}}" == "--dry-run" ]]; then
  echo "This would repoint the stage to the deployment recorded before apply,"
  echo "restoring the previous routing exactly. It does NOT delete the route configuration"
  echo "or Lambda permission, and it refuses if a third party has deployed since."
  exit 0
fi
set -e
{common}

[[ -f "$STATE_DIR/baseline_deployment" ]] || {{
  echo "FATAL: no recorded baseline deployment for $API_ID/$STAGE" >&2
  exit 44
}}
base_dep="$(cat "$STATE_DIR/baseline_deployment")"
assert_no_canary || exit $?
read_stage_deployment || exit $?
now_dep="$STAGE_DEPLOYMENT"

if [[ "$now_dep" == "$base_dep" ]]; then
  echo "SKIP_ROLLBACK stage $STAGE already on $base_dep"
  recycle_applied_marker
  exit 0
fi
[[ -f "$STATE_DIR/deployment_id" ]] || {{
  echo "FATAL: no recorded patch deployment for rollback" >&2
  exit 44
}}
applied_dep="$(cat "$STATE_DIR/deployment_id")"
[[ "$now_dep" == "$applied_dep" ]] || {{
  echo "DRIFT: rollback refused; stage is on $now_dep, neither this patch's" >&2
  echo "       ($applied_dep) nor the baseline ($base_dep). Someone else deployed." >&2
  exit 40
}}
read_stage_deployment || exit $?
[[ "$STAGE_DEPLOYMENT" == "$applied_dep" ]] || {{
  echo "DRIFT: stage moved after the rollback gate; refusing to overwrite it" >&2
  exit 40
}}
if ! st_out="$(aws apigateway update-stage --region "$REGION" --rest-api-id "$API_ID" \\
    --stage-name "$STAGE" \\
    --patch-operations "op=replace,path=/deploymentId,value=$base_dep" \\
    2>&1 >/dev/null)"; then
  printf '%s\\n' "$st_out" >&2
  echo "FATAL: could not repoint stage $STAGE to $base_dep. Patched routing remains live." >&2
  exit "$(classify_aws_error "$st_out")"
fi
deadline=$(( $(date +%s) + DEPLOY_VISIBLE_TIMEOUT ))
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  read_stage_deployment || exit $?
  [[ "$STAGE_DEPLOYMENT" == "$base_dep" ]] && break
  sleep 1
done
read_stage_deployment || exit $?
[[ "$STAGE_DEPLOYMENT" == "$base_dep" ]] || {{
  echo "FATAL: stage $STAGE did not return to $base_dep" >&2
  exit 43
}}
recycle_applied_marker
echo "ROLLED_BACK $STAGE -> deployment $base_dep"
echo "NOTE the route CONFIGURATION and Lambda permission still exist; only the deployed"
echo "     stage pointer was reverted, so no traffic reaches the retained route."
"""


def _validate_spec(manifest, spec):
    for key in (
        "api_id",
        "stage",
        "path",
        "method",
        "target_function",
        "authorization_type",
    ):
        if not spec.get(key):
            raise SystemExit(f"api_routes[0].{key} is required")
    if "api_key_required" not in spec or not isinstance(spec["api_key_required"], bool):
        raise SystemExit("api_routes[0].api_key_required must be an explicit boolean")
    if spec["authorization_type"] != "NONE":
        raise SystemExit(
            "api_routes[0].authorization_type must be NONE until authorizer or "
            "AWS_IAM bindings and signed probes are modeled"
        )
    cors = spec.get("cors")
    if not isinstance(cors, dict):
        raise SystemExit("api_routes[0].cors is required")
    for key in ("allow_origin", "allow_headers", "allow_methods"):
        if not cors.get(key):
            raise SystemExit(f"api_routes[0].cors.{key} is required")
    method = str(spec["method"]).upper()
    cors_methods = [str(value).upper() for value in cors["allow_methods"]]
    if method == "OPTIONS":
        raise SystemExit("api_routes[0].method must be the business method, not OPTIONS")
    if "OPTIONS" not in cors_methods or method not in cors_methods:
        raise SystemExit(
            "api_routes[0].cors.allow_methods must contain OPTIONS and the business method"
        )
    for coord in ("target_account", "target_region"):
        if not (spec.get(coord) or "").strip():
            raise SystemExit(
                f"api_routes[0].{coord} is required: the run checks the live caller against it, "
                "so a kit built for one account cannot silently change another."
            )
    if (
        not str(spec["target_account"]).isdigit()
        or len(str(spec["target_account"])) != 12
    ):
        raise SystemExit("api_routes[0].target_account must be a 12-digit account id")
    _validate_probe(spec)
    kind = spec.get("kind") or "lambda-proxy-route"
    if kind not in SUPPORTED_KINDS:
        raise SystemExit(
            f"api_routes[0].kind={kind!r} is not supported (supported: "
            f"{sorted(SUPPORTED_KINDS)}). An authorizer, resource policy or custom-domain change "
            "can lock every caller out of the control plane, including the operator rolling it "
            "back - handle it by hand."
        )
    if not str(spec["path"]).startswith("/"):
        raise SystemExit("api_routes[0].path must start with '/'")
    if manifest.get("status") != "READY":
        raise SystemExit(
            f"preflight: manifest.status={manifest.get('status')} (not READY) - a flagged kit "
            "is not auto-appliable"
        )


def compile_apigw_kit(kit, _repo=None):
    with open(os.path.join(kit, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    specs = manifest.get("api_routes") or []
    if len(specs) != 1:
        raise SystemExit(
            "a compiled API Gateway kit declares exactly one entry in `api_routes` "
            f"(found {len(specs)}) - one route per kit, so each gets its own verify and rollback"
        )
    spec = specs[0]
    _validate_spec(manifest, spec)

    rid = apigw_recipe_id(spec["api_id"], spec["path"], spec["method"])
    common = _common(manifest, spec)
    snapshot_bytes = Path(__file__).with_name("apigw-snapshot.py").read_bytes()
    outputs = (
        ("apply.sh", _apply(common).encode()),
        ("verify.sh", _verify(common).encode()),
        ("rollback.sh", _rollback(common).encode()),
        ("projection-diff.py", _PROJECTION_DIFF_PY.lstrip().encode()),
        ("apigw-snapshot.py", snapshot_bytes),
    )
    written = []
    for name, content in outputs:
        rel = f"lib/compiled/{rid}/{name}"
        dest = os.path.join(kit, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(content)
        if name.endswith(".sh"):
            os.chmod(dest, 0o755)
        manifest.setdefault("kit_files", {})[rel] = {"sha256": _sha(content)}
        written.append(rel)

    with open(os.path.join(kit, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return {
        "resource_id": rid,
        "api_id": spec["api_id"],
        "stage": spec["stage"],
        "route": f"{spec['method']} {spec['path']}",
        "files": written,
    }


def main(argv):
    if len(argv) < 2:
        print("usage: _compile_apigw.py <patch-kit> [<source-repo>]", file=sys.stderr)
        return 2
    print(json.dumps(compile_apigw_kit(os.path.abspath(argv[1])), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
