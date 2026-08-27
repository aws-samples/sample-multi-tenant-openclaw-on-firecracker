import base64
import gzip
import hashlib
import io
import json
import re
import zipfile

from lib.awsread import AwsReadError, AwsUnavailable
from lib.result import finding


USERDATA_FILES = {
    "launch-vm.sh", "host-agent.py", "oc-egress-sim.py",
    "stop-vm.sh", "rebuild-vm.sh", "init-host.sh",
}


def _critical(path):
    if path.startswith("deploy/lambda/api/") or path.startswith("deploy/edge/"):
        return True
    return path.startswith("deploy/userdata/") and path.rsplit("/", 1)[-1] in USERDATA_FILES


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _unavailable(check_id, label, error):
    return finding(
        check_id, "INCONCLUSIVE", "%s could not be read." % label,
        {"inspected": 0, "error": str(error)},
        remediation="Provide readable environment coordinates and retry.",
    )


def check_a1(ctx):
    rows, failures, live_count = [], [], 0
    for item in ctx.manifest_entries():
        if not _critical(item["path"]) or not item.get("sha256"):
            continue
        try:
            gateway = _sha(ctx.git_bytes(ctx.gateway_ref, item["path"]))
        except (OSError, RuntimeError) as error:
            gateway = None
            failures.append("%s gateway read: %s" % (item["path"], error))
        live = (ctx.get("live_files", {}) or {}).get(item["path"], {}).get("sha256")
        live_count += int(bool(live))
        gateway_ok = gateway == item["sha256"] if gateway else None
        live_ok = live == item["sha256"] if live else None
        if gateway_ok is False or live_ok is False:
            failures.append(item["path"])
        rows.append({
            "path": item["path"], "gateway_sha256": gateway,
            "manifest_sha256": item["sha256"], "live_sha256": live,
            "gateway_vs_manifest": gateway_ok, "manifest_vs_live": live_ok,
        })
    if not rows:
        return _unavailable("A1", "critical manifest paths", "no matching paths")
    verdict = "FAIL" if failures else ("UNVERIFIED" if not live_count else "PASS")
    return finding(
        "A1", verdict, "Gateway, manifest, and live readings were recorded separately.",
        {"inspected": len(rows), "live_inspected": live_count, "files": rows},
        failures, "Publish matching bytes or deploy the manifest bytes to the live target.",
    )


def check_a2(ctx):
    function = ctx.get("lambda_link.function")
    qualifier = ctx.get("lambda_link.serving_qualifier")
    if ctx.offline or not function:
        return _unavailable("A2", "Lambda package", "offline or function unresolved")
    try:
        package = ctx.aws.lambda_package(function, qualifier)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            names = archive.namelist()
    except (AwsReadError, AwsUnavailable, OSError, zipfile.BadZipFile) as error:
        return _unavailable("A2", "Lambda package", error)
    required = ["core/__init__.py", "core/auth.py"]
    present = {name: name in names for name in required}
    verdict = "PASS" if all(present.values()) else "FAIL"
    return finding(
        "A2", verdict, "The deployed package file set was inspected.",
        {"inspected": len(names), "required": present, "file_count": len(names)},
        [name for name, ok in present.items() if not ok],
        "Redeploy a complete overlay that preserves the full package file set.",
    )


def _decode_user_data(raw):
    decoded = base64.b64decode(raw)
    try:
        return gzip.decompress(decoded)
    except OSError:
        return decoded


def _unreplaced_tokens(text):
    found = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"\{\{[A-Za-z0-9_]+\}\}", line):
            found.append("%s:%s" % (line_no, line.strip()))
    return found


def check_a3(ctx):
    version = str(ctx.get("asg.lt_version_pinned", ""))
    lt_id = ctx.get("asg.lt_id")
    if not version:
        return _unavailable("A3", "launch template", "version unresolved")
    numeric = version.isdigit()
    if ctx.offline or not lt_id:
        verdict = "PASS" if numeric else "FAIL"
        return finding(
            "A3", verdict, "The pinned launch-template version was inspected.",
            {"inspected": 1, "version": version, "numeric": numeric,
             "user_data_inspected": 0},
            remediation="Pin a numeric version and provide launch-template coordinates.",
        )
    try:
        response = ctx.aws.call(
            "ec2", "describe_launch_template_versions",
            LaunchTemplateId=lt_id, Versions=[version])
        versions = response.get("LaunchTemplateVersions") or []
        raw = versions[0]["LaunchTemplateData"].get("UserData", "") if versions else ""
        rendered = _decode_user_data(raw)
    except (AwsReadError, AwsUnavailable, KeyError, ValueError, IndexError) as error:
        return _unavailable("A3", "launch-template user data", error)
    tokens = _unreplaced_tokens(rendered.decode("utf-8", "replace"))
    source_path = ctx.get("lt_bootstrap.source_path", "deploy/userdata/init-host.sh")
    try:
        gateway = ctx.git_bytes(ctx.gateway_ref, source_path)
        gateway_sha = _sha(gateway)
        source_tokens = sorted(set(re.findall(
            r"\{\{[A-Za-z0-9_]+\}\}", gateway.decode("utf-8", "replace"))))
    except (OSError, RuntimeError):
        gateway = None
        gateway_sha = None
        source_tokens = []
    rendered_sha = _sha(rendered)
    if gateway is None:
        source_comparable = {"comparable": False, "reason": "gateway source was unreadable"}
    elif source_tokens:
        source_comparable = {
            "comparable": False,
            "reason": "gateway source contains template tokens: %s" % ", ".join(source_tokens),
        }
    else:
        source_comparable = {"comparable": True, "reason": None}
    source_match = (
        rendered_sha == gateway_sha if source_comparable["comparable"] else None)
    failures = ([] if numeric else ["floating version"]) + tokens
    if source_match is False:
        failures.append("user data differs from gateway source")
    verdict = "FAIL" if failures else ("UNVERIFIED" if source_match is None else "PASS")
    return finding(
        "A3", verdict, "Pinned version, rendered tokens, and source bytes were compared.",
        {"inspected": len(versions), "version": version, "numeric": numeric,
         "tokens": tokens, "rendered_sha256": rendered_sha,
         "gateway_sha256": gateway_sha, "source_comparable": source_comparable,
         "source_match": source_match},
        failures, "Pin a numeric version and render user data from the gateway source.",
    )


def _export_body(ctx, response):
    body = response.get("body") if "body" in response else response.get("Body")
    raw = body.read() if hasattr(body, "read") else body
    return json.loads(bytes(raw or b"{}").decode("utf-8"))


def check_a4(ctx):
    api_id = ctx.get("control_plane_api.id")
    stages = ctx.get("control_plane_api.deployed_stages", []) or []
    expected = ctx.source_defaults().get("routes") or []
    if ctx.offline or not api_id or not stages:
        return _unavailable("A4", "deployed stage export", "stage unresolved")
    try:
        response = ctx.aws.call(
            "apigateway", "get_export", restApiId=api_id,
            stageName=stages[0]["stage"], exportType="oas30",
            parameters={"extensions": "integrations"})
        document = _export_body(ctx, response)
    except (AwsReadError, AwsUnavailable, ValueError, KeyError, TypeError) as error:
        return _unavailable("A4", "deployed stage export", error)
    paths = document.get("paths") or {}
    actual = {(path, method.upper()) for path, methods in paths.items()
              for method in methods if not method.startswith("x-")}
    missing = [row for row in expected if (row["path"], row["method"]) not in actual]
    option_auth = []
    for path, methods in paths.items():
        options = methods.get("options") if isinstance(methods, dict) else None
        if options:
            auth = (options.get("x-amazon-apigateway-auth") or {}).get("type")
            option_auth.append({"path": path, "authorizationType": auth})
    auth_values = {row["authorizationType"] for row in option_auth}
    failures = ["missing %s %s" % (row["method"], row["path"]) for row in missing]
    if len(auth_values) > 1:
        failures.append("OPTIONS authorization types differ")
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if not actual else "PASS")
    return finding(
        "A4", verdict, "Expected routes were compared with the deployed stage export.",
        {"inspected": len(actual), "expected": len(expected), "missing": missing,
         "options": option_auth},
        failures, "Deploy the missing routes and align OPTIONS authorization.",
    )
