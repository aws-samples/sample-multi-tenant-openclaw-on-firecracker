#!/usr/bin/env python3
"""Live-state probes for OpenClaw patch qualification and acceptance."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TARGET = 0
BASE = 10
ABSENT = 11
DRIFT = 12
UNKNOWN = 13
LAMBDA_PACKAGE_DOWNLOAD_ATTEMPTS = 3
LAMBDA_PACKAGE_DOWNLOAD_TIMEOUT = 30
_OBSERVATION_EVENTS: list[dict[str, str]] = []


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def record_observation(source: str, value: Any) -> None:
    _OBSERVATION_EVENTS.append(
        {"source": source, "sha256": sha256(canonical(value))}
    )


def aws(*args: str) -> Any:
    command = ["aws", *args, "--no-cli-pager"]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"AWS command failed ({result.returncode}): {result.stderr.strip()}"
        )
    value = json.loads(result.stdout) if result.stdout.strip() else None
    source = "aws:" + ":".join(args[:2])
    record_observation(source, value)
    return value


def classify(observed: dict[str, str | None], expected: dict[str, Any]) -> int:
    target = True
    base = True
    for name, rule in expected.items():
        value = observed.get(name)
        target_hash = rule["target"]
        base_hash = rule.get("base")
        target = target and value == target_hash
        base = base and (
            value == base_hash if base_hash is not None else value is None
        )
    if target:
        return TARGET
    if base:
        return BASE
    return DRIFT


def load_spec(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("spec must be a JSON object")
    return value


def lambda_package(
    function_name: str,
    qualifier: str | None = None,
    initial_function: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    arguments = [
        "lambda",
        "get-function",
        "--function-name",
        function_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    ]
    if qualifier is not None:
        arguments.extend(["--qualifier", qualifier])
    for attempt in range(LAMBDA_PACKAGE_DOWNLOAD_ATTEMPTS):
        function = (
            initial_function
            if attempt == 0 and initial_function is not None
            else aws(*arguments)
        )
        try:
            with urllib.request.urlopen(
                function["Code"]["Location"],
                timeout=LAMBDA_PACKAGE_DOWNLOAD_TIMEOUT,
            ) as response:
                payload = response.read()
        except (ConnectionError, TimeoutError, urllib.error.URLError):
            if attempt + 1 == LAMBDA_PACKAGE_DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(2**attempt)
            continue
        code_sha256 = base64.b64encode(hashlib.sha256(payload).digest()).decode()
        if code_sha256 != function["Configuration"]["CodeSha256"]:
            raise RuntimeError(f"Lambda package changed while reading {function_name}")
        return function, payload
    raise AssertionError("unreachable Lambda package retry state")


def lambda_observation(
    function_name: str, spec: dict[str, Any], qualifier: str | None = None
) -> dict[str, Any]:
    function, payload = lambda_package(function_name, qualifier)
    observed_files: dict[str, str | None] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        for name in spec["files"]:
            observed_files[name] = (
                sha256(archive.read(name)) if name in names else None
            )
    observed = {
        "files": observed_files,
        "variables": function["Configuration"]
        .get("Environment", {})
        .get("Variables", {}),
    }
    record_observation(
        f"lambda-package:{function_name}:{qualifier or '$LATEST'}",
        observed,
    )
    return observed


def lambda_state(function_name: str, spec: dict[str, Any]) -> int:
    observations = [lambda_observation(function_name, spec)]
    qualifier = spec.get("qualifier")
    if qualifier:
        observations.append(lambda_observation(function_name, spec, qualifier))
    if spec.get("esm_enabled"):
        mappings = aws(
            "lambda",
            "list-event-source-mappings",
            "--function-name",
            function_name,
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        )["EventSourceMappings"]
        if not mappings or any(item["State"] != "Enabled" for item in mappings):
            return DRIFT

    code_states = [
        classify(observation["files"], spec["files"])
        for observation in observations
    ]
    target_variables, target_state = resolve_lambda_env_targets(spec)
    if target_state != TARGET:
        return target_state
    if target_variables:
        environment_states = [
            classify_lambda_environment(
                observation["variables"], target_variables
            )
            for observation in observations
        ]
    else:
        environment_states = [TARGET] * len(observations)

    if all(state == TARGET for state in code_states + environment_states):
        return TARGET
    if not target_variables:
        return BASE if all(state == BASE for state in code_states) else DRIFT
    if any(state == DRIFT for state in code_states + environment_states):
        return DRIFT
    if qualifier:
        latest_code, alias_code = code_states
        if alias_code == BASE and latest_code in {BASE, TARGET}:
            return BASE
        return DRIFT
    if code_states[0] == BASE or (
        code_states[0] == TARGET and environment_states[0] == BASE
    ):
        return BASE
    return DRIFT


def classify_lambda_environment(
    variables: dict[str, str], target: dict[str, str]
) -> int:
    if all(variables.get(name) == value for name, value in target.items()):
        return TARGET
    if all(name not in variables for name in target):
        return BASE
    return DRIFT


def resolve_lambda_env_targets(
    spec: dict[str, Any],
) -> tuple[dict[str, str], int]:
    target = dict(spec.get("variables", {}))
    for name, source in spec.get("secret_variables", {}).items():
        secret = optional_aws(
            ("ResourceNotFoundException",),
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            source["secret_id"],
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        )
        if secret is None:
            return {}, BASE
        try:
            payload = json.loads(secret["SecretString"])
            value = payload[source.get("json_key", "key")]
        except (KeyError, TypeError, json.JSONDecodeError):
            return {}, DRIFT
        if not valid_pagination_key(value):
            return {}, DRIFT
        target[name] = value
    return target, TARGET


def s3_observation(bucket: str, spec: dict[str, Any]) -> dict[str, str | None]:
    observed: dict[str, str | None] = {}
    with tempfile.TemporaryDirectory(prefix="claw-patch-v2-s3-") as directory:
        for index, item in enumerate(spec["objects"]):
            destination = Path(directory) / str(index)
            result = subprocess.run(
                [
                    "aws",
                    "s3api",
                    "get-object",
                    "--bucket",
                    bucket,
                    "--key",
                    item["key"],
                    "--region",
                    os.environ["AWS_REGION"],
                    str(destination),
                    "--no-cli-pager",
                ],
                check=False,
                capture_output=True,
            )
            if result.returncode == 0:
                observed[item["key"]] = sha256(destination.read_bytes())
            elif b"NoSuchKey" in result.stderr:
                observed[item["key"]] = None
            else:
                raise RuntimeError(result.stderr.decode(errors="replace"))
    record_observation(f"s3-bundle:{bucket}", observed)
    return observed


def asg_instances(asg_name: str) -> list[str]:
    response = aws(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        asg_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    groups = response["AutoScalingGroups"]
    if len(groups) != 1:
        raise RuntimeError(f"expected one ASG named {asg_name}")
    instances = [
        item["InstanceId"]
        for item in groups[0]["Instances"]
        if item["LifecycleState"] == "InService" and item["HealthStatus"] == "Healthy"
    ]
    if not instances:
        raise RuntimeError(f"ASG {asg_name} has no healthy InService instances")
    return sorted(instances)


def wait_command(command_id: str, instances: list[str]) -> dict[str, str]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = aws(
            "ssm",
            "list-command-invocations",
            "--command-id",
            command_id,
            "--details",
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        )
        invocations = response["CommandInvocations"]
        if len(invocations) == len(instances) and all(
            item["Status"] in {"Success", "Failed", "TimedOut", "Cancelled"}
            for item in invocations
        ):
            outputs: dict[str, str] = {}
            for item in invocations:
                if item["Status"] != "Success":
                    detail = item.get("CommandPlugins", [{}])[0].get("Output", "")
                    raise RuntimeError(
                        f"SSM read probe {command_id} failed on "
                        f"{item['InstanceId']}: {item['Status']}: {detail}"
                    )
                outputs[item["InstanceId"]] = item["CommandPlugins"][0]["Output"]
            return outputs
        time.sleep(2)
    raise RuntimeError("SSM read probe timed out")


def ssm_observation(
    asg_name: str,
    spec: dict[str, Any],
    *,
    instances: list[str] | None = None,
) -> dict[str, str | None]:
    instances = instances or asg_instances(asg_name)
    paths = list(ssm_expected(spec))
    commands = [
        (
            "for f in "
            + " ".join(json.dumps(path) for path in paths)
            + '; do if [ -f "$f" ]; then sha256sum "$f"; '
            + 'else printf "ABSENT  %s\\n" "$f"; fi; done'
        )
    ]
    for service in spec.get("services", []):
        commands.append(
            f'systemctl is-active --quiet {json.dumps(service)} '
            f'&& printf "SERVICE_ACTIVE  {service}\\n"'
        )
    for endpoint in spec.get("http", []):
        required = " ".join(json.dumps(value) for value in endpoint["contains"])
        commands.append(
            f'body=$(curl -fsS --max-time 10 {json.dumps(endpoint["url"])}); '
            f"for value in {required}; do "
            'printf "%s" "$body" | grep -Fq "$value" || exit 41; done; '
            f'printf "HTTP_OK  {endpoint["url"]}\\n"'
        )
    response = aws(
        "ssm",
        "send-command",
        "--document-name",
        "AWS-RunShellScript",
        "--instance-ids",
        *instances,
        "--comment",
        "claw-patch-v2 read-only qualification",
        "--parameters",
        json.dumps({"commands": commands}, separators=(",", ":")),
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    outputs = wait_command(response["Command"]["CommandId"], instances)
    aggregate: dict[str, str | None] = {}
    for instance, output in outputs.items():
        lines = output.splitlines()
        for path in paths:
            match = next(
                (
                    line
                    for line in lines
                    if line.endswith(f"  {path}") or line == f"ABSENT  {path}"
                ),
                None,
            )
            if match is None:
                raise RuntimeError(f"missing hash for {path} on {instance}")
            value = None if match.startswith("ABSENT") else match.split()[0]
            aggregate[f"{instance}:{path}"] = value
        for service in spec.get("services", []):
            if f"SERVICE_ACTIVE  {service}" not in lines:
                raise RuntimeError(f"inactive service {service} on {instance}")
        for endpoint in spec.get("http", []):
            if f"HTTP_OK  {endpoint['url']}" not in lines:
                raise RuntimeError(
                    f"HTTP proof failed for {endpoint['url']} on {instance}"
                )
    record_observation(f"ssm-bundle:{asg_name}", aggregate)
    return aggregate


def expand_ssm_expected(
    observed: dict[str, str | None], spec: dict[str, Any]
) -> dict[str, Any]:
    expected = ssm_expected(spec)
    expanded = {}
    for observed_name in observed:
        _, path = observed_name.split(":", 1)
        expanded[observed_name] = expected[path]
    return expanded


def classify_ssm(
    observed: dict[str, str | None],
    spec: dict[str, Any],
) -> int:
    expected = expand_ssm_expected(observed, spec)
    state = classify(observed, expected)
    if state != DRIFT or spec.get("resume_mixed") is not True:
        return state
    if all(
        value in {expected[name].get("base"), expected[name]["target"]}
        for name, value in observed.items()
    ):
        return BASE
    return DRIFT


def fresh_host_state(asg_name: str, spec: dict[str, Any]) -> int:
    baseline = spec.get("baseline_instances")
    timeout = spec.get("fresh_host_timeout_seconds", 900)
    if (
        not isinstance(baseline, list)
        or not baseline
        or len(baseline) != len(set(baseline))
        or not all(isinstance(item, str) and item for item in baseline)
        or not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 3600
    ):
        raise RuntimeError("fresh-host contract is invalid")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = sorted(set(asg_instances(asg_name)) - set(baseline))
        if len(fresh) > 1:
            return DRIFT
        if len(fresh) == 1:
            try:
                observed = ssm_observation(
                    asg_name,
                    spec,
                    instances=fresh,
                )
            except RuntimeError:
                time.sleep(5)
                continue
            state = classify_ssm(observed, spec)
            if state == TARGET:
                return TARGET
            if state == DRIFT:
                return DRIFT
        time.sleep(5)
    raise RuntimeError("fresh host did not reach functional target")


def ssm_expected(spec: dict[str, Any]) -> dict[str, Any]:
    files = spec["files"]
    if isinstance(files, dict):
        return files
    if not isinstance(files, list):
        raise TypeError("SSM files must be an object or array")
    expected = {}
    for item in files:
        destination = item.get("destination")
        target = item.get("target_sha256")
        if not isinstance(destination, str) or not isinstance(target, str):
            raise TypeError("SSM mutation file misses destination/target_sha256")
        expected[destination] = {
            "base": item.get("base_sha256"),
            "target": target,
        }
    return expected


def api_key_value(key_id: str) -> str:
    value = aws(
        "apigateway",
        "get-api-key",
        "--api-key",
        key_id,
        "--include-value",
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    ).get("value")
    if not value:
        raise RuntimeError("API key has no value")
    return value


def http_json(
    url: str,
    key_id: str,
    *,
    method: str = "GET",
    expected_status: int = 200,
) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=b"{}" if method not in {"GET", "HEAD"} else None,
        headers={
            "x-api-key": api_key_value(key_id),
            "content-type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read()
    if status != expected_status:
        record_observation(
            "http",
            {"status": status, "body_sha256": sha256(body)},
        )
        return status, None
    payload = json.loads(body)
    record_observation(
        "http",
        {"status": status, "body_sha256": sha256(canonical(payload))},
    )
    return status, payload


def http_case_state(
    url: str,
    key_id: str,
    method: str,
    expected_status: str,
    json_types_raw: str,
) -> int:
    try:
        status_expected = int(expected_status)
        json_types = json.loads(json_types_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("HTTP acceptance case is invalid") from None
    status, payload = http_json(
        url,
        key_id,
        method=method,
        expected_status=status_expected,
    )
    if status != status_expected or not isinstance(json_types, dict):
        return DRIFT
    if payload is None:
        return TARGET if not json_types else DRIFT
    for path, expected in json_types.items():
        current: Any = payload
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return DRIFT
            current = current[part]
        actual = (
            "boolean"
            if isinstance(current, bool)
            else "integer"
            if isinstance(current, int)
            else "number"
            if isinstance(current, float)
            else "string"
            if isinstance(current, str)
            else "array"
            if isinstance(current, list)
            else "object"
            if isinstance(current, dict)
            else "null"
            if current is None
            else "unknown"
        )
        if actual != expected:
            return DRIFT
    return TARGET


def tenant_query_state(
    api_function: str,
    tenants_table: str,
    secret_id: str,
    api_base_url: str,
    key_id: str,
) -> int:
    def mismatch(reason: str) -> int:
        print(f"tenant-query mismatch: {reason}", file=sys.stderr)
        return DRIFT

    table = aws(
        "dynamodb",
        "describe-table",
        "--table-name",
        tenants_table,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )["Table"]
    indexes = {
        item["IndexName"]: item["IndexStatus"]
        for item in table.get("GlobalSecondaryIndexes", [])
    }
    expected = {
        "gsi_tenant_user",
        "gsi_host",
        "gsi_status",
        "gsi_rootfs_version",
    }
    if expected - indexes.keys() or any(indexes[name] != "ACTIVE" for name in expected):
        return ABSENT
    secret = aws(
        "secretsmanager",
        "describe-secret",
        "--secret-id",
        secret_id,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if not secret.get("ARN"):
        return ABSENT
    config = aws(
        "lambda",
        "get-function-configuration",
        "--function-name",
        api_function,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    variables = config.get("Environment", {}).get("Variables", {})
    if variables.get("TENANT_QUERY_ENABLED") != "true":
        return BASE
    if not variables.get("PAGINATION_AES_KEY"):
        return mismatch("PAGINATION_AES_KEY is missing")
    first_status, first = http_json(
        f"{api_base_url.rstrip('/')}/hosts?limit=1", key_id
    )
    if first_status != 200 or not isinstance(first, dict):
        return mismatch("first hosts page is not an HTTP 200 object")
    first_items = first.get("hosts")
    cursor = first.get("next_token")
    if not isinstance(first_items, list) or len(first_items) != 1 or not cursor:
        return mismatch("first hosts page lacks one item or next_token")
    second_status, second = http_json(
        f"{api_base_url.rstrip('/')}/hosts?"
        + urllib.parse.urlencode({"limit": 1, "next_token": cursor}),
        key_id,
    )
    if second_status != 200 or not isinstance(second, dict):
        return mismatch("second hosts page is not an HTTP 200 object")
    second_items = second.get("hosts")
    if not isinstance(second_items, list) or len(second_items) != 1:
        return mismatch("second hosts page lacks one item")
    first_id = first_items[0].get("instance_id")
    second_id = second_items[0].get("instance_id")
    if not first_id or not second_id or first_id == second_id:
        return mismatch("hosts pagination repeated or omitted an instance")
    invalid_status, _ = http_json(
        f"{api_base_url.rstrip('/')}/hosts?limit=1&next_token=invalid",
        key_id,
        expected_status=400,
    )
    if invalid_status != 400:
        return mismatch("invalid next_token was not rejected with HTTP 400")
    return TARGET


def optional_aws(not_found: tuple[str, ...], *args: str) -> Any:
    try:
        return aws(*args)
    except RuntimeError as exc:
        if any(marker in str(exc) for marker in not_found):
            return None
        raise


def valid_pagination_key(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 32


def tenant_query_foundation_state(
    table_name: str, spec_path: str
) -> int:
    spec = load_spec(spec_path)
    if spec.get("table") != table_name:
        raise RuntimeError("tenant-query foundation locator mismatch")
    table = aws(
        "dynamodb",
        "describe-table",
        "--table-name",
        table_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )["Table"]
    definitions = {
        item["AttributeName"]: item["AttributeType"]
        for item in table.get("AttributeDefinitions", [])
    }
    indexes = {
        item["IndexName"]: item
        for item in table.get("GlobalSecondaryIndexes", [])
    }
    incomplete = table.get("TableStatus") != "ACTIVE"
    for expected in spec["indexes"]:
        observed = indexes.get(expected["name"])
        if observed is None:
            incomplete = True
            continue
        if (
            observed.get("KeySchema")
            != [
                {
                    "AttributeName": expected["hash_key"],
                    "KeyType": "HASH",
                }
            ]
            or observed.get("Projection") != {"ProjectionType": "ALL"}
            or definitions.get(expected["hash_key"]) != "S"
        ):
            return DRIFT
        if observed.get("IndexStatus") != "ACTIVE":
            incomplete = True
    secret = optional_aws(
        ("ResourceNotFoundException",),
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        spec["secret"],
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if secret is None:
        incomplete = True
    else:
        try:
            payload = json.loads(secret["SecretString"])
        except (KeyError, TypeError, json.JSONDecodeError):
            return DRIFT
        if not isinstance(payload, dict) or not valid_pagination_key(
            payload.get("key")
        ):
            return DRIFT
    backfill = spec["backfill"]
    start_key = None
    while True:
        arguments = [
            "dynamodb",
            "scan",
            "--table-name",
            table_name,
            "--projection-expression",
            "#source,#target",
            "--expression-attribute-names",
            json.dumps(
                {
                    "#source": backfill["source"],
                    "#target": backfill["target"],
                },
                separators=(",", ":"),
            ),
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        ]
        if start_key is not None:
            arguments.extend(
                [
                    "--exclusive-start-key",
                    json.dumps(start_key, separators=(",", ":")),
                ]
            )
        page = aws(*arguments)
        for item in page.get("Items", []):
            source = item.get(backfill["source"], {}).get("S")
            target = item.get(backfill["target"], {}).get("S")
            if (
                not isinstance(source, str)
                or not source
                or len(source.encode("utf-8")) > 256
            ):
                continue
            if target is None:
                incomplete = True
            elif target != source:
                return DRIFT
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break
    return BASE if incomplete else TARGET


def lambda_env_state(function_name: str, spec_path: str) -> int:
    spec = load_spec(spec_path)
    if spec.get("function") != function_name:
        raise RuntimeError("lambda-env locator mismatch")
    variables = aws(
        "lambda",
        "get-function-configuration",
        "--function-name",
        function_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    ).get("Environment", {}).get("Variables", {})
    target, state = resolve_lambda_env_targets(spec)
    if state != TARGET:
        return state
    return classify_lambda_environment(variables, target)


def normalize_policy(value: dict[str, Any]) -> dict[str, Any]:
    policy = json.loads(json.dumps(value))
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        for field in ("Action", "Resource"):
            item = statement.get(field)
            if isinstance(item, str):
                statement[field] = [item]
            if isinstance(statement.get(field), list):
                statement[field] = sorted(statement[field])
    policy["Statement"] = sorted(
        statements,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    return policy


def stats_role_policy(spec: dict[str, Any], table_arn: str) -> dict[str, Any]:
    region = table_arn.split(":")[3]
    account = table_arn.split(":")[4]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadTenants",
                "Effect": "Allow",
                "Action": ["dynamodb:DescribeTable", "dynamodb:Scan"],
                "Resource": [
                    (
                        f"arn:aws:dynamodb:{region}:{account}:table/"
                        f"{spec['tenants_table']}"
                    )
                ],
            },
            {
                "Sid": "WriteStats",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                "Resource": [table_arn],
            },
            {
                "Sid": "ReadRootfsManifest",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    (
                        f"arn:aws:s3:::{spec['assets_bucket']}/"
                        f"{spec['rootfs_prefix'].rstrip('/')}/manifest.json"
                    )
                ],
            },
            {
                "Sid": "WriteLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": [f"arn:aws:logs:{region}:{account}:*"],
            },
        ],
    }


def tenant_stats_foundation_state(
    table_name: str, spec_path: str
) -> int:
    spec = load_spec(spec_path)
    if spec.get("table") != table_name:
        raise RuntimeError("tenant-stats foundation locator mismatch")
    response = optional_aws(
        ("ResourceNotFoundException",),
        "dynamodb",
        "describe-table",
        "--table-name",
        table_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if response is None:
        return BASE
    table = response["Table"]
    if (
        table.get("KeySchema")
        != [{"AttributeName": "id", "KeyType": "HASH"}]
        or {
            item["AttributeName"]: item["AttributeType"]
            for item in table.get("AttributeDefinitions", [])
        }.get("id")
        != "S"
        or table.get("BillingModeSummary", {}).get("BillingMode")
        != "PAY_PER_REQUEST"
    ):
        return DRIFT
    incomplete = table.get("TableStatus") != "ACTIVE"
    backups = aws(
        "dynamodb",
        "describe-continuous-backups",
        "--table-name",
        table_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if (
        backups["ContinuousBackupsDescription"][
            "PointInTimeRecoveryDescription"
        ]["PointInTimeRecoveryStatus"]
        != "ENABLED"
    ):
        incomplete = True
    role = optional_aws(
        ("NoSuchEntity",),
        "iam",
        "get-role",
        "--role-name",
        spec["role"],
        "--output",
        "json",
    )
    if role is None:
        incomplete = True
        role_arn = None
    else:
        role_arn = role["Role"]["Arn"]
        policy = optional_aws(
            ("NoSuchEntity",),
            "iam",
            "get-role-policy",
            "--role-name",
            spec["role"],
            "--policy-name",
            spec["role_policy_name"],
            "--output",
            "json",
        )
        if policy is None:
            incomplete = True
        elif normalize_policy(policy["PolicyDocument"]) != normalize_policy(
            stats_role_policy(spec, table["TableArn"])
        ):
            return DRIFT
    function = optional_aws(
        ("ResourceNotFoundException",),
        "lambda",
        "get-function",
        "--function-name",
        spec["function"],
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if function is None:
        incomplete = True
    else:
        function, payload = lambda_package(
            spec["function"],
            initial_function=function,
        )
        config = function["Configuration"]
        expected_environment = {
            "TENANTS_TABLE": spec["tenants_table"],
            "TENANT_STATS_TABLE": spec["table"],
            "ASSETS_BUCKET": spec["assets_bucket"],
            "ROOTFS_PREFIX": spec["rootfs_prefix"],
            "STATS_SCAN_SEGMENTS": "8",
        }
        if (
            config.get("Runtime") != "python3.12"
            or config.get("Architectures") != ["arm64"]
            or config.get("Handler") != "handler.lambda_handler"
            or config.get("MemorySize") != 8192
            or config.get("Timeout") != 50
            or config.get("Role") != role_arn
            or config.get("Environment", {}).get("Variables", {})
            != expected_environment
        ):
            return DRIFT
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                handler_hash = sha256(archive.read("handler.py"))
        except (KeyError, zipfile.BadZipFile):
            return DRIFT
        if handler_hash != spec["target_handler_sha256"]:
            return DRIFT
        concurrency = optional_aws(
            ("ResourceNotFoundException",),
            "lambda",
            "get-function-concurrency",
            "--function-name",
            spec["function"],
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        )
        reserved = (
            None
            if concurrency is None
            else concurrency.get("ReservedConcurrentExecutions")
        )
        if reserved is None:
            incomplete = True
        elif reserved != 1:
            return DRIFT
    rule = optional_aws(
        ("ResourceNotFoundException",),
        "events",
        "describe-rule",
        "--name",
        spec["schedule"],
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if rule is None:
        incomplete = True
    elif (
        rule.get("State") != "ENABLED"
        or rule.get("ScheduleExpression") != "rate(1 minute)"
    ):
        return DRIFT
    else:
        targets = aws(
            "events",
            "list-targets-by-rule",
            "--rule",
            spec["schedule"],
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        ).get("Targets", [])
        owned = next(
            (
                item
                for item in targets
                if item["Id"] == spec["schedule_target_id"]
            ),
            None,
        )
        if owned is None:
            incomplete = True
        elif (
            function is None
            or owned
            != {
                "Id": spec["schedule_target_id"],
                "Arn": function["Configuration"]["FunctionArn"],
            }
            or not stats_schedule_permission_matches(
                spec["function"],
                spec["schedule_permission_id"],
                rule["Arn"],
            )
        ):
            return DRIFT
    item = (
        aws(
            "dynamodb",
            "get-item",
            "--table-name",
            table_name,
            "--key",
            '{"id":{"S":"current"}}',
            "--consistent-read",
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        )
        or {}
    ).get("Item")
    if not item or not snapshot_is_fresh(item):
        incomplete = True
    return BASE if incomplete else TARGET


def stats_schedule_permission_matches(
    function_name: str,
    statement_id: str,
    rule_arn: str,
) -> bool:
    response = optional_aws(
        ("ResourceNotFoundException",),
        "lambda",
        "get-policy",
        "--function-name",
        function_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if response is None:
        return False
    try:
        policy = json.loads(response["Policy"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    statement = next(
        (
            item
            for item in policy.get("Statement", [])
            if item.get("Sid") == statement_id
        ),
        None,
    )
    return bool(
        statement
        and statement.get("Effect") == "Allow"
        and statement.get("Action") == "lambda:InvokeFunction"
        and statement.get("Principal") == {"Service": "events.amazonaws.com"}
        and statement.get("Condition", {})
        .get("ArnLike", {})
        .get("AWS:SourceArn")
        == rule_arn
    )


def iam_policy_state(function_name: str, spec_path: str) -> int:
    spec = load_spec(spec_path)
    if spec.get("function") != function_name:
        raise RuntimeError("IAM policy locator mismatch")
    config = aws(
        "lambda",
        "get-function-configuration",
        "--function-name",
        function_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    role_name = config["Role"].rsplit("/", 1)[-1]
    policy = optional_aws(
        ("NoSuchEntity",),
        "iam",
        "get-role-policy",
        "--role-name",
        role_name,
        "--policy-name",
        spec["policy_name"],
        "--output",
        "json",
    )
    if policy is None:
        return BASE
    if normalize_policy(policy["PolicyDocument"]) == normalize_policy(
        spec["policy_document"]
    ):
        return TARGET
    return DRIFT


def api_resources(api_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    position = None
    while True:
        arguments = [
            "apigateway",
            "get-resources",
            "--rest-api-id",
            api_id,
            "--limit",
            "500",
            "--region",
            os.environ["AWS_REGION"],
            "--output",
            "json",
        ]
        if position is not None:
            arguments.extend(["--position", position])
        page = aws(*arguments)
        items.extend(page.get("items", []))
        position = page.get("position")
        if not position:
            return items


def api_resource(api_id: str, path: str) -> dict[str, Any] | None:
    matches = [item for item in api_resources(api_id) if item.get("path") == path]
    if len(matches) > 1:
        raise RuntimeError(f"REST API has duplicate resources for {path}")
    return matches[0] if matches else None


def api_method(
    api_id: str, resource_id: str, method: str
) -> dict[str, Any] | None:
    return optional_aws(
        ("NotFoundException",),
        "apigateway",
        "get-method",
        "--rest-api-id",
        api_id,
        "--resource-id",
        resource_id,
        "--http-method",
        method,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )


def api_integration(
    api_id: str, resource_id: str, method: str
) -> dict[str, Any] | None:
    return optional_aws(
        ("NotFoundException",),
        "apigateway",
        "get-integration",
        "--rest-api-id",
        api_id,
        "--resource-id",
        resource_id,
        "--http-method",
        method,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )


def method_template(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "authorizationType": value["authorizationType"],
        "apiKeyRequired": value.get("apiKeyRequired", False),
    }
    for field in (
        "authorizerId",
        "authorizationScopes",
        "operationName",
        "requestParameters",
        "requestValidatorId",
    ):
        if field in value:
            result[field] = value[field]
    return result


def integration_template(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "type": value["type"],
        "integrationHttpMethod": value.get("httpMethod"),
    }
    for field in (
        "uri",
        "connectionType",
        "connectionId",
        "credentials",
        "requestParameters",
        "requestTemplates",
        "passthroughBehavior",
        "cacheNamespace",
        "cacheKeyParameters",
        "contentHandling",
        "timeoutInMillis",
        "tlsConfig",
    ):
        if field in value:
            result[field] = value[field]
    return {key: value for key, value in result.items() if value is not None}


def api_route_state(api_id: str, spec_path: str) -> int:
    spec = load_spec(spec_path)
    if spec.get("api_id") != api_id:
        raise RuntimeError("API route locator mismatch")
    source = api_resource(api_id, spec["source_path"])
    if source is None:
        return DRIFT
    source_method = api_method(api_id, source["id"], "GET")
    source_integration = api_integration(api_id, source["id"], "GET")
    if source_method is None or source_integration is None:
        return DRIFT
    target = api_resource(api_id, spec["target_path"])
    if target is None:
        return BASE
    method = api_method(api_id, target["id"], "GET")
    integration = api_integration(api_id, target["id"], "GET")
    options_method = api_method(api_id, target["id"], "OPTIONS")
    options_integration = api_integration(api_id, target["id"], "OPTIONS")
    if (
        method is None
        or integration is None
        or options_method is None
        or options_integration is None
    ):
        return BASE
    if (
        method_template(method) != method_template(source_method)
        or integration_template(integration)
        != integration_template(source_integration)
        or options_method.get("authorizationType") != "NONE"
        or options_method.get("apiKeyRequired", False) is not False
        or options_integration.get("type") != "MOCK"
    ):
        return DRIFT
    stage = aws(
        "apigateway",
        "get-stage",
        "--rest-api-id",
        api_id,
        "--stage-name",
        spec["stage"],
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    deployment = aws(
        "apigateway",
        "get-deployment",
        "--rest-api-id",
        api_id,
        "--deployment-id",
        stage["deploymentId"],
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if deployment.get("description") == spec["deployment_description"]:
        return TARGET
    status, _ = http_json(
        f"{spec['api_url'].rstrip('/')}{spec['target_path']}",
        spec["api_key_id"],
    )
    if status == 200:
        return TARGET
    if status == 404:
        return BASE
    return DRIFT


def ddb_ttl_state(table: str, spec_path: str) -> int:
    spec = load_spec(spec_path)
    if spec.get("table") != table:
        raise RuntimeError("DynamoDB TTL locator mismatch")
    description = aws(
        "dynamodb",
        "describe-time-to-live",
        "--table-name",
        table,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )["TimeToLiveDescription"]
    record_observation(f"ddb-ttl:{table}", description)
    status = description.get("TimeToLiveStatus")
    if status == "DISABLED":
        return TARGET
    if status in {"ENABLED", "DISABLING"}:
        return BASE
    return DRIFT


def snapshot_is_fresh(
    item: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> bool:
    refreshed_at = item.get("refreshed_at", {}).get("S")
    if not isinstance(refreshed_at, str):
        return False
    try:
        refreshed = datetime.fromisoformat(refreshed_at)
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
    except ValueError:
        return False
    current = now or datetime.now(UTC)
    age_seconds = (current - refreshed).total_seconds()
    return 0 <= age_seconds <= max_age_seconds


def tenant_stats_state(
    api_function: str,
    table_name: str,
    writer: str,
    rule_name: str,
    api_id: str,
    api_base_url: str,
    key_id: str,
    spec_path: str,
) -> int:
    spec = load_spec(spec_path)
    table = aws(
        "dynamodb",
        "describe-table",
        "--table-name",
        table_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )["Table"]
    if table["TableStatus"] != "ACTIVE":
        return UNKNOWN
    pitr = aws(
        "dynamodb",
        "describe-continuous-backups",
        "--table-name",
        table_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if (
        pitr["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"][
            "PointInTimeRecoveryStatus"
        ]
        != "ENABLED"
    ):
        return DRIFT
    config = aws(
        "lambda",
        "get-function-configuration",
        "--function-name",
        writer,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if (
        config["Runtime"] != "python3.12"
        or config["Architectures"] != ["arm64"]
        or config["MemorySize"] != 8192
        or config["Timeout"] != 50
    ):
        return DRIFT
    concurrency = aws(
        "lambda",
        "get-function-concurrency",
        "--function-name",
        writer,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if concurrency.get("ReservedConcurrentExecutions") != 1:
        return DRIFT
    rule = aws(
        "events",
        "describe-rule",
        "--name",
        rule_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if rule.get("State") != "ENABLED" or rule.get("ScheduleExpression") != "rate(1 minute)":
        return DRIFT
    if not stats_schedule_permission_matches(
        writer,
        spec["schedule_permission_id"],
        rule["Arn"],
    ):
        return DRIFT
    item = aws(
        "dynamodb",
        "get-item",
        "--table-name",
        table_name,
        "--key",
        '{"id":{"S":"current"}}',
        "--consistent-read",
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    ).get("Item")
    if not item or not snapshot_is_fresh(item):
        return DRIFT
    before = item.get("refreshed_at", {}).get("S")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(5)
        refreshed = (
            aws(
                "dynamodb",
                "get-item",
                "--table-name",
                table_name,
                "--key",
                '{"id":{"S":"current"}}',
                "--consistent-read",
                "--region",
                os.environ["AWS_REGION"],
                "--output",
                "json",
            )
            or {}
        ).get("Item")
        if (
            refreshed
            and refreshed.get("refreshed_at", {}).get("S") != before
            and snapshot_is_fresh(refreshed)
        ):
            item = refreshed
            break
    else:
        return DRIFT
    api_config = aws(
        "lambda",
        "get-function-configuration",
        "--function-name",
        api_function,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )
    if (
        api_config.get("Environment", {})
        .get("Variables", {})
        .get("TENANT_STATS_TABLE")
        != table_name
    ):
        return DRIFT
    resources = aws(
        "apigateway",
        "get-resources",
        "--rest-api-id",
        api_id,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )["items"]
    route = next((item for item in resources if item.get("path") == "/tenants-stats"), None)
    if route is None or "GET" not in route.get("resourceMethods", {}):
        return ABSENT
    status, payload = http_json(
        f"{api_base_url.rstrip('/')}/tenants-stats", key_id
    )
    if status != 200 or not isinstance(payload, dict):
        return DRIFT
    count_names = {
        "total",
        "running",
        "stopped",
        "creating",
        "failed",
        "deleted",
        "count",
    }

    def numeric_counts(value: Any) -> bool:
        if isinstance(value, dict):
            for name, item in value.items():
                if name in count_names and (
                    isinstance(item, bool) or not isinstance(item, (int, float))
                ):
                    return False
                if not numeric_counts(item):
                    return False
        elif isinstance(value, list):
            return all(numeric_counts(item) for item in value)
        return True

    if not numeric_counts(payload):
        return DRIFT
    return TARGET


def edge_lt_state(asg_name: str, spec_path: str | None = None) -> int:
    group = aws(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        asg_name,
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )["AutoScalingGroups"][0]
    template = group["LaunchTemplate"]
    version = aws(
        "ec2",
        "describe-launch-template-versions",
        "--launch-template-id",
        template["LaunchTemplateId"],
        "--versions",
        template["Version"],
        "--region",
        os.environ["AWS_REGION"],
        "--output",
        "json",
    )["LaunchTemplateVersions"][0]
    user_data = base64.b64decode(
        version["LaunchTemplateData"].get("UserData", "")
    ).decode(errors="replace")
    if spec_path is None:
        required = (
            "LOGGING_ENABLED=true",
            "ASSETS_BUCKET=",
            "AWS_REGION=",
            "FIREHOSE_DELIVERY_STREAM=",
        )
        return TARGET if all(value in user_data for value in required) else BASE
    spec = load_spec(spec_path)
    logging = spec["logging"]
    required = {
        "ENGINE_REDIS_ENDPOINT": logging["engine_redis_endpoint"],
        "EDGE_LISTEN_PORT": str(logging.get("edge_listen_port", 8080)),
        "LOGGING_ENABLED": "true",
        "ASSETS_BUCKET": logging["assets_bucket"],
        "AWS_REGION": logging["region"],
        "FIREHOSE_DELIVERY_STREAM": logging["firehose_delivery_stream"],
    }
    line = next(
        (
            value
            for value in user_data.splitlines()
            if "bash /opt/openclaw-edge/install-edge.sh" in value
        ),
        "",
    )
    matched = [
        f"{name}={value}" in line or f"{name}='{value}'" in line
        for name, value in required.items()
    ]
    if all(matched):
        return TARGET
    if not any(
        name in line
        for name in (
            "LOGGING_ENABLED",
            "ASSETS_BUCKET",
            "FIREHOSE_DELIVERY_STREAM",
        )
    ):
        return BASE
    return DRIFT


def operation_state(kind: str, argv: list[str]) -> int:
    if kind == "lambda":
        function_name, spec_path = argv
        spec = load_spec(spec_path)
        return lambda_state(function_name, spec)
    if kind == "s3":
        bucket, spec_path = argv
        spec = load_spec(spec_path)
        expected = {
            item["key"]: {"base": item.get("base"), "target": item["target"]}
            for item in spec["objects"]
        }
        return classify(s3_observation(bucket, spec), expected)
    if kind == "ssm":
        asg_name, spec_path = argv
        spec = load_spec(spec_path)
        observed = ssm_observation(asg_name, spec)
        return classify_ssm(observed, spec)
    if kind == "fresh-host":
        asg_name, spec_path = argv
        return fresh_host_state(asg_name, load_spec(spec_path))
    if kind == "tenant-query":
        return tenant_query_state(*argv)
    if kind == "http-case":
        return http_case_state(*argv)
    if kind == "tenant-query-foundation":
        return tenant_query_foundation_state(*argv)
    if kind == "lambda-env":
        return lambda_env_state(*argv)
    if kind == "tenant-stats-foundation":
        return tenant_stats_foundation_state(*argv)
    if kind == "iam-policy":
        return iam_policy_state(*argv)
    if kind == "api-route":
        return api_route_state(*argv)
    if kind == "ddb-ttl-disable":
        return ddb_ttl_state(*argv)
    if kind == "tenant-stats":
        return tenant_stats_state(*argv)
    if kind == "edge-lt":
        return edge_lt_state(*argv)
    if kind == "monitoring-off":
        return TARGET
    raise RuntimeError(f"unknown probe kind: {kind}")


def proof_evidence(
    challenge: str, check_id: str, proof_id: str, observation_sha256: str
) -> str:
    return sha256(
        canonical(
            {
                "challenge": challenge,
                "check_id": check_id,
                "proof_id": proof_id,
                "observation_sha256": observation_sha256,
            }
        )
    )


def accept(check_id: str, proof_ids: list[str], kind: str, argv: list[str]) -> int:
    _OBSERVATION_EVENTS.clear()
    state = operation_state(kind, argv)
    if state != TARGET:
        return 1
    if not _OBSERVATION_EVENTS:
        raise RuntimeError("acceptance produced no fresh live observations")
    challenge = os.environ["CLAW_PATCH_ACCEPTANCE_CHALLENGE"]
    observation = sha256(canonical(_OBSERVATION_EVENTS))
    proofs = [
        {
            "id": proof_id,
            "observation_sha256": sha256(
                canonical({"observation": observation, "proof": proof_id})
            ),
        }
        for proof_id in proof_ids
    ]
    for proof in proofs:
        proof["evidence_sha256"] = proof_evidence(
            challenge,
            check_id,
            proof["id"],
            proof["observation_sha256"],
        )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "check_id": check_id,
                "challenge_sha256": sha256(challenge.encode()),
                "proofs": proofs,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str]) -> int:
    try:
        action = argv[1]
        if action == "state":
            return operation_state(argv[2], argv[3:])
        if action == "verify":
            return 0 if operation_state(argv[2], argv[3:]) == TARGET else 1
        if action == "accept":
            check_id = argv[2]
            proof_ids = argv[3].split(",")
            return accept(check_id, proof_ids, argv[4], argv[5:])
        raise RuntimeError(f"unknown action: {action}")
    except Exception as error:  # noqa: BLE001 - probe boundary maps failures to UNKNOWN
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
