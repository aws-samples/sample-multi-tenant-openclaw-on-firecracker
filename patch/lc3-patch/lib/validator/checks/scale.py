import ipaddress
import json
import re

from lib.awsread import AwsReadError, AwsUnavailable
from lib.result import finding


def _missing(check_id, label, error="missing input"):
    return finding(
        check_id, "INCONCLUSIVE", "%s was not inspectable." % label,
        {"inspected": 0, "error": str(error)},
        remediation="Add the required values to environment.json and retry.",
    )


def check_b1(ctx):
    function = ctx.get("lambda_link.function")
    defaults = ctx.source_defaults().get("env") or {}
    if ctx.offline or not function:
        return _missing("B1", "Lambda environment")
    try:
        response = ctx.aws.call(
            "lambda", "get_function_configuration", FunctionName=function)
    except (AwsReadError, AwsUnavailable) as error:
        return _missing("B1", "Lambda environment", error)
    live = ((response.get("Environment") or {}).get("Variables") or {})
    rows, classes = [], {"MATCH": 0, "DIVERGED": 0, "UNDECLARED": 0}
    for key, actual in sorted(live.items()):
        declared = defaults.get(key)
        expected = declared.get("value") if declared else None
        state = "UNDECLARED" if declared is None else (
            "MATCH" if str(actual) == str(expected) else "DIVERGED")
        classes[state] += 1
        rows.append({"key": key, "live": actual, "gateway_default": expected,
                     "classification": state,
                     "source": declared.get("source") if declared else None})
    if not rows:
        return _missing("B1", "Lambda environment", "empty environment")
    return finding(
        "B1", "PASS", "Configuration differences are reported without judging intent.",
        {"inspected": len(rows), "classifications": classes, "variables": rows},
        remediation="Review DIVERGED and UNDECLARED values with the deployment owner.",
    )


def _component(name, left, right, operation):
    if left is None or right is None:
        return {"name": name, "verified": False, "left": left, "right": right}
    if operation == "ge":
        ok = left >= right
    else:
        ok = left == right
    return {"name": name, "verified": True, "left": left, "right": right, "ok": ok}


def _table_capacity(ctx, table_name, target):
    if not table_name or target is None:
        return _component("table_write_capacity", None, target, "ge")
    response = ctx.aws.call("dynamodb", "describe_table", TableName=table_name)
    table = response.get("Table") or {}
    mode = (table.get("BillingModeSummary") or {}).get("BillingMode")
    if mode == "PAY_PER_REQUEST":
        return {"name": "table_write_capacity", "verified": True,
                "left": mode, "right": target, "ok": True}
    capacity = (table.get("ProvisionedThroughput") or {}).get("WriteCapacityUnits")
    return _component("table_write_capacity", capacity, target, "ge")


def check_b2(ctx):
    scale = ctx.get("scale", {}) or {}
    defaults = ctx.source_defaults()
    slots = scale.get("per_host_slots")
    if slots is None:
        slots = defaults.get("config", {}).get("vm.host_launch_slots")
    components = [_component(
        "fleet_slots",
        (ctx.get("hosts.count") or 0) * slots if slots is not None else None,
        ctx.target_vms, "ge")]
    function = ctx.get("lambda_link.function")
    try:
        mappings = ctx.aws.call(
            "lambda", "list_event_source_mappings",
            FunctionName=function).get("EventSourceMappings") or []
        mapping = mappings[0] if mappings else {}
        concurrent = (mapping.get("ScalingConfig") or {}).get("MaximumConcurrency")
        batch = mapping.get("BatchSize")
        throughput = concurrent * batch if concurrent is not None and batch is not None else None
        components.append(_component(
            "esm_throughput", throughput, scale.get("target_tps"), "ge"))
        components.append(_component(
            "deadline_budget", scale.get("deadline_budget"),
            scale.get("worst_single_seconds"), "ge"))
        components.append(_table_capacity(
            ctx, scale.get("table_name"), scale.get("target_write_rate")))
    except (AwsReadError, AwsUnavailable, TypeError) as error:
        components.append({"name": "aws_capacity", "verified": False,
                           "left": None, "right": None, "error": str(error)})
    verified = [item for item in components if item.get("verified")]
    failed = [item for item in verified if not item.get("ok")]
    verdict = "FAIL" if failed else ("UNVERIFIED" if len(verified) < len(components) else "PASS")
    return finding(
        "B2", verdict, "Scale identities were evaluated from injected targets and source defaults.",
        {"inspected": len(verified), "components": components},
        [item["name"] for item in failed],
        "Align fleet, queue, deadline, and table capacity with the injected targets.",
    )


def _syntax_problem(name, value):
    lower = name.lower()
    try:
        if "cidr" in lower:
            ipaddress.ip_network(value, strict=False)
        elif "regex" in lower:
            re.compile(value)
        elif value.lstrip().startswith(("{", "[")):
            json.loads(value)
        elif lower.endswith("config") and "=" in value:
            if any("=" not in part for part in value.split(",")):
                return "invalid k=v sequence"
    except (ValueError, TypeError, re.error) as error:
        return str(error)
    return None


def _type_problem(spec, value):
    parser = (spec or {}).get("type")
    try:
        if parser == "int":
            int(value)
        elif parser == "float":
            float(value)
    except (TypeError, ValueError):
        return "value is not %s" % parser
    allowed = (spec or {}).get("allowed")
    if allowed and value not in allowed:
        return "value is outside the declared enum"
    return None


def check_b3(ctx):
    paths = ctx.get("ssm.parameter_paths", []) or []
    if ctx.offline or not paths:
        return _missing("B3", "SSM parameters")
    parameters = []
    try:
        for path in paths:
            response = ctx.aws.call(
                "ssm", "get_parameters_by_path", Path=path, Recursive=True)
            parameters.extend(response.get("Parameters") or [])
    except (AwsReadError, AwsUnavailable) as error:
        return _missing("B3", "SSM parameters", error)
    if not parameters:
        return _missing("B3", "SSM parameters", "empty result")
    specs = ctx.source_defaults().get("parameter_specs") or {}
    steady = ctx.get("ssm.steady_state", {}) or {}
    references = ctx.get("ssm.references", {}) or {}
    rows, failures = [], []
    for parameter in parameters:
        name, value = parameter.get("Name", ""), str(parameter.get("Value", ""))
        key = name.rstrip("/").rsplit("/", 1)[-1]
        syntax = _syntax_problem(key, value)
        type_error = _type_problem(specs.get(key), value)
        switch = steady.get(key)
        refs = references.get(key, [])
        ref_errors = [item for item in refs if not item.get("exists") or
                      item.get("expected_role", item.get("role")) != item.get("role")]
        problems = [item for item in (syntax, type_error) if item]
        if switch is not None and str(value) != str(switch):
            problems.append("not in source-declared steady state")
        if ref_errors:
            problems.append("coordinate missing or wrong role")
        failures.extend("%s: %s" % (name, item) for item in problems)
        rows.append({"name": name, "value": value, "syntax_error": syntax,
                     "type_error": type_error, "steady_state": switch,
                     "reference_checks": refs, "problems": problems})
    return finding(
        "B3", "FAIL" if failures else "PASS",
        "Parameter coordinates, syntax, switches, and types were inspected.",
        {"inspected": len(rows), "parameters": rows}, failures,
        "Correct invalid coordinates, syntax, switch states, or source-declared types.",
    )


def check_b4(ctx):
    queue_url = ctx.get("scale.queue_url") or ctx.get("queues.lifecycle_url")
    promised = ctx.get("scale.deadline_budget")
    if promised is None:
        values = list((ctx.source_defaults().get("deadlines") or {}).values())
        promised = min(values) if values else None
    if ctx.offline or not queue_url or promised is None:
        return _missing("B4", "queue visibility and deadline")
    try:
        response = ctx.aws.call(
            "sqs", "get_queue_attributes", QueueUrl=queue_url,
            AttributeNames=["VisibilityTimeout"])
        raw = (response.get("Attributes") or {}).get("VisibilityTimeout")
        visibility = int(raw)
    except (AwsReadError, AwsUnavailable, TypeError, ValueError) as error:
        return _missing("B4", "queue visibility and deadline", error)
    mismatch = visibility > promised
    return finding(
        "B4", "FAIL" if mismatch else "PASS",
        "Queue visibility was compared with the declared deadline commitment.",
        {"inspected": 1, "visibility_timeout": visibility,
         "declared_deadline": promised, "later_than_commitment": mismatch},
        ["visibility exceeds the declared deadline"] if mismatch else [],
        "Set visibility no later than the declared deadline or revise the commitment.",
    )
