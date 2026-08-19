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

Scope: `--scope dataplane`. The control-plane half of the contract (items 12-14) needs a
`cdk synth` of the gateway tree for its expected baseline and is NOT implemented here;
asking for it exits 3 rather than reporting a comparison this tool did not make.

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

EXIT_OK, EXIT_DRIFT, EXIT_INCONCLUSIVE, EXIT_TOOL = 0, 1, 2, 3
SAMPLE_CAP = 5
LATEST_LAYER_MIN = 2  # two machines on the newest template, so one oddity cannot pass as fact


class ToolError(Exception):
    """Something about the tool or its inputs is wrong; never reported as fleet drift."""


def aws(profile: str | None, region: str, *args: str) -> dict | list:
    cmd = ["aws", *args, "--region", region, "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    done = subprocess.run(cmd, capture_output=True, text=True)
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


def parse_args(argv):
    p = argparse.ArgumentParser(description="Compare the data plane against a gateway release.")
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
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.scope == "controlplane":
        print("controlplane scope is not implemented: its expected baseline needs a cdk synth "
              "of the gateway tree (issue #521 items 12-14). Refusing rather than reporting a "
              "comparison this tool did not make.", file=sys.stderr)
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
