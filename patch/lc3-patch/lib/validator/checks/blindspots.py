import ast
from pathlib import Path

from lib.awsread import AwsReadError, AwsUnavailable
from lib.comparison import compare_alias_versions
from lib.result import finding


KNOWN_FALSE_REDS = [
    "route verification reports service-added CORS keys",
    "consistency drift reflects an intentional out-of-band overlay",
    "launch-template token grep includes comment-only placeholders",
    "git show lacks the patch commit environment value",
    "grep count returns nonzero when the valid match count is zero",
]
def _missing(check_id, subject, error="nothing to inspect"):
    return finding(
        check_id, "INCONCLUSIVE", "%s was not inspectable." % subject,
        {"inspected": 0, "error": str(error)},
        remediation="Provide the missing observation in environment.json and retry.",
    )
def _config_view(config):
    return {
        "code_sha256": config.get("CodeSha256"),
        "environment": ((config.get("Environment") or {}).get("Variables") or {}),
        "dead_letter": config.get("DeadLetterConfig") or {},
        "layers": [item.get("Arn") for item in config.get("Layers") or []],
    }
def _lambda_versions(ctx, function):
    supplied = ctx.get("lambda_versions")
    if supplied:
        return {key: _config_view(value) for key, value in supplied.items()}, []
    latest = ctx.aws.call(
        "lambda", "get_function_configuration",
        FunctionName=function, Qualifier="$LATEST")
    aliases = ctx.aws.call("lambda", "list_aliases", FunctionName=function)
    versions, alias_rows = {"$LATEST": _config_view(latest)}, []
    for alias in aliases.get("Aliases") or []:
        version = alias.get("FunctionVersion")
        config = ctx.aws.call(
            "lambda", "get_function_configuration",
            FunctionName=function, Qualifier=version)
        versions[str(version)] = _config_view(config)
        alias_rows.append({"alias": alias.get("Name"), "version": str(version)})
    return versions, alias_rows
def check_e1(ctx):
    function = ctx.get("lambda_link.function")
    if ctx.offline or not function:
        return _missing("E1", "Lambda aliases")
    try:
        versions, aliases = _lambda_versions(ctx, function)
    except (AwsReadError, AwsUnavailable, KeyError) as error:
        return _missing("E1", "Lambda aliases", error)
    if not aliases:
        aliases = [{"alias": "serving", "version": key}
                   for key in versions if key != "$LATEST"]
    latest = versions.get("$LATEST")
    rows, failures, comparable_pairs = compare_alias_versions(latest, aliases, versions)
    if not comparable_pairs:
        reason = ("no alias versions were available" if not rows
                  else "no alias pairs were comparable")
        result = _missing(
            "E1", "Lambda aliases",
            "no divergence could be observed because %s" % reason)
        result.readings.update({"aliases": rows, "comparable_pairs": comparable_pairs})
        return result
    return finding(
        "E1", "FAIL" if failures else "PASS",
        "Alias versions and latest configuration were compared beyond code bytes.",
        {"inspected": len(rows), "comparable_pairs": comparable_pairs, "aliases": rows,
         "api_serves": ctx.get("lambda_link.serving_qualifier"),
         "sqs_serves": ctx.get("lambda_link.dispatch_sqs_esm_binds")},
        failures, "Publish the intended configuration and move every serving qualifier together.",
    )


def _unsafe_zero_grep(ctx):
    rows = []
    for verification in ctx.manifest.get("verifications") or []:
        action = str(verification.get("action") or "")
        if "grep -c" in action and not any(token in action for token in ("|| true", "|| :", "; true")):
            rows.append(action)
    return rows


def check_e2(ctx):
    probes = ctx.get("probes", []) or []
    if not probes:
        return _missing("E2", "probe observations")
    failures, rows = [], []
    for probe in probes:
        count = probe.get("object_count")
        status = probe.get("status")
        problem = None
        if not count:
            problem = "probe inspected zero objects"
        elif probe.get("kind") == "curl" and str(status).zfill(3) == "000":
            problem = "transport returned status 000"
        if problem:
            failures.append(problem)
        rows.append({"kind": probe.get("kind"), "object_count": count,
                     "status": status, "problem": problem})
    unsafe = _unsafe_zero_grep(ctx)
    failures.extend("unsafe zero-match grep: %s" % item for item in unsafe)
    return finding(
        "E2", "FAIL" if failures else "PASS",
        "Probe counts and transport semantics were checked for vacuous results.",
        {"inspected": len(rows), "probes": rows, "unsafe_zero_grep": unsafe},
        failures, "Make zero matches an explicit reading and distinguish transport failure.",
    )


def check_e3(ctx):
    live = ctx.get("live_files", {}) or {}
    rows, failures = [], []
    for item in ctx.manifest_entries():
        observed = live.get(item["path"])
        if not observed or not item.get("sha256"):
            continue
        hash_ok = observed.get("sha256") == item["sha256"]
        mode_ok = item.get("mode") is None or str(observed.get("mode")) == str(item["mode"])
        if not hash_ok or not mode_ok:
            failures.append(item["path"])
        rows.append({"path": item["path"], "manifest_sha256": item["sha256"],
                     "live_sha256": observed.get("sha256"),
                     "manifest_mode": item.get("mode"), "live_mode": observed.get("mode"),
                     "hash_match": hash_ok, "mode_match": mode_ok})
    if not rows:
        return _missing("E3", "live kit files")
    return finding(
        "E3", "FAIL" if failures else "PASS",
        "Live bytes and modes were compared with replayable kit declarations.",
        {"inspected": len(rows), "files": rows}, failures,
        "Capture every live hotfix in the kit with matching bytes and install mode.",
    )


def check_e4(ctx):
    samples = ctx.get("route_samples", []) or []
    if not samples:
        return _missing("E4", "tenant route samples")
    rows, failures = [], []
    for sample in samples:
        control, data = sample.get("control") or {}, sample.get("data") or {}
        match = control.get("host") == data.get("host") and control.get("port") == data.get("port")
        if not match:
            failures.append(str(sample.get("tenant_id")))
        rows.append({"tenant_id": sample.get("tenant_id"), "control": control,
                     "data": data, "match": match})
    return finding(
        "E4", "FAIL" if failures else "PASS",
        "Control-plane and data-plane route coordinates were compared.",
        {"inspected": len(rows), "samples": rows}, failures,
        "Repair the data-plane route so it matches the control-plane assignment.",
    )


def check_e5(ctx):
    endpoints = ctx.get("replica_endpoints", []) or []
    if not endpoints:
        return _missing("E5", "replica endpoint roles")
    failures = [item["name"] for item in endpoints
                if item.get("declared_role") != item.get("actual_role")]
    rows = [{"name": item.get("name"), "value": item.get("value"),
             "declared_role": item.get("declared_role"),
             "actual_role": item.get("actual_role"),
             "match": item.get("declared_role") == item.get("actual_role")}
            for item in endpoints]
    return finding(
        "E5", "FAIL" if failures else "PASS",
        "Endpoint roles were judged from observed roles rather than node numbering.",
        {"inspected": len(rows), "endpoints": rows}, failures,
        "Update consumers and parameters to use the endpoint's observed role.",
    )


def check_e6(ctx):
    endpoints = ctx.get("replica_endpoints", []) or []
    if not endpoints:
        return _missing("E6", "semantic endpoint parameters")
    rows, failures = [], []
    for item in endpoints:
        name = str(item.get("name", "")).lower()
        promised = "reader" if "reader" in name else ("primary" if "primary" in name else None)
        actual = item.get("actual_role")
        mismatch = promised is not None and promised != actual
        if mismatch:
            failures.append(item.get("name"))
        rows.append({"name": item.get("name"), "value": item.get("value"),
                     "name_promises": promised, "actual_role": actual,
                     "semantic_match": not mismatch})
    return finding(
        "E6", "FAIL" if failures else "PASS",
        "Parameter names were compared with the observed role of their values.",
        {"inspected": len(rows), "parameters": rows}, failures,
        "Rename the parameter or store a value with the role its name promises.",
    )


def _literal(node):
    values = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
    return " ".join(values)


def _fail_messages(path):
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return []
    rows = []
    for node in ast.walk(tree):
        message = ""
        if isinstance(node, ast.Raise) and node.exc:
            message = _literal(node.exc)
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name == "_err" and node.args:
                status = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
                if isinstance(status, int) and status >= 500:
                    message = _literal(node)
        if message and not message.isascii():
            rows.append({"line": getattr(node, "lineno", None), "message": message})
    return rows


def check_e7(ctx):
    files = list((Path(ctx.repo) / "deploy/lambda/api").rglob("*.py"))
    if not files:
        return _missing("E7", "gateway fail-closed source")
    rows = []
    for path in files:
        for item in _fail_messages(path):
            item["path"] = str(path.relative_to(ctx.repo))
            rows.append(item)
    return finding(
        "E7", "FAIL" if rows else "PASS",
        "Fail-closed exception messages were scanned for ASCII safety.",
        {"inspected": len(files), "non_ascii_messages": rows},
        ["%s:%s" % (item["path"], item["line"]) for item in rows],
        "Use an ASCII-safe transport message and keep localized detail in structured logs.",
    )


def check_e8(ctx):
    root = Path(ctx.repo) / "deploy/lambda"
    rows, failures = [], []
    for path in root.rglob("*.py"):
        try:
            text = path.read_text()
        except OSError:
            continue
        if "hosts_table" not in text or ".scan(" not in text:
            continue
        excludes = "startswith(\"__\")" in text or "startswith('__')" in text
        rows.append({"path": str(path.relative_to(ctx.repo)),
                     "host_scan": True, "excludes_synthetic": excludes})
        if not excludes:
            failures.append(str(path.relative_to(ctx.repo)))
    if not rows:
        return _missing("E8", "host enumeration source")
    return finding(
        "E8", "FAIL" if failures else "PASS",
        "Host scans were checked for synthetic-row exclusion.",
        {"inspected": len(rows), "enumerations": rows}, failures,
        "Exclude rows whose host identifier uses the synthetic prefix.",
    )


def check_e9(ctx):
    observations = ctx.get("residue_observations", []) or []
    if not observations:
        return _missing("E9", "test residue observations")
    present = [item for item in observations if item.get("present")]
    return finding(
        "E9", "FAIL" if present else "PASS",
        "Test tenants, VM directories, processes, host entries, and tunnels were reviewed.",
        {"inspected": len(observations), "observations": observations},
        ["%s: %s" % (item.get("kind"), item.get("value")) for item in present],
        "Remove only the identified test residue and repeat the read-only inspection.",
    )


def check_e10(ctx):
    annotations = ctx.get("known_false_red_observations", []) or []
    return finding(
        "E10", "PASS",
        "Known false-red shapes are annotated and are not rollback triggers alone.",
        {"inspected": len(KNOWN_FALSE_REDS), "known_count": len(KNOWN_FALSE_REDS),
         "known_false_reds": KNOWN_FALSE_REDS, "observed_annotations": annotations},
        remediation="Correlate an annotated signal with independent evidence before rollback.",
    )
