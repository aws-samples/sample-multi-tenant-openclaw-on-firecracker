#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""oc-consistency — is the data plane what the gateway release says it is? (issue #521)

Answers one question per managed file, in three places at once:

    (a) the bytes on an in-service machine's disk
    (b) the S3 object that machine's replacement will read at boot
    (c) the file in the gateway checkout

(a) == (c) while (b) != (c) is the #458 shape: correct now, silently reverted on the next
boot. A two-point check cannot see it, so every row carries all three and an attribution.

Deliberately a separate file from cli/oc.py: that one is a zero-dependency REST client a
customer can run with an endpoint and an api-key, and mixing boto3/SSM/git into it would
destroy that property (issue #521 implementation contract, item 16).

Scope: `--scope dataplane` (the three-point check above) and `--scope controlplane`
(contract items 12-14): every lambda field with no sampling, out-of-band overwrite
detection from package entry timestamps, event-source mappings located by both ends, and
the OAS30 export digested per path+method. The expected baseline for the field comparison
is a JSON artifact from `--write-baseline` on an accepted deployment, not a `cdk synth`;
whatever a run could not judge is listed as not judged instead of counted as passing.

Exit codes (contract item 15), deliberately four:
    0  every checked place matches the gateway release
    1  drift
    2  INCONCLUSIVE — the sample could not be formed, or a probe/point is missing
    3  tool error — no gateway tree, no credentials, unsupported request

`2` is separate from `1` so a patch run can tell "check the environment" from "check the
drift", and `3` is separate from both because a broken tool must never read as a broken
fleet.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import pathlib
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

EXIT_OK, EXIT_DRIFT, EXIT_INCONCLUSIVE, EXIT_TOOL = 0, 1, 2, 3
SAMPLE_CAP = 5
LATEST_LAYER_MIN = 2  # two machines on the newest template, so one oddity cannot pass as fact


class ToolError(Exception):
    """Something about the tool or its inputs is wrong; never reported as fleet drift."""


def aws(profile: str | None, region: str, *args: str) -> dict | list:
    cmd = ["aws", *args, "--region", region, "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        # No `aws` on PATH raises here. Letting it propagate would leave Python to exit 1, and 1
        # means DRIFT in this tool's contract -- a missing CLI would read as a broken fleet.
        raise ToolError(f"could not run the aws CLI ({exc}); is it installed and on PATH?")
    if done.returncode != 0:
        raise ToolError(f"aws {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}")
    return json.loads(done.stdout or "null")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── sampling (contract items 6-10) ───────────────────────────────────────────


def metal_hosts(profile, region) -> list[dict]:
    """Hosts come from the Role tag, not the ASG.

    Measured once: the ASG held four while the tag held five, and the missing one carried
    real tenants. Sampling by ASG builds a blind spot into the monitoring itself.
    """
    out = aws(profile, region, "ec2", "describe-instances",
              "--filters", "Name=tag:Role,Values=metal-host",
              "Name=instance-state-name,Values=running")
    return [
        {"id": i["InstanceId"], "role": "host", "launched": i["LaunchTime"],
         "project": next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Project"), None)}
        for r in out.get("Reservations", []) for i in r.get("Instances", [])
    ]


def edge_instances(profile, region, asg_name: str) -> list[dict]:
    out = aws(profile, region, "autoscaling", "describe-auto-scaling-groups",
              "--auto-scaling-group-names", asg_name)
    groups = out.get("AutoScalingGroups") or []
    if not groups:
        return []
    return [{"id": i["InstanceId"], "role": "edge",
             "lt_version": (i.get("LaunchTemplate") or {}).get("Version")}
            for i in groups[0].get("Instances", [])]


def effective_lt_version(profile, region, asg_name: str) -> tuple[str, list[str], str]:
    """The version the NEXT machine will actually use, plus any disagreement as a finding.

    An ASG can reference `$Default`, `$Latest`, or a pinned number, and the fleet can be
    running a mix. The judgement is "what does the next launch get", so the alias is
    resolved; the spread is reported without changing the exit code.
    """
    findings: list[str] = []
    out = aws(profile, region, "autoscaling", "describe-auto-scaling-groups",
              "--auto-scaling-group-names", asg_name)
    groups = out.get("AutoScalingGroups") or []
    if not groups:
        raise ToolError(f"ASG {asg_name} not found")
    g = groups[0]
    spec = (g.get("LaunchTemplate")
            or ((g.get("MixedInstancesPolicy") or {}).get("LaunchTemplate") or {})
            .get("LaunchTemplateSpecification") or {})
    ref = spec.get("Version") or "$Default"
    lt_id = spec.get("LaunchTemplateId")
    if not lt_id:
        raise ToolError(f"ASG {asg_name} references no launch template")
    versions = aws(profile, region, "ec2", "describe-launch-template-versions",
                   "--launch-template-id", lt_id)["LaunchTemplateVersions"]
    default = next((str(v["VersionNumber"]) for v in versions if v.get("DefaultVersion")), None)
    latest = str(max(int(v["VersionNumber"]) for v in versions))
    resolved = {"$Default": default, "$Latest": latest}.get(ref, str(ref))
    if resolved is None:
        raise ToolError(f"cannot resolve {ref} for launch template {lt_id}")
    if ref not in ("$Default", "$Latest") and resolved != default:
        findings.append(
            f"{asg_name} pins version {resolved} while the template default is {default}: "
            "a promote would not reach new machines")
    running = sorted({str(i.get("lt_version")) for i in
                      [{"lt_version": (x.get("LaunchTemplate") or {}).get("Version")}
                       for x in g.get("Instances", [])] if i.get("lt_version")})
    if len(running) > 1:
        findings.append(f"{asg_name} fleet spans launch-template versions {running}")
    return resolved, findings, lt_id


BOOT_ASSIGNMENT = re.compile(
    r"""^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?:"(?P<double>[^"\r\n]*)"|"""
    r"""'(?P<single>[^'\r\n]*)'|(?P<bare>[^\s"']*))\s*$""",
    re.M,
)
UNRENDERED_PLACEHOLDER = re.compile(r"\{\{.*?\}\}")


def _parse_boot_vars(userdata: str) -> dict[str, str]:
    """Boot-time assignments, dropping any name this cannot pin to one value.

    The pattern is line-oriented, so it also matches `NAME=value` lines that are heredoc payload
    rather than shell -- the real init-host.sh writes a systemd unit whose `Environment=` lines
    match, twice, with different values. Letting the last match win would resolve a guard from a
    line that never executed, and the failure would be silent in the worst direction: a wrongly
    resolved guard marks its row NOT_APPLICABLE, so a file that should have been compared is
    skipped and the run still reads as clean.

    So a name with more than one distinct value is omitted entirely. Its guard then fails to
    resolve, which the caller already turns into a stated finding and a normal comparison. An
    unresolved guard costs noise; a wrongly resolved one costs a check.
    """
    candidates: dict[str, set[str]] = {}
    for match in BOOT_ASSIGNMENT.finditer(userdata):
        value = next(
            group for group in (
                match.group("double"), match.group("single"), match.group("bare")
            )
            if group is not None
        )
        if not UNRENDERED_PLACEHOLDER.search(value):
            candidates.setdefault(match.group("name"), set()).add(value)
    return {name: values.pop() for name, values in candidates.items() if len(values) == 1}


def boot_vars(profile, region, lt_id: str, version: str) -> dict[str, str]:
    out = aws(profile, region, "ec2", "describe-launch-template-versions",
              "--launch-template-id", lt_id, "--versions", version)
    versions = out.get("LaunchTemplateVersions") or []
    if not versions:
        raise ToolError(f"launch template {lt_id} version {version} not found")
    encoded = (versions[0].get("LaunchTemplateData") or {}).get("UserData")
    if not encoded:
        return {}
    try:
        userdata = base64.b64decode(encoded, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolError(
            f"launch template {lt_id} version {version} has invalid UserData"
        ) from exc
    return _parse_boot_vars(userdata)


def pick_sample(instances: list[dict], latest_version: str, seed: str | None) -> tuple[list[dict], str]:
    """Force in the newest-template machines, then fill the rest at random, capped.

    Forcing is what makes the #458 class visible at all: a sample of long-lived machines is
    green by construction. The forced layer does not consume the random quota.
    """
    latest = [i for i in instances if str(i.get("lt_version")) == str(latest_version)]
    older = [i for i in instances if str(i.get("lt_version")) != str(latest_version)]
    rng = random.Random(seed if seed is not None else "|".join(sorted(i["id"] for i in instances)))
    rng.shuffle(older)
    chosen = latest + older[: max(0, SAMPLE_CAP - len(latest))]
    for i in chosen:
        i["group"] = "latest-lt" if str(i.get("lt_version")) == str(latest_version) else "older"
    how = (f"forced {len(latest)} on version {latest_version}; "
           f"{len(chosen) - len(latest)} of {len(older)} older sampled; cap {SAMPLE_CAP}")
    return chosen[:SAMPLE_CAP] if len(chosen) > SAMPLE_CAP else chosen, how


# ── probing (contract item 6 evidence notes) ─────────────────────────────────


PROBE = r"""
set +e
for f in %s; do
  if [ -f "$f" ]; then printf '%%s\t%%s\n' "$f" "$(sha256sum "$f" | awk '{print $1}')"
  elif [ -e "$f" ]; then printf '%%s\tNOT_A_FILE\n' "$f"
  else printf '%%s\tABSENT\n' "$f"; fi
done
"""


def probe_instance(profile, region, instance_id: str, paths: list[str]) -> dict[str, str]:
    """One SSM call per instance, output gzip+base64 on a single line.

    Both halves are load-bearing and both were learned the hard way: the shorthand
    `commands=[...]` form flattens a multi-line script into the literal letter n while
    still returning Success, and the default plugin output is capped at 2500 characters,
    so a plain listing is silently truncated mid-probe.
    """
    script = PROBE % " ".join(f"'{p}'" for p in paths)
    payload = base64.b64encode(script.encode()).decode()
    one_liner = (f"echo {payload} | base64 -d | bash | gzip -9c | base64 | tr -d '\\n'")
    params = json.dumps({"commands": [one_liner]})
    sent = aws(profile, region, "ssm", "send-command", "--instance-ids", instance_id,
               "--document-name", "AWS-RunShellScript", "--comment",
               "oc-consistency dataplane probe (read-only)",
               "--parameters", params, "--timeout-seconds", "120")
    cid = sent["Command"]["CommandId"]
    inv: dict = {}
    for _ in range(30):
        time.sleep(4)
        try:
            inv = aws(profile, region, "ssm", "get-command-invocation",
                      "--command-id", cid, "--instance-id", instance_id)
        except ToolError as exc:
            # Documented eventual consistency: the invocation is not queryable the instant
            # send-command returns. Aborting here drops a machine from the sample, which
            # quietly narrows the very coverage the sampling rules just fought for.
            if "InvocationDoesNotExist" not in str(exc):
                raise
            continue
        if inv.get("Status") not in ("Pending", "InProgress", "Delayed"):
            break
    if inv.get("Status") != "Success":
        raise ToolError(f"probe of {instance_id} ended {inv.get('Status')} (command {cid})")
    raw = (inv.get("StandardOutputContent") or "").strip()
    if not raw:
        raise ToolError(f"probe of {instance_id} returned nothing (command {cid})")
    text = gzip.decompress(base64.b64decode(raw)).decode()
    return dict(
        line.split("\t", 1) for line in text.splitlines() if "\t" in line
    )


# ── managed inventory (contract item 3, derived not hardcoded) ────────────────


CP_LINE = re.compile(
    # `[\s\\]+` and not `\s+`: the adot fetch puts its destination on the next line behind a
    # backslash continuation, and a whitespace-only separator misses it -- which is how the
    # one file with a non-conventional destination fell out of the inventory entirely.
    r"""aws s3 cp[\s\\]+["']?s3://\S*?/(?P<key>deployment/[^\s"']+?)["']?[\s\\]+["']?(?P<dest>/[^\s"']+)""")
FOR_LOOP = re.compile(r"^for\s+(?P<var>\w+)\s+in\s+(?P<words>[^;\n]+?);\s*do$", re.M)
UNRESOLVED = re.compile(r"[$*?{]")
SUPPORTED_GUARD = re.compile(
    r"""^\s*if\s*\[\s*"\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}"\s*(?:==|=)\s*"""
    r"""(?P<quote>["'])(?P<equals>[^"'\r\n]*)(?P=quote)\s*\]\s*;\s*then\s*$"""
)
IF_OPEN = re.compile(r"^\s*if\b")
IF_BRANCH = re.compile(r"^\s*(?:elif|else)\b")
IF_CLOSE = re.compile(r"^\s*fi\b")
INLINE_IF_CLOSE = re.compile(r"\bfi\s*$")
CASE_OPEN = re.compile(r"^\s*case\b")
CASE_CLOSE = re.compile(r"^\s*esac\b")


def _key_tail(key: str) -> str:
    """`deployment/scripts/lib/x.sh` -> `lib/x.sh`; `deployment/observability/adot/y` -> `adot/y`."""
    parts = key.split("/", 2)
    return parts[2] if len(parts) == 3 else parts[-1]


def _expand_loops(text: str) -> str:
    """Emit one copy of a `for X in a b c; do ... done` body per word.

    Not cosmetic. init-host.sh fetches five scripts (migrate-vm, clone-data, resize-disk,
    start-all-vms, stop-all-vms) only through such a loop, and a scanner that reads literal
    paths drops all five WITHOUT SAYING SO -- the exact "a file nobody was checking" failure
    this tool exists to end, rebuilt inside the tool.
    """
    out, cursor = [], 0
    for m in FOR_LOOP.finditer(text):
        if m.start() < cursor:  # inside a loop already expanded; not handled, left as-is
            continue
        rest = text[m.end():]
        end = re.search(r"^done\b", rest, re.M)
        body = rest[: end.start()] if end else rest
        out.append(text[cursor:m.start()])
        # The loop region is REPLACED, not appended to: leaving the `${_s}` original in the
        # scanned text would make every expanded line ALSO surface as unresolved, and a
        # warning that always fires is a warning nobody reads.
        for w in m.group("words").split():
            out.append(body.replace("${%s}" % m.group("var"), w)
                           .replace("$%s" % m.group("var"), w))
        cursor = m.end() + (end.end() if end else len(rest))
    out.append(text[cursor:])
    return "\n".join(out)


def _copy_guards(text: str) -> dict[int, dict[str, str]]:
    """Return supported guards for copy matches that are in one conditional only."""
    copies = list(CP_LINE.finditer(text))
    guarded: dict[int, dict[str, str]] = {}
    stack: list[dict] = []
    copy_index = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        line_end = offset + len(line)
        stripped = line.rstrip("\r\n")
        starts_conditional = bool(
            IF_OPEN.match(stripped) or IF_BRANCH.match(stripped)
            or IF_CLOSE.match(stripped) or CASE_OPEN.match(stripped)
            or CASE_CLOSE.match(stripped)
        )
        while copy_index < len(copies) and copies[copy_index].start() < line_end:
            match = copies[copy_index]
            if (match.start() >= offset and not starts_conditional
                    and len(stack) == 1 and stack[0].get("guard")):
                guarded[match.start()] = stack[0]["guard"]
            copy_index += 1

        if IF_CLOSE.match(stripped) or CASE_CLOSE.match(stripped):
            if stack:
                stack.pop()
        elif IF_BRANCH.match(stripped):
            if stack and stack[-1]["kind"] == "if":
                stack[-1]["guard"] = None
        elif IF_OPEN.match(stripped):
            match = SUPPORTED_GUARD.fullmatch(stripped)
            guard = ({"var": match.group("var"), "equals": match.group("equals")}
                     if match else None)
            if not INLINE_IF_CLOSE.search(stripped):
                stack.append({"kind": "if", "guard": guard})
        elif CASE_OPEN.match(stripped):
            stack.append({"kind": "case", "guard": None})
        offset = line_end
    return guarded


def _find_in_tree(gateway: pathlib.Path, rel: str) -> pathlib.Path | None:
    """Locate the source of a published object without assuming one source directory.

    Most objects come from deploy/userdata/, but not all: the Fluent Bit installer lives in
    deploy/edge/fluent-bit/. Assuming a single directory made it NOT_IN_GATEWAY, which the
    tool then reports as INCONCLUSIVE -- honest, but it checks nothing.

    An ambiguous basename returns None on purpose: guessing between two same-named files
    would compare against the wrong one and call the result a match.
    """
    direct = gateway / "deploy/userdata" / rel
    if direct.is_file():
        return direct
    for pattern in (f"deploy/**/{rel}", f"deploy/**/{rel.split('/')[-1]}"):
        hits = [p for p in gateway.glob(pattern) if p.is_file()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def managed_files(gateway: pathlib.Path) -> tuple[list[dict], list[str]]:
    """Read the fetch list out of init-host.sh instead of keeping a copy of it here.

    A hardcoded list stops covering whatever gets added next, and the whole failure family
    this tool exists for is "a file nobody was checking".

    Both sides of each `aws s3 cp` are parsed -- key AND destination. Deriving the host path
    from the key instead (say, `.py` goes to /opt/openclaw) guesses wrong for anything that
    does not follow the convention: adot-config.yaml lands in the collector's own etc/ dir
    under a different name, and guessing produced ABSENT on every host, reported as drift.

    Returns (files, unresolved). `unresolved` holds fetch lines whose key or destination
    still contains shell expansion after loop expansion; the caller must surface them and
    refuse a clean verdict rather than let them vanish from the inventory.
    """
    init_host = gateway / "deploy/userdata/init-host.sh"
    if not init_host.is_file():
        raise ToolError(f"no init-host.sh under {gateway}; is this a gateway checkout?")
    text = init_host.read_text()
    expanded = _expand_loops(text)
    copy_guards = _copy_guards(expanded)
    by_dest: dict[str, dict] = {}
    guards_by_dest: dict[str, list[dict[str, str] | None]] = {}
    unresolved: list[str] = []
    for m in CP_LINE.finditer(expanded):
        key, dest = m.group("key"), m.group("dest")
        if UNRESOLVED.search(key) or UNRESOLVED.search(dest):
            line = f"unresolved fetch line: {key} -> {dest}"
            if line not in unresolved:
                unresolved.append(line)
            continue
        if dest.endswith("/"):  # `cp <key> <dir>/` keeps the object's own name
            dest += key.split("/")[-1]
        # Label by the key path below deployment/<area>/ so lib/cred-inject.sh stays
        # distinguishable from a same-named object somewhere else.
        row = by_dest.setdefault(dest, {"host_path": dest, "s3_keys": [],
                                        "rel": _key_tail(key)})
        guards_by_dest.setdefault(dest, []).append(copy_guards.get(m.start()))
        if key not in row["s3_keys"]:
            row["s3_keys"].append(key)
    if not by_dest:
        raise ToolError("init-host.sh named no deployment/ objects; CP_LINE needs review")
    for dest, row in by_dest.items():
        # envsubst rewrites the file in place, so the bytes on disk can never equal the
        # object. The host side is then not comparable -- which is not the same as drift.
        row["rendered"] = bool(re.search(r"envsubst[^\n]*" + re.escape(dest), text))
        row["s3_key"] = row["s3_keys"][0]
        row["gateway_file"] = _find_in_tree(gateway, row["rel"])
        guards = guards_by_dest[dest]
        if guards[0] is not None and all(guard == guards[0] for guard in guards):
            row["guard"] = guards[0]
    return sorted(by_dest.values(), key=lambda f: f["rel"]), unresolved


def s3_digest(profile, region, bucket: str, key: str) -> str:
    """Download and hash: ETag is not a sha256 for multipart objects.

    A private temp dir rather than a name derived from the key: two runs against different
    buckets would otherwise land on the same path, and the second would hash the first's
    bytes — a wrong ANSWER, not a crash.
    """
    with tempfile.TemporaryDirectory(prefix="occ-") as staging:
        tmp = pathlib.Path(staging) / "object"
        done = subprocess.run(
            ["aws", "s3", "cp", f"s3://{bucket}/{key}", str(tmp), "--no-progress",
             "--region", region] + (["--profile", profile] if profile else []),
            capture_output=True, text=True)
        if done.returncode != 0:
            return ("ABSENT" if "Not Found" in done.stderr or "404" in done.stderr
                    else "UNREADABLE")
        return sha256_bytes(tmp.read_bytes())


# ── comparison and attribution (original DoD item 4) ─────────────────────────


def attribute(gw: str, s3: str, host_states: dict[str, str]) -> tuple[str, str]:
    """Name the side that is off, because the two sides have opposite remedies."""
    if s3 == "UNREADABLE":
        # A throttle or a denied GetObject is not a fleet fact. Comparing the literal
        # string would have made it unequal to the gateway digest and reported drift.
        return "INCONCLUSIVE", "the S3 object could not be read; the (b) point is missing"
    readable = {i: s for i, s in host_states.items() if s not in ("UNREADABLE",)}
    if not readable:
        return "INCONCLUSIVE", "no machine answered for this file"
    hosts_ok = all(s == gw for s in readable.values())
    s3_ok = s3 == gw
    spread = " hosts-disagree=yes" if len(set(readable.values())) > 1 else ""
    if s3 == "ABSENT":
        return "DRIFT", ("s3-object-missing (the next machine to boot cannot fetch it)"
                         + ("" if hosts_ok else "; existing hosts also differ"))
    if s3_ok and hosts_ok:
        return "OK", ""
    if s3_ok:
        return "DRIFT", f"only-existing-hosts-off{spread}"
    if hosts_ok:
        return "DRIFT", "only-future-machines-off (correct now, reverts on next boot)"
    return "DRIFT", f"both-sides-off{spread}"


def compare(files, s3_by_key, probes, variables=None) -> list[dict]:
    variables = variables or {}
    rows = []
    for f in files:
        gw = sha256_bytes(f["gateway_file"].read_bytes()) if f["gateway_file"] else "NOT_IN_GATEWAY"
        host_states = {i: p.get(f["host_path"], "UNREADABLE") for i, p in probes.items()}
        s3, note = _s3_side(f, s3_by_key)
        guard = f.get("guard")
        actual = variables.get(guard["var"]) if guard else None
        if guard and guard["var"] in variables and actual != guard["equals"]:
            verdict = "NOT_APPLICABLE"
            why = (f"init-host fetches it only when {guard['var']}={guard['equals']!r}; "
                   f"the effective launch template sets {actual!r}")
        elif guard and guard["var"] not in variables:
            # The guard exists but this run could not judge it -- the variable is absent from the
            # userdata, or the sample spans launch-template versions so no single value speaks for
            # every machine. Comparing anyway would hand out a real verdict on a file that may be
            # correctly absent: absent everywhere reads as INCONCLUSIVE by luck, absent on some
            # machines reads as OK or DRIFT depending on which ones answered. Say "not judged"
            # instead, and name the variable so the next step is obvious.
            verdict = "INCONCLUSIVE"
            why = (f"init-host fetches it only when {guard['var']}={guard['equals']!r}, and this "
                   "run could not establish that value; not judged rather than guessed")
        elif f.get("rendered"):
            # The publish side is still worth comparing: both (b) and (c) hold the
            # pre-render template. Only the host side is off the table, and saying which
            # part was not checked beats either a false DRIFT or a silent omission.
            host_states = {i: "RENDERED" for i in probes}
            verdict, why = (("OK", "host side not comparable: rendered in place by envsubst")
                            if gw == s3 else
                            ("DRIFT", "template differs from the gateway release "
                                      "(host side not comparable: rendered by envsubst)"))
        elif gw == "NOT_IN_GATEWAY":
            verdict, why = "INCONCLUSIVE", "init-host fetches it but the gateway tree has no such file"
        else:
            verdict, why = attribute(gw, s3, host_states)
        rows.append({"file": f["rel"], "gateway": gw, "s3": s3, "hosts": host_states,
                     "verdict": verdict, "why": (why + note).strip()})
    return rows


def _s3_side(f, s3_by_key) -> tuple[str, str]:
    """First key wins; a later key is init-host's own fallback prefix, not a second file.

    adot-config.yaml is fetched from deployment/observability/adot/ and only then from the
    old deployment/scripts/ prefix (#229). Reporting the primary as missing while the
    fallback is what machines actually use would send an operator to fix the wrong object.
    """
    primary = s3_by_key.get(f["s3_key"], "UNREADABLE")
    if primary != "ABSENT":
        return primary, ""
    for alt in f.get("s3_keys", [])[1:]:
        if s3_by_key.get(alt) not in (None, "ABSENT", "UNREADABLE"):
            return s3_by_key[alt], f" [via fallback prefix {alt}]"
    return primary, ""


# ── reporting (contract item 15: three tables) ────────────────────────────────


def report(rows, sample, sampling_note, findings) -> None:
    print("\n== sample ==")
    print(f"  {sampling_note}")
    for i in sample:
        print(f"  {i['id']}  role={i['role']}  group={i['group']}  "
              f"lt={i.get('lt_version')}  launched={i.get('launched', '-')}")
    ok = [r for r in rows if r["verdict"] == "OK"]
    print(f"\n== identical to the gateway release ({len(ok)}) ==")
    for r in ok:
        print(f"  {r['file']}  {r['gateway'][:16]}")
    # A row nobody compared does not belong under "not identical" -- that heading reads as a
    # problem, and the whole point of NOT_APPLICABLE is to say "correctly absent", not "wrong".
    skipped = [r for r in rows if r["verdict"] == "NOT_APPLICABLE"]
    if skipped:
        print(f"\n== not applicable: init-host never fetches these here ({len(skipped)}) ==")
        for r in skipped:
            print(f"  {r['file']}  {r['why']}")
    bad = [r for r in rows if r["verdict"] not in ("OK", "NOT_APPLICABLE")]
    print(f"\n== not identical ({len(bad)}) ==")
    for r in bad:
        print(f"  {r['file']}  {r['verdict']}  {r['why']}")
        print(f"      gateway={r['gateway'][:16]}  s3={r['s3'][:16]}")
        for i, s in r["hosts"].items():
            print(f"      {i}={s[:16]}")
    if findings:
        print("\n== findings (reported, no effect on the exit code) ==")
        for f in findings:
            print(f"  {f}")


# ── control plane (contract items 12-14) ─────────────────────────────────────
#
# Three of the four judgements here need no expected baseline at all, which is why they
# exist: each one is a property of the live object that is either true or false on its own
# terms. A `cdk synth` baseline was the blocker that kept this half unimplemented, and
# waiting for it meant shipping nothing -- while the two accidents this is meant to catch
# both a dispatch and a lifecycle ESM) are both self-evident from the live state.
#
# The fourth judgement -- every field equal to what it should be -- does need a baseline.
# It takes one as a JSON artifact (`--baseline`), written by `--write-baseline` against a
# deployment that has been accepted. Without it the run says which items it did not judge
# and exits 2; it never prints a comparison it did not make.

CDK_ZERO_YMD = (1980, 1, 1)
# Vendored single modules keep their upstream mtime through CDK packaging, so they carry a
# real timestamp without anybody having pushed code out of band. Named, not pattern-matched:
# an accidental broad pattern here would hide the very entries this looks for.
OOB_TIMESTAMP_WHITELIST = ("typing_extensions.py",)
# How much of a package has to be zeroed before a real mtime is evidence of a push rather than of a
# 576/578 zeroed, and the live apse1 packages are ~0.5.
CDK_ZEROED_FRACTION_MIN = 0.9
LAMBDA_FIELDS = ("CodeSha256", "Runtime", "Handler", "Timeout", "MemorySize",
                 "Layers", "EnvKeys")


def package_shape(zip_path) -> tuple[int, int]:
    """(file entries, of which normalised to 1980). Printed with every verdict: "OVERWRITTEN" and
    "MIXED" are conclusions about a ratio, and a reader cannot check the conclusion without it."""
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    return len(infos), sum(1 for i in infos if i.date_time[:3] == CDK_ZERO_YMD)


def package_overwrite_scan(zip_path, whitelist=OOB_TIMESTAMP_WHITELIST) -> tuple[str, list[str]]:
    """Which entries were pushed after the CDK deploy, from the timestamps alone.

    CDK zeroes every entry to 1980-01-01 for reproducible builds, so an entry with a real
    mtime in an otherwise-zeroed package was written by `update-function-code` or by hand.
    That is a binary discriminator; #444 established it on the live `openclaw-api`, where
    exactly two of 578 entries carried real times. Counting lines or diffing against the
    repo cannot do this -- both need a baseline, and the baseline is what was in doubt.

    Returns ("CDK", offenders) or ("NOT_CDK", []) when no entry is zeroed at all: a package
    built some other way carries no signal, and calling that clean would be a false OK.
    """
    with zipfile.ZipFile(zip_path) as zf:
        # Directory entries are not code and can never be the target of a push, and pip leaves its
        # own mtimes on them. Counting them was measured (apse1, `openclaw-api`) to turn the report
        # into a list of `aws_lambda_powertools/` directories.
        infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    if not infos:
        return "NOT_CDK", []
    zeroed = [i for i in infos if i.date_time[:3] == CDK_ZERO_YMD]
    if not zeroed:
        return "NOT_CDK", []
    # Measured on the live apse1 deployment: `openclaw-api` and `openclaw-lifecycle-consumer` carry
    # ~50% real mtimes because their dependencies are pip-installed into the asset directory, which
    # So the discriminator only speaks when zeroing dominates; a mixed package means "this build does
    # not normalise timestamps", which is a reason to say the check does not apply, not a reason to
    # name hundreds of files as pushed by hand.
    if len(zeroed) / len(infos) < CDK_ZEROED_FRACTION_MIN:
        return "MIXED", []
    return "CDK", sorted(
        i.filename for i in infos
        if i.date_time[:3] != CDK_ZERO_YMD
        and pathlib.PurePosixPath(i.filename).name not in whitelist
    )


def vendored_real_mtime_entries(zip_path,
                                whitelist=OOB_TIMESTAMP_WHITELIST) -> list[str]:
    """Entries excused by the whitelist that still carry a real mtime.

    The whitelist exists so a vendored module does not read as an out-of-band push. Left silent it
    becomes a hiding place: an actual overwrite of `typing_extensions.py` would be excused with no
    trace (cross-model review finding). Reported separately -- visible, and not a verdict by itself.
    """
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(i.filename for i in zf.infolist()
                      if not i.filename.endswith("/")
                      and i.date_time[:3] != CDK_ZERO_YMD
                      and pathlib.PurePosixPath(i.filename).name in whitelist)


def esm_rows(mappings: list[dict]) -> tuple[list[dict], list[str]]:
    """One row per (function ARN, source ARN) pair, and a finding when a pair is not unique.

    Located by BOTH ends on purpose. A stack can hold a dispatch ESM and a lifecycle ESM at
    once, so "the first event-source mapping in the stack" compares whichever one the API
    happened to return first -- a wrong answer that reads like a checked one (this is also
    how a batch_size of 10 -> 1 got attributed to dispatch when it was lifecycle's).
    """
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for m in mappings:
        key = (m.get("FunctionArn") or "", m.get("EventSourceArn") or "")
        by_pair.setdefault(key, []).append(m)
    rows, findings = [], []
    for (fn, src), group in sorted(by_pair.items()):
        row = {"function_arn": fn, "source_arn": src,
               "batch_size": group[0].get("BatchSize"),
               "batching_window": group[0].get("MaximumBatchingWindowInSeconds"),
               "state": group[0].get("State"),
               "unique": len(group) == 1}
        if len(group) > 1:
            findings.append(
                f"{len(group)} event-source mappings share function {fn.split(':')[-1]!r} and "
                f"source {src.split(':')[-1]!r} (uuids {[m.get('UUID') for m in group]}); "
                "not judged — picking one would compare an arbitrary object")
        rows.append(row)
    return rows, findings


def normalise_oas(doc: dict, rest_api_id: str, account: str | None,
                  region: str | None) -> dict:
    """Strip what changes between two exports of the same API, keep what changes with the API.

    `info.version` is the export timestamp and `servers[].url` embeds the api id and stage,
    so leaving them in makes every export differ from every other and the digest says
    nothing. Account/region/api-id are rewritten to placeholders instead of dropped: the
    integration URIs are part of the contract, and a changed target must still show up.
    """
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    for real, placeholder in ((rest_api_id, "<api>"), (account, "<account>"),
                              (region, "<region>")):
        if real:
            body = body.replace(real, placeholder)
    out = json.loads(body)
    (out.get("info") or {}).pop("version", None)
    out.pop("servers", None)
    return out


def oas_digests(doc: dict, rest_api_id: str, account=None, region=None) -> tuple[str, dict]:
    """Whole-document digest plus one per path+method, so a diff lands on an operation.

    "the summary is not equal" is not actionable; #521 asks for path/method level.
    """
    norm = normalise_oas(doc, rest_api_id, account, region)
    whole = sha256_bytes(json.dumps(norm, sort_keys=True, separators=(",", ":")).encode())
    per_op = {}
    for path, ops in sorted((norm.get("paths") or {}).items()):
        for method, body in sorted(ops.items()):
            blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            per_op[f"{method.upper()} {path}"] = sha256_bytes(blob)
    return whole, per_op


def lambda_row(cfg: dict, aliases: dict[str, str]) -> dict:
    """Every field #521 names, per function -- no sampling on the control plane."""
    env_keys = sorted((cfg.get("Environment") or {}).get("Variables", {}) or {})
    return {
        "name": cfg.get("FunctionName"),
        "CodeSha256": cfg.get("CodeSha256"),
        "Runtime": cfg.get("Runtime"),
        "Handler": cfg.get("Handler"),
        "Timeout": cfg.get("Timeout"),
        "MemorySize": cfg.get("MemorySize"),
        "Layers": sorted(layer.get("Arn", "") for layer in cfg.get("Layers") or []),
        "EnvKeys": env_keys,
        "Aliases": dict(sorted(aliases.items())),
    }


def compare_to_baseline(current: list[dict], baseline: dict) -> tuple[list[dict], list[str]]:
    """Per-field verdicts against an accepted deployment, plus set differences.

    Env vars are compared as a KEY SET, never as values: values hold endpoints and secret
    names that legitimately differ, while a missing key is the shape of a feature whose code
    shipped and whose switch did not (measured on apse1: five keys short, the fix-430 code
    present and dark, and the verify of the day asserted "key count unchanged" and passed).
    """
    want = {f["name"]: f for f in baseline.get("functions", [])}
    rows, findings = [], []
    for cur in current:
        expected = want.get(cur["name"])
        if expected is None:
            findings.append(f"{cur['name']}: live but not in the baseline; not judged")
            continue
        diffs = {}
        for field in LAMBDA_FIELDS:
            live, want_v = cur.get(field), expected.get(field)
            # Set-valued fields are compared as sets: env vars and layers have no meaningful
            # order, and reporting a reordering as drift would send someone to look for a
            # change that did not happen.
            if isinstance(live, list) or isinstance(want_v, list):
                same = sorted(live or []) == sorted(want_v or [])
            else:
                same = live == want_v
            if not same:
                diffs[field] = {"live": live, "baseline": want_v}
        live_aliases = cur.get("Aliases") or {}
        want_aliases = expected.get("Aliases") or {}
        # Both directions: an alias the baseline does not have is as much a difference as one it has
        # and the live function does not. Iterating the baseline alone let an extra alias pass.
        for alias in sorted(set(live_aliases) | set(want_aliases)):
            if live_aliases.get(alias) != want_aliases.get(alias):
                diffs[f"alias:{alias}"] = {"live": live_aliases.get(alias),
                                           "baseline": want_aliases.get(alias)}
        rows.append({"name": cur["name"], "verdict": "DRIFT" if diffs else "OK",
                     "diffs": diffs})
    live_names = {f["name"] for f in current}
    for gone in sorted(set(want) - live_names):
        rows.append({"name": gone, "verdict": "DRIFT",
                     "diffs": {"function": {"live": None, "baseline": "present"}}})
    return rows, findings


def compare_esm(current: list[dict], baseline: dict) -> tuple[list[dict], list[str]]:
    """Per-mapping verdicts against the recorded set, keyed by both ends.

    Without this the rows were printed and then dropped: the run showed `batch_size=1` and compared
    it to nothing. A batching change is exactly the kind of drift that produces no error and a
    different failure mode under load (the 10 -> 1 row in the patch audit is one of these).
    """
    want = {(r.get("function_arn"), r.get("source_arn")): r
            for r in baseline.get("event_source_mappings", [])}
    rows, findings = [], []
    for cur in current:
        key = (cur["function_arn"], cur["source_arn"])
        expected = want.get(key)
        if expected is None:
            rows.append({"pair": key, "verdict": "DRIFT",
                         "diffs": {"mapping": {"live": "present", "baseline": None}}})
            continue
        if not cur.get("unique"):
            # Two facts at once: a second mapping on this pair definitely appeared (DRIFT), and which
            # one's fields to compare is unknowable (so they are not compared). Reporting only the
            # first would compare an arbitrary object; reporting only INCONCLUSIVE would bury the fact.
            rows.append({"pair": key, "verdict": "DRIFT",
                         "diffs": {"unique": {"live": False, "baseline": expected.get("unique")}},
                         "fields_not_compared": True})
            continue
        diffs = {}
        for field in ("batch_size", "batching_window", "state", "unique"):
            if cur.get(field) != expected.get(field):
                diffs[field] = {"live": cur.get(field), "baseline": expected.get(field)}
        rows.append({"pair": key, "verdict": "DRIFT" if diffs else "OK", "diffs": diffs})
    live_keys = {(r["function_arn"], r["source_arn"]) for r in current}
    for gone in sorted(set(want) - live_keys):
        rows.append({"pair": gone, "verdict": "DRIFT",
                     "diffs": {"mapping": {"live": None, "baseline": "present"}}})
    return rows, findings


def fetch_lambdas(profile, region, prefix: str) -> list[dict]:
    out = aws(profile, region, "lambda", "list-functions")
    fns = [f for f in out.get("Functions", [])
           if str(f.get("FunctionName", "")).startswith(prefix)]
    rows = []
    for f in fns:
        got = aws(profile, region, "lambda", "list-aliases",
                  "--function-name", f["FunctionName"])
        aliases = {a["Name"]: a.get("FunctionVersion")
                   for a in (got.get("Aliases") or [])}
        rows.append(lambda_row(f, aliases))
    return sorted(rows, key=lambda r: r["name"])


def scan_packages(profile, region, names: list[str]) -> tuple[dict, list[str]]:
    """Download each deployment package and read its entry timestamps."""
    verdicts, findings, excused, shapes = {}, [], {}, {}
    for name in names:
        got = aws(profile, region, "lambda", "get-function", "--function-name", name)
        url = ((got.get("Code") or {}).get("Location") or "")
        if not url:
            findings.append(f"{name}: no code location returned; package not scanned")
            verdicts[name] = ("UNREADABLE", [])
            continue
        with tempfile.TemporaryDirectory(prefix="occ-cp-") as staging:
            local = pathlib.Path(staging) / "fn.zip"
            try:
                with urllib.request.urlopen(url) as resp:  # nosec B310 — Lambda-signed URL
                    local.write_bytes(resp.read())
                verdicts[name] = package_overwrite_scan(local)
                excused[name] = vendored_real_mtime_entries(local)
                shapes[name] = package_shape(local)
            except (OSError, zipfile.BadZipFile) as exc:
                findings.append(f"{name}: package could not be read ({exc}); not scanned")
                verdicts[name] = ("UNREADABLE", [])
    return verdicts, findings, excused, shapes


def fetch_oas(profile, region, rest_api_id: str, stage: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="occ-oas-") as staging:
        out = pathlib.Path(staging) / "oas.json"
        done = subprocess.run(
            ["aws", "apigateway", "get-export", "--rest-api-id", rest_api_id,
             "--stage-name", stage, "--export-type", "oas30",
             "--parameters", "extensions=integrations", "--accepts", "application/json",
             str(out), "--region", region] + (["--profile", profile] if profile else []),
            capture_output=True, text=True)
        if done.returncode != 0:
            raise ToolError(f"apigateway get-export failed: {done.stderr.strip()[:300]}")
        return json.loads(out.read_text())


def report_controlplane(fn_rows, pkg, esm, oas, compared, findings, not_judged,
                        vendored=None, shapes=None, esm_compared=None) -> None:
    print(f"\n== lambda functions ({len(fn_rows)}) — every field, no sampling ==")
    verdict_by_name = {r["name"]: r for r in compared}
    for row in fn_rows:
        got = verdict_by_name.get(row["name"])
        mark = got["verdict"] if got else "NOT_JUDGED"
        print(f"  {row['name']}  {mark}  code={str(row['CodeSha256'])[:16]} "
              f"runtime={row['Runtime']} handler={row['Handler']} "
              f"timeout={row['Timeout']} mem={row['MemorySize']} "
              f"layers={len(row['Layers'])} env_keys={len(row['EnvKeys'])} "
              f"aliases={row['Aliases']}")
        for field, sides in (got or {}).get("diffs", {}).items():
            print(f"      {field}: live={sides['live']!r} baseline={sides['baseline']!r}")
    vendored, shapes = vendored or {}, shapes or {}

    def _shape(name):
        total, zeroed = shapes.get(name, (0, 0))
        return f" [{zeroed}/{total} entries normalised]" if total else ""

    print("\n== out-of-band overwrites (entry timestamps; needs no baseline) ==")
    for name, (state, offenders) in sorted(pkg.items()):
        if state == "CDK" and not offenders:
            print(f"  {name}  CLEAN{_shape(name)} (every entry zeroed to 1980-01-01 by CDK)")
        elif state == "CDK":
            shown = ", ".join(offenders[:10])
            more = f" (+{len(offenders) - 10} more)" if len(offenders) > 10 else ""
            print(f"  {name}  OVERWRITTEN{_shape(name)}: {shown}{more}")
        elif state == "MIXED":
            print(f"  {name}  MIXED{_shape(name)} (the build does not normalise entry timestamps, so a real mtime "
                  "is not evidence of a push; this package cannot be judged this way)")
        else:
            print(f"  {name}  {state}{_shape(name)} (no zeroed entry: the discriminator does not apply)")
    for name, excused in sorted(vendored.items()):
        if excused:
            print(f"  {name}  note: {len(excused)} entry(ies) excused by the vendored-module "
                  f"whitelist while carrying a real mtime: {', '.join(excused)}")
    print("\n== event-source mappings (located by both ends) ==")
    esm_verdict = {r["pair"]: r for r in (esm_compared or [])}
    for row in esm:
        got = esm_verdict.get((row["function_arn"], row["source_arn"]))
        mark = got["verdict"] if got else "NOT_JUDGED"
        print(f"  {row['function_arn'].split(':')[-1]} <- {row['source_arn'].split(':')[-1]}  "
              f"{mark}  batch_size={row['batch_size']} window={row['batching_window']} "
              f"state={row['state']} unique={row['unique']}")
        for field, sides in (got or {}).get("diffs", {}).items():
            print(f"      {field}: live={sides['live']!r} baseline={sides['baseline']!r}")
    for row in (esm_compared or []):
        # 判「mapping 键是否存在」,不能靠 .get("mapping", {}) 的默认空 dict:那样任何没有
        # mapping 键的 DRIFT 行(例如只有 batch_size 漂移)也会取到 live=None,于是同一对
        # ESM 既报 DRIFT 又被谎报成「基线里有、线上没有」。us-east-1 实测复现过。
        mapping_diff = row["diffs"].get("mapping")
        if row["verdict"] == "DRIFT" and mapping_diff is not None and mapping_diff.get("live") is None:
            print(f"  MISSING {row['pair'][0].split(':')[-1]} <- {row['pair'][1].split(':')[-1]}  "
                  "recorded in the baseline, absent live")
    if oas:
        whole, per_op, drifted = oas
        print(f"\n== api gateway schema ==\n  document={whole[:16]} operations={len(per_op)}")
        for op in drifted:
            print(f"  DRIFT {op}")
    if findings:
        print("\n== findings ==")
        for f in findings:
            print(f"  {f}")
    if not_judged:
        print("\n== NOT judged in this run ==")
        for n in not_judged:
            print(f"  {n}")


def run_controlplane(args) -> int:
    findings: list[str] = []
    not_judged: list[str] = []
    fn_rows = fetch_lambdas(args.profile, args.region, args.fn_prefix)
    if not fn_rows:
        print(f"no lambda function name starts with {args.fn_prefix!r}", file=sys.stderr)
        return EXIT_INCONCLUSIVE

    pkg: dict[str, tuple[str, list[str]]] = {}
    vendored: dict[str, list[str]] = {}
    shapes: dict[str, tuple[int, int]] = {}
    if args.scan_packages:
        pkg, pkg_findings, vendored, shapes = scan_packages(args.profile, args.region,
                                                            [r["name"] for r in fn_rows])
        findings += pkg_findings
    else:
        not_judged.append("out-of-band overwrite scan (--no-scan-packages)")

    esm, esm_findings = esm_rows(
        (aws(args.profile, args.region, "lambda", "list-event-source-mappings")
         or {}).get("EventSourceMappings", []))
    findings += esm_findings

    oas = None
    if args.rest_api_id:
        whole, per_op = oas_digests(
            fetch_oas(args.profile, args.region, args.rest_api_id, args.stage),
            args.rest_api_id, args.account, args.region)
        expected = (json.loads(pathlib.Path(args.baseline).read_text())
                    if args.baseline else {}).get("api", {})
        drifted = sorted(
            op for op in set(per_op) | set(expected.get("operations", {}))
            if per_op.get(op) != expected.get("operations", {}).get(op)
        ) if expected else []
        if not expected:
            not_judged.append("api gateway schema vs an expected baseline "
                              "(exported and digested, but nothing to compare to)")
        oas = (whole, per_op, drifted)
    else:
        not_judged.append("api gateway schema (--rest-api-id not given)")

    compared: list[dict] = []
    esm_compared: list[dict] = []
    if args.baseline:
        baseline = json.loads(pathlib.Path(args.baseline).read_text())
        compared, cmp_findings = compare_to_baseline(fn_rows, baseline)
        findings += cmp_findings
        if "event_source_mappings" in baseline:
            esm_compared, esm_cmp_findings = compare_esm(esm, baseline)
            findings += esm_cmp_findings
        else:
            not_judged.append("event-source mappings vs an expected baseline (this baseline was "
                              "written before they were recorded; re-run --write-baseline)")
        unbaselined = sorted({r["name"] for r in fn_rows}
                             - {r["name"] for r in compared})
        if unbaselined:
            not_judged.append(
                "per-field comparison of " + ", ".join(unbaselined)
                + " (live but absent from the baseline; a finding alone does not move the exit "
                  "code, so it is counted as not judged)")
    else:
        not_judged.append("per-field comparison of every lambda (--baseline not given; "
                          "the inventory below is reported, not judged)")

    report_controlplane(fn_rows, pkg, esm, oas, compared, findings, not_judged,
                        vendored, shapes, esm_compared)

    if args.write_baseline:
        pathlib.Path(args.write_baseline).write_text(json.dumps(
            {"functions": fn_rows,
             "event_source_mappings": esm,
             "api": ({"rest_api_id": args.rest_api_id, "stage": args.stage,
                      "document": oas[0], "operations": oas[1]} if oas else {})},
            indent=2, sort_keys=True))
        print(f"\nbaseline written: {args.write_baseline}")

    overwritten = [n for n, (state, bad) in pkg.items() if state == "CDK" and bad]
    # NOT_CDK / MIXED / UNREADABLE all mean the same thing for the exit code: this package was not
    # judged. Lumping them under "unreadable" is the honest reading -- none of them is a clean bill.
    unreadable = [n for n, (state, _b) in pkg.items() if state != "CDK"]
    field_drift = [r["name"] for r in compared if r["verdict"] == "DRIFT"]
    esm_drift = [r["pair"] for r in esm_compared if r["verdict"] == "DRIFT"]
    api_drift = list(oas[2]) if oas else []
    ambiguous = [r for r in esm if not r["unique"]]

    # A positive finding outranks an unjudged item: DRIFT means "this is wrong", and
    # downgrading it to INCONCLUSIVE because some *other* item had no baseline would bury a
    # fact that was established. INCONCLUSIVE keeps its meaning -- nothing definite found,
    # and something could not be told.
    if overwritten or field_drift or api_drift or esm_drift:
        print(f"\nverdict=DRIFT out_of_band={len(overwritten)} fields={len(field_drift)} "
              f"api_operations={len(api_drift)} event_source_mappings={len(esm_drift)} "
              f"functions={len(fn_rows)}")
        return EXIT_DRIFT
    if unreadable or ambiguous or not_judged:
        print(f"\nverdict=INCONCLUSIVE functions={len(fn_rows)} "
              f"unreadable_packages={len(unreadable)} ambiguous_esm={len(ambiguous)} "
              f"not_judged={len(not_judged)}", file=sys.stderr)
        return EXIT_INCONCLUSIVE
    print(f"\nverdict=CONSISTENT functions={len(fn_rows)} esm={len(esm)} "
          f"api_operations={len(oas[1]) if oas else 0}")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    """A usage error is a tool error (3), not INCONCLUSIVE (2).

    argparse exits 2 by default, and 2 is this tool's "a machine or a point could not be read".
    A typo in a flag would then be indistinguishable from an unreadable fleet.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: usage error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_TOOL)


def parse_args(argv):
    p = _Parser(description="Compare the data plane against a gateway release.")
    p.add_argument("--region", required=True)
    p.add_argument("--gateway-dir", required=True, help="local checkout of the gateway branch")
    p.add_argument("--scope", default="dataplane", choices=["dataplane", "controlplane"])
    p.add_argument("--profile")
    p.add_argument("--assets-bucket", required=True,
                   help="taken from the LT userdata s3:// URL, never guessed from the region: "
                        "one region uses a bare name and another a -<region> suffix, and a wrong "
                        "guess reads another deployment's objects without erroring")
    p.add_argument("--host-asg", default="openclaw-hosts-asg")
    p.add_argument("--seed", help="reproducible sampling; defaults to the instance-id list")
    # control plane (items 12-14). The data-plane run does not read these.
    p.add_argument("--fn-prefix", default="openclaw",
                   help="control plane: which functions belong to this deployment")
    p.add_argument("--rest-api-id", help="control plane: the API whose OAS30 export is checked")
    p.add_argument("--stage", default="v1")
    p.add_argument("--account", help="control plane: rewritten to a placeholder before digesting")
    p.add_argument("--baseline", help="control plane: JSON from a --write-baseline run on an "
                                     "accepted deployment; without it the per-field comparison "
                                     "is reported as not judged, never as passing")
    p.add_argument("--write-baseline", help="control plane: record the live state as the baseline")
    p.add_argument("--no-scan-packages", dest="scan_packages", action="store_false",
                   help="control plane: skip downloading the deployment packages")
    p.set_defaults(scan_packages=True)
    args = p.parse_args(argv)
    if args.scope == "controlplane" and args.baseline and args.write_baseline:
        p.error("--baseline and --write-baseline are opposite directions; pick one")
    return args


def main(argv=None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.scope == "controlplane":
        try:
            return run_controlplane(args)
        except ToolError as exc:
            print(f"tool error: {exc}", file=sys.stderr)
            return EXIT_TOOL
        except Exception as exc:  # noqa: BLE001 — same reason as the data-plane path below
            import traceback
            traceback.print_exc()
            print(f"tool error (unexpected {type(exc).__name__}); exiting {EXIT_TOOL}, not 1: "
                  "a broken tool must never read as a broken control plane.", file=sys.stderr)
            return EXIT_TOOL
    try:
        gateway = pathlib.Path(args.gateway_dir).expanduser().resolve()
        files, unresolved = managed_files(gateway)
        latest, findings, lt_id = effective_lt_version(
            args.profile, args.region, args.host_asg
        )
        variables = boot_vars(args.profile, args.region, lt_id, latest)
        findings += unresolved
        for f in files:
            guard = f.get("guard")
            if guard and guard["var"] not in variables:
                findings.append(
                    f"{f['rel']}: boot-time variable {guard['var']} did not resolve; "
                    "compared without applying its init-host guard"
                )
        hosts = metal_hosts(args.profile, args.region)
        foreign = [h for h in hosts if h["project"] != "openclaw"]
        for h in foreign:
            findings.append(f"{h['id']} carries Role=metal-host but Project={h['project']!r}; "
                            "excluded — this tool does not send SSM to another fleet")
        hosts = [h for h in hosts if h["project"] == "openclaw"]
        for h in hosts:
            h["lt_version"] = None
        asg_ids = {i["id"]: i for i in edge_instances(args.profile, args.region, args.host_asg)}
        for h in hosts:
            if h["id"] in asg_ids:
                h["lt_version"] = asg_ids[h["id"]]["lt_version"]
            else:
                findings.append(f"{h['id']} carries Role=metal-host but is not in {args.host_asg}")
        if not hosts:
            # "none carry the tag" and "all that carry it were excluded" send an operator to
            # different places, so the exclusions are printed rather than summarised away.
            print("no in-fleet instance carries Role=metal-host", file=sys.stderr)
            for f in findings:
                print(f"  - {f}", file=sys.stderr)
            return EXIT_INCONCLUSIVE
        sample, note = pick_sample(hosts, latest, args.seed)
        in_latest = [i for i in sample if i["group"] == "latest-lt"]
        if len(in_latest) < LATEST_LAYER_MIN:
            report([], sample, note, findings)
            print(f"\nINCONCLUSIVE: {len(in_latest)} machine(s) on launch-template version "
                  f"{latest}, need {LATEST_LAYER_MIN}. Roll a refresh first: a sample without "
                  "the newest template cannot see the class of drift this checks for.",
                  file=sys.stderr)
            return EXIT_INCONCLUSIVE
        probes, probe_errors = {}, []
        for i in sample:
            try:
                probes[i["id"]] = probe_instance(args.profile, args.region, i["id"],
                                                 [f["host_path"] for f in files])
            except ToolError as exc:
                probe_errors.append(str(exc))
        s3_by_key = {k: s3_digest(args.profile, args.region, args.assets_bucket, k)
                     for f in files for k in f["s3_keys"]}
        # The guards were resolved from ONE launch-template version: the one the next machine
        # gets. Applying them to a machine that booted from a different version would mark a file
        # not-applicable on a host that legitimately has it -- during a rolling change that flips
        # the guard variable, that hides exactly the drift this tool exists to find. So the
        # resolution is used only when the whole sample sits on the effective version; otherwise
        # nothing resolves, every row is compared as before, and the reason is stated.
        off_version = sorted({str(i.get("lt_version")) for i in sample
                              if str(i.get("lt_version")) != str(latest)})
        if off_version and variables:
            findings.append(
                f"sample spans launch-template version(s) {off_version} besides the effective "
                f"{latest}; init-host guards were NOT applied, because a guard read from one "
                "version cannot speak for a machine that booted from another"
            )
            variables = {}
        rows = compare(files, s3_by_key, probes, variables)
        report(rows, sample, note, findings + probe_errors)
        skipped = [r for r in rows if r["verdict"] == "NOT_APPLICABLE"]
        compared = [r for r in rows if r["verdict"] != "NOT_APPLICABLE"]
        if skipped:
            print(f"\nNOT_APPLICABLE: skipped {len(skipped)} row(s) because their "
                  "init-host guards are false in the effective launch template.")
        if (probe_errors or unresolved or not probes
                or not compared
                or any(r["verdict"] == "INCONCLUSIVE" for r in compared)):
            print("\nINCONCLUSIVE: at least one of the three points could not be read.",
                  file=sys.stderr)
            return EXIT_INCONCLUSIVE
        drift = [r for r in compared if r["verdict"] == "DRIFT"]
        print(f"\nverdict={'DRIFT' if drift else 'CONSISTENT'} "
              f"identical={len(compared) - len(drift)} drift={len(drift)} "
              f"not_applicable={len(skipped)} "
              f"machines={len(probes)} gateway={gateway}")
        return EXIT_DRIFT if drift else EXIT_OK
    except ToolError as exc:
        print(f"tool error: {exc}", file=sys.stderr)
        return EXIT_TOOL
    except Exception as exc:  # noqa: BLE001 — see below; deliberate, not a swallow
        # An unhandled failure would otherwise leave Python to exit 1, and 1 means DRIFT.
        # A missing aws CLI or a malformed response would then read as a broken fleet and
        # send someone to redeploy. Re-raising the traceback for the operator, mapped to 3.
        import traceback
        traceback.print_exc()
        print(f"tool error (unexpected {type(exc).__name__}); exiting {EXIT_TOOL}, not 1: "
              "a broken tool must never read as a broken fleet.", file=sys.stderr)
        return EXIT_TOOL


if __name__ == "__main__":
    raise SystemExit(main())
