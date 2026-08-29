import ast
import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
import textwrap
import time
from pathlib import Path

from lib.awsread import AwsReadError, AwsUnavailable, quote_path
from lib.result import finding


# Terminal-status polling for the read-only SSM probes. Pending/InProgress/Delayed are the
# three non-terminal states the API can report; everything else (Success, Failed,
# TimedOut, Cancelled, Cancelling, Undeliverable, InvalidPlatform, AccessDenied,
# DeliveryTimedOut, ExecutionTimedOut) is final and must be reported as-is rather than
# retried.
SSM_NON_TERMINAL_STATUSES = frozenset({"Pending", "InProgress", "Delayed"})
SSM_POLL_INTERVAL_SEC = 4
# A sha256sum over three 6-8 GiB ext4 images is minutes of disk read, so the ceiling is
# generous. A probe that exceeds it reports its last non-terminal status and the caller
# turns that into INCONCLUSIVE.
SSM_POLL_TIMEOUT_SEC = 900

MARKER_PATH = "/etc/openclaw/.ami-provisioned"
PLATFORM_ENV_PATH = "/etc/platform.env"
SKILL_STATE_PATH = "/var/lib/openclaw/shared-skills-sync-state.json"
SSM_AGENT_CONFIG_PATH = "/etc/amazon/ssm/amazon-ssm-agent.json"
ASSET_DIR = "/home/ubuntu/firecracker-assets"
DISK_PATHS = (
    ASSET_DIR + "/openclaw-rootfs.ext4",
    ASSET_DIR + "/openclaw-data-template.ext4",
    ASSET_DIR + "/openclaw-immutable.ext4",
)
OBS_RESOURCE_TYPES = {
    "AWS::KinesisFirehose::DeliveryStream",
    "AWS::OpenSearchService::Domain",
}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _gateway_bytes(ctx, path):
    return ctx.git_bytes(ctx.gateway_ref, path)


def _gateway_text(ctx, path):
    return _gateway_bytes(ctx, path).decode("utf-8", "replace")


def _unresolved_reason(discovered, coordinate):
    for item in discovered.get("unresolved") or []:
        if isinstance(item, dict) and item.get("coordinate") == coordinate:
            return str(item.get("reason") or "coordinate was unresolved")
        if isinstance(item, str) and item.startswith(coordinate):
            return item
    return "coordinate %s was unresolved" % coordinate


def _coordinate(ctx, coordinate):
    try:
        discovered = ctx.discovered()
    except (AwsReadError, AwsUnavailable, OSError, ValueError, TypeError) as error:
        return None, "discovered", str(error)
    value = discovered.get(coordinate)
    source = (discovered.get("sources") or {}).get(
        coordinate, "discovered")
    if value is None or value == "" or value == [] or value == {}:
        return None, source, _unresolved_reason(discovered, coordinate)
    return value, source, None


def _missing(check_id, subject, reason, readings=None, verdict="INCONCLUSIVE"):
    values = {"inspected": 0, "subject": subject, "reason": str(reason)}
    values.update(readings or {})
    return finding(
        check_id, verdict, "%s could not be verified." % subject,
        values,
        remediation="Restore the missing read-only evidence and run the check again.",
    )


def _body_bytes(ctx, response):
    if hasattr(ctx.aws, "body_bytes"):
        return ctx.aws.body_bytes(response)
    body = response.get("Body")
    if body is None:
        return b""
    return body.read() if hasattr(body, "read") else bytes(body)


def _decode_user_data(raw):
    decoded = base64.b64decode(raw or "")
    try:
        return gzip.decompress(decoded)
    except OSError:
        return decoded


def _active_tokens(text):
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"\{\{[^{}]+\}\}", line):
            rows.append(line_number)
    return rows


def _asg_launch_template(asg):
    direct = asg.get("LaunchTemplate") or {}
    if direct:
        return direct
    mixed = asg.get("MixedInstancesPolicy") or {}
    return ((mixed.get("LaunchTemplate") or {}).get(
        "LaunchTemplateSpecification") or {})


def _coordinate_classification(ctx, coordinate):
    try:
        discovered = ctx.discovered()
    except (AwsReadError, AwsUnavailable, OSError, ValueError, TypeError):
        return None
    return ((discovered.get("classifications") or {}).get(
        coordinate) or {}).get("classified_by")


def _numeric_version(value):
    text = str(value or "")
    return int(text) if text.isdigit() else None


def _launch_template_version_data(ctx, lt_id, version):
    response = ctx.aws.call(
        "ec2", "describe_launch_template_versions",
        LaunchTemplateId=lt_id, Versions=[str(version)])
    versions = response.get("LaunchTemplateVersions") or []
    if not versions:
        return None, "launch-template version returned no data"
    row = versions[0]
    raw = (row.get("LaunchTemplateData") or {}).get("UserData", "")
    if not raw:
        return {
            "effective_version": (
                _numeric_version(row.get("VersionNumber"))
                or _numeric_version(version)),
            "user_data": "",
        }, "launch-template user data is empty"
    return {
        "effective_version": (
            _numeric_version(row.get("VersionNumber"))
            or _numeric_version(version)),
        "user_data": _decode_user_data(raw).decode("utf-8", "replace"),
    }, None


def _effective_launch_template(ctx, lt_id, version_source):
    source = str(version_source or "")
    result = {
        "version_source": source,
        "effective_version": None,
        "user_data": "",
        "reason": None,
    }
    try:
        if source == "$Default":
            response = ctx.aws.call(
                "ec2", "describe_launch_templates",
                LaunchTemplateIds=[lt_id])
            templates = response.get("LaunchTemplates") or []
            if not templates:
                result["reason"] = "launch template returned no metadata"
                return result
            effective = _numeric_version(
                templates[0].get("DefaultVersionNumber"))
            if effective is None:
                result["reason"] = (
                    "launch template returned no numeric DefaultVersionNumber")
                return result
        elif source == "$Latest":
            data, reason = _launch_template_version_data(
                ctx, lt_id, source)
            if data:
                result.update(data)
            result["reason"] = reason
            return result
        else:
            effective = _numeric_version(source)
            if effective is None:
                result["reason"] = (
                    "unsupported launch-template version token %s" % source)
                return result
        data, reason = _launch_template_version_data(
            ctx, lt_id, effective)
    except (AwsReadError, AwsUnavailable, ValueError) as error:
        result["reason"] = str(error)
        return result
    if data:
        result.update(data)
    result["effective_version"] = effective
    result["reason"] = reason
    return result


def _bootstrap_reference(user_data):
    match = re.search(
        r"deployment/bootstrap/host/([0-9a-fA-F]{64})/init-host\.sh",
        str(user_data or ""))
    if not match:
        return None, None
    digest = match.group(1).lower()
    return digest, "deployment/bootstrap/host/%s/init-host.sh" % digest


def _not_found(error):
    text = str(error).lower()
    return any(token in text for token in (
        "404", "not found", "nosuchkey", "resourcenotfound",
    ))


def _describe_asg(ctx, name):
    response = ctx.aws.call(
        "autoscaling", "describe_auto_scaling_groups",
        AutoScalingGroupNames=[name])
    rows = response.get("AutoScalingGroups") or []
    return rows[0] if rows else None


def _host_bootstrap(ctx):
    asg_name, asg_source, asg_error = _coordinate(ctx, "host_asg")
    lt_id, lt_source, lt_error = _coordinate(ctx, "host_lt")
    if not asg_name or not lt_id:
        return None, {
            "host_asg": asg_name, "host_asg_source": asg_source,
            "host_lt": lt_id, "host_lt_source": lt_source,
            "reason": asg_error or lt_error,
        }
    asg = _describe_asg(ctx, asg_name)
    if not asg:
        return None, {
            "host_asg": asg_name, "host_asg_source": asg_source,
            "host_lt": lt_id, "host_lt_source": lt_source,
            "reason": "describe_auto_scaling_groups returned no host ASG",
        }
    spec = _asg_launch_template(asg)
    version_source = str(spec.get("Version") or "")
    effective_lt = str(spec.get("LaunchTemplateId") or lt_id)
    if not version_source:
        return None, {
            "host_asg": asg_name, "host_asg_source": asg_source,
            "host_lt": effective_lt, "host_lt_source": lt_source,
            "version_source": version_source,
            "reason": "host ASG launch-template version is empty",
        }
    resolved = _effective_launch_template(
        ctx, effective_lt, version_source)
    return {
        "asg_name": asg_name,
        "asg_source": asg_source,
        "lt_id": effective_lt,
        "lt_source": lt_source,
        "classified_by": _coordinate_classification(ctx, "host_lt"),
        "version": version_source,
        "version_source": version_source,
        "effective_version": resolved["effective_version"],
        "numeric": version_source.isdigit(),
        "user_data": resolved["user_data"],
        "reason": resolved["reason"],
    }, None


def _instance_rows(response):
    rows = []
    for reservation in response.get("Reservations") or []:
        rows.extend(reservation.get("Instances") or [])
    return rows


def _describe_instances(ctx, filters=None, instance_ids=None):
    rows = []
    token = None
    while True:
        kwargs = {}
        if filters:
            kwargs["Filters"] = filters
        if instance_ids:
            kwargs["InstanceIds"] = list(instance_ids)
        if token:
            kwargs["NextToken"] = token
        response = ctx.aws.call("ec2", "describe_instances", **kwargs)
        rows.extend(_instance_rows(response))
        token = response.get("NextToken")
        if not token:
            return rows


def _version_number(instance):
    return _numeric_version(
        (instance.get("LaunchTemplate") or {}).get("Version"))


def _sample_metal_hosts(ctx):
    cached = getattr(ctx, "_validator_metal_hosts", None)
    if cached is not None:
        return cached
    lt_id, source, reason = _coordinate(ctx, "host_lt")
    if not lt_id:
        result = ([], {
            "host_lt": lt_id, "source": source, "reason": reason,
        })
        setattr(ctx, "_validator_metal_hosts", result)
        return result
    latest_response = ctx.aws.call(
        "ec2", "describe_launch_template_versions",
        LaunchTemplateId=lt_id, Versions=["$Latest"])
    latest_rows = latest_response.get("LaunchTemplateVersions") or []
    latest = latest_rows[0] if latest_rows else {}
    latest_number = latest.get("VersionNumber")
    latest_number = int(latest_number) if str(latest_number).isdigit() else None
    instances = _describe_instances(ctx, filters=[
        {"Name": "tag:Role", "Values": ["metal-host"]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    samples = []
    before = 0
    after = 0
    latest_created = latest.get("CreateTime")
    for instance in instances:
        instance_id = str(instance.get("InstanceId") or "")
        if not instance_id:
            continue
        number = _version_number(instance)
        generation = None
        if latest_number is not None and number is not None:
            generation = "before" if number < latest_number else "after"
        elif latest_created is not None and instance.get("LaunchTime") is not None:
            generation = (
                "before" if instance.get("LaunchTime") < latest_created else "after")
        before += int(generation == "before")
        after += int(generation == "after")
        samples.append({
            "instance_id": instance_id,
            "lt_version": number,
            "generation": generation,
            "architecture": instance.get("Architecture"),
            "launch_time": instance.get("LaunchTime"),
        })
    error = None
    if not samples:
        error = "no running Role=metal-host instances were returned"
    elif before == 0 or after == 0:
        error = (
            "sampling requires at least one host before and one host at or after "
            "the newest launch-template version")
    result = (samples, {
        "host_lt": lt_id,
        "source": source,
        "latest_version": latest_number,
        "before": before,
        "after": after,
        "reason": error,
    })
    setattr(ctx, "_validator_metal_hosts", result)
    return result


def _oldest_launched_host(hosts):
    if not hosts or any(host.get("launch_time") is None for host in hosts):
        return None

    def launch_key(host):
        value = host["launch_time"]
        text = value.isoformat() if hasattr(value, "isoformat") else str(value)
        return text, host["instance_id"]

    return min(hosts, key=launch_key)


def _record_fleet_divergence(failures, reference_id, instance_id, key):
    failures.append("%s:%s" % (reference_id, key))
    failures.append("%s:%s" % (instance_id, key))


def _edge_instances(ctx):
    asg_name, source, reason = _coordinate(ctx, "edge_asg")
    if not asg_name:
        return [], {"edge_asg": None, "source": source, "reason": reason}
    asg = _describe_asg(ctx, asg_name)
    if not asg:
        return [], {
            "edge_asg": asg_name, "source": source,
            "reason": "describe_auto_scaling_groups returned no edge ASG",
        }
    launch_template = _asg_launch_template(asg)
    launch_template_id = str(
        launch_template.get("LaunchTemplateId") or "")
    version_source = str(launch_template.get("Version") or "")
    identifiers = [
        str(item.get("InstanceId")) for item in asg.get("Instances") or []
        if item.get("InstanceId")
    ]
    if not identifiers:
        return [], {
            "edge_asg": asg_name, "source": source,
            "launch_template_id": launch_template_id,
            "version_source": version_source,
            "reason": "edge ASG has no instances",
        }
    instances = _describe_instances(ctx, instance_ids=identifiers)
    rows = [{"instance_id": str(item.get("InstanceId") or "")}
            for item in instances if item.get("InstanceId")]
    return rows, {
        "edge_asg": asg_name, "source": source,
        "launch_template_id": launch_template_id,
        "version_source": version_source,
        "reason": None if rows else "edge instances could not be described",
    }


def _ssm_read(ctx, instance_id, command):
    response = ctx.aws.call(
        "ssm", "send_command",
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]})
    command_id = str((response.get("Command") or {}).get("CommandId") or "")
    if not command_id:
        return {"ok": False, "status": "MissingCommandId", "output": ""}
    # SSM never answers Success on the first read -- send_command returns as soon as the
    # request is accepted, so a single get_command_invocation always reports InProgress.
    # Reading once made every host probe unreadable on real infrastructure, which turned
    # all eight host-side channels into permanent INCONCLUSIVE and, worse, let F7 read a
    # never-finished `stat` as "directory absent" and report a FAIL. Poll to a terminal
    # status instead, and let the caller distinguish "did not finish" from "disagrees".
    status = ""
    output = ""
    deadline = time.time() + SSM_POLL_TIMEOUT_SEC
    while True:
        try:
            invocation = ctx.aws.call(
                "ssm", "get_command_invocation",
                CommandId=command_id, InstanceId=instance_id)
        except (AwsReadError, AwsUnavailable) as error:
            # send_command returns once the request is accepted, before the per-instance
            # invocation record exists, so the first read can legitimately 404 with
            # InvocationDoesNotExist. Letting that propagate killed an entire check
            # non-deterministically -- F6 died this way on a healthy fleet while its
            # neighbours passed. Keep polling until the deadline; a record that never
            # appears surfaces as a status, not as an exception.
            if "invocationdoesnotexist" not in str(error).lower():
                raise
            if time.time() >= deadline:
                return {"ok": False, "status": "InvocationDoesNotExist", "output": ""}
            time.sleep(SSM_POLL_INTERVAL_SEC)
            continue
        status = str(invocation.get("Status") or "")
        output = str(invocation.get("StandardOutputContent") or "")
        if status not in SSM_NON_TERMINAL_STATUSES:
            break
        if time.time() >= deadline:
            break
        time.sleep(SSM_POLL_INTERVAL_SEC)
    return {"ok": status == "Success", "status": status, "output": output}


def _parse_hashes(output):
    rows = {}
    for line in output.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match:
            rows[match.group(2)] = match.group(1).lower()
    return rows


def _hash_paths(ctx, hosts, paths):
    command = "sha256sum " + " ".join(quote_path(path) for path in paths)
    rows = {}
    for host in hosts:
        instance_id = host["instance_id"]
        result = _ssm_read(ctx, instance_id, command)
        rows[instance_id] = {
            "status": result["status"],
            "hashes": _parse_hashes(result["output"]),
        }
    return rows


def _parse_key_values(text):
    values = {}
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value
    return values


def _parse_firecracker_pins(ctx):
    text = _gateway_text(ctx, "deploy/userdata/provision-host.sh")
    version_match = re.search(
        r'FC_VER="\$\{FC_VERSION:-([^}]+)\}"', text)
    pins = {}
    pattern = re.compile(
        r"([A-Za-z0-9._-]+):(aarch64|x86_64)\)\s+"
        r"printf\s+'([0-9a-fA-F]{64})'")
    for version, architecture, digest in pattern.findall(text):
        pins[(version, architecture)] = digest.lower()
    return {
        "version": version_match.group(1) if version_match else None,
        "pins": pins,
    }


def _s3_digest(ctx, bucket, key):
    head = ctx.aws.call("s3", "head_object", Bucket=bucket, Key=key)
    response = ctx.aws.call("s3", "get_object", Bucket=bucket, Key=key)
    data = _body_bytes(ctx, response)
    return {
        "sha256": _sha(data),
        "version_id": (
            response.get("VersionId") or head.get("VersionId")),
        "bytes": data,
    }


def _required_scripts(ctx):
    text = _gateway_text(ctx, "deploy/userdata/required-scripts.list")
    paths = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    paths.extend([
        "spire-kit/spire-kit-setup.sh",
        "spire-kit/install.sh",
        "spire-kit/spire-join-broker.py",
        "spire-kit/spire-join-broker.service",
    ])
    return paths


def _static_assignments(text):
    assignments = {}
    for name, value in re.findall(
            r"^\s*([A-Z][A-Z0-9_]*)=([^\s#]+)", text, re.MULTILINE):
        assignments[name] = value.strip("\"'")
    return assignments


def _substitute_shell(text, values):
    for name, value in values.items():
        text = text.replace("${%s}" % name, value)
        text = text.replace("$%s" % name, value)
    return text


def _landing_paths(ctx):
    text = _gateway_text(ctx, "deploy/userdata/init-host.sh")
    text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#"))
    text = text.replace("\\\n", " ")
    assignments = _static_assignments(text)
    expanded = [text]
    loop_pattern = re.compile(
        r"for\s+(_[A-Za-z0-9_]+)\s+in\s+([^;]+);\s*do(.*?)done",
        re.DOTALL)
    for variable, raw_items, body in loop_pattern.findall(text):
        for item in raw_items.split():
            values = dict(assignments)
            values[variable] = item
            expanded.append(_substitute_shell(body, values))
    combined = "\n".join(expanded)
    rows = {}
    pattern = re.compile(
        r"(?:aws\s+s3\s+cp|_s3_get)\s+"
        r"[\"']?s3://\$\{ASSETS_BUCKET\}/deployment/scripts/"
        r"([^\"'\s]+)[\"']?\s+([^;\s]+)")
    for source, destination in pattern.findall(combined):
        source = _substitute_shell(source, assignments).strip("\"'")
        destination = _substitute_shell(
            destination, assignments).strip("\"'")
        if "${" in source or "${" in destination:
            continue
        rows[source] = destination
    return rows


# `if [ "${VAR}" = "value" ]` and `if [ "${VAR:-default}" = "value" ]`, which are the two
# shapes init-host.sh actually uses to gate a fetch. Anything more exotic is deliberately
# not matched: an unrecognised guard must leave the fetch unconditional so a genuinely
# missing file is still reported, rather than being excused by a guard we mis-read.
_FETCH_GUARD_OPEN = re.compile(
    r"""^\s*if\s*\[\s*"\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}"\s*"""
    r"""(?:==|=)\s*(?P<quote>["'])(?P<equals>[^"'\r\n]*)(?P=quote)\s*\]\s*;\s*then\s*$"""
)
_CP_SOURCE = re.compile(
    r"""(?:aws\s+s3\s+cp|_s3_get)\s+["']?s3://\$\{ASSETS_BUCKET\}"""
    r"""/deployment/scripts/([^"'\s]+)"""
)


_FOR_OPEN = re.compile(
    r"^\s*for\s+(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s+in\s+(?P<items>[^;\n]+?)\s*;\s*do\s*$")
# `re.MULTILINE` is load-bearing, not decoration: this pattern is `finditer`-ed over the WHOLE
# joined script, so without it `^` only anchors at offset 0 and the only assignment that can
# ever match is one sitting on the first non-blank line. On the real init-host.sh that meant
# zero matches -- so `/openclaw/spire-kit/enabled` was never learned, the spire-kit gate fell
# through to "unrecognised" and its four files were reported missing on every correctly
# configured fleet. The unit fixtures hid it by putting the assignment first; proven on a live
# fleet 2026-08-29 (0 matches without the flag, 7 with it). `--name` may sit on a backslash
# continuation line, which is why the gap before it is `[^)]*?` rather than `[^)\n]*?`.
_SSM_PARAM_ASSIGN = re.compile(
    r"""^\s*(?P<var>_?[A-Za-z_][A-Za-z0-9_]*)=\$\(\s*aws\s+ssm\s+get-parameter\b"""
    r"""[^)]*?--name\s+(?P<name>[^\s)"']+)""",
    re.MULTILINE,
)
# `if [ "$( ... "${VAR}" ... )" = "value" ]` -- the shape init-host.sh uses to normalise an
# SSM parameter before comparing it. Only the variable identity and the literal matter.
_FETCH_GUARD_SUBSHELL = re.compile(
    r"""^\s*if\s*\[\s*"\$\((?P<body>.*?)\)"\s*(?:==|=)\s*"""
    r"""(?P<quote>["'])(?P<equals>[^"'\r\n]*)(?P=quote)\s*\]\s*;\s*then\s*$"""
)
# `if ! aws s3 cp <primary>; then` -- the fallback arm. init-host.sh uses it once, for
# adot-config.yaml: the ADOT config is normally taken from `deployment/observability/adot/`
# and only re-fetched from the old `deployment/scripts/` prefix when that fails. So the
# `deployment/scripts/` copy is absent on every healthy host, which is the opposite of drift.
# Recognising the shape is what lets F3 reach PASS at all; without it the check sat at
# INCONCLUSIVE on a correct fleet, and a check that can never go green cannot be used as the
# baseline for an injected-drift test.
_FETCH_FALLBACK_ARM = re.compile(r"^\s*if\s*!\s*(?:aws\s+s3\s+cp|_s3_get)\b")
# Heredoc openers: `<<TAG`, `<<'TAG'`, `<<"TAG"` and the `<<-` variants. The body has to be
# skipped before the `if`/`fi` stack is walked, because init-host.sh embeds a Python heredoc
# inside the `EGRESS_MODE = deny` block and Python's `if not port:` lines look exactly like
# shell `if` to a line-based parser -- with no `fi` to ever pop them. The stack then never
# drains and every fetch AFTER that block inherits `EGRESS_MODE = deny` as its guard, which is
# the dangerous direction: a guard that is not satisfied makes absence excusable, so 24 of the
# 27 managed scripts silently stopped being judged at all (found on a live fleet, 2026-08-29).
# `<<<` here-strings do not match: the third `<` is neither a quote nor a tag character.
_HEREDOC_OPEN = re.compile(
    r"""<<-?(?P<quote>["']?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"""
)


def _fetch_guards(ctx):
    """Map each managed-script fetch to the condition that encloses it, if any.

    Absence on a host is only drift when the fetch was supposed to run. init-host.sh gates
    several fetches: oc-guest-log-reader.py and the ADOT config behind LOGGING_ENABLED=true,
    oc-egress-chain.sh / oc-egress-sim.py behind EGRESS_MODE=deny, and the four spire-kit
    files behind the SSM parameter /openclaw/spire-kit/enabled. Reporting those as unreadable
    turned a correctly-deployed fleet into rows of missing evidence -- the same false-red
    shape as treating an unfinished probe as a negative observation.

    Two structures have to be tracked at once, because both gated groups above live inside a
    `for` loop nested in an `if`: expanding the loops in a separate pass (the way
    `_landing_paths` does) loses the enclosing condition, and tracking only the condition
    leaves the loop variable unresolved so the group never appears in the map at all.
    """
    text = _gateway_text(ctx, "deploy/userdata/init-host.sh")
    lines = []
    heredoc_tag = None
    for line in text.splitlines():
        if heredoc_tag is not None:
            if line.strip() == heredoc_tag:
                heredoc_tag = None
            continue
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
        heredoc = _HEREDOC_OPEN.search(line)
        if heredoc:
            heredoc_tag = heredoc.group("tag")
    assignments = _static_assignments("\n".join(lines))
    ssm_params = {}
    for match in _SSM_PARAM_ASSIGN.finditer("\n".join(lines)):
        ssm_params[match.group("var")] = match.group("name").strip("\"'")
    guards = {}
    stack = []
    loops = []
    for line in lines:
        stripped = line.strip()
        opened = _FETCH_GUARD_OPEN.match(line)
        subshell = None if opened else _FETCH_GUARD_SUBSHELL.match(line)
        pushed_if = False
        if opened:
            stack.append({"var": opened.group("var"),
                          "equals": opened.group("equals")})
            pushed_if = True
        elif subshell:
            named = [name for name in ssm_params
                     if ("${%s}" % name) in subshell.group("body")
                     or ("$%s" % name) in subshell.group("body")]
            stack.append({"ssm_parameter": ssm_params[named[0]],
                          "equals": subshell.group("equals")} if named else None)
            pushed_if = True
        elif _FETCH_FALLBACK_ARM.match(line):
            stack.append({"fallback": True})
            pushed_if = True
        elif re.match(r"^\s*if\b", line):
            stack.append(None)          # unrecognised guard: excuse nothing
            pushed_if = True
        elif re.match(r"^\s*(elif|else)\b", line) and stack:
            stack[-1] = None            # the other arm is not the guarded one
        elif re.match(r"^\s*fi\b", stripped) and stack:
            stack.pop()
        # A one-line `if ... ; then ... ; fi` has to pop on the same line it pushed. Without
        # this the stack leaks the same way the heredoc did (init-host.sh has seven of them,
        # e.g. the ksm/smt writes), and the leak direction is again "absence becomes
        # excusable". Only checked when this line pushed: `_FETCH_GUARD_OPEN` ends in `then`,
        # so it can never also end in `fi`, and `\b` keeps words like `wifi` from matching.
        if pushed_if and re.search(r"\bfi\s*;?\s*$", line) and stack:
            stack.pop()
        loop = _FOR_OPEN.match(line)
        if loop:
            loops.append({"var": loop.group("var"),
                          "items": loop.group("items").split()})
        elif re.match(r"^\s*done\b", stripped) and loops:
            loops.pop()
        found = _CP_SOURCE.search(line)
        if not found:
            continue
        raw = found.group(1)
        active = next((item for item in reversed(stack) if item), None)
        candidates = [_substitute_shell(raw, assignments)]
        for loop_frame in reversed(loops):
            expanded = []
            for candidate in candidates:
                for item in loop_frame["items"]:
                    expanded.append(_substitute_shell(
                        candidate, {loop_frame["var"]: item}))
            candidates = expanded or candidates
        for candidate in candidates:
            source = candidate.strip("\"'")
            if "${" in source or "$" in source:
                continue
            # A fetch reached through more than one arm is unconditional in at least one of
            # them, so an unguarded sighting always wins over a guarded one.
            if source in guards and guards[source] is None:
                continue
            guards[source] = active
    return guards


def _rendered_gate_values(ctx, bucket, key):
    """Read gate values from the RENDERED init-host.sh object, not from the gateway source.

    The gateway copy still carries `{{LOGGING_ENABLED}}` placeholders; the launch-template
    user data is only a bootstrap that downloads and sha256-verifies the rendered object, so
    the object at deployment/bootstrap/host/<sha>/init-host.sh is the only place the
    deployment's actual gate values exist. Returns ({VAR: value}, reason).
    """
    if not bucket or not key:
        return {}, "no rendered init-host.sh coordinate"
    try:
        response = ctx.aws.call("s3", "get_object", Bucket=bucket, Key=key)
        body = ctx.aws.body_bytes(response)
    except (AwsReadError, AwsUnavailable) as error:
        return {}, "rendered init-host.sh unreadable: %s" % error
    text = body.decode("utf-8", "replace")
    values = {}
    for name, value in re.findall(
            r"^\s*([A-Z][A-Z0-9_]*)=(\S+)", text, re.MULTILINE):
        values.setdefault(name, value.strip("\"'"))
    return values, None


def _ssm_parameter_value(ctx, name):
    """Read one SSM parameter, returning (value, reason). Absent is a value, not an error.

    An absent switch is exactly how init-host.sh's spire-kit gate reads "off": the
    assignment falls back to the empty string when get-parameter fails, so a missing
    parameter means the guarded fetch never ran.
    """
    try:
        response = ctx.aws.call("ssm", "get_parameter", Name=name)
    except (AwsReadError, AwsUnavailable) as error:
        if _not_found(error) or "parameternotfound" in str(error).lower():
            return "", None
        return None, "%s unreadable: %s" % (name, error)
    return str((response.get("Parameter") or {}).get("Value") or ""), None


def _guard_satisfied(guard, gate_values, ssm_values=None):
    """True when the fetch should have run, False when it was gated off, None when unknown."""
    if not guard:
        return True
    if guard.get("ssm_parameter"):
        observed = (ssm_values or {}).get(guard["ssm_parameter"])
        if observed is None:
            return None
        # init-host.sh lowercases the parameter before comparing, so match that.
        return observed.strip().lower() == guard["equals"].strip().lower()
    if guard.get("fallback"):
        # A fetch that only runs because an earlier fetch failed is never expected to have
        # landed. Absence is the healthy state; presence would mean the primary channel broke.
        # Treating it as an ordinary gated fetch would leave F3 permanently INCONCLUSIVE on
        # every correct deployment, and a check that can never go green cannot detect drift.
        return False
    if guard.get("var") not in gate_values:
        return None
    return gate_values[guard["var"]] == guard["equals"]


def _obs_assets(ctx):
    text = _gateway_text(ctx, "deploy/stacks/obs_assets.py")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            matches = any(
                isinstance(target, ast.Name) and target.id == "OBS_ASSETS"
                for target in node.targets)
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            matches = (
                isinstance(node.target, ast.Name)
                and node.target.id == "OBS_ASSETS")
            value_node = node.value
        else:
            matches = False
            value_node = None
        if not matches or value_node is None:
            continue
        value = ast.literal_eval(value_node)
        return [
            {"source_path": source, "relative_key": key}
            for _construct, source, key in value
        ]
    raise ValueError("OBS_ASSETS could not be parsed")


def _obs_landing(item):
    key = item["relative_key"]
    if key == "adot/adot-config.yaml":
        return "/opt/aws/aws-otel-collector/etc/config.yaml", "host"
    if key == "fluent-bit/install-fluent-bit.sh":
        return "/opt/openclaw/fluent-bit/install-fluent-bit.sh", "host"
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "fluent-bit":
        return "/etc/fluent-bit/" + parts[-1], parts[1]
    return None, None


def _logging_state(ctx):
    value, source, reason = _coordinate(ctx, "logging_enabled")
    if value is None:
        return None, source, reason
    return bool(value), source, None


def _rootfs_manifest_key(ctx, bucket):
    supplied = ctx.get("rootfs_prefix")
    if supplied is None:
        supplied = ctx.get("s3.rootfs_prefix")
    if supplied:
        return str(supplied).strip("/") + "/manifest.json", "environment-json", None
    bootstrap, error = _host_bootstrap(ctx)
    if not bootstrap:
        return None, "discovered", error.get("reason")
    match = re.search(
        r"deployment/bootstrap/host/([0-9a-fA-F]{64})/init-host\.sh",
        bootstrap["user_data"])
    if not match:
        return None, "discovered", "host user data names no immutable init-host object"
    key = "deployment/bootstrap/host/%s/init-host.sh" % match.group(1).lower()
    try:
        init_object = _s3_digest(ctx, bucket, key)
    except (AwsReadError, AwsUnavailable) as read_error:
        return None, "discovered", str(read_error)
    text = init_object["bytes"].decode("utf-8", "replace")
    manifest_match = re.search(
        r's3://\$\{ASSETS_BUCKET\}/([^"\s]+)/manifest\.json', text)
    if not manifest_match or "{{" in manifest_match.group(1):
        return None, "discovered", "rendered init-host did not reveal ROOTFS_PREFIX"
    return manifest_match.group(1).strip("/") + "/manifest.json", "discovered", None


def _edge_bundle_constants(ctx):
    path = "deploy/stacks/edge_bundle.py"
    text = _gateway_text(ctx, path)
    tree = ast.parse(text)
    constants = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [
            target.id for target in node.targets
            if isinstance(target, ast.Name)
        ]
        for name in names:
            if name in {"BUNDLE_OBJECT_NAME", "_B64_LINE_WIDTH"}:
                constants[name] = ast.literal_eval(node.value)
            elif name == "_EXCLUDE_DIRS":
                call = node.value
                if isinstance(call, ast.Call) and call.args:
                    constants[name] = frozenset(ast.literal_eval(call.args[0]))
    patterns = {
        "tar_mtime": r"info\.mtime\s*=\s*(\d+)",
        "executable_mode": r"info\.mode\s*=\s*(0o[0-7]+)\s+if",
        "regular_mode": r"else\s+(0o[0-7]+)",
        "gzip_level": r"compresslevel\s*=\s*(\d+)",
        "gzip_mtime": r"GzipFile\([^\n]+mtime\s*=\s*(\d+)",
        "executable_suffix": r'name\.endswith\("([^"]+)"\)',
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if not match:
            raise ValueError("edge bundle constant %s was not parseable" % name)
        constants[name] = match.group(1)
    if "tarfile.GNU_FORMAT" not in text:
        raise ValueError("edge bundle tar format was not parseable")
    if not re.search(r"info\.uid\s*=\s*info\.gid\s*=\s*0", text):
        raise ValueError("edge bundle uid/gid normalization was not parseable")
    if not re.search(r'info\.uname\s*=\s*info\.gname\s*=\s*""', text):
        raise ValueError("edge bundle owner-name normalization was not parseable")
    return constants


def _edge_bundle(ctx):
    constants = _edge_bundle_constants(ctx)
    try:
        paths = ctx.git_tree_paths(ctx.gateway_ref, "deploy/edge")
    except AttributeError:
        root = Path(ctx.repo) / "deploy" / "edge"
        paths = [
            str(path.relative_to(ctx.repo))
            for path in root.rglob("*") if path.is_file()
        ]
    members = []
    excluded = set(constants["_EXCLUDE_DIRS"])
    for path in paths:
        if not path.startswith("deploy/edge/"):
            continue
        relative = path[len("deploy/edge/"):]
        if excluded.intersection(relative.split("/")[:-1]):
            continue
        members.append((relative, _gateway_bytes(ctx, path)))
    if not members:
        raise ValueError("deploy/edge contained no bundle members")
    tar_buffer = io.BytesIO()
    with tarfile.open(
            fileobj=tar_buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data in sorted(members):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(constants["tar_mtime"])
            info.mode = int(
                constants["executable_mode"], 8
            ) if name.endswith(constants["executable_suffix"]) else int(
                constants["regular_mode"], 8)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(
            fileobj=gzip_buffer, mode="wb",
            compresslevel=int(constants["gzip_level"]),
            mtime=int(constants["gzip_mtime"])) as stream:
        stream.write(tar_buffer.getvalue())
    encoded = base64.b64encode(gzip_buffer.getvalue()).decode("ascii")
    text = "\n".join(textwrap.wrap(
        encoded, int(constants["_B64_LINE_WIDTH"]))) + "\n"
    return {
        "sha256": _sha(text.encode("ascii")),
        "bytes": text.encode("ascii"),
        "object_name": constants["BUNDLE_OBJECT_NAME"],
        "member_count": len(members),
        "constants": {
            "exclude_dirs": sorted(excluded),
            "line_width": int(constants["_B64_LINE_WIDTH"]),
            "tar_mtime": int(constants["tar_mtime"]),
            "executable_mode": constants["executable_mode"],
            "regular_mode": constants["regular_mode"],
            "gzip_level": int(constants["gzip_level"]),
            "gzip_mtime": int(constants["gzip_mtime"]),
        },
    }


def core_object_inventory(ctx):
    rows = []
    gaps = []
    try:
        landing = _landing_paths(ctx)
        for relative in _required_scripts(ctx):
            source_path = "deploy/userdata/" + relative
            try:
                digest = _sha(_gateway_bytes(ctx, source_path))
            except (OSError, RuntimeError) as error:
                gaps.append("%s: %s" % (source_path, error))
                continue
            rows.append({
                "channel": "F3",
                "key": "deployment/scripts/" + relative,
                "expected_sha256": digest,
                "source_path": source_path,
                "landing_path": landing.get(relative),
            })
    except (OSError, RuntimeError, ValueError) as error:
        gaps.append("F3 inventory: %s" % error)
    try:
        for item in _obs_assets(ctx):
            rows.append({
                "channel": "F4",
                "key": "deployment/observability/" + item["relative_key"],
                "expected_sha256": _sha(
                    _gateway_bytes(ctx, item["source_path"])),
                "source_path": item["source_path"],
            })
    except (OSError, RuntimeError, ValueError, SyntaxError) as error:
        gaps.append("F4 inventory: %s" % error)
    try:
        bundle = _edge_bundle(ctx)
        rows.append({
            "channel": "F7",
            "key": "deployment/bootstrap/edge/%s/%s" % (
                bundle["sha256"], bundle["object_name"]),
            "expected_sha256": bundle["sha256"],
            "source_path": "deploy/edge/**",
        })
    except (OSError, RuntimeError, ValueError, SyntaxError) as error:
        gaps.append("F7 inventory: %s" % error)
    try:
        pins = _parse_firecracker_pins(ctx)
        for (version, architecture), digest in sorted(pins["pins"].items()):
            rows.append({
                "channel": "F8",
                "key": (
                    "deployment/binaries/firecracker/%s/"
                    "firecracker-%s-%s.tgz"
                ) % (version, version, architecture),
                "expected_sha256": digest,
                "source_path": "deploy/userdata/provision-host.sh",
            })
        if not pins["pins"]:
            gaps.append("F8 inventory: no pinned Firecracker digests")
    except (OSError, RuntimeError, ValueError) as error:
        gaps.append("F8 inventory: %s" % error)
    return rows, gaps


def check_f1(ctx):
    try:
        hosts, sample = _sample_metal_hosts(ctx)
        pins = _parse_firecracker_pins(ctx)
    except (AwsReadError, AwsUnavailable, OSError, RuntimeError, ValueError) as error:
        return _missing("F1", "golden AMI marker", error)
    if sample.get("reason"):
        return _missing("F1", "golden AMI marker", sample["reason"], sample)
    rows = []
    failures = []
    unreadable = []
    fields_by_host = {}
    for host in hosts:
        instance_id = host["instance_id"]
        result = _ssm_read(ctx, instance_id, "cat " + MARKER_PATH)
        values = _parse_key_values(result["output"]) if result["ok"] else {}
        if not values:
            baked = _ssm_read(
                ctx, instance_id, "stat /opt/openclaw/baked/vmlinux")
            if baked["ok"]:
                failures.append(instance_id)
            else:
                unreadable.append(instance_id)
        selected = {
            key: values.get(key) for key in (
                "recipe_version", "firecracker_version",
                "guest_kernel", "provisioned_arch")
        }
        pin = pins["pins"].get((
            selected.get("firecracker_version"),
            selected.get("provisioned_arch")))
        if values and (not all(selected.values()) or not pin):
            failures.append(instance_id)
        fields_by_host[instance_id] = selected
        rows.append({
            "instance_id": instance_id,
            "status": result["status"],
            "marker_present": bool(values),
            "fields": selected,
            "pinned_digest": pin,
        })
    reference = _oldest_launched_host(hosts)
    reference_id = reference["instance_id"] if reference else None
    if reference_id is None:
        unreadable.append("host launch times")
    else:
        reference_fields = fields_by_host[reference_id]
        for instance_id, observed in fields_by_host.items():
            if instance_id == reference_id:
                continue
            for key, reference_value in reference_fields.items():
                observed_value = observed.get(key)
                if (reference_value is not None and observed_value is not None
                        and observed_value != reference_value):
                    _record_fleet_divergence(
                        failures, reference_id, instance_id, key)
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if unreadable else "PASS")
    return finding(
        "F1", verdict,
        "Golden-AMI provenance markers and live Firecracker pins were compared.",
        {"inspected": len(rows), "sample": sample, "hosts": rows,
         "unreadable": unreadable,
         "reference_instance_id": reference_id,
         "reference_basis": "oldest-launched"},
        sorted(set(failures)),
        "Re-bake or replace hosts whose marker is absent, incomplete, or divergent.",
    )


def check_f2(ctx):
    # F2 intentionally accepts $Default because the deployed TrackDefaultLTVersion
    # resource makes it the reviewed steady state. A3 still rejects it under its
    # older criterion; that disagreement is deliberate until A3 can be revised.
    bucket, bucket_source, bucket_error = _coordinate(ctx, "assets_bucket")
    if not bucket:
        return _missing(
            "F2", "host launch-template bootstrap", bucket_error,
            {"assets_bucket_source": bucket_source})
    try:
        bootstrap, error = _host_bootstrap(ctx)
    except (AwsReadError, AwsUnavailable, ValueError, KeyError) as read_error:
        return _missing("F2", "host launch-template bootstrap", read_error)
    if not bootstrap:
        return _missing("F2", "host launch-template bootstrap",
                        error.get("reason"), error)
    tokens = _active_tokens(bootstrap["user_data"])
    effective_sha, key = _bootstrap_reference(bootstrap["user_data"])
    failures = []
    missing = []
    version_source = bootstrap["version_source"]
    if version_source == "$Latest":
        failures.append("host launch-template version %s" % bootstrap["version"])
    elif version_source != "$Default" and not bootstrap["numeric"]:
        failures.append("host launch-template version %s" % bootstrap["version"])
    if bootstrap["reason"]:
        missing.append(bootstrap["reason"])
    if bootstrap["user_data"] and not key:
        failures.append("host bootstrap key")
    failures.extend("unresolved token at line %s" % line for line in tokens)
    object_present = None
    object_digest_matches_key = None
    object_sha256 = None
    if key:
        try:
            # Presence is not enough. The key is content-addressed -- the sha256 the user data
            # verifies is literally IN the key -- so the object's bytes have to be hashed and
            # compared with it. Only checking `head_object` let a tampered init-host.sh pass:
            # the key never changes when the bytes do, so nothing else in the tool noticed
            # (verified on a live fleet 2026-08-29 -- F2 and F7 both said PASS while both
            # rendered bootstrap objects carried an injected line).
            #
            # The consequence of that drift is not "hosts run tampered code": the user data
            # sha-verifies before executing, so it fails closed and every FUTURE launch stops
            # booting. That is exactly the kind of breakage that must not sit behind a green
            # check -- the fleet looks healthy until the next scale-out.
            digest = _s3_digest(ctx, bucket, key)
            object_present = True
            object_sha256 = digest["sha256"]
            object_digest_matches_key = object_sha256 == effective_sha
            if not object_digest_matches_key:
                failures.append("%s: object sha256 %s does not match the sha in its own key"
                                % (key, object_sha256))
        except (AwsReadError, AwsUnavailable) as object_error:
            if _not_found(object_error):
                object_present = False
                failures.append(key)
            else:
                missing.append(key)
    instance_rows = []
    effective_version = bootstrap["effective_version"]
    if effective_version is None:
        missing.append("effective launch-template version")
    else:
        try:
            instances = _describe_instances(ctx, filters=[
                {"Name": "tag:Role", "Values": ["metal-host"]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ])
        except (AwsReadError, AwsUnavailable, ValueError) as instance_error:
            instances = []
            missing.append("running host instances: %s" % instance_error)
        if not instances and not any(
                item.startswith("running host instances") for item in missing):
            missing.append("running host instances")
        # ec2:DescribeInstances does NOT report which launch-template version an instance
        # was launched from -- there is no LaunchTemplate key on its Instances[] items.
        # Reading it from there made every host report ":launch-template-version" missing
        # and pushed F2 to INCONCLUSIVE on a healthy fleet. The AutoScalingGroup does
        # carry it, per instance, under Instances[].LaunchTemplate.Version (verified live:
        # a three-host fleet reported 2/2/3 across a default-version bump).
        asg_versions = {}
        try:
            host_asg = _describe_asg(ctx, bootstrap["asg_name"])
        except (AwsReadError, AwsUnavailable, ValueError) as asg_error:
            host_asg = None
            missing.append("host ASG membership: %s" % asg_error)
        for member in (host_asg or {}).get("Instances") or []:
            member_id = str(member.get("InstanceId") or "")
            member_lt = member.get("LaunchTemplate") or {}
            if member_id:
                asg_versions[member_id] = member_lt
        version_cache = {
            (bootstrap["lt_id"], effective_version): {
                "user_data": bootstrap["user_data"],
                "reason": bootstrap["reason"],
            },
        }
        for instance in instances:
            instance_id = str(instance.get("InstanceId") or "")
            # ASG membership first (it is the only source that carries the version),
            # EC2 second so a future API that does report it still works.
            launch_template = (asg_versions.get(instance_id)
                               or instance.get("LaunchTemplate") or {})
            instance_lt = str(
                launch_template.get("LaunchTemplateId")
                or bootstrap["lt_id"])
            instance_version = _numeric_version(
                launch_template.get("Version"))
            instance_sha = None
            version_differs = (
                instance_version is not None
                and instance_version != effective_version)
            if instance_version is None:
                missing.append(
                    "%s:launch-template-version (not an ASG member of %s)"
                    % (instance_id, bootstrap["asg_name"]))
            else:
                cache_key = (instance_lt, instance_version)
                if cache_key not in version_cache:
                    try:
                        data, reason = _launch_template_version_data(
                            ctx, instance_lt, instance_version)
                    except (AwsReadError, AwsUnavailable, ValueError) as error:
                        data = None
                        reason = str(error)
                    version_cache[cache_key] = {
                        "user_data": data["user_data"] if data else "",
                        "reason": reason,
                    }
                version_data = version_cache[cache_key]
                instance_sha, _instance_key = _bootstrap_reference(
                    version_data["user_data"])
                if version_data["reason"] or not instance_sha:
                    missing.append(
                        "%s:bootstrap-version-%s"
                        % (instance_id, instance_version))
                elif (version_differs and effective_sha
                      and instance_sha != effective_sha):
                    failures.append("%s:bootstrap-sha" % instance_id)
            instance_rows.append({
                "instance_id": instance_id,
                "launch_template_id": instance_lt,
                "launch_template_version": instance_version,
                "bootstrap_sha256": instance_sha,
                "version_differs": version_differs,
                "bootstrap_differs": (
                    instance_sha != effective_sha
                    if instance_sha and effective_sha else None),
            })
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F2", verdict,
        "Host launch-template bootstrap state was checked at its effective version and on running instances.",
        {"inspected": 1, "version": bootstrap["version"],
         "version_source": version_source,
         "effective_version": effective_version,
         "numeric": bootstrap["numeric"], "key": key,
         "effective_bootstrap_sha256": effective_sha,
         "object_present": object_present,
         "object_sha256": object_sha256,
         "object_digest_matches_key": object_digest_matches_key,
         "active_token_lines": tokens,
         "host_asg_source": bootstrap["asg_source"],
         "host_lt_source": bootstrap["lt_source"],
         "classified_by": bootstrap["classified_by"],
         "assets_bucket_source": bucket_source,
         "instances": instance_rows, "missing": sorted(set(missing))},
        sorted(set(failures)),
        "Use $Default or a reviewed numeric host LT version, publish its rendered immutable bootstrap object, and replace instances whose bootstrap SHA differs.",
    )


def check_f3(ctx):
    bucket, source, reason = _coordinate(ctx, "assets_bucket")
    if not bucket:
        return _missing(
            "F3", "managed script channel", reason,
            {"assets_bucket_source": source})
    try:
        hosts, sample = _sample_metal_hosts(ctx)
        landing = _landing_paths(ctx)
        required = _required_scripts(ctx)
        guards = _fetch_guards(ctx)
    except (AwsReadError, AwsUnavailable, OSError, RuntimeError, ValueError) as error:
        return _missing("F3", "managed script channel", error)
    if sample.get("reason"):
        return _missing("F3", "managed script channel",
                        sample["reason"], sample)
    # Gate values come from the rendered object the hosts actually executed, so a fetch
    # that init-host.sh skipped is scored as expected-absent rather than unreadable.
    try:
        bootstrap, _bootstrap_reason = _host_bootstrap(ctx)
    except (AwsReadError, AwsUnavailable, ValueError):
        bootstrap = None
    _rendered_sha, rendered_key = _bootstrap_reference(
        (bootstrap or {}).get("user_data") or "")
    gate_values, gate_reason = _rendered_gate_values(ctx, bucket, rendered_key)
    ssm_values = {}
    ssm_reasons = []
    for guard in guards.values():
        name = (guard or {}).get("ssm_parameter")
        if not name or name in ssm_values:
            continue
        value, reason = _ssm_parameter_value(ctx, name)
        if reason:
            ssm_reasons.append(reason)
        else:
            ssm_values[name] = value
    paths = [landing.get(relative) for relative in required if landing.get(relative)]
    host_hashes = _hash_paths(ctx, hosts, sorted(set(paths))) if paths else {}
    rows = []
    failures = []
    missing = []
    for relative in required:
        source_path = "deploy/userdata/" + relative
        key = "deployment/scripts/" + relative
        destination = landing.get(relative)
        try:
            gateway_sha = _sha(_gateway_bytes(ctx, source_path))
        except (OSError, RuntimeError) as error:
            gateway_sha = None
            missing.append("%s: %s" % (source_path, error))
        try:
            s3 = _s3_digest(ctx, bucket, key)
        except (AwsReadError, AwsUnavailable):
            s3 = None
            missing.append(key)
        host_rows = []
        if not destination:
            missing.append("%s landing path" % relative)
        guard = guards.get(relative)
        satisfied = _guard_satisfied(guard, gate_values, ssm_values)
        for host in hosts:
            probe = host_hashes.get(host["instance_id"]) or {}
            # "The file is absent" and "the probe never read this host" both surface as a
            # missing hash, and they carry opposite severities. The discriminator is whether
            # the probe produced ANY hash for this host: one batched `sha256sum` covers every
            # declared path, so at least one result proves it executed and a gap in the results
            # is then a genuine absence. The host-level SSM status cannot be used for this --
            # `sha256sum` exits non-zero as soon as one of its arguments is missing, so a fleet
            # with one real gap reports `Failed` for the whole host while the other 26 digests
            # came back fine.
            probe_ran = bool(probe.get("hashes"))
            observed = (probe.get("hashes", {}).get(destination)
                        if destination else None)
            match = (
                observed == s3["sha256"]
                if destination and observed and s3 else None)
            if destination:
                if not observed and satisfied is False:
                    # init-host.sh never ran this fetch in this deployment, so the file is
                    # supposed to be absent. Scoring it as unreadable turned a correct
                    # logging-disabled / egress-off fleet into rows of missing evidence.
                    pass
                elif not observed and satisfied is None:
                    missing.append("%s:%s (guard %s unresolved%s)" % (
                        host["instance_id"], relative,
                        (guard or {}).get("var", "?"),
                        "" if not gate_reason else ": " + gate_reason))
                elif not observed and probe_ran:
                    # The fetch was supposed to run and the file is not there: that is drift,
                    # not an unread reading. Scoring it INCONCLUSIVE would understate exactly
                    # the case this channel exists to catch -- one host in the fleet missing a
                    # managed script its peers have.
                    failures.append("%s:%s absent" % (
                        host["instance_id"], relative))
                elif not observed:
                    missing.append("%s:%s (host probe returned no digests)" % (
                        host["instance_id"], relative))
                elif not s3:
                    missing.append("%s:%s S3 digest" % (
                        host["instance_id"], relative))
                elif observed != s3["sha256"]:
                    failures.append("%s:%s" % (
                        host["instance_id"], relative))
            host_rows.append({
                "instance_id": host["instance_id"],
                "sha256": observed,
                "matches_s3": match,
                "probe_ran": probe_ran,
            })
        source_match = bool(
            gateway_sha and s3 and gateway_sha == s3["sha256"])
        if s3 and gateway_sha and not source_match:
            failures.append(key)
        rows.append({
            "path": relative, "key": key, "landing_path": destination,
            "gateway_sha256": gateway_sha,
            "s3_sha256": s3["sha256"] if s3 else None,
            "gateway_matches_s3": source_match if s3 and gateway_sha else None,
            "guard": guard,
            "expected_absent": satisfied is False,
            "hosts": host_rows,
        })
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F3", verdict,
        "Declared managed scripts were compared across gateway, S3, and hosts.",
        {"inspected": len(rows), "sample": sample,
         "assets_bucket_source": source, "files": rows, "missing": missing,
         "gate_values": sorted(gate_values), "gate_reason": gate_reason,
         "gate_ssm_parameters": sorted(ssm_values),
         "gate_ssm_reasons": ssm_reasons,
         "rendered_init_host_key": rendered_key},
        sorted(set(failures)),
        "Publish every declared script and replace hosts whose landed digest differs.",
    )


def check_f4(ctx):
    bucket, bucket_source, bucket_error = _coordinate(ctx, "assets_bucket")
    logging_enabled, logging_source, logging_error = _logging_state(ctx)
    if not bucket or logging_enabled is None:
        return _missing(
            "F4", "observability assets",
            bucket_error or logging_error,
            {"assets_bucket_source": bucket_source,
             "logging_source": logging_source})
    try:
        assets = _obs_assets(ctx)
        host_samples, host_sample = _sample_metal_hosts(ctx)
        edge_samples, edge_sample = _edge_instances(ctx)
    except (AwsReadError, AwsUnavailable, OSError, RuntimeError,
            ValueError, SyntaxError) as error:
        return _missing("F4", "observability assets", error)
    if logging_enabled and host_sample.get("reason"):
        return _missing("F4", "observability assets",
                        host_sample["reason"], host_sample)
    if logging_enabled and any(
            _obs_landing(item)[1] == "edge" for item in assets
    ) and edge_sample.get("reason"):
        return _missing("F4", "observability assets",
                        edge_sample["reason"], edge_sample)
    targets = {"host": host_samples, "edge": edge_samples}
    hash_cache = {}
    if logging_enabled:
        for role in ("host", "edge"):
            role_paths = sorted({
                _obs_landing(item)[0] for item in assets
                if _obs_landing(item)[1] == role and _obs_landing(item)[0]
            })
            if role_paths and targets[role]:
                hash_cache[role] = _hash_paths(
                    ctx, targets[role], role_paths)
    rows = []
    failures = []
    missing = []
    for item in assets:
        key = "deployment/observability/" + item["relative_key"]
        landing_path, role = _obs_landing(item)
        gateway_sha = _sha(_gateway_bytes(ctx, item["source_path"]))
        try:
            s3 = _s3_digest(ctx, bucket, key)
        except (AwsReadError, AwsUnavailable):
            s3 = None
            missing.append(key)
        if s3 and s3["sha256"] != gateway_sha:
            failures.append(key)
        host_rows = []
        if logging_enabled and landing_path and role:
            for target in targets[role]:
                instance_id = target["instance_id"]
                digest = ((hash_cache.get(role) or {}).get(instance_id) or {}).get(
                    "hashes", {}).get(landing_path)
                if not digest:
                    missing.append("%s:%s" % (instance_id, key))
                elif s3 and digest != s3["sha256"]:
                    failures.append("%s:%s" % (instance_id, key))
                host_rows.append({
                    "instance_id": instance_id, "sha256": digest,
                    "matches_s3": (
                        digest == s3["sha256"] if digest and s3 else None),
                })
        rows.append({
            "key": key, "source_path": item["source_path"],
            "landing_path": landing_path, "role": role,
            "gateway_sha256": gateway_sha,
            "s3_sha256": s3["sha256"] if s3 else None,
            "expected_absent": not logging_enabled,
            "hosts": host_rows,
        })
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F4", verdict,
        "Observability assets were compared with the deployment logging gate.",
        {"inspected": len(rows), "logging_enabled": logging_enabled,
         "logging_source": logging_source,
         "assets_bucket_source": bucket_source,
         "expected_absent": not logging_enabled,
         "host_sample": host_sample, "edge_sample": edge_sample,
         "assets": rows, "missing": missing},
        sorted(set(failures)),
        "Publish matching observability assets and converge enabled fleet instances.",
    )


def check_f5(ctx):
    bucket, bucket_source, bucket_error = _coordinate(ctx, "assets_bucket")
    if not bucket:
        return _missing(
            "F5", "guest image manifest", bucket_error,
            {"assets_bucket_source": bucket_source})
    try:
        hosts, sample = _sample_metal_hosts(ctx)
    except (AwsReadError, AwsUnavailable, ValueError) as error:
        return _missing("F5", "guest image manifest", error)
    if sample.get("reason"):
        return _missing("F5", "guest image manifest",
                        sample["reason"], sample)
    key, key_source, key_error = _rootfs_manifest_key(ctx, bucket)
    if not key:
        return _missing(
            "F5", "guest image manifest", key_error,
            {"manifest_key_source": key_source, "sample": sample})
    try:
        s3 = _s3_digest(ctx, bucket, key)
        expected = json.loads(s3["bytes"].decode("utf-8"))
    except (AwsReadError, AwsUnavailable, ValueError, TypeError) as error:
        return _missing("F5", "guest image manifest", error,
                        {"manifest_key": key, "sample": sample})
    fields = ("rootfs", "data_template", "immutable", "version")
    expected_fields = {name: expected.get(name) for name in fields}
    disk_hashes = _hash_paths(ctx, hosts, DISK_PATHS)
    rows = []
    failures = []
    missing = []
    fields_by_host = {}
    disks_by_host = {}
    for host in hosts:
        instance_id = host["instance_id"]
        manifest_read = _ssm_read(
            ctx, instance_id, "cat " + ASSET_DIR + "/manifest.json")
        try:
            observed_document = json.loads(manifest_read["output"])
        except (TypeError, ValueError):
            observed_document = None
        observed_fields = (
            {name: observed_document.get(name) for name in fields}
            if isinstance(observed_document, dict) else {})
        if not observed_fields:
            missing.append("%s:manifest.json" % instance_id)
        elif observed_fields != expected_fields:
            failures.append("%s:manifest.json" % instance_id)
        fields_by_host[instance_id] = observed_fields
        hashes = (disk_hashes.get(instance_id) or {}).get("hashes", {})
        disks_by_host[instance_id] = hashes
        for path in DISK_PATHS:
            if not hashes.get(path):
                missing.append("%s:%s" % (instance_id, path))
        rows.append({
            "instance_id": instance_id,
            "manifest_status": manifest_read["status"],
            "manifest_fields": observed_fields,
            "manifest_matches_s3": (
                observed_fields == expected_fields if observed_fields else None),
            "disk_sha256": {path: hashes.get(path) for path in DISK_PATHS},
        })
    reference = _oldest_launched_host(hosts)
    reference_id = reference["instance_id"] if reference else None
    if reference_id is None:
        missing.append("host launch times")
    else:
        reference_fields = fields_by_host[reference_id]
        reference_disks = disks_by_host[reference_id]
        for instance_id, observed_fields in fields_by_host.items():
            if instance_id == reference_id:
                continue
            for name in fields:
                reference_value = reference_fields.get(name)
                observed_value = observed_fields.get(name)
                if (reference_value is not None and observed_value is not None
                        and observed_value != reference_value):
                    _record_fleet_divergence(
                        failures, reference_id, instance_id,
                        "manifest.%s" % name)
            observed_disks = disks_by_host[instance_id]
            for path in DISK_PATHS:
                reference_digest = reference_disks.get(path)
                observed_digest = observed_disks.get(path)
                if (reference_digest and observed_digest
                        and observed_digest != reference_digest):
                    _record_fleet_divergence(
                        failures, reference_id, instance_id, path)
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F5", verdict,
        "Guest image manifests and decompressed disk digests were compared.",
        {"inspected": len(rows), "sample": sample,
         "manifest_key": key, "manifest_key_source": key_source,
         "assets_bucket_source": bucket_source,
         "s3_manifest_sha256": s3["sha256"],
         "s3_fields": expected_fields, "hosts": rows, "missing": missing,
         "reference_instance_id": reference_id,
         "reference_basis": "oldest-launched"},
        sorted(set(failures)),
        "Republish the current image manifest or refresh hosts with divergent disks.",
    )


def check_f6(ctx):
    try:
        hosts, sample = _sample_metal_hosts(ctx)
    except (AwsReadError, AwsUnavailable, ValueError) as error:
        return _missing("F6", "guest kernel", error)
    if sample.get("reason"):
        return _missing("F6", "guest kernel", sample["reason"], sample)
    paths = (ASSET_DIR + "/vmlinux", "/opt/openclaw/baked/vmlinux")
    hashes = _hash_paths(ctx, hosts, paths)
    rows = []
    failures = []
    missing = []
    values_by_host = {}
    for host in hosts:
        instance_id = host["instance_id"]
        observed = (hashes.get(instance_id) or {}).get("hashes", {})
        marker = _ssm_read(ctx, instance_id, "cat " + MARKER_PATH)
        marker_values = _parse_key_values(marker["output"]) if marker["ok"] else {}
        guest_kernel = marker_values.get("guest_kernel")
        if not observed.get(paths[0]) or not observed.get(paths[1]) or not guest_kernel:
            missing.append(instance_id)
        if observed.get(paths[0]) and observed.get(paths[1]) and (
                observed[paths[0]] != observed[paths[1]]):
            failures.append("%s:%s" % (instance_id, paths[0]))
        row = {
            "instance_id": instance_id,
            "assets_vmlinux_sha256": observed.get(paths[0]),
            "baked_vmlinux_sha256": observed.get(paths[1]),
            "guest_kernel": guest_kernel,
        }
        rows.append(row)
        values_by_host[instance_id] = row
    reference = _oldest_launched_host(hosts)
    reference_id = reference["instance_id"] if reference else None
    if reference_id is None:
        missing.append("host launch times")
    else:
        reference_values = values_by_host[reference_id]
        comparison_keys = (
            ("assets_vmlinux_sha256", paths[0]),
            ("baked_vmlinux_sha256", paths[1]),
            ("guest_kernel", "guest_kernel"),
        )
        for instance_id, observed in values_by_host.items():
            if instance_id == reference_id:
                continue
            for field, evidence_key in comparison_keys:
                reference_value = reference_values.get(field)
                observed_value = observed.get(field)
                if (reference_value and observed_value
                        and observed_value != reference_value):
                    _record_fleet_divergence(
                        failures, reference_id, instance_id, evidence_key)
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F6", verdict,
        "Running and baked guest kernels were compared across the sampled fleet.",
        {"inspected": len(rows), "sample": sample, "hosts": rows,
         "missing": missing,
         "reference_instance_id": reference_id,
         "reference_basis": "oldest-launched"},
        sorted(set(failures)),
        "Replace hosts whose running, baked, or marker kernel identity diverges.",
    )


def check_f7(ctx):
    try:
        bundle = _edge_bundle(ctx)
    except (OSError, RuntimeError, ValueError, SyntaxError) as error:
        return _missing("F7", "edge bundle", error)
    if ctx.offline:
        return finding(
            "F7", "PASS",
            "The deterministic edge bundle was recomputed from gateway source.",
            {"inspected": bundle["member_count"],
             "bundle_sha256": bundle["sha256"],
             "normalization": bundle["constants"], "offline": True},
            remediation="Keep the edge bundling normalization aligned with the source module.",
        )
    bucket, bucket_source, bucket_error = _coordinate(ctx, "assets_bucket")
    lt_id, lt_source, lt_error = _coordinate(ctx, "edge_lt")
    if not bucket or not lt_id:
        return _missing(
            "F7", "edge bundle", bucket_error or lt_error,
            {"assets_bucket_source": bucket_source,
             "edge_lt_source": lt_source,
             "bundle_sha256": bundle["sha256"]})
    missing = []
    # Collected inside the read block below, which runs before `failures` exists.
    failures_early = []
    try:
        edges, edge_sample = _edge_instances(ctx)
        configured_lt = (
            edge_sample.get("launch_template_id") or lt_id)
        version_source = str(edge_sample.get("version_source") or "")
        if not version_source:
            return _missing(
                "F7", "edge bundle",
                "edge ASG launch-template version is empty",
                {"bundle_sha256": bundle["sha256"],
                 "edge_sample": edge_sample})
        resolved = _effective_launch_template(
            ctx, configured_lt, version_source)
        user_data = resolved["user_data"]
        named = re.search(
            r"deployment/bootstrap/edge/([0-9a-fA-F]{64})/"
            + re.escape(bundle["object_name"]), user_data)
        named_sha = named.group(1).lower() if named else None
        key = "deployment/bootstrap/edge/%s/%s" % (
            bundle["sha256"], bundle["object_name"])
        object_present = None
        object_sha256 = None
        object_digest_matches_key = None
        try:
            # Same reason as F2: this key is content-addressed, so `head_object` proves nothing
            # about the bytes. A tampered edge bundle keeps the key and the LT reference intact
            # and only the digest moves, which is why presence-only left it invisible here (it
            # was G4 that caught the live tamper on 2026-08-29, and G4 does not cover the host
            # side of the same pair).
            digest = _s3_digest(ctx, bucket, key)
            object_present = True
            object_sha256 = digest["sha256"]
            object_digest_matches_key = object_sha256 == bundle["sha256"]
            if not object_digest_matches_key:
                failures_early.append(
                    "%s: object sha256 %s does not match the sha in its own key"
                    % (key, object_sha256))
        except (AwsReadError, AwsUnavailable):
            missing.append(key)
    except (AwsReadError, AwsUnavailable, ValueError, IndexError) as error:
        return _missing("F7", "edge bundle", error)
    if edge_sample.get("reason"):
        return _missing("F7", "edge bundle", edge_sample["reason"],
                        {"bundle_sha256": bundle["sha256"],
                         "edge_sample": edge_sample})
    failures = list(failures_early)
    if version_source == "$Latest":
        failures.append("edge launch-template version $Latest")
    elif version_source != "$Default" and not version_source.isdigit():
        failures.append(
            "edge launch-template version %s" % version_source)
    if resolved["reason"]:
        missing.append(resolved["reason"])
    if user_data and named_sha != bundle["sha256"]:
        failures.append(configured_lt)
    host_rows = []
    directory = "/opt/openclaw-edge/" + bundle["sha256"]
    for edge in edges:
        result = _ssm_read(
            ctx, edge["instance_id"], "stat " + quote_path(directory))
        # A probe that never reached a terminal status proves nothing about the directory.
        # Treating it as absent reported a FAIL against an edge instance whose bundle sha,
        # launch-template reference and S3 object had all already matched -- a false red
        # manufactured out of missing evidence. Only a probe that RAN and said the path is
        # not there is drift; anything else is a gap in the reading.
        finished = result["status"] not in SSM_NON_TERMINAL_STATUSES
        exists = result["ok"] if finished else None
        if not finished:
            missing.append("%s: probe did not finish (%s)"
                           % (edge["instance_id"], result["status"] or "unknown"))
        elif not result["ok"]:
            failures.append(edge["instance_id"])
        host_rows.append({
            "instance_id": edge["instance_id"],
            "directory": directory, "exists": exists,
            "status": result["status"],
        })
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F7", verdict,
        "The edge bundle digest, LT bootstrap, S3 object, and edge directories were compared.",
        {"inspected": len(host_rows), "bundle_sha256": bundle["sha256"],
         "normalization": bundle["constants"], "member_count": bundle["member_count"],
         "version_source": version_source,
         "effective_version": resolved["effective_version"],
         "lt_named_sha256": named_sha, "key": key,
         "object_present": object_present,
         "object_sha256": object_sha256,
         "object_digest_matches_key": object_digest_matches_key,
         "assets_bucket_source": bucket_source,
         "edge_lt_source": lt_source,
         "classified_by": _coordinate_classification(ctx, "edge_lt"),
         "edge_sample": edge_sample, "instances": host_rows,
         "missing": sorted(set(missing))},
        sorted(set(failures)),
        "Deploy the recomputed edge bundle and replace edge instances on another version.",
    )


def check_f8(ctx):
    bucket, bucket_source, bucket_error = _coordinate(ctx, "assets_bucket")
    if not bucket:
        return _missing(
            "F8", "Firecracker binaries", bucket_error,
            {"assets_bucket_source": bucket_source})
    try:
        pins = _parse_firecracker_pins(ctx)
        hosts, sample = _sample_metal_hosts(ctx)
    except (AwsReadError, AwsUnavailable, OSError, RuntimeError, ValueError) as error:
        return _missing("F8", "Firecracker binaries", error)
    if sample.get("reason"):
        return _missing("F8", "Firecracker binaries",
                        sample["reason"], sample)
    if not pins["pins"]:
        return _missing("F8", "Firecracker binaries", "no pinned digests parsed")
    rows = []
    failures = []
    missing = []
    for (version, architecture), expected in sorted(pins["pins"].items()):
        key = (
            "deployment/binaries/firecracker/%s/firecracker-%s-%s.tgz"
            % (version, version, architecture))
        try:
            observed = _s3_digest(ctx, bucket, key)["sha256"]
        except (AwsReadError, AwsUnavailable):
            observed = None
        if observed is None:
            missing.append(key)
        elif observed != expected:
            failures.append(key)
        rows.append({
            "key": key, "version": version, "architecture": architecture,
            "pinned_sha256": expected, "s3_sha256": observed,
            "match": observed == expected if observed else None,
        })
    binary_paths = ("/usr/local/bin/firecracker", "/usr/local/bin/jailer")
    host_hashes = _hash_paths(ctx, hosts, binary_paths)
    binaries_by_host = {}
    for path in binary_paths:
        for host in hosts:
            digest = (host_hashes.get(host["instance_id"]) or {}).get(
                "hashes", {}).get(path)
            binaries_by_host.setdefault(host["instance_id"], {})[path] = digest
            if not digest:
                missing.append("%s:%s" % (host["instance_id"], path))
    host_rows = [{
        "instance_id": host["instance_id"],
        "sha256": binaries_by_host[host["instance_id"]],
    } for host in hosts]
    reference = _oldest_launched_host(hosts)
    reference_id = reference["instance_id"] if reference else None
    if reference_id is None:
        missing.append("host launch times")
    else:
        reference_binaries = binaries_by_host[reference_id]
        for instance_id, observed in binaries_by_host.items():
            if instance_id == reference_id:
                continue
            for path in binary_paths:
                reference_digest = reference_binaries.get(path)
                observed_digest = observed.get(path)
                if (reference_digest and observed_digest
                        and observed_digest != reference_digest):
                    _record_fleet_divergence(
                        failures, reference_id, instance_id, path)
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F8", verdict,
        "Pinned Firecracker archives and installed fleet binaries were compared.",
        {"inspected": len(rows), "sample": sample,
         "assets_bucket_source": bucket_source,
         "objects": rows, "hosts": host_rows, "missing": missing,
         "reference_instance_id": reference_id,
         "reference_basis": "oldest-launched"},
        sorted(set(failures)),
        "Restore pinned mirror objects and replace hosts with divergent binaries.",
    )


def _current_skill_versions(ctx, bucket):
    keys = set()
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": "skills/"}
        if token:
            kwargs["ContinuationToken"] = token
        response = ctx.aws.call("s3", "list_objects_v2", **kwargs)
        keys.update(
            str(item.get("Key")) for item in response.get("Contents") or []
            if item.get("Key") and not str(item.get("Key")).endswith("/"))
        token = response.get("NextContinuationToken")
        if not token:
            break
    versions = {}
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": "skills/"}
        if token:
            kwargs["KeyMarker"] = token
        response = ctx.aws.call("s3", "list_object_versions", **kwargs)
        for item in response.get("Versions") or []:
            key = str(item.get("Key") or "")
            if item.get("IsLatest") and key in keys:
                versions[key] = str(item.get("VersionId") or "")
        deleted = {
            str(item.get("Key")) for item in response.get("DeleteMarkers") or []
            if item.get("IsLatest")
        }
        for key in deleted:
            versions.pop(key, None)
            keys.discard(key)
        if not response.get("IsTruncated"):
            break
        token = response.get("NextKeyMarker")
        if not token:
            break
    return {key: versions.get(key, "") for key in sorted(keys)}


def check_f9(ctx):
    bucket, bucket_source, bucket_error = _coordinate(ctx, "assets_bucket")
    if not bucket:
        return _missing(
            "F9", "shared skills", bucket_error,
            {"assets_bucket_source": bucket_source})
    try:
        hosts, sample = _sample_metal_hosts(ctx)
        desired = _current_skill_versions(ctx, bucket)
    except (AwsReadError, AwsUnavailable, ValueError) as error:
        return _missing("F9", "shared skills", error)
    if sample.get("reason"):
        return _missing("F9", "shared skills", sample["reason"], sample)
    if not desired:
        # An empty skills/ prefix is the steady state of a deployment that has never
        # published a shared skill, not a gap in the reading. It only becomes a finding if
        # a host claims to have applied one, which would mean the platform lost the source
        # of something it already shipped -- so the hosts are still read before passing.
        claimed = []
        for host in hosts:
            result = _ssm_read(ctx, host["instance_id"], "cat " + SKILL_STATE_PATH)
            if not result["ok"]:
                continue
            try:
                document = json.loads(result["output"])
            except (TypeError, ValueError):
                continue
            objects = document.get("objects") if isinstance(document, dict) else None
            if isinstance(objects, dict) and objects:
                claimed.append("%s:%d" % (host["instance_id"], len(objects)))
        return finding(
            "F9",
            "FAIL" if claimed else "PASS",
            "The skills/ prefix is empty; hosts were checked for orphaned applied skills.",
            {"inspected": len(hosts), "sample": sample,
             "assets_bucket_source": bucket_source,
             "prefix_empty": True, "expected_absent": not claimed,
             "hosts_claiming_applied_skills": claimed},
            sorted(claimed),
            "Publish the shared skills the hosts already applied, or clear the stale "
            "host-side sync state.",
        )
    rows = []
    failures = []
    unverified = []
    for host in hosts:
        instance_id = host["instance_id"]
        result = _ssm_read(ctx, instance_id, "cat " + SKILL_STATE_PATH)
        try:
            document = json.loads(result["output"])
            objects = document.get("objects") if isinstance(document, dict) else None
        except (TypeError, ValueError):
            objects = None
        if not isinstance(objects, dict):
            unverified.append(instance_id)
            observed = {}
        else:
            observed = {
                str(key): str((value or {}).get("version_id") or "")
                for key, value in objects.items()
            }
            if observed != desired:
                failures.append(instance_id)
        rows.append({
            "instance_id": instance_id,
            "state_recorded": isinstance(objects, dict),
            "object_count": len(observed),
            "missing_keys": sorted(set(desired).difference(observed)),
            "extra_keys": sorted(set(observed).difference(desired)),
            "version_mismatches": sorted(
                key for key in set(desired).intersection(observed)
                if desired[key] != observed[key]),
        })
    verdict = (
        "FAIL" if failures else ("UNVERIFIED" if unverified else "PASS"))
    return finding(
        "F9", verdict,
        "S3 shared-skill VersionIds were compared with each host's applied-state record.",
        {"inspected": len(rows), "sample": sample,
         "assets_bucket_source": bucket_source,
         "desired": desired, "hosts": rows,
         "unverified_hosts": unverified},
        failures,
        "Restore the applied VersionId record or re-run the exact-version skill synchronizer.",
    )


def _agent_versions(ctx):
    rows = {}
    token = None
    while True:
        kwargs = {}
        if token:
            kwargs["NextToken"] = token
        response = ctx.aws.call(
            "ssm", "describe_instance_information", **kwargs)
        for item in response.get("InstanceInformationList") or []:
            if item.get("InstanceId"):
                rows[str(item["InstanceId"])] = item.get("AgentVersion")
        token = response.get("NextToken")
        if not token:
            return rows


def check_f10(ctx):
    try:
        hosts, sample = _sample_metal_hosts(ctx)
        versions = _agent_versions(ctx)
    except (AwsReadError, AwsUnavailable, ValueError) as error:
        return _missing("F10", "host OS and SSM agent configuration", error)
    if sample.get("reason"):
        return _missing(
            "F10", "host OS and SSM agent configuration",
            sample["reason"], sample)
    rows = []
    failures = []
    missing = []
    values_by_host = {}
    for host in hosts:
        instance_id = host["instance_id"]
        result = _ssm_read(
            ctx, instance_id, "cat " + SSM_AGENT_CONFIG_PATH)
        try:
            document = json.loads(result["output"])
            mds = document.get("Mds") if isinstance(document, dict) else None
        except (TypeError, ValueError):
            mds = None
        worker = mds.get("CommandWorkersLimit") if isinstance(mds, dict) else None
        buffer_limit = (
            mds.get("CommandWorkerBufferLimit")
            if isinstance(mds, dict) else None)
        agent = versions.get(instance_id)
        if worker is None or buffer_limit is None or not agent:
            missing.append(instance_id)
        row = {
            "instance_id": instance_id,
            "command_workers_limit": worker,
            "command_worker_buffer_limit": buffer_limit,
            "agent_version": agent,
        }
        rows.append(row)
        values_by_host[instance_id] = row
    reference = _oldest_launched_host(hosts)
    reference_id = reference["instance_id"] if reference else None
    if reference_id is None:
        missing.append("host launch times")
    else:
        reference_values = values_by_host[reference_id]
        comparison_keys = (
            "command_workers_limit",
            "command_worker_buffer_limit",
            "agent_version",
        )
        for instance_id, observed in values_by_host.items():
            if instance_id == reference_id:
                continue
            for key in comparison_keys:
                reference_value = reference_values.get(key)
                observed_value = observed.get(key)
                if (reference_value is not None and observed_value is not None
                        and observed_value != reference_value):
                    _record_fleet_divergence(
                        failures, reference_id, instance_id, key)
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F10", verdict,
        "Fleet-internal SSM agent settings were compared; no source-side expectation exists.",
        {"inspected": len(rows), "sample": sample,
         "source_expectation": "none", "hosts": rows, "missing": missing,
         "reference_instance_id": reference_id,
         "reference_basis": "oldest-launched"},
        sorted(set(failures)),
        "Converge the SSM agent config and agent version across the sampled fleet.",
    )


def _sanitized_env(text):
    values = _parse_key_values(text)
    return {
        key: _sha(value.encode("utf-8"))[:12]
        for key, value in values.items()
    }


def check_f11(ctx):
    try:
        hosts, sample = _sample_metal_hosts(ctx)
        logging_enabled, logging_source, logging_error = _logging_state(ctx)
    except (AwsReadError, AwsUnavailable, ValueError) as error:
        return _missing("F11", "rendered host artifacts", error)
    if sample.get("reason"):
        return _missing("F11", "rendered host artifacts",
                        sample["reason"], sample)
    if logging_enabled is None:
        return _missing(
            "F11", "rendered host artifacts", logging_error,
            {"sample": sample, "logging_source": logging_source})
    rows = []
    failures = []
    missing = []
    env_by_host = {}
    fluent_by_host = {}
    for host in hosts:
        instance_id = host["instance_id"]
        result = _ssm_read(ctx, instance_id, "cat " + PLATFORM_ENV_PATH)
        sanitized = _sanitized_env(result["output"]) if result["ok"] else {}
        if not sanitized:
            missing.append(instance_id)
        env_by_host[instance_id] = sanitized
        fluent_digest = None
        fluent_status = "not-expected"
        if logging_enabled:
            fluent = _ssm_read(
                ctx, instance_id, "cat /etc/fluent-bit/fluent-bit.conf")
            fluent_status = fluent["status"]
            if fluent["ok"] and fluent["output"]:
                fluent_digest = _sha(fluent["output"].encode("utf-8"))
            else:
                missing.append("%s:fluent-bit.conf" % instance_id)
        fluent_by_host[instance_id] = fluent_digest
        rows.append({
            "instance_id": instance_id,
            "platform_env_status": result["status"],
            "key_names": sorted(sanitized),
            "value_sha256_12": sanitized,
            "fluent_bit_status": fluent_status,
            "fluent_bit_sha256": fluent_digest,
        })
    reference = _oldest_launched_host(hosts)
    reference_id = reference["instance_id"] if reference else None
    if reference_id is None:
        missing.append("host launch times")
    else:
        reference_env = env_by_host[reference_id]
        if reference_env:
            for instance_id, observed in env_by_host.items():
                if instance_id == reference_id or not observed:
                    continue
                keys = set(reference_env).union(observed)
                keys.discard("INSTANCE_ID")
                for key in keys:
                    if observed.get(key) != reference_env.get(key):
                        _record_fleet_divergence(
                            failures, reference_id, instance_id, key)
        reference_fluent = fluent_by_host[reference_id]
        if logging_enabled and reference_fluent is not None:
            for instance_id, observed in fluent_by_host.items():
                if (instance_id != reference_id and observed is not None
                        and observed != reference_fluent):
                    _record_fleet_divergence(
                        failures, reference_id, instance_id,
                        "fluent-bit.conf")
    verdict = "FAIL" if failures else ("INCONCLUSIVE" if missing else "PASS")
    return finding(
        "F11", verdict,
        "Rendered artifacts were compared using key names and truncated value digests only.",
        {"inspected": len(rows), "sample": sample,
         "logging_enabled": logging_enabled,
         "logging_source": logging_source,
         "hosts": rows, "missing": missing,
         "reference_instance_id": reference_id,
         "reference_basis": "oldest-launched"},
        sorted(set(failures)),
        "Regenerate rendered files and replace hosts whose sanitized digests diverge.",
    )
