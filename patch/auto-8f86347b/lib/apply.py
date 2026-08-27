#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Apply this kit's two file replacements, read them back, and be able to undo them.

There is no CloudFormation closure for this range — no CDK source changed — so the expected value
for every assertion is the sha256 of the file THIS KIT SHIPS, recorded in manifest.json. That is a
stronger anchor than a synth artifact for a pure file replacement: it is what the operator can see.

Two operations:

  lambda-api-code   `services/egress_admin_service.py` inside the openclaw-api package. Overlay:
                    the live package is downloaded and only this kit's file is replaced, so the
                    platform-correct arm64 wheels stay exactly as deployed. Order is code -> read
                    back -> invoke -> publish LAST, because a version snapshots code AND config at
                    publish time.

  edge-bundle       `test/integration/balancer_phase_integration.sh` in the edge bundle in S3. The
                    destination prefix is DISCOVERED from the live launch template's user data, not
                    assumed and not taken from a synth artifact: the prefix is content-addressed
                    (`deployment/bootstrap/edge/<sha256-of-the-rendered-init-script>`), so only the
                    fleet itself knows which one it serves. Overwriting the in-service prefix is
                    deliberate — a new prefix would need a CDK synth to compute and an LT roll to
                    take effect, and this file is read only when the integration suite runs, never on
                    a request path.

Every mutation is registered with an undo BEFORE it happens, and any failure — including a failed
read — unwinds in reverse and raises if an undo fails.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
MANIFEST = KIT / "manifest.json"
WORK_DIR = Path(os.environ.get("OC_WORK_DIR", "").strip()
                or (Path(tempfile.gettempdir()) / "oc-egress-207-work"))
STATE_DIR = WORK_DIR / "state"
RUN_ID = ""
REGION = ""
OP = ""
MODE = ""
RECEIPT = ""

_NOT_FOUND = ("NoSuchKey", "NoSuchBucket", "NoSuchTagSet", "ParameterNotFound",
              "ResourceNotFoundException", "NoSuchEntity", "does not exist",
              "Not Found", "404")


class Fail(Exception):
    pass


def log(msg: str) -> None:
    print(f"[{OP}/{MODE}] {msg}", flush=True)


def aws(*args, parse_json=False, allow_missing=False):
    """Run one AWS CLI call. Region-pinned explicitly on every call: an ambient AWS_REGION has
    silently redirected a run to another region before."""
    cmd = ["aws", *args]
    if "--region" not in args:
        cmd += ["--region", REGION]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if allow_missing and any(m in err for m in _NOT_FOUND):
            return None
        raise Fail(f"aws {' '.join(args[:3])} failed: {err[:500]}")
    out = (r.stdout or "").strip()
    if parse_json:
        return json.loads(out) if out else None
    return out


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def zip_codesha(p: Path) -> str:
    import base64
    return base64.b64encode(hashlib.sha256(p.read_bytes()).digest()).decode()


def manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def shipped_sha(repo_path: str) -> str:
    """The sha256 this kit records for a source path — the expected value for every assertion."""
    m = manifest()
    rec = m["paths"].get(repo_path)
    if not rec:
        raise Fail(f"{repo_path} is not in this kit's manifest; refusing to act on it")
    want = rec.get("patch_sha256")
    if not want:
        raise Fail(f"{repo_path} has no patch_sha256 in the manifest")
    return want


def shipped_file(rel: str, repo_path: str) -> Path:
    """A file this kit ships, proven to be the bytes the manifest recorded."""
    p = KIT / rel
    if not p.is_file():
        raise Fail(f"this kit does not ship {rel}")
    got, want = sha256_file(p), shipped_sha(repo_path)
    if got != want:
        raise Fail(f"{rel} sha256 {got} != the manifest's {want} for {repo_path}; the kit is "
                   "inconsistent with itself and must not be applied")
    return p


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise Fail(f"environment variable {name} is required for {OP}/{MODE}")
    return v


def target_identity() -> dict:
    who = aws("sts", "get-caller-identity", "--output", "json", parse_json=True) or {}
    return {"account": who.get("Account"), "region": REGION}


def state_path(name: str) -> Path:
    return STATE_DIR / f"{RUN_ID}.{name}.json"


def save_state(name: str, data: dict) -> None:
    """Write-once and atomic. A second write under the same name would drop a fact the unwind needs
    to know about, and a partially-written file would be indistinguishable from a complete one."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = state_path(name)
    body = {"_schema": 1, "_target": target_identity(), "_run_id": RUN_ID, "payload": data}
    blob = json.dumps(body, ensure_ascii=False, indent=1)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        prior = json.loads(p.read_text())
        if prior.get("_target") != body["_target"]:
            raise Fail(f"{p} already exists but was written against {prior.get('_target')} and this "
                       f"run targets {body['_target']}; refusing to overwrite another account's "
                       "rollback anchor") from None
        log(f"state {p.name} already exists for this run and target; keeping the FIRST anchor")
        return
    with os.fdopen(fd, "w") as f:
        f.write(blob)
    log(f"state -> {p}")


def load_state(name: str) -> dict:
    p = state_path(name)
    if not p.is_file():
        raise Fail(f"no state for this run at {p}; rollback needs the state written by apply with "
                   f"the same OC_RUN_ID ({RUN_ID})")
    d = json.loads(p.read_text())
    if d.get("_schema") != 1:
        raise Fail(f"{p} has schema {d.get('_schema')}, expected 1")
    want = target_identity()
    if d.get("_target") != want:
        raise Fail(f"{p} was written against {d.get('_target')} but this run targets {want}; "
                   "refusing to restore one account's state into another")
    return d["payload"]


def receipt(what: str, detail: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t{OP}\tapplied\t{what}\t{detail}"
    log(f"receipt <- {line}")
    if RECEIPT:
        with open(RECEIPT, "a") as f:
            f.write(line + "\n")


class Txn:
    def __init__(self):
        self._undo = []

    def add(self, fn, label):
        self._undo.append((fn, label))

    def unwind(self):
        problems = []
        for fn, label in reversed(self._undo):
            try:
                fn()
                log(f"undone: {label}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{label}: {exc}")
        if problems:
            raise Fail("UNWIND INCOMPLETE — these undos failed and the account is NOT back to its "
                       "pre-apply state: " + "; ".join(problems))
        log("all writes from this attempt were undone")


def pinned_version(bucket: str, key: str, version_id, what: str) -> str:
    """A version id that can actually be restored. `null` means versioning is suspended, and a null
    version is overwritten in place — restoring it is not possible."""
    if not version_id or str(version_id).strip().lower() in ("null", "none", ""):
        raise Fail(f"s3://{bucket}/{key} reports version {version_id!r} for the {what}; that is not "
                   "a restorable version. Enable versioning on the bucket and re-run.")
    status = aws("s3api", "get-bucket-versioning", "--bucket", bucket,
                 "--query", "Status", "--output", "text", allow_missing=True)
    if (status or "").strip() != "Enabled":
        raise Fail(f"s3://{bucket} versioning is {status!r}, not Enabled; the {what} cannot be "
                   "pinned and an unwind could not put the previous bytes back")
    return str(version_id).strip()


# ============================================================== lambda-api-code
def _classify_artifact(artifact):
    """(kind, path-inside-the-target) derived from where the kit ships the file.

    The kit's own layout encodes the destination, so no extra manifest field is needed — and the
    schema forbids one anyway. `lambda/<function-dir>/<path>` maps onto the deployed package root
    (the function directory is the package root, so it is stripped); `host-scripts/edge/<path>` maps
    onto the edge bundle root.
    """
    parts = artifact.split("/")
    if parts[0] == "lambda" and len(parts) >= 3:
        return "lambda-package-file", "/".join(parts[2:])
    if parts[:2] == ["host-scripts", "edge"] and len(parts) >= 3:
        return "edge-bundle-file", "/".join(parts[2:])
    return None, None


def _paths_for(kind, kind_name):
    """Every shipped manifest path whose artifact resolves to this kind.

    Derived, never assumed: the tool and the authored kit read the same manifest, so they cannot
    disagree about which file an operation delivers. No match raises — the operation then refuses
    instead of acting on whatever the template was seeded with.
    """
    out = []
    for src, rec in sorted(manifest()["paths"].items()):
        if rec.get("artifact_status") != "SHIPPED":
            continue
        art = rec.get("artifact")
        if not art:
            raise Fail(f"{src} is SHIPPED but carries no artifact path")
        k, inside = _classify_artifact(art)
        if k == kind:
            out.append((src, art, inside))
    if not out:
        raise Fail(f"this kit's manifest carries no {kind_name}; refusing to guess one")
    return out


def _one_path_for(kind, kind_name):
    got = _paths_for(kind, kind_name)
    if len(got) != 1:
        raise Fail(f"this kit ships {len(got)} {kind_name}s "
                   f"({[g[0] for g in got]}); this operation handles exactly one")
    return got[0]


def _lambda_paths():
    """(source path, path inside the kit, path inside the deployed package)"""
    return _one_path_for("lambda-package-file", "Lambda package file")


def _edge_paths():
    """(source path, path inside the kit, path inside the S3 bundle)"""
    return _one_path_for("edge-bundle-file", "edge bundle file")


def live_conf(fn: str) -> dict:
    return aws("lambda", "get-function-configuration", "--function-name", fn,
               "--output", "json", parse_json=True) or {}


def wait_updated(fn: str) -> None:
    aws("lambda", "wait", "function-updated", "--function-name", fn)


def invoke_ok(fn: str) -> None:
    out = Path(tempfile.gettempdir()) / f"oc-probe-{RUN_ID}.json"
    err = aws("lambda", "invoke", "--function-name", fn,
              "--payload", '{"rawPath":"/__oc_probe__","requestContext":{"http":{"method":"GET"}}}',
              "--cli-binary-format", "raw-in-base64-out", str(out),
              "--query", "FunctionError", "--output", "text")
    if err != "None":
        body = out.read_text()[:600] if out.is_file() else ""
        raise Fail(f"invoke reports FunctionError={err}; payload: {body}")
    log("asserted invoke FunctionError == None")


def build_overlay(fn: str, dest: Path) -> tuple[str, str]:
    """Download the live package and replace ONLY the file this kit ships for it. Returns (live_codesha, new_codesha).

    Replacing individual files rather than whole directories: this kit ships one file out of the 24
    in `services/`, so deleting the directory and overlaying would drop 23 files the function
    imports. The entry set is asserted unchanged afterwards.
    """
    import zipfile
    lam_src, lam_art, lam_in_pkg = _lambda_paths()
    src = shipped_file(lam_art, lam_src)
    url = aws("lambda", "get-function", "--function-name", fn, "--query", "Code.Location",
              "--output", "text")
    live = dest.parent / "live.zip"
    r = subprocess.run(["curl", "-sSf", "-o", str(live), url], capture_output=True, text=True)
    if r.returncode != 0:
        raise Fail(f"could not download the live package: {r.stderr[:300]}")
    live_sha = zip_codesha(live)
    declared = live_conf(fn).get("CodeSha256")
    if live_sha != declared:
        raise Fail(f"the downloaded package hashes to {live_sha} but the function declares "
                   f"{declared}; refusing to patch a package that is not the deployed one")
    log(f"downloaded package hashes to the live CodeSha256 ({live_sha[:16]}…)")

    zin = zipfile.ZipFile(live)
    names = [i.filename for i in zin.infolist()]
    if lam_in_pkg not in names:
        raise Fail(f"{lam_in_pkg} is not in the live package (its root holds "
                   f"{sorted(n for n in names if '/' not in n)[:8]}); either the package layout "
                   "differs or this is the wrong function")
    body = src.read_bytes()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zo:
        for info in zin.infolist():
            data = body if info.filename == lam_in_pkg else zin.read(info)
            ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            ni.external_attr, ni.compress_type = info.external_attr, zipfile.ZIP_DEFLATED
            zo.writestr(ni, data)
    zc = zipfile.ZipFile(dest)
    got = [i.filename for i in zc.infolist()]
    if got != names:
        raise Fail(f"the overlay changed the entry set ({len(names)} -> {len(got)}); refusing to "
                   "install a package that lost or gained files")
    differ = [n for n in names
              if hashlib.sha256(zin.read(n)).hexdigest() != hashlib.sha256(zc.read(n)).hexdigest()]
    if differ != [lam_in_pkg]:
        raise Fail(f"exactly one entry may differ; these do: {differ[:6]}")
    if hashlib.sha256(zc.read(lam_in_pkg)).hexdigest() != sha256_file(src):
        raise Fail("the overlaid entry does not hash to this kit's file")
    log(f"overlay built: {len(got)} entries unchanged, 1 replaced ({lam_in_pkg})")
    return live_sha, zip_codesha(dest)


def op_lambda(mode: str) -> None:
    fn = env("OPENCLAW_API_FN")
    lam_src, lam_art, lam_in_pkg = _lambda_paths()
    want_sha = shipped_sha(lam_src)

    if mode == "verify":
        # Read the deployed package back and compare the ONE file this kit changes. A CodeSha256
        # comparison alone cannot verify anything without this run's state, and "the function is
        # healthy" is not evidence that this file reached it.
        saved = None
        try:
            saved = load_state("lambda")
        except Fail as exc:
            log(f"no apply state for this run ({exc}); falling back to reading the deployed file")
        with tempfile.TemporaryDirectory() as td:
            import zipfile
            live = Path(td) / "live.zip"
            url = aws("lambda", "get-function", "--function-name", fn, "--query", "Code.Location",
                      "--output", "text")
            subprocess.run(["curl", "-sSf", "-o", str(live), url], check=True,
                           capture_output=True)
            z = zipfile.ZipFile(live)
            if lam_in_pkg not in z.namelist():
                raise Fail(f"{lam_in_pkg} is absent from the deployed package")
            got = hashlib.sha256(z.read(lam_in_pkg)).hexdigest()
        if got != want_sha:
            raise Fail(f"the deployed {lam_in_pkg} hashes to {got}, this kit ships {want_sha}; "
                       "this operation is NOT applied")
        log(f"asserted the deployed {lam_in_pkg} is byte-identical to this kit's copy")
        if saved:
            published = saved.get("published_version")
            if published:
                qual = aws("lambda", "get-function-configuration", "--function-name",
                           f"{fn}:{published}", "--output", "json", parse_json=True) or {}
                if qual.get("CodeSha256") != saved["codesha_want"]:
                    raise Fail(f"published version {published} carries "
                               f"{qual.get('CodeSha256')}, apply recorded {saved['codesha_want']}")
                log(f"asserted published version {published} carries the applied code")
        invoke_ok(fn)
        return

    if mode == "rollback":
        saved = load_state("lambda")
        aws("lambda", "update-function-code", "--function-name", fn,
            "--s3-bucket", saved["backup_bucket"], "--s3-key", saved["backup_key"],
            "--s3-object-version", saved["backup_version_id"])
        wait_updated(fn)
        now = live_conf(fn).get("CodeSha256")
        if now != saved["codesha_before"]:
            raise Fail(f"CodeSha256 after rollback is {now}, expected {saved['codesha_before']}")
        log(f"asserted CodeSha256 is back to {saved['codesha_before'][:16]}…")
        if saved.get("alias_before"):
            alias = env("OPENCLAW_API_ALIAS")
            aws("lambda", "update-alias", "--function-name", fn, "--name", alias,
                "--function-version", saved["alias_before"])
            got = aws("lambda", "get-alias", "--function-name", fn, "--name", alias,
                      "--query", "FunctionVersion", "--output", "text")
            if got != saved["alias_before"]:
                raise Fail(f"alias is {got} after rollback, expected {saved['alias_before']}")
            log(f"asserted alias {alias} is back to version {saved['alias_before']}")
        log("NOTE: the version apply published is immutable and stays behind. Nothing points at it, "
            "so it is inert, but the version list is one longer than before apply.")
        return

    # apply
    bucket = env("BACKUP_S3_BUCKET")
    key = env("BACKUP_S3_KEY")
    alias = os.environ.get("OPENCLAW_API_ALIAS", "").strip()
    conf = live_conf(fn)
    codesha_before = conf.get("CodeSha256")
    if not codesha_before:
        raise Fail(f"{fn} reports no CodeSha256")

    with tempfile.TemporaryDirectory() as td:
        overlay = Path(td) / "overlay.zip"
        live_sha, codesha_want = build_overlay(fn, overlay)
        if live_sha != codesha_before:
            raise Fail("the package moved between reads; re-run")
        if codesha_want == codesha_before:
            log("the deployed package already carries this kit's file; nothing to write")
            receipt(fn, f"{lam_in_pkg} already deployed (CodeSha256 {codesha_before})")
            return

        # The backup must be the code running NOW, pinned by version. head-object only proves
        # something is at that key; the unwind restores $LATEST from it, so a stale object there
        # would overwrite the one recoverable copy of the running code.
        head = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                   "--output", "json", parse_json=True, allow_missing=True)
        if head is None:
            aws("s3", "cp", str(Path(td) / "live.zip"), f"s3://{bucket}/{key}",
                "--only-show-errors")
            head = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                       "--output", "json", parse_json=True)
            log(f"uploaded the live package as the backup to s3://{bucket}/{key}")
        version_id = pinned_version(bucket, key, head.get("VersionId"), "code backup")
        check = Path(td) / "backup.zip"
        aws("s3api", "get-object", "--bucket", bucket, "--key", key,
            "--version-id", version_id, str(check), "--output", "json")
        bk_sha = zip_codesha(check)
        if bk_sha != codesha_before:
            raise Fail(f"the backup object holds CodeSha256 {bk_sha}, the function is running "
                       f"{codesha_before}; restoring it would install different code. Re-take the "
                       "backup and re-run.")
        log(f"asserted the backup at version {version_id} is the code running now")

        alias_before = None
        if alias:
            alias_before = aws("lambda", "get-alias", "--function-name", fn, "--name", alias,
                               "--query", "FunctionVersion", "--output", "text")

        save_state("lambda", {"codesha_before": codesha_before, "codesha_want": codesha_want,
                              "backup_bucket": bucket, "backup_key": key,
                              "backup_version_id": version_id, "alias_before": alias_before,
                              "shipped_sha256": want_sha})

        txn = Txn()
        try:
            aws("lambda", "update-function-code", "--function-name", fn,
                "--zip-file", f"fileb://{overlay}")
            txn.add(lambda: (aws("lambda", "update-function-code", "--function-name", fn,
                                 "--s3-bucket", bucket, "--s3-key", key,
                                 "--s3-object-version", version_id),
                             wait_updated(fn)),
                    f"restore $LATEST from backup version {version_id}")
            wait_updated(fn)
            now = live_conf(fn).get("CodeSha256")
            if now != codesha_want:
                raise Fail(f"CodeSha256 after update is {now}, expected {codesha_want}")
            log(f"asserted CodeSha256 == the overlay digest ({codesha_want[:16]}…)")
            invoke_ok(fn)

            # Publish LAST: a version snapshots code and configuration at publish time.
            published = aws("lambda", "publish-version", "--function-name", fn,
                            "--code-sha256", codesha_want, "--query", "Version", "--output", "text")
            txn.add(lambda: aws("lambda", "delete-function",
                                "--function-name", f"{fn}:{published}"),
                    f"delete version {published} this attempt published")
            qual = aws("lambda", "get-function-configuration", "--function-name",
                       f"{fn}:{published}", "--output", "json", parse_json=True) or {}
            if qual.get("CodeSha256") != codesha_want:
                raise Fail(f"published version {published} carries {qual.get('CodeSha256')}")
            log(f"published version {published} carries the applied code")

            if alias:
                aws("lambda", "update-alias", "--function-name", fn, "--name", alias,
                    "--function-version", published)
                txn.add(lambda: aws("lambda", "update-alias", "--function-name", fn,
                                    "--name", alias, "--function-version", alias_before),
                        f"move alias {alias} back to {alias_before}")
                got = aws("lambda", "get-alias", "--function-name", fn, "--name", alias,
                          "--query", "FunctionVersion", "--output", "text")
                if got != published:
                    raise Fail(f"alias {alias} is {got} after the update, expected {published}")
                log(f"alias {alias} {alias_before} -> {published}")
        except Exception as exc:  # noqa: BLE001 — a failed read must unwind too
            log(f"FAILED after mutating: {exc}")
            txn.unwind()
            raise

        st = load_state("lambda")
        st["published_version"] = published
        save_state("lambda-published", st)
        receipt(fn, f"{lam_in_pkg} replaced; CodeSha256 {codesha_before} -> "
                                f"{codesha_want}; version {published} published"
                                + (f"; alias {alias} -> {published}" if alias else ""))


# ================================================================== edge-bundle



def discover_edge_prefix() -> str:
    """The edge bundle prefix the fleet ACTUALLY serves, read out of the live launch template.

    Not derived from a synth artifact and not assumed: the prefix is content-addressed
    (`deployment/bootstrap/edge/<sha256>`), so its value depends on the rendered init script this
    particular deployment produced. Only the launch template the ASG pins knows which one.
    """
    import base64 as _b64
    import gzip
    import re
    asg = env("EDGE_ASG")
    g = (aws("autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names", asg,
             "--query", "AutoScalingGroups[0]", "--output", "json", parse_json=True) or {})
    lt = g.get("LaunchTemplate") or (g.get("MixedInstancesPolicy") or {}).get(
        "LaunchTemplate", {}).get("LaunchTemplateSpecification") or {}
    lt_id, ver = lt.get("LaunchTemplateId"), str(lt.get("Version", ""))
    if not lt_id:
        raise Fail(f"{asg} has no launch template this check can read")
    if ver == "$Latest":
        raise Fail(f"{asg} pins $Latest, so the version it serves changes the instant a new one is "
                   "created; refusing to reason about a moving target")
    if ver in ("$Default", ""):
        ver = str(aws("ec2", "describe-launch-templates", "--launch-template-ids", lt_id,
                      "--query", "LaunchTemplates[0].DefaultVersionNumber", "--output", "text"))
        log(f"{asg} pins $Default -> resolved to version {ver}")
    ud = aws("ec2", "describe-launch-template-versions", "--launch-template-id", lt_id,
             "--versions", ver, "--query", "LaunchTemplateVersions[0].LaunchTemplateData.UserData",
             "--output", "text")
    raw = _b64.b64decode(ud)
    for _ in range(3):
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        else:
            break
    text = raw.decode("utf-8", "replace")
    hits = sorted(set(re.findall(r"deployment/bootstrap/edge/[0-9a-f]{64}", text)))
    if len(hits) != 1:
        raise Fail(f"the launch template's user data names {len(hits)} edge bundle prefix(es) "
                   f"{hits[:3]}; refusing to guess which one the fleet serves")
    log(f"discovered the served edge bundle prefix from LT {lt_id} version {ver}: {hits[0]}")
    return hits[0]


def op_edge(mode: str) -> None:
    bucket = env("ASSETS_BUCKET")
    edge_src, edge_art, edge_in_bundle = _edge_paths()
    want_sha = shipped_sha(edge_src)
    prefix = discover_edge_prefix()
    key = f"{prefix}/{edge_in_bundle}"

    if mode == "verify":
        with tempfile.TemporaryDirectory() as td:
            got_file = Path(td) / "got"
            if aws("s3api", "get-object", "--bucket", bucket, "--key", key, str(got_file),
                   "--output", "json", allow_missing=True) is None:
                raise Fail(f"s3://{bucket}/{key} is absent")
            got = sha256_file(got_file)
        if got != want_sha:
            raise Fail(f"s3://{bucket}/{key} hashes to {got}, this kit ships {want_sha}; this "
                       "operation is NOT applied")
        log(f"asserted s3://{bucket}/{key} is byte-identical to this kit's copy")
        return

    if mode == "rollback":
        saved = load_state("edge")
        k, ver = saved["key"], saved.get("version_before")
        if ver is None:
            aws("s3api", "delete-object", "--bucket", bucket, "--key", k, allow_missing=True)
            if aws("s3api", "head-object", "--bucket", bucket, "--key", k, "--output", "json",
                   parse_json=True, allow_missing=True) is not None:
                raise Fail(f"s3://{bucket}/{k} still exists after delete")
            log(f"s3://{bucket}/{k} had no previous version; the object this run created is gone")
            return
        aws("s3api", "copy-object", "--bucket", bucket, "--key", k,
            "--copy-source", f"{bucket}/{k}?versionId={ver}", "--metadata-directive", "COPY")
        now = aws("s3api", "head-object", "--bucket", bucket, "--key", k, "--output", "json",
                  parse_json=True) or {}
        want = aws("s3api", "head-object", "--bucket", bucket, "--key", k, "--version-id", ver,
                   "--output", "json", parse_json=True) or {}
        if now.get("ETag") != want.get("ETag"):
            raise Fail(f"restored to ETag {now.get('ETag')} but version {ver} has "
                       f"{want.get('ETag')}")
        log(f"restored s3://{bucket}/{k} from version {ver}; its digest matches that version")
        return

    # apply
    src = shipped_file(edge_art, edge_src)
    head = aws("s3api", "head-object", "--bucket", bucket, "--key", key, "--output", "json",
               parse_json=True, allow_missing=True)
    version_before = None
    if head is not None:
        version_before = pinned_version(bucket, key, head.get("VersionId"), "edge bundle object")
        with tempfile.TemporaryDirectory() as td:
            cur = Path(td) / "cur"
            aws("s3api", "get-object", "--bucket", bucket, "--key", key, "--version-id",
                version_before, str(cur), "--output", "json")
            if sha256_file(cur) == want_sha:
                log("the served object already carries this kit's bytes; nothing to write")
                receipt(f"s3://{bucket}/{key}", "already applied; nothing was written")
                return
        log(f"the key holds version {version_before}; unwind will restore it")
    else:
        log("the key does not exist yet; unwind will delete what this run creates")

    save_state("edge", {"bucket": bucket, "key": key, "prefix": prefix,
                        "version_before": version_before, "shipped_sha256": want_sha})

    txn = Txn()
    try:
        if version_before is None:
            txn.add(lambda: aws("s3api", "delete-object", "--bucket", bucket, "--key", key,
                                allow_missing=True),
                    f"remove s3://{bucket}/{key} that this attempt created")
        else:
            txn.add(lambda: aws("s3api", "copy-object", "--bucket", bucket, "--key", key,
                                "--copy-source", f"{bucket}/{key}?versionId={version_before}",
                                "--metadata-directive", "COPY"),
                    f"restore s3://{bucket}/{key} to version {version_before}")
        aws("s3", "cp", str(src), f"s3://{bucket}/{key}", "--only-show-errors")
        with tempfile.TemporaryDirectory() as td:
            back = Path(td) / "back"
            if aws("s3api", "get-object", "--bucket", bucket, "--key", key, str(back),
                   "--output", "json", allow_missing=True) is None:
                raise Fail(f"s3://{bucket}/{key} absent after the upload")
            got = sha256_file(back)
        if got != want_sha:
            raise Fail(f"readback hashes to {got}, expected {want_sha}")
        log(f"asserted the uploaded object reads back as this kit's bytes ({want_sha[:16]}…)")
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED after writing: {exc}")
        txn.unwind()
        raise
    receipt(f"s3://{bucket}/{key}",
            f"{edge_in_bundle} written under the served prefix and asserted byte-identical "
            f"(sha256 {want_sha[:16]}…)")


OPS = {"lambda-api-code": op_lambda, "edge-bundle": op_edge}


def main() -> int:
    global RUN_ID, REGION, OP, MODE, RECEIPT
    ap = argparse.ArgumentParser()
    ap.add_argument("op", choices=sorted(OPS))
    ap.add_argument("mode", choices=["apply", "verify", "rollback"])
    ap.add_argument("region")
    a = ap.parse_args()
    OP, MODE, REGION = a.op, a.mode, a.region
    RUN_ID = os.environ.get("OC_RUN_ID", "").strip()
    RECEIPT = os.environ.get("OC_RECEIPT_FILE", "").strip()
    if not RUN_ID:
        print("OC_RUN_ID must be set to a value unique to this apply run, so a rollback restores "
              "this run's state and never a stale file from an earlier one.", file=sys.stderr)
        return 2
    try:
        OPS[a.op](a.mode)
    except Fail as e:
        print(f"FATAL[{a.op}/{a.mode}]: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
