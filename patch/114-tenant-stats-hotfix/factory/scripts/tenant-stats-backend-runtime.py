#!/usr/bin/env python3
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent

def die(message, code):
    print("FATAL: " + message, file=sys.stderr)
    raise SystemExit(code)

def load(path, code=46):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"cannot read JSON {path}: {exc}", code)
    return value

CFG = load(HERE / "config.json", 44)
TRUST = load(HERE / "trust-policy.json", 44)
POLICY = load(HERE / "inline-policy.json", 44)
TAG_KEY = CFG["tag_key"]
MARKER = CFG["marker"]
ACCOUNT = CFG["target_account"]
REGION = CFG["target_region"]
TABLE = CFG["table"]["name"]
WRITER = CFG["writer"]
SCHEDULE = CFG["schedule"]
FUNCTION = WRITER["function_name"]
ROLE = WRITER["role_name"]
POLICY_NAME = WRITER["policy_name"]
RULE = SCHEDULE["rule_name"]
FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FUNCTION}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"
RULE_ARN = f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{RULE}"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}"
TRANSIENT = (
    "Throttl", "LimitExceeded", "ServiceUnavailable", "InternalServer",
    "InternalFailure", "RequestTimeout", "RequestLimitExceeded",
)
PERMANENT = (
    "AccessDenied", "Unauthorized", "AuthFailure", "InvalidClientTokenId",
    "ValidationException", "InvalidParameter", "MissingParameter",
    "MalformedPolicy", "UnrecognizedClient",
)

def classify(text):
    if any(token in text for token in TRANSIENT):
        return 41
    if any(token in text for token in PERMANENT):
        return 49
    return 46

def aws_raw(*args, missing=()):
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    env["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] = "true"
    try:
        result = subprocess.run(
            ["aws", *map(str, args)], capture_output=True, text=True,
            timeout=max(1.0, timeout_seconds()), env=env,
        )
    except subprocess.TimeoutExpired:
        die("AWS CLI call timed out: " + " ".join(args[:2]), 42)
    if result.returncode == 0:
        return result.stdout
    text = (result.stderr or result.stdout).strip()
    if missing and any(token in text for token in missing):
        return None
    if text:
        print(text, file=sys.stderr)
    die("AWS read or write failed: " + " ".join(args[:2]), classify(text))

def aws_json(*args, missing=(), empty=False):
    output = aws_raw(*args, missing=missing)
    if output is None:
        return None
    if not output.strip() and empty:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        die("AWS returned unreadable JSON for " + " ".join(args[:2]), 46)

def timeout_seconds():
    raw = os.environ.get("OC_PATCH_TIMEOUT_SECONDS", "600")
    try:
        value = float(raw)
    except ValueError:
        die("OC_PATCH_TIMEOUT_SECONDS must be numeric", 49)
    if value < 0:
        die("OC_PATCH_TIMEOUT_SECONDS must not be negative", 49)
    return value

def poll_seconds():
    raw = os.environ.get("OC_PATCH_POLL_SECONDS", "2")
    try:
        value = float(raw)
    except ValueError:
        die("OC_PATCH_POLL_SECONDS must be numeric", 49)
    if value < 0:
        die("OC_PATCH_POLL_SECONDS must not be negative", 49)
    return value

def tags(values):
    if isinstance(values, dict):
        return values
    return {
        item["Key"]: item["Value"]
        for item in (values or [])
        if isinstance(item, dict) and "Key" in item and "Value" in item
    }

def policy_doc(value):
    if isinstance(value, str):
        try:
            value = json.loads(unquote(value))
        except json.JSONDecodeError:
            die("AWS returned an unreadable IAM policy document", 46)
    return value

def bind_target():
    account = os.environ.get("OC_PATCH_ACCOUNT")
    region = os.environ.get("OC_PATCH_REGION")
    if not account or not region:
        die("OC_PATCH_ACCOUNT and OC_PATCH_REGION are required", 3)
    if account != ACCOUNT or region != REGION:
        die("OC_PATCH_ACCOUNT/REGION do not match this target-bound kit", 3)
    live = aws_raw(
        "sts", "get-caller-identity", "--region", REGION,
        "--query", "Account", "--output", "text",
    ).strip()
    if not re.fullmatch(r"[0-9]{12}", live):
        die("STS returned an invalid account id", 3)
    if live != ACCOUNT:
        die("live AWS credentials belong to the wrong account", 3)

def review_fingerprint():
    receipt = load(HERE.parents[3] / "REVIEW.json", 44)
    fingerprint = receipt.get("kit_fingerprint") if isinstance(receipt, dict) else None
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        die("REVIEW.json has no valid final kit fingerprint", 44)
    return fingerprint

def state_dir():
    configured = os.environ.get("OC_PATCH_STATE_ROOT")
    if configured:
        root = Path(configured).expanduser()
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
        root /= "openclaw-patches"
    path = (
        root / ACCOUNT / REGION / CFG["artifact_id"] / CFG["patch_sha"]
        / review_fingerprint() / CFG["resource_id"]
    )
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    except OSError as exc:
        die(f"cannot create patch state directory: {exc}", 44)
    return path

def atomic_new(path, value):
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        die(f"cannot create state file {path}: {exc}", 44)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return True

def write_anchor(state, phase, created):
    anchor = state / "anchor.json"
    if anchor.exists():
        archive = state / "archive"
        archive.mkdir(exist_ok=True)
        os.replace(anchor, archive / f"anchor-{time.time_ns()}.json")
    atomic_new(
        anchor,
        {
            "schema_version": 1, "marker": MARKER,
            "recipe_sha256": CFG["recipe_sha256"], "phase": phase,
            "created": created, "updated_at_ns": time.time_ns(),
        },
    )

def anchor(state, required=True):
    path = state / "anchor.json"
    if not path.is_file():
        if required:
            die("no owned patch anchor exists", 44)
        return None
    value = load(path, 44)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("marker") != MARKER
        or value.get("recipe_sha256") != CFG["recipe_sha256"]
        or value.get("phase") not in {"applied", "rolled_back"}
        or not isinstance(value.get("created"), dict)
    ):
        die("patch anchor is not owned by this compiled recipe", 44)
    return value

def table_state():
    out = aws_json(
        "dynamodb", "describe-table", "--region", REGION, "--table-name", TABLE,
        "--output", "json",
        missing=("ResourceNotFoundException",),
    )
    if out is None:
        return None
    description = out.get("Table")
    if not isinstance(description, dict):
        die("DescribeTable omitted Table", 46)
    tag_out = aws_json(
        "dynamodb", "list-tags-of-resource", "--region", REGION,
        "--resource-arn", description.get("TableArn", ""), "--output", "json",
    )
    pitr = aws_json(
        "dynamodb", "describe-continuous-backups", "--region", REGION,
        "--table-name", TABLE, "--output", "json",
    )
    return {"description": description, "tags": tag_out.get("Tags"), "pitr": pitr}

def role_state():
    out = aws_json(
        "iam", "get-role", "--region", REGION, "--role-name", ROLE, "--output", "json",
        missing=("NoSuchEntity",),
    )
    if out is None:
        return None
    role = out.get("Role")
    if not isinstance(role, dict):
        die("GetRole omitted Role", 46)
    tag_out = aws_json(
        "iam", "list-role-tags", "--region", REGION, "--role-name", ROLE,
        "--output", "json",
    )
    inline = aws_json(
        "iam", "get-role-policy", "--region", REGION, "--role-name", ROLE,
        "--policy-name", POLICY_NAME, "--output", "json",
        missing=("NoSuchEntity",),
    )
    return {"role": role, "tags": tag_out.get("Tags"), "policy": inline}

def function_state():
    out = aws_json(
        "lambda", "get-function", "--region", REGION, "--function-name", FUNCTION,
        "--output", "json", missing=("ResourceNotFoundException",),
    )
    if out is None:
        return None
    config = out.get("Configuration")
    if not isinstance(config, dict):
        die("GetFunction omitted Configuration", 46)
    concurrency = aws_json(
        "lambda", "get-function-concurrency", "--region", REGION,
        "--function-name", FUNCTION, "--output", "json",
    )
    permission = aws_json(
        "lambda", "get-policy", "--region", REGION, "--function-name", FUNCTION,
        "--output", "json", missing=("ResourceNotFoundException",),
    )
    return {
        "configuration": config, "tags": out.get("Tags"),
        "concurrency": concurrency, "permission": permission,
    }

def rule_state():
    rule = aws_json(
        "events", "describe-rule", "--region", REGION, "--name", RULE,
        "--output", "json", missing=("ResourceNotFoundException",),
    )
    if rule is None:
        return None
    arn = rule.get("Arn")
    if not isinstance(arn, str) or not arn:
        die("DescribeRule omitted Arn", 46)
    tag_out = aws_json(
        "events", "list-tags-for-resource", "--region", REGION,
        "--resource-arn", arn, "--output", "json",
    )
    target_out = aws_json(
        "events", "list-targets-by-rule", "--region", REGION, "--rule", RULE,
        "--output", "json",
    )
    return {"rule": rule, "tags": tag_out.get("Tags"), "targets": target_out.get("Targets")}

def discover():
    return {
        "table": table_state(), "role": role_state(),
        "function": function_state(), "rule": rule_state(),
    }

def require_markers(found):
    for name in ("table", "role", "function", "rule"):
        resource = found[name]
        if resource is not None and tags(resource.get("tags")).get(TAG_KEY) != MARKER:
            die(f"same-name {name} exists without this manifest marker", 40)

def pitr_status(resource):
    return (
        ((resource.get("pitr") or {}).get("ContinuousBackupsDescription") or {})
        .get("PointInTimeRecoveryDescription", {})
        .get("PointInTimeRecoveryStatus")
    )

def check_table(resource, require_active=True):
    if resource is None:
        die("owned DynamoDB table is missing", 40)
    value = resource["description"]
    expected = CFG["table"]
    if (
        value.get("TableName") != TABLE or value.get("TableArn") != TABLE_ARN
        or value.get("KeySchema") != [{"AttributeName": "id", "KeyType": "HASH"}]
        or value.get("AttributeDefinitions")
        != [{"AttributeName": "id", "AttributeType": "S"}]
        or (value.get("BillingModeSummary") or {}).get("BillingMode")
        != expected["billing_mode"]
    ):
        die("DynamoDB table schema, ARN, or billing mode drifted", 40)
    if require_active and value.get("TableStatus") != "ACTIVE":
        die("DynamoDB table did not become ACTIVE", 43)

def check_role(resource, allow_missing_policy=False):
    if resource is None:
        die("owned IAM role is missing", 40)
    role = resource["role"]
    if role.get("RoleName") != ROLE or role.get("Arn") != ROLE_ARN:
        die("IAM role identity drifted", 40)
    if policy_doc(role.get("AssumeRolePolicyDocument")) != TRUST:
        die("IAM role trust policy drifted", 40)
    inline = resource.get("policy")
    if inline is None and allow_missing_policy:
        return
    if inline is None or policy_doc(inline.get("PolicyDocument")) != POLICY:
        die("IAM inline policy drifted", 40)

def permission_statement(resource):
    wrapped = resource.get("permission") if resource else None
    if wrapped is None:
        return None
    raw = wrapped.get("Policy")
    value = policy_doc(raw)
    statements = value.get("Statement") if isinstance(value, dict) else None
    if not isinstance(statements, list):
        die("Lambda resource policy has no Statement list", 46)
    return next(
        (item for item in statements if item.get("Sid") == SCHEDULE["permission_statement_id"]),
        None,
    )

def check_permission(resource, allow_missing=False):
    statement = permission_statement(resource)
    if statement is None and allow_missing:
        return
    condition = (statement or {}).get("Condition") or {}
    if (
        statement is None
        or statement.get("Effect") != "Allow"
        or statement.get("Action") != "lambda:InvokeFunction"
        or statement.get("Resource") != FUNCTION_ARN
        or statement.get("Principal") != {"Service": "events.amazonaws.com"}
        or (condition.get("ArnLike") or {}).get("AWS:SourceArn") != RULE_ARN
        or (condition.get("StringEquals") or {}).get("AWS:SourceAccount") != ACCOUNT
    ):
        die("Lambda EventBridge permission drifted or conflicts", 40)

def check_function(resource, allow_missing_concurrency=False):
    if resource is None:
        die("owned writer Lambda is missing", 40)
    value = resource["configuration"]
    expected = WRITER
    if (
        value.get("FunctionName") != FUNCTION
        or value.get("FunctionArn") != FUNCTION_ARN
        or value.get("Runtime") != expected["runtime"]
        or value.get("Architectures") != [expected["architecture"]]
        or value.get("Handler") != expected["handler"]
        or value.get("Role") != ROLE_ARN
        or value.get("Timeout") != expected["timeout"]
        or value.get("MemorySize") != expected["memory_size"]
        or value.get("Environment") != {"Variables": expected["environment"]}
        or value.get("CodeSha256") != CFG["writer_code_sha256"]
    ):
        die("writer Lambda code or configuration drifted", 40)
    concurrency = (resource.get("concurrency") or {}).get("ReservedConcurrentExecutions")
    if concurrency is None and allow_missing_concurrency:
        return
    if concurrency != expected["reserved_concurrency"]:
        die("writer Lambda reserved concurrency drifted", 40)

def target_value():
    return {"Id": SCHEDULE["target_id"], "Arn": FUNCTION_ARN}

def check_rule(resource, allow_disabled=False, allow_missing_target=False):
    if resource is None:
        die("owned EventBridge rule is missing", 40)
    value = resource["rule"]
    if (
        value.get("Name") != RULE or value.get("Arn") != RULE_ARN
        or value.get("ScheduleExpression") != SCHEDULE["expression"]
        or value.get("State") not in ({"ENABLED", "DISABLED"} if allow_disabled else {"ENABLED"})
    ):
        die("EventBridge rule identity, schedule, or state drifted", 40)
    current = resource.get("targets") or []
    if not current and allow_missing_target:
        return
    if current != [target_value()]:
        die("EventBridge targets drifted or conflict", 40)

def preflight(found):
    if found["table"] is not None:
        check_table(found["table"], require_active=False)
    if found["role"] is not None:
        check_role(found["role"], allow_missing_policy=True)
    if found["function"] is not None:
        check_function(found["function"], allow_missing_concurrency=True)
        check_permission(found["function"], allow_missing=True)
    if found["rule"] is not None:
        check_rule(found["rule"], allow_disabled=True, allow_missing_target=True)

def wait_for(read, ready, label):
    deadline = time.monotonic() + timeout_seconds()
    while True:
        value = read()
        if value is not None and ready(value):
            return value
        if time.monotonic() >= deadline:
            die(f"timed out waiting for {label}", 42)
        time.sleep(poll_seconds())

def ensure_table(existing, created):
    if existing is None:
        aws_json(
            "dynamodb", "create-table", "--region", REGION, "--table-name", TABLE,
            "--attribute-definitions", "AttributeName=id,AttributeType=S",
            "--key-schema", "AttributeName=id,KeyType=HASH",
            "--billing-mode", "PAY_PER_REQUEST",
            "--tags", f"Key={TAG_KEY},Value={MARKER}", "--output", "json",
        )
        created["table"] = True
    current = wait_for(
        table_state,
        lambda item: item["description"].get("TableStatus") == "ACTIVE",
        "DynamoDB table ACTIVE",
    )
    require_markers({"table": current, "role": None, "function": None, "rule": None})
    check_table(current)
    if pitr_status(current) != "ENABLED":
        aws_json(
            "dynamodb", "update-continuous-backups", "--region", REGION,
            "--table-name", TABLE,
            "--point-in-time-recovery-specification",
            "PointInTimeRecoveryEnabled=true", "--output", "json",
        )
        current = wait_for(table_state, lambda item: pitr_status(item) == "ENABLED", "PITR")
    if pitr_status(current) != "ENABLED":
        die("DynamoDB PITR was not enabled", 43)

def ensure_role(existing, created):
    if existing is None:
        aws_json(
            "iam", "create-role", "--region", REGION, "--role-name", ROLE,
            "--assume-role-policy-document", f"file://{HERE / 'trust-policy.json'}",
            "--tags", f"Key={TAG_KEY},Value={MARKER}", "--output", "json",
        )
        created["role"] = True
        existing = wait_for(role_state, lambda value: value is not None, "IAM role")
    if existing.get("policy") is None:
        aws_json(
            "iam", "put-role-policy", "--region", REGION, "--role-name", ROLE,
            "--policy-name", POLICY_NAME,
            "--policy-document", f"file://{HERE / 'inline-policy.json'}",
            empty=True,
        )
    current = role_state()
    require_markers({"table": None, "role": current, "function": None, "rule": None})
    check_role(current)

def create_function():
    deadline = time.monotonic() + timeout_seconds()
    command = [
        "lambda", "create-function", "--region", REGION, "--function-name", FUNCTION,
        "--runtime", WRITER["runtime"], "--architectures", WRITER["architecture"],
        "--handler", WRITER["handler"], "--role", ROLE_ARN,
        "--zip-file", f"fileb://{HERE / 'writer.zip'}",
        "--timeout", str(WRITER["timeout"]), "--memory-size", str(WRITER["memory_size"]),
        "--environment", f"file://{HERE / 'environment.json'}",
        "--tags", f"{TAG_KEY}={MARKER}", "--output", "json",
    ]
    while True:
        env = os.environ.copy()
        env.update({"AWS_PAGER": "", "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true"})
        try:
            result = subprocess.run(
                ["aws", *command], capture_output=True, text=True, env=env,
                timeout=max(1.0, timeout_seconds()),
            )
        except subprocess.TimeoutExpired:
            die("CreateFunction AWS CLI call timed out", 42)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                die("CreateFunction returned unreadable JSON", 46)
        text = (result.stderr or result.stdout).strip()
        if "cannot be assumed by Lambda" not in text:
            if text:
                print(text, file=sys.stderr)
            die("CreateFunction failed", classify(text))
        if time.monotonic() >= deadline:
            die("timed out waiting for IAM role propagation", 42)
        time.sleep(poll_seconds())

def ensure_function(existing, created):
    if existing is None:
        create_function()
        created["function"] = True
        existing = wait_for(
            function_state,
            lambda value: value["configuration"].get("State") == "Active"
            and value["configuration"].get("LastUpdateStatus") in (None, "Successful"),
            "writer Lambda ACTIVE",
        )
    if (existing.get("concurrency") or {}).get("ReservedConcurrentExecutions") is None:
        aws_json(
            "lambda", "put-function-concurrency", "--region", REGION,
            "--function-name", FUNCTION, "--reserved-concurrent-executions",
            str(WRITER["reserved_concurrency"]), "--output", "json",
        )
    current = function_state()
    require_markers({"table": None, "role": None, "function": current, "rule": None})
    check_function(current)

def ensure_rule(existing, created):
    if existing is None:
        aws_json(
            "events", "put-rule", "--region", REGION, "--name", RULE,
            "--schedule-expression", SCHEDULE["expression"], "--state", "DISABLED",
            "--tags", f"Key={TAG_KEY},Value={MARKER}", "--output", "json",
        )
        created["rule"] = True
    elif existing["rule"].get("State") == "ENABLED":
        aws_json(
            "events", "disable-rule", "--region", REGION, "--name", RULE, empty=True,
        )
    current = rule_state()
    require_markers({"table": None, "role": None, "function": None, "rule": current})
    check_rule(current, allow_disabled=True, allow_missing_target=True)

def ensure_permission():
    current = function_state()
    if permission_statement(current) is None:
        aws_json(
            "lambda", "add-permission", "--region", REGION,
            "--function-name", FUNCTION,
            "--statement-id", SCHEDULE["permission_statement_id"],
            "--action", "lambda:InvokeFunction", "--principal", "events.amazonaws.com",
            "--source-arn", RULE_ARN, "--source-account", ACCOUNT, "--output", "json",
        )
    check_permission(function_state())

def ensure_target():
    current = rule_state()
    if not (current.get("targets") or []):
        result = aws_json(
            "events", "put-targets", "--region", REGION, "--rule", RULE,
            "--targets", json.dumps([target_value()], separators=(",", ":")),
            "--output", "json",
        )
        if result.get("FailedEntryCount") != 0:
            die("EventBridge rejected the Lambda target", 43)
    check_rule(rule_state(), allow_disabled=True)

def enable_rule():
    current = rule_state()
    check_rule(current, allow_disabled=True)
    if current["rule"].get("State") != "ENABLED":
        aws_json(
            "events", "enable-rule", "--region", REGION, "--name", RULE, empty=True,
        )
    check_rule(rule_state())

def snapshot_current():
    result = aws_json(
        "dynamodb", "get-item", "--region", REGION, "--table-name", TABLE,
        "--key", '{"id":{"S":"current"}}', "--consistent-read", "--output", "json",
    )
    item = result.get("Item")
    if not isinstance(item, dict) or item.get("id") != {"S": "current"}:
        die("tenant statistics current snapshot is absent", 43)
    refreshed = item.get("refreshed_at")
    if (
        not isinstance(refreshed, dict)
        or set(refreshed) != {"S"}
        or not isinstance(refreshed["S"], str)
    ):
        die("tenant statistics snapshot has no valid refreshed_at", 43)
    try:
        parsed = datetime.fromisoformat(refreshed["S"].replace("Z", "+00:00"))
    except ValueError:
        die("tenant statistics snapshot refreshed_at is invalid", 43)
    if parsed.tzinfo is None:
        die("tenant statistics snapshot refreshed_at lacks timezone", 43)
    return item

def invoke_once(state):
    output = state / "invocations" / f"response-{time.time_ns()}.json"
    output.parent.mkdir(exist_ok=True)
    metadata = aws_json(
        "lambda", "invoke", "--region", REGION, "--function-name", FUNCTION,
        "--invocation-type", "RequestResponse", "--payload", "{}",
        "--cli-binary-format", "raw-in-base64-out", "--output", "json", str(output),
    )
    if metadata.get("StatusCode") != 200 or metadata.get("FunctionError"):
        die("synchronous writer invocation failed", 43)
    response = load(output, 43)
    if not isinstance(response, dict) or not response.get("refreshed_at"):
        die("writer response did not prove a completed refresh", 43)
    snapshot = snapshot_current()
    if snapshot["refreshed_at"] != {"S": response["refreshed_at"]}:
        die("writer response does not match the published snapshot", 43)

def verify_active():
    found = discover()
    require_markers(found)
    check_table(found["table"])
    if pitr_status(found["table"]) != "ENABLED":
        die("DynamoDB PITR is not enabled", 40)
    check_role(found["role"])
    check_function(found["function"])
    check_permission(found["function"])
    check_rule(found["rule"])
    snapshot_current()

def write_backup(state, found):
    path = state / "backup.json"
    if path.exists():
        existing = load(path, 44)
        if existing.get("recipe_sha256") != CFG["recipe_sha256"]:
            die("backup belongs to a different compiled recipe", 44)
        return
    atomic_new(
        path,
        {
            "schema_version": 1, "recipe_sha256": CFG["recipe_sha256"],
            "captured_at_ns": time.time_ns(), "resources": found,
        },
    )

def apply(state):
    previous = anchor(state, required=False)
    found = discover()
    require_markers(found)
    write_backup(state, found)
    preflight(found)
    if previous and previous["phase"] == "applied":
        verify_active()
        print(f"SKIP tenant-stats backend already applied marker={MARKER}")
        return
    created = (previous or {}).get(
        "created", {"table": False, "role": False, "function": False, "rule": False}
    )
    ensure_table(found["table"], created)
    ensure_role(found["role"], created)
    ensure_function(found["function"], created)
    ensure_rule(found["rule"], created)
    ensure_permission()
    ensure_target()
    invoke_once(state)
    enable_rule()
    verify_active()
    write_anchor(state, "applied", created)
    print(f"APPLIED tenant-stats backend marker={MARKER}")

def verify(state):
    current = anchor(state)
    if current["phase"] != "applied":
        die("owned anchor says this backend is rolled back", 44)
    verify_active()
    print(f"VERIFIED tenant-stats backend marker={MARKER}")

def rollback(state):
    current = anchor(state)
    if current["phase"] == "rolled_back":
        print(f"SKIP tenant-stats backend already rolled back marker={MARKER}")
        return
    found = discover()
    require_markers(found)
    check_table(found["table"])
    check_role(found["role"])
    check_function(found["function"])
    check_rule(found["rule"], allow_disabled=True, allow_missing_target=True)
    check_permission(found["function"], allow_missing=True)
    if found["rule"]["rule"].get("State") != "DISABLED":
        aws_json(
            "events", "disable-rule", "--region", REGION, "--name", RULE, empty=True,
        )
    if found["rule"].get("targets"):
        aws_json(
            "events", "remove-targets", "--region", REGION, "--rule", RULE,
            "--ids", json.dumps([SCHEDULE["target_id"]]), "--output", "json",
        )
    if permission_statement(found["function"]) is not None:
        aws_json(
            "lambda", "remove-permission", "--region", REGION,
            "--function-name", FUNCTION,
            "--statement-id", SCHEDULE["permission_statement_id"], empty=True,
        )
    after_rule = rule_state()
    after_function = function_state()
    if (
        after_rule["rule"].get("State") != "DISABLED"
        or after_rule.get("targets")
        or permission_statement(after_function) is not None
    ):
        die("rollback did not disable every invocation path", 43)
    atomic_new(
        state / "manual-cleanup.json",
        {
            "schema_version": 1, "marker": MARKER,
            "table": {"name": TABLE, "action": "retain-never-delete"},
            "function": {"name": FUNCTION, "action": "manual-cleanup"},
            "role": {"name": ROLE, "action": "manual-cleanup"},
            "rule": {"name": RULE, "action": "disabled-manual-cleanup"},
        },
    )
    write_anchor(state, "rolled_back", current["created"])
    print("ROLLED_BACK invocation paths disabled; resources retained for manual cleanup")

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"apply", "verify", "rollback"}:
        die("usage: backend.py apply|verify|rollback", 49)
    bind_target()
    state = state_dir()
    lock = open(state / ".lock", "a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    {"apply": apply, "verify": verify, "rollback": rollback}[sys.argv[1]](state)

if __name__ == "__main__":
    main()
