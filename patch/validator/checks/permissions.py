import ast
import datetime
import re
from pathlib import Path
from lib.awsread import AwsReadError, AwsUnavailable
from lib.result import finding
BOTO_CLIENT = "boto3" + ".client"
def _missing(check_id, subject, error="missing input"):
    return finding(
        check_id, "INCONCLUSIVE", "%s was not inspectable." % subject,
        {"inspected": 0, "error": str(error)},
        remediation="Provide readable role and runtime coordinates, then retry.",
    )
def _name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))
def _operation(method):
    return "".join(part.capitalize() for part in method.split("_"))
def _client_handles(tree):
    handles = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Call):
                    continue
                call = child.value
                if _name(call.func) == BOTO_CLIENT and call.args:
                    service = call.args[0].value if isinstance(call.args[0], ast.Constant) else None
                    if service:
                        handles[node.name] = service
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if _name(call.func) == BOTO_CLIENT and call.args:
            service = call.args[0].value if isinstance(call.args[0], ast.Constant) else None
            for target in node.targets:
                if isinstance(target, ast.Name) and service:
                    handles[target.id] = service
        if _name(call.func).endswith(".Table"):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    handles[target.id] = "dynamodb"
    return handles
def _root_handle(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None
def _actions(repo, sources):
    client_file = Path(repo) / "deploy/lambda/api/core/clients.py"
    handles, rows = {}, []
    for path in [client_file] + [Path(repo) / item for item in sources]:
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
        handles.update(_client_handles(tree))
    for relative in sources:
        path = Path(repo) / relative
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            root = _root_handle(node.func.value)
            service = handles.get(root)
            if isinstance(node.func.value, ast.Attribute):
                service = handles.get(node.func.value.attr, service)
            if isinstance(node.func.value, ast.Call):
                service = handles.get(_name(node.func.value.func), service)
            if service and method not in ("client", "resource", "Table"):
                rows.append({"action": service + ":" + _operation(method),
                             "site": "%s:%s" % (relative, node.lineno),
                             "method": method})
    unique = {(row["action"], row["site"]): row for row in rows}
    return list(unique.values())
def check_c1(ctx):
    roles = ctx.get("iam.roles", []) or []
    if ctx.offline or not roles:
        return _missing("C1", "IAM role mappings")
    rows, missing = [], []
    for role in roles:
        calls = _actions(ctx.repo, role.get("sources") or [])
        actions = sorted({item["action"] for item in calls})
        if not actions:
            rows.append({"role": role.get("name"), "actions": [], "inspected": 0})
            continue
        try:
            response = ctx.aws.call(
                "iam", "simulate_principal_policy",
                PolicySourceArn=role["source_arn"], ActionNames=actions,
                ResourceArns=["*"])
        except (AwsReadError, AwsUnavailable, KeyError) as error:
            return _missing("C1", "IAM simulation", error)
        decisions = {item.get("EvalActionName"): item.get("EvalDecision")
                     for item in response.get("EvaluationResults") or [] if
                     item.get("EvalActionName")}
        if not decisions and len(actions) == 1:
            result = (response.get("EvaluationResults") or [{}])[0]
            decisions[actions[0]] = result.get("EvalDecision")
        denied = [action for action in actions if decisions.get(action) != "allowed"]
        missing.extend("%s: %s" % (role.get("name"), action) for action in denied)
        rows.append({"role": role.get("name"), "inspected": len(actions),
                     "actions": calls, "decisions": decisions, "under_grant": denied})
    inspected = sum(row["inspected"] for row in rows)
    if not inspected:
        return _missing("C1", "source AWS call sites", "no calls found")
    return finding(
        "C1", "FAIL" if missing else "PASS",
        "Source call methods were simulated against their runtime roles.",
        {"inspected": inspected, "roles": rows}, missing,
        "Grant each missing action at the narrowest resource scope used by the call site.",
    )

def _statements(document):
    statements = document.get("Statement") or []
    return [statements] if isinstance(statements, dict) else list(statements)


def _deny_traps(documents):
    traps = []
    for item in documents:
        for statement in _statements(item.get("document") or {}):
            condition = statement.get("Condition") or {}
            values = condition.get("ForAllValues:StringEquals") or {}
            for key, protected in values.items():
                if statement.get("Effect") == "Deny" and key.endswith("LeadingKeys"):
                    actions = statement.get("Action") or []
                    traps.append({
                        "role": item.get("role"), "key": key,
                        "protected": protected if isinstance(protected, list) else [protected],
                        "actions": actions if isinstance(actions, list) else [actions],
                    })
    return traps


def check_c2(ctx):
    documents = ctx.get("iam.policy_documents", []) or []
    traps = _deny_traps(documents)
    if ctx.offline or not traps:
        return _missing("C2", "Deny statements", "no matching statements")
    roles = {item.get("name"): item for item in ctx.get("iam.roles", []) or []}
    rows, failures = [], []
    for trap in traps:
        role = roles.get(trap["role"], {})
        mixed = list(trap["protected"]) + ["OTHER_KEY_PLACEHOLDER"]
        try:
            response = ctx.aws.call(
                "iam", "simulate_principal_policy",
                PolicySourceArn=role["source_arn"], ActionNames=trap["actions"],
                ResourceArns=["*"], ContextEntries=[{
                    "ContextKeyName": trap["key"],
                    "ContextKeyValues": mixed,
                    "ContextKeyType": "stringList",
                }])
        except (AwsReadError, AwsUnavailable, KeyError) as error:
            return _missing("C2", "mixed-key deny simulation", error)
        decisions = [item.get("EvalDecision") for item in
                     response.get("EvaluationResults") or []]
        ok = bool(decisions) and all(value == "explicitDeny" for value in decisions)
        if not ok:
            failures.append("%s mixed-key batch was not explicitly denied" % trap["role"])
        rows.append({"role": trap["role"], "key": trap["key"],
                     "mixed_context": mixed, "decisions": decisions, "ok": ok})
    return finding(
        "C2", "FAIL" if failures else "PASS",
        "Deny conditions were tested with protected and unrelated keys together.",
        {"inspected": len(rows), "simulations": rows}, failures,
        "Replace the all-values condition with policy logic that denies mixed batches.",
    )


def _request_id(message):
    match = re.search(r"(?:requestId|request_id)[=: ]+([A-Za-z0-9-]+)", message)
    return match.group(1) if match else None


def check_c3(ctx):
    if ctx.offline:
        return _missing("C3", "Lambda logs", "offline")
    functions = ctx.get("lambda_functions")
    try:
        if not functions:
            response = ctx.aws.call("lambda", "list_functions")
            functions = [item.get("FunctionName") for item in
                         response.get("Functions") or [] if item.get("FunctionName")]
        if not functions:
            return _missing("C3", "Lambda logs", "no functions")
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=3)
        rows, issues = [], []
        for name in functions:
            logs = ctx.aws.call(
                "logs", "filter_log_events",
                logGroupName="/aws/lambda/" + name,
                startTime=int(start.timestamp() * 1000),
                endTime=int(end.timestamp() * 1000),
                filterPattern="?ERROR ?WARN").get("events") or []
            metric = ctx.aws.call(
                "cloudwatch", "get_metric_statistics",
                Namespace="AWS/Lambda", MetricName="Invocations",
                Dimensions=[{"Name": "FunctionName", "Value": name}],
                StartTime=start, EndTime=end, Period=int((end - start).total_seconds()),
                Statistics=["Sum"])
            invocations = sum(item.get("Sum", 0) for item in metric.get("Datapoints") or [])
            entries = [{"timestamp": item.get("timestamp"),
                        "requestId": _request_id(item.get("message", "")),
                        "message": item.get("message", "")} for item in logs]
            issues.extend("%s: %s" % (name, item["message"]) for item in entries)
            rows.append({"function": name, "invocations": invocations,
                         "events": entries, "event_count": len(entries)})
    except (AwsReadError, AwsUnavailable, TypeError) as error:
        return _missing("C3", "Lambda logs", error)
    return finding(
        "C3", "FAIL" if issues else "PASS",
        "Recent warning and error logs were listed with invocation context.",
        {"inspected": len(rows), "window_days": 3, "functions": rows}, issues,
        "Investigate every listed event and correlate it with the invocation count.",
    )


def check_c4(ctx):
    if ctx.offline:
        return _missing("C4", "alarms", "offline")
    try:
        alarms = ctx.aws.call("cloudwatch", "describe_alarms").get("MetricAlarms") or []
    except (AwsReadError, AwsUnavailable) as error:
        return _missing("C4", "alarms", error)
    if not alarms:
        return _missing("C4", "alarms", "empty result")
    known = ctx.get("known_dimension_values", {}) or {}
    rows, failures = [], []
    now = datetime.datetime.now(datetime.timezone.utc)
    for alarm in alarms:
        missing = [item for item in alarm.get("Dimensions") or []
                   if item.get("Name") in known and item.get("Value") not in known[item["Name"]]]
        stale = False
        updated = alarm.get("StateUpdatedTimestamp")
        window = alarm.get("Period", 0) * alarm.get("EvaluationPeriods", 0)
        if alarm.get("StateValue") == "INSUFFICIENT_DATA" and updated and window:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=datetime.timezone.utc)
            stale = (now - updated).total_seconds() > window
        if missing or stale:
            failures.append(alarm.get("AlarmName", "unnamed alarm"))
        rows.append({"alarm": alarm.get("AlarmName"), "state": alarm.get("StateValue"),
                     "missing_dimensions": missing, "stale_insufficient_data": stale})
    return finding(
        "C4", "FAIL" if failures else "PASS",
        "Alarm dimensions and prolonged insufficient-data states were inspected.",
        {"inspected": len(rows), "alarms": rows}, failures,
        "Correct missing dimension targets and repair alarms without recent data.",
    )
