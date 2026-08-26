#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-resource apply / verify / rollback for the changes this kit computed.

Design rules, each one earned from a blocking review:

* The EXPECTED value always comes out of the captured closure, never from the live
  resource. A check that reads the live value and accepts it proves nothing.
* apply = gate -> mutate -> read the live resource back -> assert it equals the
  expected value -> only then write a receipt line. A failed assertion triggers the
  operation's own rollback before it exits non-zero, so a half-applied state is not
  left behind.
* verify re-reads the live resource and compares against the same expected value. It
  never reads the receipt file, so the receipt is evidence, not an assertion.
* Every resource an operation claims (`--logical-ids`) must be individually asserted.
  An operation may not write a receipt for a resource it did not touch.
* Physical names come from the closure too. A name prefix is never used to select what
  to mutate: a prefix that matches nothing makes a destructive command succeed while
  doing nothing.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ORCH = "OpenClawOrchestrator"
IMG = "OpenClawImage"
# Never the current directory: these operations are run FROM the kit, and writing state or plan
# artifacts there makes the kit fail its own validator (measured on the 2026-08-26 live run) and
# changes a tree whose hashes another operator is expected to re-verify.
# Override with OC_WORK_DIR to keep the artifacts somewhere you control.
WORK_DIR = Path(os.environ.get("OC_WORK_DIR", "").strip()
                or (Path(tempfile.gettempdir()) / "oc-patch-work"))
STATE_DIR = WORK_DIR / "state"


class Fail(Exception):
    pass


class Txn:
    """Undo stack for a single apply.

    Every mutation registers its own undo immediately after it succeeds, so an exception
    anywhere later — including a read that fails — unwinds in reverse order. Without this each
    op had to remember to restore by hand, and the ones that read something after two writes
    could leave a half-applied resource behind.
    """

    def __init__(self) -> None:
        self._undo: list[tuple] = []
        self.wrote = False

    def add(self, fn, desc: str) -> None:
        self._undo.append((fn, desc))
        self.wrote = True

    def unwind(self) -> None:
        if not self._undo:
            log("nothing was written, so there is nothing to unwind")
            return
        failed = []
        for fn, desc in reversed(self._undo):
            try:
                fn()
                log(f"undone: {desc}")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{desc}: {exc}")
                log(f"UNDO FAILED — {desc}: {exc}")
        self._undo.clear()
        if failed:
            raise Fail("automatic restore did not complete: " + "; ".join(failed))
        log("all writes from this attempt were undone")


def log(msg: str) -> None:
    print(f"[{OP}/{MODE}] {msg}", flush=True)


# Only these mean "the thing is not there". Anything else — AccessDenied, Throttling, a network
# error, an expired token — is a READ FAILURE, and treating it as absence is how an operation
# overwrites or deletes state it never actually looked at. This is the single most load-bearing
# predicate in this file.
_NOT_FOUND_MARKERS = (
    "NoSuchKey", "NoSuchBucket", "NoSuchTagSet", "NoSuchTagSetError",
    "ParameterNotFound", "ResourceNotFoundException", "ResourceNotFound",
    "NoSuchEntity", "ValidationError: Alarm", "does not exist", "Not Found",
    "404", "AlarmNotFound", "NoSuchConfiguration",
)


def _is_not_found(stderr: str) -> bool:
    return any(m.lower() in stderr.lower() for m in _NOT_FOUND_MARKERS)


def aws(*args: str, parse_json: bool = False, allow_fail: bool = False,
        allow_missing: bool = False):
    """Run one AWS CLI call.

    allow_missing=True  -> return None ONLY when the error is an explicit not-found; any other
                           failure still raises. Use this for "does it exist?" reads.
    allow_fail=True     -> return None on ANY failure. Reserved for best-effort cleanup inside an
                           unwind, where the operation is already failing and a second error must
                           not mask the first. Never use it to decide whether to write.
    """
    cmd = ["aws", "--region", REGION, *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if allow_missing and _is_not_found(err):
            return None
        if allow_fail:
            log(f"IGNORED (best-effort): aws {' '.join(args[:3])}: {err[:200]}")
            return None
        raise Fail(f"aws {' '.join(args[:3])} failed: {err[:400]}")
    out = r.stdout.strip()
    if not parse_json:
        return out
    return json.loads(out) if out else None


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise Fail(f"environment variable {name} is required for {OP}/{MODE}")
    return v


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------ closure access
def template(stack: str, side: str) -> dict:
    f = CLOSURE / f"{stack}.{side}.json"
    if not f.is_file():
        raise Fail(f"closure template missing: {f}")
    return json.loads(f.read_text()).get("Resources", {})


def expected_props(stack: str, logical_id: str) -> dict:
    r = template(stack, "patch").get(logical_id)
    if r is None:
        raise Fail(f"{logical_id} absent from the patch closure of {stack}")
    return r["Properties"]


def base_props(stack: str, logical_id: str) -> dict:
    r = template(stack, "base").get(logical_id)
    if r is None:
        raise Fail(f"{logical_id} absent from the base closure of {stack}")
    return r["Properties"]


# ------------------------------------------------------------------------ receipt
def receipt(logical_id: str, evidence: str) -> None:
    """Only ever called after this run WROTE the resource and read it back. A resource that was
    already converged gets `noop()` instead, which writes no receipt line at all."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t{OP}\tapplied\t{logical_id}\t{evidence}\n"
    Path(RECEIPT).open("a", encoding="utf-8").write(line)
    log(f"receipt <- applied {logical_id}  {evidence}")


def noop(logical_id: str, why: str) -> None:
    """No write happened, so no receipt. Say so loudly instead of implying an apply."""
    log(f"NO RECEIPT for {logical_id}: {why} (nothing was written, so nothing is claimed)")


def pinned_version(bucket: str, key: str, version_id, what: str) -> str:
    """Return a version id that can actually be restored, or fail closed.

    A versioning-SUSPENDED bucket answers head-object with the literal string "null", and a null
    version is overwritten IN PLACE — so accepting it means the "pinned" backup is destroyed by the
    very write it was supposed to protect. Also confirm the bucket says Enabled: Suspended can hold
    old versions while making every NEW write unrecoverable.
    """
    if not version_id or str(version_id).strip().lower() in ("null", "none", ""):
        raise Fail(
            f"{what}: s3://{bucket}/{key} reports version id {version_id!r}, which is what an "
            "unversioned or versioning-SUSPENDED bucket returns. A null version is overwritten in "
            "place, so it cannot serve as a rollback anchor. Enable bucket versioning and re-take "
            "the object before running this operation."
        )
    status = aws("s3api", "get-bucket-versioning", "--bucket", bucket,
                 "--query", "Status", "--output", "text", allow_missing=True)
    if (status or "").strip() != "Enabled":
        raise Fail(
            f"{what}: bucket {bucket} versioning status is {status!r}, not Enabled. Only Enabled "
            "guarantees the version this operation pins survives the write it protects."
        )
    return str(version_id)


def state_path(name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{RUN_ID}.{name}.json"


def _target_identity() -> dict:
    """Account + region this run is pointed at. Cheap, and it is the only thing that makes a state
    file safe to read back: a run id reused against another account would otherwise restore one
    deployment's recorded values into a different one."""
    ident = aws("sts", "get-caller-identity", "--output", "json", parse_json=True) or {}
    return {"account": ident.get("Account"), "region": REGION}


def save_state(name: str, data) -> Path:
    """Write this run's pre-change anchor ONCE, atomically, bound to the target identity.

    A second apply in the same run must not overwrite it: the second read of the live resource sees
    what the FIRST apply produced, so overwriting turns the rollback anchor into the converged value
    and `rollback` then restores the change instead of undoing it.

    The payload is nested under "payload" so metadata can never be mistaken for a payload key — the
    previous flat shape put `_target` alongside real keys and one rollback iterated straight over it.
    Creation is O_EXCL, so two concurrent invocations cannot both believe they wrote the anchor.
    """
    p = state_path(name)
    target = _target_identity()
    body = {"_schema": 2, "_target": target, "_run_id": RUN_ID, "payload": data}
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise Fail(f"the state file {p} exists but is unreadable ({exc}); refusing to touch it, "
                       "because it is the only rollback anchor for this run") from exc
        prior = existing.get("_target") if isinstance(existing, dict) else None
        if prior and prior != target:
            # Fail closed. Keeping the old anchor and carrying on would apply changes here while the
            # only rollback anchor describes a DIFFERENT account or region.
            raise Fail(
                f"{p} already exists and was written against account {prior.get('account')} in "
                f"{prior.get('region')}, but this invocation targets {target.get('account')} in "
                f"{target.get('region')}. Reusing a run id across environments would leave this one "
                "unrollbackable. Use a run id unique to this environment."
            )
        if existing.get("payload") == data:
            log(f"state unchanged -> {p}")
        else:
            log(f"state ALREADY EXISTS for this run -> {p}; keeping the FIRST anchor. A retry must "
                "roll back to the state before the first attempt, not to what that attempt left.")
        return p
    with os.fdopen(fd, "w") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=1)
    log(f"state -> {p}")
    return p


def load_state(name: str):
    """Validate the metadata, then return ONLY the payload.

    Returning the metadata alongside the payload is how `_target` ended up being iterated as an S3
    key by a rollback. Callers never see it.
    """
    p = state_path(name)
    if not p.is_file():
        raise Fail(f"no state for this run at {p}; rollback needs the state written by apply "
                   f"with the same OC_RUN_ID ({RUN_ID})")
    data = json.loads(p.read_text())
    if not isinstance(data, dict) or "payload" not in data or "_target" not in data:
        # Fail closed, not a warning: a state file with no identity cannot be proven to belong to
        # this account, and restoring it could apply one deployment's values to another.
        raise Fail(
            f"{p} is not a schema-2 state file (no identity-bound payload). It was written by an "
            "older version of this tool, so it cannot be proven to belong to this account and "
            "region. Re-run apply with a fresh OC_RUN_ID, or restore by hand after reading it."
        )
    now = _target_identity()
    if data["_target"] != now:
        raise Fail(
            f"{p} was written against account {data['_target'].get('account')} in "
            f"{data['_target'].get('region')}, but this invocation is pointed at "
            f"{now.get('account')} in {now.get('region')}. Restoring it here would apply one "
            "deployment's recorded values to a different one. Use the run id that belongs to THIS "
            "environment."
        )
    return data["payload"]


def assert_eq(got, want, what: str) -> None:
    if got != want:
        raise Fail(f"{what}: live value {got!r} != expected {want!r}")
    log(f"asserted {what} == {want!r}")


# ============================================================ iam-edge-putmetricdata
def _iam_decision() -> str:
    out = aws(
        "iam", "simulate-principal-policy",
        "--policy-source-arn", env("EDGE_ROLE_ARN"),
        "--action-names", "cloudwatch:PutMetricData",
        "--resource-arns", "*",
        "--context-entries",
        "ContextKeyName=cloudwatch:namespace,ContextKeyType=string,ContextKeyValues=OpenClaw/Edge",
        "--query", "EvaluationResults[0].EvalDecision", "--output", "text",
    )
    return out or "unknown"


def _iam_expected_statement() -> dict:
    """The statement the closure adds to EdgeRoleDefaultPolicy."""
    b = base_props(ORCH, "EdgeRoleDefaultPolicy3148FA4F")["PolicyDocument"]["Statement"]
    p = expected_props(ORCH, "EdgeRoleDefaultPolicy3148FA4F")["PolicyDocument"]["Statement"]
    added = [s for s in p if s not in b]
    if len(added) != 1:
        raise Fail(f"expected exactly one added statement, closure has {len(added)}")
    return added[0]


def op_iam(mode: str) -> None:
    want = _iam_expected_statement()
    if want.get("Action") != "cloudwatch:PutMetricData":
        raise Fail(f"closure's added statement is not PutMetricData: {want}")
    want_ns = want["Condition"]["StringEquals"]["cloudwatch:namespace"]

    if mode in ("verify", "gate"):
        d = _iam_decision()
        log(f"effective decision = {d} (namespace condition expected: {want_ns})")
        if d != "allowed":
            raise Fail(
                f"EdgeRole cannot PutMetricData in {want_ns} (decision={d}). "
                "Run 'iam-edge-putmetricdata apply' first: without it the metric is permanently "
                "absent and the alarm can never fire, which looks exactly like an unapplied kit."
            )
        return

    if mode == "rollback":
        log("RETAIN: this grant is deliberately never rolled back. Removing it re-blinds fleet "
            "convergence, so no action is taken and this exits 0.")
        return

    # apply
    doc = Path("iam/edge-putmetricdata.json")
    if not doc.is_file():
        raise Fail("iam/edge-putmetricdata.json not found")
    shipped = json.loads(doc.read_text())["Statement"][0]
    for k in ("Action", "Resource"):
        got = shipped[k] if isinstance(shipped[k], str) else shipped[k][0]
        exp = want[k] if isinstance(want[k], str) else want[k][0]
        assert_eq(got, exp, f"shipped policy {k} matches the closure")
    assert_eq(shipped["Condition"]["StringEquals"]["cloudwatch:namespace"], want_ns,
              "shipped policy namespace condition matches the closure")

    if _iam_decision() == "allowed":
        noop("EdgeRoleDefaultPolicy3148FA4F",
             "an equivalent grant already evaluates to allowed, so nothing was written")
        return

    txn = Txn()
    role = env("EDGE_ROLE_NAME")
    policy_name = "openclaw-edge-putmetricdata"
    # put-role-policy OVERWRITES a policy of the same name. An unconditional delete on unwind would
    # then destroy a document that was there before this run. Read it first, and distinguish
    # "absent" from "could not read" — allow_missing raises on AccessDenied instead of pretending
    # the policy does not exist.
    prior = aws("iam", "get-role-policy", "--role-name", role, "--policy-name", policy_name,
                "--output", "json", parse_json=True, allow_missing=True)
    prior_doc = (prior or {}).get("PolicyDocument")
    if prior_doc is not None:
        log(f"an inline policy named {policy_name} already exists on {role}; unwind will RESTORE "
            "its document rather than delete the policy")

    def _undo_iam():
        if prior_doc is None:
            aws("iam", "delete-role-policy", "--role-name", role, "--policy-name", policy_name)
            return
        aws("iam", "put-role-policy", "--role-name", role, "--policy-name", policy_name,
            "--policy-document", json.dumps(prior_doc))
        got = aws("iam", "get-role-policy", "--role-name", role, "--policy-name", policy_name,
                  "--output", "json", parse_json=True)
        assert_eq(json.dumps((got or {}).get("PolicyDocument"), sort_keys=True),
                  json.dumps(prior_doc, sort_keys=True),
                  f"{policy_name} document after restore")

    try:
        aws("iam", "put-role-policy", "--role-name", role,
            "--policy-name", policy_name,
            "--policy-document", f"file://{doc}")
        txn.add(_undo_iam,
                "restore the previous inline policy document" if prior_doc is not None
                else "delete the inline policy this attempt created")
        d = _iam_decision()
        if d != "allowed":
            raise Fail(f"readback says decision={d} after applying; the grant did not take effect")
    except Fail:
        txn.unwind()
        raise
    receipt("EdgeRoleDefaultPolicy3148FA4F",
            f"inline policy written and simulate-principal-policy=allowed for namespace {want_ns}")


# ================================================================ lambda-api-code
DEADLINE_PREFIX = "LIFECYCLE_DEADLINE_SEC_"


def _expected_deadline_env() -> dict:
    e = expected_props(ORCH, "ApiHandler5E7490E8")["Environment"]["Variables"]
    want = {k: v for k, v in e.items() if k.startswith(DEADLINE_PREFIX)}
    if len(want) != 8:
        raise Fail(f"closure carries {len(want)} {DEADLINE_PREFIX}* keys, expected 8")
    for k, v in want.items():
        if not (str(v).isdigit() and int(v) > 0):
            raise Fail(f"closure value for {k} is not a positive integer: {v!r}")
    return want


def _live_conf() -> dict:
    return aws("lambda", "get-function-configuration", "--function-name", env("OPENCLAW_API_FN"),
               "--output", "json", parse_json=True)


def _zip_codesha(zip_path: Path) -> str:
    import base64
    return base64.b64encode(hashlib.sha256(zip_path.read_bytes()).digest()).decode()


def _assert_overlay_carries_shipped_sources(zip_path: Path) -> None:
    """The overlay zip is built by the operator, so it is not trustworthy on its own. Assert that
    every first-party file this kit ships under lambda/ is present in the zip with the SAME bytes.
    Without this the operation would happily install an arbitrary zip and then assert only that
    its own digest matched itself."""
    import zipfile
    root = Path("lambda")
    shipped = {str(f.relative_to(root)): sha256_file(f) for f in sorted(root.rglob("*")) if f.is_file()}
    if not shipped:
        raise Fail("the kit ships no lambda/ sources; refusing to install an unverified zip")
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        missing, differing = [], []
        for rel, want in shipped.items():
            if rel not in names:
                missing.append(rel)
                continue
            if hashlib.sha256(z.read(rel)).hexdigest() != want:
                differing.append(rel)
    if missing or differing:
        raise Fail(f"overlay zip does not carry this kit's sources: missing={missing[:5]} "
                   f"differing={differing[:5]} (of {len(shipped)} shipped files)")
    log(f"asserted the overlay zip carries all {len(shipped)} shipped lambda/ files byte-for-byte")


def _wait_updated() -> None:
    aws("lambda", "wait", "function-updated", "--function-name", env("OPENCLAW_API_FN"))


def _invoke_ok() -> None:
    err = aws("lambda", "invoke", "--function-name", env("OPENCLAW_API_FN"),
              "--payload", '{"rawPath":"/__oc_probe__","requestContext":{"http":{"method":"GET"}}}',
              "--cli-binary-format", "raw-in-base64-out", "/tmp/oc-probe.json",
              "--query", "FunctionError", "--output", "text")
    if err != "None":
        detail = Path("/tmp/oc-probe.json").read_text()[:600] if Path("/tmp/oc-probe.json").is_file() else ""
        raise Fail(f"invoke reports FunctionError={err}; payload: {detail}")
    log("asserted invoke FunctionError == None")


def _restore_code_and_env(saved: dict) -> None:
    log("restoring code and environment from this run's saved state")
    b, k = saved.get("backup_bucket"), saved.get("backup_key")
    ver = saved.get("backup_version_id")
    fn = env("OPENCLAW_API_FN")
    if b and k:
        if not ver:
            raise Fail(f"this run's state has no backup_version_id, so s3://{b}/{k} cannot be "
                       "pinned. Restoring from the mutable key could install whatever is there now "
                       "instead of the code that was running. Restore by hand from the version id "
                       "recorded when the backup was taken.")
        # No --publish: a restore is not a release, and publishing here created an extra immutable
        # version on every failure path.
        aws("lambda", "update-function-code", "--function-name", fn,
            "--s3-bucket", b, "--s3-key", k, "--s3-object-version", ver)
        _wait_updated()
        if saved.get("codesha_before"):
            assert_eq(_live_conf().get("CodeSha256"), saved["codesha_before"],
                      "CodeSha256 after restoring from the pinned backup version")
    else:
        log("WARNING: no S3 backup recorded, so the code cannot be restored automatically; "
            "the environment is still restored below")
    aws("lambda", "update-function-configuration", "--function-name", fn,
        "--environment", json.dumps({"Variables": saved["env_before"]}))
    _wait_updated()
    live = (_live_conf().get("Environment") or {}).get("Variables") or {}
    for name, want in sorted(saved["env_before"].items()):
        assert_eq(live.get(name), want, f"env {name} after restore")


def op_lambda_code(mode: str) -> None:
    want_env = _expected_deadline_env()
    fn = env("OPENCLAW_API_FN")

    if mode == "verify":
        conf = _live_conf()
        live = conf.get("Environment", {}).get("Variables", {})
        for k, v in sorted(want_env.items()):
            assert_eq(live.get(k), v, f"live env {k}")
        # Without this run's state there is no trusted expectation for the code digest or the
        # published version, so "the env is right and invoke is clean" is not a verification of this
        # operation — it is a green light for a function that may still be on the old code.
        try:
            saved = load_state("lambda-code")
        except Fail as exc:
            raise Fail(
                f"no apply state for run {RUN_ID}, so this check has no trusted expected digest and "
                "cannot verify that THIS operation was applied. The environment and a clean invoke "
                f"were confirmed, which is not the same thing. ({exc})"
            ) from exc
        published = load_state("lambda-published").get("published_version")
        if not published:
            raise Fail(f"run {RUN_ID} recorded no published version; apply did not complete")
        assert_eq(conf.get("CodeSha256"), saved["codesha_want"], "live CodeSha256")
        # Read the QUALIFIED version, not just its number: the alias points at that version, so its
        # code AND environment are what actually serves traffic.
        qconf = aws("lambda", "get-function-configuration", "--function-name",
                    f"{env('OPENCLAW_API_FN')}:{published}", "--output", "json",
                    parse_json=True) or {}
        assert_eq(qconf.get("CodeSha256"), saved["codesha_want"],
                  f"CodeSha256 of published version {published}")
        qenv = (qconf.get("Environment") or {}).get("Variables") or {}
        for k, v in sorted(want_env.items()):
            assert_eq(qenv.get(k), v, f"env {k} inside published version {published}")
        if True:
            ver = published
            if ver:
                log(f"apply published version {ver}")
        _invoke_ok()
        return

    if mode == "rollback":
        _restore_code_and_env(load_state("lambda-code"))
        conf = _live_conf()
        saved = load_state("lambda-code")
        assert_eq(conf.get("CodeSha256"), saved["codesha_before"], "CodeSha256 after rollback")
        for k, v in saved["env_before"].items():
            assert_eq(conf.get("Environment", {}).get("Variables", {}).get(k), v,
                      f"env {k} after rollback")
        log("NOTE: the dispatch event-source mapping binds $LATEST, which this restored. "
            "Roll the alias back too with 'lambda-api-alias rollback'.")
        return

    # apply
    zip_path = Path(env("OVERLAY_ZIP"))
    if not zip_path.is_file():
        raise Fail(f"overlay zip {zip_path} not found")
    _assert_overlay_carries_shipped_sources(zip_path)
    codesha_want = _zip_codesha(zip_path)
    conf = _live_conf()
    before = {
        "codesha_before": conf.get("CodeSha256"),
        "codesha_want": codesha_want,
        "env_before": conf.get("Environment", {}).get("Variables", {}),
        "backup_bucket": os.environ.get("BACKUP_S3_BUCKET", "").strip() or None,
        "backup_key": os.environ.get("BACKUP_S3_KEY", "").strip() or None,
    }
    if not (before["backup_bucket"] and before["backup_key"]):
        raise Fail(
            "BACKUP_S3_BUCKET and BACKUP_S3_KEY are required: without them the code half of the "
            "rollback cannot run. APPLY step 1 downloads the live package and uploads it to that "
            "location before this operation is allowed to mutate anything."
        )
    # head-object only proves SOMETHING is at that key. The unwind restores $LATEST from it, so a
    # stale or wrong object would be discovered only after it had overwritten the one recoverable
    # copy of the running code. Download it and prove its zip hashes to the CURRENT CodeSha256.
    _bk_head = aws("s3api", "head-object", "--bucket", before["backup_bucket"],
                   "--key", before["backup_key"], "--output", "json", parse_json=True,
                   allow_missing=True)
    if _bk_head is None:
        raise Fail(f"backup object s3://{before['backup_bucket']}/{before['backup_key']} does not "
                   "exist; create it in APPLY step 1 first")
    # Pin the VERSION, not the key. A key is mutable: by the time an unwind runs, something else may
    # have written there, and the restore would install that instead of the code running now.
    _bk_version = _bk_head.get("VersionId")
    if not _bk_version:
        raise Fail(
            f"s3://{before['backup_bucket']}/{before['backup_key']} has no VersionId, so the bucket "
            "is not versioned. This operation restores $LATEST from that object on failure, and an "
            "unversioned key cannot be pinned — enable versioning on the backup bucket, re-take the "
            "backup, and re-run."
        )
    before["backup_version_id"] = pinned_version(
        before["backup_bucket"], before["backup_key"], _bk_version, "lambda code backup")
    log(f"pinned the backup to version {before['backup_version_id']}")
    with tempfile.TemporaryDirectory() as _td:
        _bk = Path(_td) / "backup.zip"
        aws("s3api", "get-object", "--bucket", before["backup_bucket"],
            "--key", before["backup_key"], "--version-id", _bk_version, str(_bk),
            "--output", "json")
        _bk_sha = _zip_codesha(_bk)
        if _bk_sha != before["codesha_before"]:
            raise Fail(
                "the backup object does not contain the code that is running now: its Lambda "
                f"CodeSha256 is {_bk_sha}, the live function's is {before['codesha_before']}. "
                "Restoring from it would REPLACE the running code with something else. Re-take the "
                "backup in APPLY step 1 against the current version and re-run."
            )
        log(f"asserted the backup object's CodeSha256 equals the live function's "
            f"({_bk_sha[:16]}…)")
    save_state("lambda-code", before)

    txn = Txn()
    try:
        # Order matters: a version snapshots code AND configuration at publish time. Publishing
        # first produced a version without the eight deadline values, and the alias then pointed
        # at exactly that version — the tier the config omitted would still raise at import.
        aws("lambda", "update-function-code", "--function-name", fn,
            "--zip-file", f"fileb://{zip_path}")
        txn.add(lambda: (aws("lambda", "update-function-code", "--function-name", fn,
                             "--s3-bucket", before["backup_bucket"],
                             "--s3-key", before["backup_key"],
                             "--s3-object-version", before["backup_version_id"]),
                         _wait_updated(),
                         assert_eq(_live_conf().get("CodeSha256"), before["codesha_before"],
                                   "CodeSha256 after restoring from the pinned backup version")),
                f"restore $LATEST code from backup version {before['backup_version_id']}")
        _wait_updated()
        conf = _live_conf()
        assert_eq(conf.get("CodeSha256"), codesha_want, "CodeSha256 equals the overlay zip digest")

        merged = dict(conf.get("Environment", {}).get("Variables", {}))
        merged.update(want_env)      # apply the closure's exact values, keep every other key
        # Optimistic concurrency: the environment is read-modify-written, so without a RevisionId
        # guard a change someone else made between the read and the write is silently discarded.
        _rev = conf.get("RevisionId")
        if not _rev:
            raise Fail("the live configuration has no RevisionId; refusing a read-modify-write of "
                       "the environment without a concurrency guard")
        aws("lambda", "update-function-configuration", "--function-name", fn,
            "--environment", json.dumps({"Variables": merged}),
            "--revision-id", _rev)
        txn.add(lambda: (aws("lambda", "update-function-configuration", "--function-name", fn,
                             "--environment", json.dumps({"Variables": before["env_before"]})),
                         _wait_updated()),
                "restore the previous environment")
        _wait_updated()
        conf = _live_conf()
        live = conf.get("Environment", {}).get("Variables", {})
        for k, v in sorted(want_env.items()):
            assert_eq(live.get(k), v, f"env {k} after update")
        _invoke_ok()

        # Publish LAST, so the immutable version carries both the code and the environment.
        published = aws("lambda", "publish-version", "--function-name", fn,
                        "--code-sha256", codesha_want,
                        "--query", "Version", "--output", "text")
        # A version is immutable, so a later assertion failure would otherwise leave an unrecorded
        # one behind and the next retry would publish another.
        txn.add(lambda: aws("lambda", "delete-function", "--function-name", f"{fn}:{published}"),
                f"delete the version {published} this attempt published")
        _wait_updated()
        qual = aws("lambda", "get-function-configuration", "--function-name",
                   f"{fn}:{published}", "--output", "json", parse_json=True) or {}
        assert_eq(qual.get("CodeSha256"), codesha_want,
                  f"CodeSha256 of the published version {published}")
        qenv = (qual.get("Environment") or {}).get("Variables") or {}
        for k, v in sorted(want_env.items()):
            assert_eq(qenv.get(k), v, f"env {k} INSIDE published version {published}")
    except Exception as exc:  # noqa: BLE001 — any failure, including a read, must unwind
        log(f"FAILED after mutating: {exc}")
        txn.unwind()
        conf = _live_conf()
        assert_eq(conf.get("CodeSha256"), before["codesha_before"], "CodeSha256 after the unwind")
        raise

    st = load_state("lambda-code")
    st["published_version"] = published
    # A SEPARATE slot. `lambda-code` holds this run's rollback anchor and is write-once by design,
    # so writing the published version into it was silently dropped and op_lambda_alias then had no
    # version to move to. A fact learned AFTER the anchor gets its own file.
    save_state("lambda-published", st)
    receipt("ApiHandler5E7490E8",
            f"CodeSha256 {before['codesha_before']} -> {codesha_want}; "
            f"{len(want_env)} {DEADLINE_PREFIX}* values applied from the closure; invoke clean")
    receipt(f"ApiHandlerCurrentVersion(published={published})",
            f"publish-version={published} with CodeSha256 {codesha_want}")


# =============================================================== lambda-api-alias
def _alias_version() -> str:
    return aws("lambda", "get-alias", "--function-name", env("OPENCLAW_API_FN"),
               "--name", env("OPENCLAW_API_ALIAS"), "--query", "FunctionVersion", "--output", "text")


def op_lambda_alias(mode: str) -> None:
    if mode == "verify":
        saved = load_state("lambda-published")
        target = saved.get("published_version")
        if not target:
            raise Fail(f"no published version recorded for run {RUN_ID}; run 'lambda-api-code "
                       "apply' first with the same OC_RUN_ID")
        assert_eq(_alias_version(), target, "alias FunctionVersion equals the version apply published")
        vconf = aws("lambda", "get-function-configuration", "--function-name",
                    f"{env('OPENCLAW_API_FN')}:{target}", "--output", "json", parse_json=True)
        assert_eq(vconf.get("CodeSha256"), saved["codesha_want"],
                  f"CodeSha256 of the qualified version {target}")
        return

    if mode == "rollback":
        saved = load_state("lambda-alias")
        aws("lambda", "update-alias", "--function-name", env("OPENCLAW_API_FN"),
            "--name", env("OPENCLAW_API_ALIAS"), "--function-version", saved["before"])
        assert_eq(_alias_version(), saved["before"], "alias FunctionVersion after rollback")
        log("NOTE: $LATEST is a separate path bound by the dispatch event-source mapping. "
            "Run 'lambda-api-code rollback' as well; neither half alone reverts both.")
        return

    # apply
    # The published version lives in its OWN slot: `lambda-code` is the write-once rollback anchor,
    # so a fact learned after it was written could never be added there.
    code_state = load_state("lambda-published")
    target = code_state.get("published_version")
    if not target:
        raise Fail(f"no published version recorded for run {RUN_ID}; run 'lambda-api-code apply' "
                   "first, and use the same OC_RUN_ID")
    before = _alias_version()
    save_state("lambda-alias", {"before": before, "target": target})
    if before == target:
        noop("ApiHandlerLive539BAFFE", f"the alias already points at {target}; nothing was written")
        return
    txn = Txn()
    try:
        aws("lambda", "update-alias", "--function-name", env("OPENCLAW_API_FN"),
            "--name", env("OPENCLAW_API_ALIAS"), "--function-version", target)
        txn.add(lambda: aws("lambda", "update-alias", "--function-name", env("OPENCLAW_API_FN"),
                            "--name", env("OPENCLAW_API_ALIAS"), "--function-version", before),
                f"move the alias back to {before}")
        got = _alias_version()
        assert_eq(got, target, "alias FunctionVersion after update")
    except Exception as exc:  # noqa: BLE001 — a failed READ must also unwind
        log(f"FAILED after moving the alias: {exc}")
        txn.unwind()
        assert_eq(_alias_version(), before, "alias FunctionVersion after the unwind")
        raise
    got = target
    receipt("ApiHandlerLive539BAFFE", f"alias {before} -> {got} (CodeSha256 {code_state['codesha_want']})")


# ============================================================ ssm-deadline-params
def _expected_ssm_params() -> dict:
    out = {}
    for lid, r in template(ORCH, "patch").items():
        if r.get("Type") == "AWS::SSM::Parameter" and "Deadline" in lid:
            pr = r["Properties"]
            name, value = pr.get("Name"), pr.get("Value")
            if not isinstance(name, str) or not isinstance(value, str):
                raise Fail(f"{lid} name/value is not a literal in the closure: {name!r}/{value!r}")
            out[name] = value
    if len(out) != 8:
        raise Fail(f"closure carries {len(out)} deadline SSM parameters, expected 8")
    return out


def op_ssm_deadlines(mode: str) -> None:
    want = _expected_ssm_params()
    if mode == "rollback":
        saved = load_state("ssm-deadlines")
        problems = []
        for name, val in saved.items():
            # Per-parameter try: the first failure must not stop the remaining parameters from being
            # restored, or a partial rollback is left behind AND the operator is told it failed
            # without knowing how much was restored.
            try:
                if val is None:
                    # allow_missing, not allow_fail: a permission or network failure must not read as
                    # a successful restore. Every outcome is read back.
                    aws("ssm", "delete-parameter", "--name", name, allow_missing=True)
                    still = aws("ssm", "get-parameter", "--name", name, "--query", "Parameter.Value",
                                "--output", "text", allow_missing=True)
                    if still is not None:
                        problems.append(f"{name} still exists after delete (value {still!r})")
                else:
                    aws("ssm", "put-parameter", "--name", name, "--value", val,
                        "--type", "String", "--overwrite")
                    got = aws("ssm", "get-parameter", "--name", name, "--query", "Parameter.Value",
                              "--output", "text", allow_missing=True)
                    if got != val:
                        problems.append(f"{name} reads {got!r} after restore, expected {val!r}")
            except Fail as exc:
                problems.append(f"{name}: {exc}")
        if problems:
            # Collected, not raised per-parameter, so one failure does not stop the others from
            # being restored — but the operation must NOT report a successful rollback.
            raise Fail("rolling back the deadline parameters left the system inconsistent: "
                       + "; ".join(problems[:6]))
        log(f"restored and read back all {len(saved)} recorded parameter values")
        return

    if mode == "verify":
        for name, val in sorted(want.items()):
            got = aws("ssm", "get-parameter", "--name", name, "--query", "Parameter.Value",
                      "--output", "text", allow_missing=True)
            assert_eq(got, val, f"ssm {name}")
        return

    # apply
    before = {}
    for name in want:
        before[name] = aws("ssm", "get-parameter", "--name", name, "--query", "Parameter.Value",
                           "--output", "text", allow_missing=True)
    save_state("ssm-deadlines", before)

    def _undo(n: str, prev):
        # allow_missing, not allow_fail: a delete that fails for any reason OTHER than "already
        # gone" must propagate, or Txn reports a clean unwind while the parameter stays changed.
        if prev is None:
            def _del():
                aws("ssm", "delete-parameter", "--name", n, allow_missing=True)
                still = aws("ssm", "get-parameter", "--name", n, "--query", "Parameter.Value",
                            "--output", "text", allow_missing=True)
                if still is not None:
                    raise Fail(f"{n} still exists after delete during unwind")
            return _del

        def _put():
            aws("ssm", "put-parameter", "--name", n, "--value", prev,
                "--type", "String", "--overwrite")
            got = aws("ssm", "get-parameter", "--name", n, "--query", "Parameter.Value",
                      "--output", "text")
            assert_eq(got, prev, f"{n} after restoring during unwind")
        return _put

    txn = Txn()
    written = set()
    try:
        for name, val in sorted(want.items()):
            if before[name] == val:
                continue
            aws("ssm", "put-parameter", "--name", name, "--value", val,
                "--type", "String", "--overwrite")
            txn.add(_undo(name, before[name]), f"restore {name}")
            written.add(name)
        for name, val in sorted(want.items()):
            got = aws("ssm", "get-parameter", "--name", name, "--query", "Parameter.Value",
                      "--output", "text")
            assert_eq(got, val, f"ssm {name} after put")
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED partway through the parameter set: {exc}")
        txn.unwind()
        for name, prev in before.items():
            got = aws("ssm", "get-parameter", "--name", name, "--query", "Parameter.Value",
                      "--output", "text", allow_missing=True)
            assert_eq(got, prev, f"ssm {name} after the unwind")
        raise
    for lid, r in template(ORCH, "patch").items():
        if r.get("Type") != "AWS::SSM::Parameter" or "Deadline" not in lid:
            continue
        nm = r["Properties"]["Name"]
        if nm in written:
            receipt(lid, f"{nm}={r['Properties']['Value']} written and asserted by readback")
        else:
            noop(lid, f"{nm} already carried the closure value; nothing was written")


# ================================================================= s3-edge-bundle
def _edge_prefix() -> str:
    props = expected_props(ORCH, "EdgeBundleAssetDeploymentCustomResource6700B3C3")
    pref = props.get("DestinationBucketKeyPrefix")
    if not isinstance(pref, str) or not pref:
        raise Fail("the closure's edge bundle prefix is not a literal string")
    return pref


def _assets_tag_delta() -> tuple[str, str]:
    b = {t["Key"]: t["Value"] for t in base_props(ORCH, "Assets560B5C73").get("Tags", [])}
    p = {t["Key"]: t["Value"] for t in expected_props(ORCH, "Assets560B5C73").get("Tags", [])}
    added = [k for k in p if k not in b]
    removed = [k for k in b if k not in p]
    if len(added) != 1 or len(removed) != 1:
        raise Fail(f"expected exactly one tag added and one removed; got +{added} -{removed}")
    return added[0], removed[0]


def _assets_tag_value(key: str) -> str:
    """The tag VALUE the closure declares, not an assumed 'true'."""
    for t in expected_props(ORCH, "Assets560B5C73").get("Tags", []):
        if t["Key"] == key:
            v = t["Value"]
            if not isinstance(v, str):
                raise Fail(f"the closure's value for {key[:50]}… is not a literal: {v!r}")
            return v
    raise Fail(f"{key[:50]}… absent from the patch closure's tag set")


def _local_payload(root: Path) -> dict:
    return {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}


def _assert_s3_payload(bucket: str, prefix: str, payload: dict) -> None:
    import tempfile
    for rel, want in sorted(payload.items()):
        key = f"{prefix.rstrip('/')}/{rel}"
        with tempfile.NamedTemporaryFile() as tf:
            if aws("s3api", "get-object", "--bucket", bucket, "--key", key, tf.name,
                   allow_missing=True) is None:
                raise Fail(f"s3://{bucket}/{key} is absent")
            got = sha256_file(Path(tf.name))
        if got != want:
            raise Fail(f"s3://{bucket}/{key} content sha256 {got} != shipped {want}")
    log(f"asserted {len(payload)} object(s) under {prefix} match the shipped bytes")


def op_s3_edge_bundle(mode: str) -> None:
    bucket = env("ASSETS_BUCKET")
    prefix = _edge_prefix()
    payload = _local_payload(Path("host-scripts/edge"))
    if not payload:
        raise Fail("host-scripts/edge is empty; nothing to upload")
    tag_add, tag_remove = _assets_tag_delta()

    if mode == "verify":
        _assert_s3_payload(bucket, prefix, payload)
        # verify must check the SAME delta apply asserted: the new tag with the closure's VALUE and
        # the superseded tag gone. Checking only that the key exists passes on a wrong value, and
        # a leftover old tag makes two prefixes look owned.
        want_value = _assets_tag_value(tag_add)
        live = {t["Key"]: t["Value"] for t in
                (aws("s3api", "get-bucket-tagging", "--bucket", bucket, "--output", "json",
                     parse_json=True, allow_missing=True) or {}).get("TagSet", [])}
        assert_eq(live.get(tag_add), want_value, f"cr-owned tag value for {tag_add[:44]}…")
        if tag_remove in live:
            raise Fail(f"the superseded tag {tag_remove[:60]}… is still present")
        log("verified the full tag delta: new tag value matches the closure, old tag absent")
        tags = {t["Key"]: t["Value"] for t in
                (aws("s3api", "get-bucket-tagging", "--bucket", bucket, "--output", "json",
                     parse_json=True, allow_missing=True) or {}).get("TagSet", [])}
        if tag_add not in tags:
            raise Fail(f"bucket tag {tag_add[:60]}… is absent; the cr-owned marker was not applied")
        log(f"asserted bucket tag {tag_add[:60]}… present")
        return

    if mode == "rollback":
        saved = load_state("s3-edge-bundle")
        aws("s3api", "put-bucket-tagging", "--bucket", bucket,
            "--tagging", json.dumps({"TagSet": saved["tags_before"]}))
        log("bucket tags restored. The uploaded objects are left in place on purpose: removing them "
            "is only safe once no Launch Template version references this prefix. Revert the LT "
            "first with apply-lt.sh rollback.")
        return

    # apply — the fail-closed prerequisite is checked here, not just documented
    op_iam("gate")
    want_tag_value = _assets_tag_value(tag_add)
    tags_before = (aws("s3api", "get-bucket-tagging", "--bucket", bucket, "--output", "json",
                       parse_json=True, allow_missing=True) or {}).get("TagSet", [])

    # Record, per key, what was there BEFORE. An unwind that deletes every key it uploaded also
    # deletes objects a previous run (or the original deployment) put there, which is a live edge
    # bundle. allow_missing means an unreadable key raises instead of being recorded as absent.
    # Record the VersionId, not just an ETag: an overwritten object can only be put back BY version,
    # and an undo that could merely delete it would leave the prefix short of a file the live edge
    # bundle needs.
    pre = {}
    for rel in payload:
        key = f"{prefix.rstrip('/')}/{rel}"
        head = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                   "--output", "json", parse_json=True, allow_missing=True)
        pre[rel] = None if head is None else {"version_id": head.get("VersionId"),
                                              "etag": head.get("ETag")}
        if head is not None:
            pre[rel]["version_id"] = pinned_version(bucket, key, pre[rel]["version_id"],
                                                    "edge bundle object")
    # ONE state write, carrying every fact the unwind needs. Two writes under the same name would be
    # dropped by write-once, which is what protects the rollback anchor.
    save_state("s3-edge-bundle", {"tags_before": tags_before, "prefix": prefix,
                                  "objects_before": pre, "tag_add": tag_add,
                                  "tag_remove": tag_remove})
    created = [rel for rel, meta in pre.items() if meta is None]
    overwritten = [rel for rel, meta in pre.items() if meta is not None]
    log(f"{len(created)} of {len(payload)} objects under {prefix} do not exist yet (unwind deletes "
        f"those); {len(overwritten)} exist and will be restored by version id")

    def _undo_bundle():
        problems = []
        for rel in created:
            key = f"{prefix.rstrip('/')}/{rel}"
            try:
                aws("s3api", "delete-object", "--bucket", bucket, "--key", key, allow_missing=True)
                if aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                       "--output", "json", parse_json=True, allow_missing=True) is not None:
                    problems.append(f"{key} still exists after delete")
            except Fail as exc:
                problems.append(f"{key}: {exc}")
        # Objects that EXISTED go back to the exact version recorded before the write, and the
        # restore is proven by comparing the ETag against that version's own.
        for rel in overwritten:
            key = f"{prefix.rstrip('/')}/{rel}"
            meta = pre[rel]
            try:
                aws("s3api", "copy-object", "--bucket", bucket, "--key", key,
                    "--copy-source", f"{bucket}/{key}?versionId={meta['version_id']}",
                    "--metadata-directive", "COPY")
                now = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                          "--output", "json", parse_json=True)
                if (now or {}).get("ETag") != meta["etag"]:
                    problems.append(f"{key} restored to {(now or {}).get('ETag')} but version "
                                    f"{meta['version_id']} has {meta['etag']}")
            except Fail as exc:
                problems.append(f"{key}: {exc}")
        if problems:
            raise Fail("unwinding the edge bundle failed: "
                       + "; ".join(str(x) for x in problems[:5]))


    txn = Txn()
    try:
        # Registered BEFORE the upload: `aws s3 cp --recursive` can copy some objects and then exit
        # non-zero, and an undo registered afterwards would never run for those.
        txn.add(_undo_bundle,
                f"remove the {len(created)} object(s) this attempt created and restore the "
                f"{len(overwritten)} it overwrote, under {prefix}")
        aws("s3", "cp", "host-scripts/edge/", f"s3://{bucket}/{prefix.rstrip('/')}/",
            "--recursive", "--only-show-errors")
        _assert_s3_payload(bucket, prefix, payload)

        # Filter BOTH keys before appending, or a retry leaves two entries with the same Key and the
        # tag set stops being idempotent.
        new_tags = [t for t in tags_before if t["Key"] not in (tag_remove, tag_add)]
        new_tags.append({"Key": tag_add, "Value": want_tag_value})
        aws("s3api", "put-bucket-tagging", "--bucket", bucket,
            "--tagging", json.dumps({"TagSet": new_tags}))
        txn.add(lambda: aws("s3api", "put-bucket-tagging", "--bucket", bucket,
                            "--tagging", json.dumps({"TagSet": tags_before})),
                "restore the previous bucket tag set")
        tags = {t["Key"]: t["Value"] for t in
                (aws("s3api", "get-bucket-tagging", "--bucket", bucket, "--output", "json",
                     parse_json=True) or {}).get("TagSet", [])}
        assert_eq(tags.get(tag_add), want_tag_value, f"cr-owned tag value for {tag_add[:44]}…")
        if tag_remove in tags:
            raise Fail(f"the superseded tag {tag_remove[:60]}… is still present; the closure "
                       "removes it, and leaving it makes two prefixes look owned")
        log(f"asserted the superseded tag {tag_remove[:44]}… is gone")
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED after writing: {exc}")
        txn.unwind()
        raise
    receipt("EdgeBundleAssetDeploymentCustomResource6700B3C3",
            f"{len(payload)} objects under {prefix} written and asserted byte-identical")
    receipt("Assets560B5C73",
            f"cr-owned tag {tag_add[:44]}…={want_tag_value} written; {tag_remove[:44]}… removed")


# ================================================================== s3-obs-assets
OBS_IDS = ["ObsFbInstallerCustomResourceB2298A62",
           "ObsFbEdgeConfCustomResourceA22C09AC",
           "ObsFbHostConfCustomResource27F6488F"]


def _obs_prefix() -> str:
    """Read the destination prefix out of the closure rather than trusting an env override.
    A wrong prefix would upload to a path nothing reads and still pass a naive check."""
    prefixes = set()
    for lid in OBS_IDS:
        pr = expected_props(ORCH, lid).get("DestinationBucketKeyPrefix")
        if isinstance(pr, str) and pr:
            prefixes.add(pr.rstrip("/"))
    if len(prefixes) == 1:
        return prefixes.pop()
    if not prefixes:
        raise Fail("no DestinationBucketKeyPrefix in the closure for the observability "
                   "deployments; refusing to guess where they belong")
    raise Fail(f"the observability deployments target different prefixes {sorted(prefixes)}; "
               "this operation cannot cover them as one unit")


def _restore_obs_versions(bucket: str, saved: dict) -> None:
    """Restore each key to the exact version recorded at apply, and PROVE each restore.

    An undo that swallows its own errors lets Txn report a clean unwind while objects stay changed,
    so every call here raises on failure and the result is read back.
    """
    problems = []
    for key, ver in saved.items():
        try:
            if ver in (None, "None", ""):
                aws("s3api", "delete-object", "--bucket", bucket, "--key", key,
                    allow_missing=True)
                still = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                            "--output", "json", parse_json=True, allow_missing=True)
                if still is not None:
                    problems.append(f"{key} still exists after delete")
                else:
                    log(f"{key} had no previous version; the object this run created is gone")
                continue
            aws("s3api", "copy-object", "--bucket", bucket, "--key", key,
                "--copy-source", f"{bucket}/{key}?versionId={ver}", "--metadata-directive", "COPY")
            head = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                       "--output", "json", parse_json=True)
            # Content, not existence: a copy that silently produced different bytes would pass an
            # existence check. Compare against the recorded version's own digest.
            want = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                       "--version-id", ver, "--output", "json", parse_json=True)
            if (head or {}).get("ETag") != (want or {}).get("ETag"):
                problems.append(f"{key} restored to ETag {(head or {}).get('ETag')} but version "
                                f"{ver} has {(want or {}).get('ETag')}")
            else:
                log(f"{key} restored from version {ver} and its digest matches that version")
        except Fail as exc:
            problems.append(f"{key}: {exc}")
    if problems:
        raise Fail("restoring the observability objects failed: " + "; ".join(problems[:5]))


def op_s3_obs(mode: str) -> None:
    bucket = env("ASSETS_BUCKET")
    prefix = _obs_prefix()
    payload = _local_payload(Path("host-scripts/edge/fluent-bit"))
    if not payload:
        raise Fail("host-scripts/edge/fluent-bit is empty")
    for lid in OBS_IDS:
        expected_props(ORCH, lid)   # fail closed if the closure does not carry it

    if mode == "verify":
        _assert_s3_payload(bucket, prefix, payload)
        return

    if mode == "rollback":
        saved = load_state("s3-obs")
        _restore_obs_versions(bucket, saved)
        for key, ver in saved.items():
            head = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                       "--output", "json", parse_json=True, allow_missing=True)
            if ver in (None, "None", ""):
                if head is not None:
                    raise Fail(f"{key} still exists after rollback but had no previous version")
                continue
            if head is None:
                raise Fail(f"{key} is absent after rollback; the restore did not take effect")
        log("every recorded object was restored or removed, and each outcome was read back")
        return

    # apply
    before = {}
    for rel in payload:
        key = f"{prefix}/{rel}"
        # head-object returns the CURRENT version for this exact key. list-object-versions --prefix
        # is a prefix match (so `a.conf` also matches `a.conf.bak`), its Versions[0] is not
        # guaranteed to be current, and it ignores DeleteMarkers entirely — a key whose latest
        # state is a delete marker would be recorded as "restore this old version", which
        # resurrects an object the deployment had removed.
        head = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                   "--output", "json", parse_json=True, allow_missing=True)
        before[key] = (head or {}).get("VersionId")
        if head is not None:
            before[key] = pinned_version(bucket, key, before[key], "observability object")
    save_state("s3-obs", before)

    txn = Txn()
    try:
        # Registered BEFORE the upload, for the same reason: a partial recursive copy that then
        # fails must still be undone.
        txn.add(lambda: _restore_obs_versions(bucket, before),
                "restore the previous object versions")
        aws("s3", "cp", "host-scripts/edge/fluent-bit/", f"s3://{bucket}/{prefix}/",
            "--recursive", "--only-show-errors")
        _assert_s3_payload(bucket, prefix, payload)
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED after uploading: {exc}")
        txn.unwind()
        raise
    for lid in OBS_IDS:
        receipt(lid, f"{len(payload)} object(s) under {prefix} written and asserted byte-identical")


# ========================================================= codebuild-golden-image
def _golden_expected_location() -> tuple[str, str]:
    """(bucket, key) exactly as the closure declares them. Taking the bucket from the
    environment while taking the key from the closure would let the two disagree."""
    loc = expected_props(IMG, "GoldenImageBuilderCEF13562")["Source"]["Location"]
    if not isinstance(loc, str) or "/" not in loc:
        raise Fail(f"closure source location is not a literal bucket/key: {loc!r}")
    bucket, key = loc.split("/", 1)
    if not bucket or not key:
        raise Fail(f"closure source location {loc!r} does not split into bucket and key")
    return bucket, key


def _golden_expected_asset_key() -> str:
    return _golden_expected_location()[1]



def _closure_asset_arn_suffixes() -> tuple[str, str]:
    """(base_arn_suffix, patch_arn_suffix) for the build role's asset statement.

    Read from the closure rather than assembled from the environment: the change IS the key, so the
    two sides of the closure are the only authority on what moved.
    """
    lid = "GoldenImageBuildRoleDefaultPolicy94E47D88"
    def arns(side: str) -> set:
        doc = template(IMG, side)[lid]["Properties"]["PolicyDocument"]
        found = set()
        for st in doc.get("Statement", []):
            res = st.get("Resource")
            for item in (res if isinstance(res, list) else [res]):
                if isinstance(item, dict):
                    for part in item.get("Fn::Join", ["", []])[1]:
                        if isinstance(part, str) and part.endswith(".zip"):
                            found.add(part)
                elif isinstance(item, str) and item.endswith(".zip"):
                    found.add(item)
        return found
    b, pa = arns("base"), arns("patch")
    gone, added = sorted(b - pa), sorted(pa - b)
    if len(added) != 1 or len(gone) != 1:
        raise Fail(f"expected exactly one asset ARN to move in {lid}; the closure shows "
                   f"removed={gone} added={added}")
    return gone[0], added[0]


def _build_role_policy(role: str) -> tuple[str, dict]:
    """(policy_name, document) of the role's inline policy that carries the asset statement."""
    names = (aws("iam", "list-role-policies", "--role-name", role, "--output", "json",
                 parse_json=True) or {}).get("PolicyNames", [])
    if not names:
        raise Fail(f"{role} has no inline policies; the CDK-managed asset grant is not there")
    _gone, added = _closure_asset_arn_suffixes()
    # Match on the asset KEY, never on the bucket segment: the closure's ARN carries the synth-time
    # placeholder account, so a bucket-based match finds nothing in a real environment.
    key_only = added.rsplit("/", 1)[-1]
    hits = []
    for n in names:
        doc = (aws("iam", "get-role-policy", "--role-name", role, "--policy-name", n,
                   "--output", "json", parse_json=True) or {}).get("PolicyDocument")
        if doc and key_only in json.dumps(doc):
            hits.append((n, doc))
    if not hits:
        # Fall back to "the policy that references ANY asset zip", so the operation can still name
        # exactly which policy it would edit instead of failing with no information.
        for n in names:
            doc = (aws("iam", "get-role-policy", "--role-name", role, "--policy-name", n,
                       "--output", "json", parse_json=True) or {}).get("PolicyDocument")
            if doc and ".zip" in json.dumps(doc):
                hits.append((n, doc))
    if len(hits) != 1:
        raise Fail(f"expected exactly one inline policy on {role} referencing the asset bucket, "
                   f"found {len(hits)} of {names}; refusing to guess which to edit")
    return hits[0]


def _statement_with_arn(doc: dict, needle: str) -> dict | None:
    for st in doc.get("Statement", []):
        res = st.get("Resource")
        for item in (res if isinstance(res, list) else [res]):
            if isinstance(item, str) and needle in item:
                return st
    return None


def _grant_new_asset_arn(role: str, txn: "Txn") -> tuple[bool, str, dict]:
    """Add the new asset ARN alongside the old one. Returns (wrote, policy_name, prior_document).

    Addition, not replacement: removing the old ARN would drop a permission this operation was not
    asked to remove, and keeping both makes a re-run a no-op.
    """
    gone, added = _closure_asset_arn_suffixes()
    name, doc = _build_role_policy(role)
    prior = json.loads(json.dumps(doc))
    # By key, for the same reason: the live ARN's bucket differs from the closure's placeholder.
    st = (_statement_with_arn(doc, gone.rsplit("/", 1)[-1])
          or _statement_with_arn(doc, added.rsplit("/", 1)[-1]))
    if st is None:
        raise Fail(f"neither the old nor the new asset key appears in {name}; the live policy does "
                   "not match the closure, so this operation will not edit it")
    res = st["Resource"]
    if isinstance(res, str):
        res = [res]
    old_key, new_key_only = gone.rsplit("/", 1)[-1], added.rsplit("/", 1)[-1]
    old_full = next((r for r in res if old_key in r), None)
    new_full = next((r for r in res if new_key_only in r), None)
    if new_full is not None:
        log(f"{name} already grants the new asset key; nothing to write")
        return False, name, prior
    if old_full is None:
        raise Fail(f"{name} carries neither key as a literal ARN; refusing to synthesize one")
    # Derive the new ARN from the LIVE one by swapping only the key, so the account and bucket in the
    # resulting ARN are this environment's, not the closure's placeholders.
    res.append(old_full.replace(old_key, new_key_only))
    st["Resource"] = res
    aws("iam", "put-role-policy", "--role-name", role, "--policy-name", name,
        "--policy-document", json.dumps(doc))

    def _undo_policy():
        aws("iam", "put-role-policy", "--role-name", role, "--policy-name", name,
            "--policy-document", json.dumps(prior))
        back = (aws("iam", "get-role-policy", "--role-name", role, "--policy-name", name,
                    "--output", "json", parse_json=True) or {}).get("PolicyDocument")
        assert_eq(json.dumps(back, sort_keys=True), json.dumps(prior, sort_keys=True),
                  f"{name} document after restore")

    # Registered HERE, between the write and the readback. Registering it in the caller meant a
    # failed readback raised with the role already widened and nothing recorded to undo it.
    txn.add(_undo_policy, f"restore {name} to the document this run read")
    got = (aws("iam", "get-role-policy", "--role-name", role, "--policy-name", name,
               "--output", "json", parse_json=True) or {}).get("PolicyDocument")
    if new_key_only not in json.dumps(got):
        raise Fail(f"{name} readback does not contain the new asset key after put-role-policy")
    log(f"{name}: added the new asset ARN alongside the old one (nothing was removed)")
    return True, name, prior


def _saved_source(saved: dict) -> dict:
    """The complete Source block recorded at apply.

    `--source type=S3,location=...` REPLACES the block, so a rollback built from the location alone
    silently drops buildspec, gitCloneDepth, insecureSsl and any auth — the project then fails its
    next build for a reason unrelated to the patch. Refuse rather than send a partial block.
    """
    src = saved.get("source_before")
    if not isinstance(src, dict) or not src.get("location"):
        raise Fail(
            "this run's state has no complete `source_before` block, so a rollback here could only "
            "send a partial Source and would drop buildspec/auth fields. Restore the project's "
            "source by hand from the state file, or re-run apply with a run id whose state was "
            "written by this version of the tool."
        )
    return src


def op_codebuild(mode: str) -> None:
    project = env("GOLDEN_IMAGE_PROJECT")
    closure_bucket, want_key = _golden_expected_location()
    # The KEY is authoritative: a CDK asset key is the content hash of the bundled source, so it is
    # identical in every account. The BUCKET is not: the closure was synthesized with
    # CDK_DEFAULT_ACCOUNT/REGION placeholders (see the recorded synth argv), so its bucket name is
    # `cdk-hnb659fds-assets-123456789012-us-east-1` no matter where the kit is applied. Requiring
    # them to match made this operation refuse in every real environment.
    bucket = os.environ.get("CDK_ASSETS_BUCKET", "").strip()
    if not bucket:
        raise Fail("CDK_ASSETS_BUCKET is required: the closure's bucket name is a synth-time "
                   f"placeholder ({closure_bucket}) and cannot be used against a live account")
    ident = _target_identity()
    expected_shape = f"cdk-hnb659fds-assets-{ident.get('account')}-{REGION}"
    if bucket != expected_shape:
        raise Fail(
            f"CDK_ASSETS_BUCKET={bucket} is not this account and region's CDK assets bucket "
            f"(expected {expected_shape} for account {ident.get('account')} in {REGION}). Uploading "
            "the build source to another account's bucket, or to another region's, would point the "
            "builder at an asset this deployment cannot read."
        )
    if closure_bucket != bucket:
        log(f"closure bucket {closure_bucket} is a synth placeholder; using this account's "
            f"{bucket} and taking only the content-addressed key from the closure")
    want_loc = f"{bucket}/{want_key}"
    role_lid = "GoldenImageBuildRoleDefaultPolicy94E47D88"

    def live_loc() -> str:
        return aws("codebuild", "batch-get-projects", "--names", project,
                   "--query", "projects[0].source.location", "--output", "text")

    def role_allows_key() -> bool:
        arn = env("GOLDEN_IMAGE_ROLE_ARN")
        d = aws("iam", "simulate-principal-policy", "--policy-source-arn", arn,
                "--action-names", "s3:GetObject",
                "--resource-arns", f"arn:aws:s3:::{bucket}/{want_key}",
                "--query", "EvaluationResults[0].EvalDecision", "--output", "text")
        log(f"role decision for the new asset key: {d}")
        return d == "allowed"

    if mode == "verify":
        assert_eq(live_loc(), want_loc, "codebuild source location equals the closure value")
        if not role_allows_key():
            raise Fail("the build role cannot read the new asset key; the build would fail")
        # Read the OBJECT, not only the project's pointer: a project pointing at a key whose bytes
        # are wrong (or gone) passes a pointer-only check and then fails at the next image build.
        head = aws("s3api", "head-object", "--bucket", bucket, "--key", want_key,
                   "--output", "json", parse_json=True, allow_missing=True)
        if head is None:
            raise Fail(f"s3://{bucket}/{want_key} is absent although the project's source points at "
                       "it; the next build would fail")
        want_digest = (load_state("codebuild").get("zip_sha256")
                       if state_path("codebuild").is_file() else None)
        if want_digest:
            with tempfile.TemporaryDirectory() as vt:
                vb = Path(vt) / "verify.zip"
                aws("s3api", "get-object", "--bucket", bucket, "--key", want_key, str(vb),
                    "--output", "json")
                assert_eq(sha256_file(vb), want_digest,
                          f"s3://{bucket}/{want_key} content digest vs the digest apply recorded")
        else:
            raise Fail(
                f"no zip_sha256 recorded for run {RUN_ID}, so the object's CONTENT cannot be "
                "verified. Presence plus a matching pointer is not verification: the key could hold "
                "a truncated or replaced archive and the next image build would run it. Re-run "
                "apply with this run id, or verify the object by hand against the digest you built."
            )
        return

    if mode == "rollback":
        saved = load_state("codebuild")
        problems = []

        # 1. the project's complete Source block
        try:
            aws("codebuild", "update-project", "--name", project,
                "--source", json.dumps(_saved_source(saved)))
            assert_eq(live_loc(), saved["before"], "source location after rollback")
        except Fail as exc:
            problems.append(f"project source: {exc}")

        # 2. the S3 asset object. apply either CREATED it or OVERWROTE a recorded version; rollback
        # has to undo whichever happened, or the builder keeps reading bytes this run put there.
        want_key = saved.get("want", "").split("/", 1)[1] if "/" in saved.get("want", "") else None
        bucket_name = saved.get("want", "").split("/", 1)[0] if "/" in saved.get("want", "") else None
        prior_version = saved.get("object_version_before")
        if want_key and bucket_name:
            try:
                if saved.get("object_existed") and prior_version:
                    aws("s3api", "copy-object", "--bucket", bucket_name, "--key", want_key,
                        "--copy-source", f"{bucket_name}/{want_key}?versionId={prior_version}",
                        "--metadata-directive", "COPY")
                    now = aws("s3api", "head-object", "--bucket", bucket_name, "--key", want_key,
                              "--output", "json", parse_json=True)
                    if (now or {}).get("ETag") != saved.get("object_etag_before"):
                        problems.append(
                            f"s3://{bucket_name}/{want_key} restored to {(now or {}).get('ETag')} "
                            f"but version {prior_version} has {saved.get('object_etag_before')}")
                    else:
                        log(f"s3://{bucket_name}/{want_key} restored to version {prior_version}")
                elif saved.get("object_existed") is False:
                    aws("s3api", "delete-object", "--bucket", bucket_name, "--key", want_key,
                        allow_missing=True)
                    if aws("s3api", "head-object", "--bucket", bucket_name, "--key", want_key,
                           "--output", "json", parse_json=True, allow_missing=True) is not None:
                        problems.append(f"s3://{bucket_name}/{want_key} still exists after delete")
                    else:
                        log(f"s3://{bucket_name}/{want_key} removed (this run created it)")
                else:
                    problems.append(
                        f"the state for run {RUN_ID} does not record whether "
                        f"s3://{bucket_name}/{want_key} existed before apply, so the object cannot "
                        "be rolled back automatically — inspect it by hand")
            except Fail as exc:
                problems.append(f"asset object: {exc}")
        else:
            problems.append("the state does not record the asset location, so the object was not "
                            "rolled back")

        # 3. the build role's inline policy, if apply widened it
        role_prior = saved.get("role_policy_before")
        role_name = saved.get("role_name")
        policy_name = saved.get("role_policy_name")
        if saved.get("role_policy_written"):
            if not (role_prior and role_name and policy_name):
                problems.append("apply widened the build role but the state does not carry enough "
                                "to restore it; restore the policy by hand")
            else:
                try:
                    aws("iam", "put-role-policy", "--role-name", role_name,
                        "--policy-name", policy_name, "--policy-document", json.dumps(role_prior))
                    back = (aws("iam", "get-role-policy", "--role-name", role_name,
                                "--policy-name", policy_name, "--output", "json",
                                parse_json=True) or {}).get("PolicyDocument")
                    if json.dumps(back, sort_keys=True) != json.dumps(role_prior, sort_keys=True):
                        problems.append(f"{policy_name} does not match the recorded document after "
                                        "restore")
                    else:
                        log(f"{policy_name} restored to the document apply recorded")
                except Fail as exc:
                    problems.append(f"build role policy: {exc}")
        else:
            log("apply did not modify the build role policy, so there is nothing to restore there")

        if problems:
            raise Fail("the CodeBuild rollback did not fully restore this run's changes: "
                       + "; ".join(problems[:6]))
        log("rolled back all three changes this run made: project source, asset object, role policy")
        return

    # apply
    zip_path = Path(env("REPO_SOURCE_ZIP"))
    if not zip_path.is_file():
        raise Fail(f"{zip_path} not found; it is the repo source zip this range produces")
    # `--source type=S3,location=...` REPLACES the whole Source block, dropping buildspec,
    # gitCloneDepth, insecureSsl, sourceIdentity and any auth. Snapshot the full object, send it
    # back with only Location changed, and keep it for the unwind.
    # The operator supplies REPO_SOURCE_ZIP, so it is not trustworthy: assert it is a real zip and
    # that it carries this kit's own shipped sources before it becomes the builder's input.
    import zipfile
    if not zipfile.is_zipfile(zip_path):
        raise Fail(f"{zip_path} is not a zip archive")
    with zipfile.ZipFile(zip_path) as _z:
        _bad = _z.testzip()
        if _bad is not None:
            raise Fail(f"{zip_path} is corrupt at member {_bad}")
        _names = set(_z.namelist())
    # ALL THREE, not any one: a zip with only setup.sh is not a repository the golden-image builder
    # can build from, and "any marker present" would accept an arbitrary archive.
    _required = ("setup.sh", "build-rootfs.sh", "deploy/app.py")
    _absent = [n for n in _required if n not in _names]
    if _absent:
        raise Fail(
            f"{zip_path} is missing {_absent} — it is not a repository archive the golden-image "
            "builder can build from. Refusing to make it the project's source."
        )
    # Bind it to THIS kit: every file the kit ships under host-scripts/deploy-machine/ that the zip
    # also carries must match byte-for-byte. That is what makes a wrong or tampered archive fail
    # here instead of at the next image build.
    _root = Path("host-scripts/deploy-machine")
    _checked, _differing = 0, []
    if _root.is_dir():
        with zipfile.ZipFile(zip_path) as _z2:
            for _f in sorted(_root.rglob("*")):
                if not _f.is_file():
                    continue
                _rel = str(_f.relative_to(_root))
                if _rel not in _names:
                    continue
                _checked += 1
                if hashlib.sha256(_z2.read(_rel)).hexdigest() != sha256_file(_f):
                    _differing.append(_rel)
    if _differing:
        raise Fail(f"{zip_path} disagrees with this kit's shipped sources on {_differing[:5]} "
                   f"(of {_checked} comparable file(s)); it is not built from this revision")
    _zip_digest = sha256_file(zip_path)
    log(f"asserted {zip_path.name} is a valid zip carrying all of {_required}, and that its "
        f"{_checked} comparable file(s) match this kit byte-for-byte (sha256 {_zip_digest[:16]}…)")
    full_before = (aws("codebuild", "batch-get-projects", "--names", project,
                       "--output", "json", parse_json=True) or {}).get("projects", [])
    if len(full_before) != 1:
        raise Fail(f"expected exactly one CodeBuild project named {project}, got {len(full_before)}")
    src_before = full_before[0].get("source") or {}
    if not src_before.get("location"):
        raise Fail(f"{project} has no readable source location")
    obj_before = aws("s3api", "head-object", "--bucket", bucket, "--key", want_key,
                     "--query", "ETag", "--output", "text", allow_missing=True)
    # Everything rollback needs, written BEFORE the first mutation. `object_existed` alone was not
    # enough: restoring an overwritten object needs its version id and its digest.
    obj_before_version = None
    if obj_before is not None:
        obj_before_version = aws("s3api", "head-object", "--bucket", bucket, "--key", want_key,
                                 "--query", "VersionId", "--output", "text", allow_missing=True)
        obj_before_version = pinned_version(bucket, want_key, obj_before_version,
                                            "codebuild source asset")
        log(f"the asset key already holds version {obj_before_version}; unwind will restore it")

    save_state("codebuild", {"before": live_loc(), "want": want_loc,
                            "source_before": src_before,
                            "object_existed": obj_before is not None,
                            "object_version_before": obj_before_version,
                            "object_etag_before": obj_before,
                            "role_name": os.environ.get("GOLDEN_IMAGE_ROLE_NAME", ""),
                            "zip_sha256": _zip_digest})

    src_after = json.loads(json.dumps(src_before))
    src_after["location"] = want_loc
    src_after["type"] = "S3"


    txn = Txn()
    try:
        aws("s3", "cp", str(zip_path), f"s3://{bucket}/{want_key}", "--only-show-errors")
        if obj_before is None:
            txn.add(lambda: aws("s3api", "delete-object", "--bucket", bucket, "--key", want_key,
                                allow_missing=True),
                    "remove the asset object this attempt created")
        else:
            txn.add(lambda: aws("s3api", "copy-object", "--bucket", bucket, "--key", want_key,
                                "--copy-source",
                                f"{bucket}/{want_key}?versionId={obj_before_version}",
                                "--metadata-directive", "COPY"),
                    f"restore the asset object to version {obj_before_version}")
        if aws("s3api", "head-object", "--bucket", bucket, "--key", want_key,
               "--output", "json", parse_json=True, allow_missing=True) is None:
            raise Fail(f"s3://{bucket}/{want_key} absent after upload")
        # Existence is not content. Download it back and compare the digest, or a truncated or
        # replaced upload would pass and the next image build would run the wrong source.
        with tempfile.TemporaryDirectory() as _rt:
            _rb = Path(_rt) / "readback.zip"
            aws("s3api", "get-object", "--bucket", bucket, "--key", want_key, str(_rb),
                "--output", "json")
            assert_eq(sha256_file(_rb), _zip_digest, f"s3://{bucket}/{want_key} content digest")
        _role_name = env("GOLDEN_IMAGE_ROLE_NAME")
        _wrote_role, _role_policy_name, _role_prior = _grant_new_asset_arn(_role_name, txn)
        # Recorded for `rollback`, which runs in a separate process and would otherwise have no way
        # to know the role was widened or what it looked like before.
        _sp = state_path("codebuild")
        _body = json.loads(_sp.read_text())
        _body["payload"].update({"role_policy_written": _wrote_role,
                                 "role_policy_name": _role_policy_name,
                                 "role_policy_before": _role_prior})
        _sp.write_text(json.dumps(_body, ensure_ascii=False, indent=1))

        aws("codebuild", "update-project", "--name", project, "--source", json.dumps(src_after))
        txn.add(lambda: (aws("codebuild", "update-project", "--name", project,
                             "--source", json.dumps(src_before)),
                         assert_eq(live_loc(), src_before["location"],
                                   f"{project} source location after restore")),
                "restore the project's complete Source block")
        after = (aws("codebuild", "batch-get-projects", "--names", project,
                     "--output", "json", parse_json=True) or {}).get("projects", [])
        if len(after) != 1:
            raise Fail("project readback returned an unexpected number of projects")
        src_now = after[0].get("source") or {}
        # Prove nothing but the location moved: a dropped buildspec breaks the next build.
        for k in sorted(set(src_before) | set(src_now)):
            if k == "location":
                continue
            if src_before.get(k) != src_now.get(k):
                raise Fail(f"{project} source field {k} changed from {src_before.get(k)!r} to "
                           f"{src_now.get(k)!r}; only the location may move")
        log("asserted every Source field except location is unchanged")
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED after mutating the project or the asset: {exc}")
        txn.unwind()
        raise
    got = live_loc()
    if got != want_loc:
        saved = load_state("codebuild")
        aws("codebuild", "update-project", "--name", project,
            "--source", json.dumps(_saved_source(saved)))
        raise Fail(f"source readback {got} != {want_loc}; reverted")
    if not role_allows_key():
        saved = load_state("codebuild")
        aws("codebuild", "update-project", "--name", project,
            "--source", json.dumps(_saved_source(saved)))
        raise Fail(
            "the build role cannot read the new asset key, so the project would fail its next "
            "build. Reverted the source. Widen the role's S3 read to the new key first — the "
            f"closure changes {role_lid} for exactly this reason."
        )
    receipt("GoldenImageBuilderCEF13562", f"source {load_state('codebuild')['before']} -> {got}")
    # The closure's only change to this policy is one Resource ARN: the old asset key becomes the
    # new one. Asserting the effect was not enough — when the grant is genuinely absent (the normal
    # case for a NEW key) there was nothing to do but fail. The ARN is now ADDED next to the old
    # one, which is idempotent, removes no existing permission, and is undone by restoring the
    # document this run read.
    if _wrote_role:
        receipt(role_lid,
                f"{_role_policy_name}: the new asset ARN was added alongside the old one and read "
                "back; simulate-principal-policy then returned allowed for the new key")
    else:
        noop(role_lid,
             f"{_role_policy_name} already granted the new asset key, so nothing was written; "
             "simulate-principal-policy confirmed the effective decision is allowed")


# ============================================== cw-drop-replication-lag-alarms
def _alarms_to_delete() -> list[str]:
    """Exact physical names from the closure — never a name prefix. A prefix that matches
    nothing would make a destructive delete succeed while changing nothing."""
    b, p = template(ORCH, "base"), template(ORCH, "patch")
    names = []
    for lid, r in b.items():
        if r.get("Type") != "AWS::CloudWatch::Alarm" or lid in p:
            continue
        n = r["Properties"].get("AlarmName")
        if not isinstance(n, str):
            raise Fail(f"{lid} has no literal AlarmName in the closure; refusing to guess")
        names.append(n)
    if not names:
        raise Fail("the closure deletes no alarm; this operation must not run")
    return sorted(names)


def _gate_no_replica_reads() -> None:
    param = env("EDGE_READ_REPLICA_PARAM")
    sw = aws("ssm", "get-parameter", "--name", param, "--query", "Parameter.Value",
             "--output", "text", allow_missing=True)
    if sw is None:
        raise Fail(f"cannot read {param}; refusing to delete an alarm on an unknown configuration")
    norm = str(sw).strip().lower()
    if norm != "false":
        raise Fail(
            f"{param}={sw!r}. This gate only proceeds on an EXPLICIT 'false'. Anything else — "
            "'true', an empty value, a typo, a JSON blob — means the configuration is not known "
            "to be replica-free, and these alarms may still be watching a real replica."
        )
    log(f"switch {param}={sw} (explicit false)")

    members = aws("elasticache", "describe-replication-groups",
                  "--replication-group-id", env("REDIS_REPLICATION_GROUP_ID"),
                  "--query", "ReplicationGroups[0].MemberClusters", "--output", "json",
                  parse_json=True) or []
    if not members:
        raise Fail("the replication group reports no member clusters; that is not a topology this "
                   "gate can reason about, so it fails closed")
    log(f"replication group member clusters: {len(members)} -> {members}")

    asg = env("EDGE_ASG")
    asg_ids = sorted((aws("autoscaling", "describe-auto-scaling-groups",
                          "--auto-scaling-group-names", asg,
                          "--query", "AutoScalingGroups[0].Instances[?LifecycleState=='InService'].InstanceId",
                          "--output", "json", parse_json=True) or []))
    if not asg_ids:
        raise Fail(f"the ASG {asg} reports no InService instance; the gate cannot prove that no "
                   "running edge reads a replica")
    given = sorted(x for x in os.environ.get("EDGE_INSTANCE_IDS", "").split() if x)
    if given and given != asg_ids:
        raise Fail(f"EDGE_INSTANCE_IDS {given} does not match the ASG's InService set {asg_ids}; "
                   "the list must be exhaustive, so it is taken from the ASG rather than trusted")
    log(f"checking every InService edge instance: {asg_ids}")

    # Read the coordinates the running edge is CONFIGURED with, not an install log. The install
    # log records what one past boot decided; a box reloaded since then can be serving different
    # coordinates, and a rotated log makes the check silently return nothing.
    probe = ("commands=grep -hoE \"(reader|redis)_(host|port)[^\\\"]*\\\"[^\\\"]+\\\"\" "
             "/usr/local/openresty/nginx/conf/nginx.conf | tr -d \\\" | tail -8; "
             "echo ---; grep -hoE \"ENGINE_REDIS[A-Z_]*=[^ ]+\" /etc/claw-edge.env 2>/dev/null | tail -8")
    for iid in asg_ids:
        cid = aws("ssm", "send-command", "--instance-ids", iid,
                  "--document-name", "AWS-RunShellScript",
                  "--parameters", probe,
                  "--query", "Command.CommandId", "--output", "text")
        out = None
        for _ in range(12):
            time.sleep(5)
            # InvocationDoesNotExist is normal while the command is still dispatching, so this
            # read tolerates not-found only; a permission error must not look like "still running"
            # forever and then fall through as an empty result.
            inv = aws("ssm", "get-command-invocation", "--command-id", cid, "--instance-id", iid,
                      "--output", "json", parse_json=True, allow_missing=True)
            if inv and inv.get("Status") in ("Success", "Failed", "TimedOut", "Cancelled"):
                out = inv
                break
        if not out or out.get("Status") != "Success":
            raise Fail(f"could not read the effective Redis target from {iid} "
                       f"(status={out.get('Status') if out else 'no response'}); the gate fails closed")
        text = (out.get("StandardOutputContent") or "").strip()
        hosts = {}
        for line in text.splitlines():
            for role in ("reader", "redis", "primary"):
                if f"{role}_host" in line or f"{role.upper()}_HOST" in line:
                    val = line.split("=", 1)[-1].strip().strip(chr(34)).strip()
                    if val:
                        hosts.setdefault("reader" if role == "reader" else "primary", val)
        reader, primary = hosts.get("reader"), hosts.get("primary")
        log(f"  {iid}: configured reader={reader} primary={primary}")
        if not reader or not primary:
            raise Fail(f"{iid} did not report BOTH a configured reader host and a primary host "
                       f"(got {hosts}); the gate cannot prove it is not reading a replica, so it "
                       "fails closed")
        if reader.split(":")[0].lower() != primary.split(":")[0].lower():
            raise Fail(f"{iid} is configured with reader {reader}, which is not its primary "
                       f"{primary}: a running edge IS reading a replica. Do not delete the alarms.")
    log("every InService edge resolves its reader to the primary")


ALARM_COMPARED = (
    "AlarmDescription", "MetricName", "Namespace", "Statistic", "Period", "EvaluationPeriods",
    "Threshold", "ComparisonOperator", "TreatMissingData", "DatapointsToAlarm",
    "ExtendedStatistic", "Unit", "EvaluateLowSampleCountPercentile", "ActionsEnabled",
)
ALARM_LISTS = ("AlarmActions", "OKActions", "InsufficientDataActions")


def _recreate_alarms(saved_alarms: list[dict]) -> None:
    for a in saved_alarms:
        cmd = ["cloudwatch", "put-metric-alarm", "--alarm-name", a["AlarmName"]]
        for k in ALARM_COMPARED:
            v = a.get(k)
            if v in (None, ""):
                continue
            flag = "--" + "".join("-" + c.lower() if c.isupper() else c for c in k).lstrip("-")
            if isinstance(v, bool):
                cmd.append(flag if v else flag.replace("--", "--no-", 1))
            else:
                cmd += [flag, str(v)]
        if a.get("Dimensions"):
            cmd += ["--dimensions"] + [json.dumps(d) for d in a["Dimensions"]]
        for k in ALARM_LISTS:
            if a.get(k):
                flag = "--" + "".join("-" + c.lower() if c.isupper() else c for c in k).lstrip("-")
                cmd += [flag] + list(a[k])
        if a.get("Metrics"):
            cmd += ["--metrics", json.dumps(a["Metrics"])]
        aws(*cmd)


def _assert_alarms_restored(saved_alarms: list[dict]) -> None:
    """Compare the FULL definition, not a hand-picked subset. A rollback that only checks the
    threshold can silently drop the alarm's actions, which is the part that pages a human."""
    names = [a["AlarmName"] for a in saved_alarms]
    live = (aws("cloudwatch", "describe-alarms", "--alarm-names", *names, "--output", "json",
                parse_json=True) or {}).get("MetricAlarms", [])
    got = {m["AlarmName"]: m for m in live}
    problems = []
    for a in saved_alarms:
        m = got.get(a["AlarmName"])
        if not m:
            problems.append(f"{a['AlarmName']} was not recreated")
            continue
        for k in ALARM_COMPARED:
            if a.get(k) not in (None, "") and m.get(k) != a.get(k):
                problems.append(f"{a['AlarmName']}.{k}: {m.get(k)!r} != {a.get(k)!r}")
        for k in ALARM_LISTS:
            if sorted(a.get(k) or []) != sorted(m.get(k) or []):
                problems.append(f"{a['AlarmName']}.{k}: {m.get(k)} != {a.get(k)}")
        key = lambda ds: sorted(f"{d['Name']}={d['Value']}" for d in (ds or []))  # noqa: E731
        if key(a.get("Dimensions")) != key(m.get("Dimensions")):
            problems.append(f"{a['AlarmName']}.Dimensions: {m.get('Dimensions')} != {a.get('Dimensions')}")
        if json.dumps(a.get("Metrics") or [], sort_keys=True) != json.dumps(m.get("Metrics") or [], sort_keys=True):
            problems.append(f"{a['AlarmName']}.Metrics differs")
    if problems:
        raise Fail("restored alarms do not match the saved definitions: " + "; ".join(problems[:6]))
    log(f"asserted {len(saved_alarms)} alarm(s) restored with every compared field and action list intact")


def op_cw_drop_lag(mode: str) -> None:
    names = _alarms_to_delete()
    log(f"exact alarm names from the closure: {names}")

    if mode == "verify":
        live = aws("cloudwatch", "describe-alarms", "--alarm-names", *names,
                   "--query", "MetricAlarms[].AlarmName", "--output", "json", parse_json=True) or []
        if live:
            raise Fail(f"these alarms still exist: {live}")
        log("asserted both named alarms are absent")
        return

    if mode == "rollback":
        saved = load_state("cw-alarms")
        _recreate_alarms(saved["alarms"])
        _assert_alarms_restored(saved["alarms"])
        return

    # apply
    _gate_no_replica_reads()
    live = aws("cloudwatch", "describe-alarms", "--alarm-names", *names, "--output", "json",
               parse_json=True) or {}
    alarms = live.get("MetricAlarms", [])
    if not alarms:
        log("neither named alarm exists; nothing to delete")
        return
    if len(alarms) != len(names):
        found = [a["AlarmName"] for a in alarms]
        log(f"WARNING: only {found} of {names} exist; deleting exactly what exists")
    save_state("cw-alarms", {"alarms": alarms, "names": names})
    txn = Txn()
    try:
        aws("cloudwatch", "delete-alarms", "--alarm-names", *[a["AlarmName"] for a in alarms])
        txn.add(lambda: (_recreate_alarms(alarms), _assert_alarms_restored(alarms)),
                "recreate the alarms this attempt deleted")
        left = (aws("cloudwatch", "describe-alarms", "--alarm-names", *names, "--output", "json",
                    parse_json=True) or {}).get("MetricAlarms", [])
        if left:
            raise Fail(f"delete readback still shows {[a['AlarmName'] for a in left]}")
    except Exception as exc:  # noqa: BLE001 — a failed READ must also put the alarms back
        log(f"FAILED after deleting: {exc}")
        txn.unwind()
        raise
    by_name = {a["AlarmName"]: a for a in alarms}
    for lid, r in template(ORCH, "base").items():
        if r.get("Type") == "AWS::CloudWatch::Alarm" and lid not in template(ORCH, "patch"):
            n = r["Properties"]["AlarmName"]
            if n in by_name:
                receipt(lid, f"{n} deleted; full definition saved to {state_path('cw-alarms')}")



# --------------------------------------------------------------------------------------------
# host-init-bootstrap: the rendered init-host.sh in S3 + the Host LT version that requests it.
#
# Why this cannot ship a digest from the closure: ha_edge.py computes
#     _init_sha256 = sha256(rendered init_sh)
#     prefix       = f"deployment/bootstrap/host/{_init_sha256}"
# so the prefix IS the content digest of the script AFTER the ~31 {{PLACEHOLDER}} substitutions.
# The closure was synthesized from config.yml.example, so its digest belongs to example host
# reservations, rootfs prefix, egress CIDRs and so on. Installing that object would boot hosts
# configured for a different deployment. The expected digest can therefore only be COMPUTED, from
# this environment's own currently-served rendered script plus this range's change.
#
# The change arrives as a TEMPLATE-level unified diff (base template -> patch template). Applying
# it to a RENDERED file cannot use literal context matching: this range's single hunk has
# `OVERCOMMIT_BY_FAMILY={{OVERCOMMIT_BY_FAMILY}}` as a context line, which in the live script reads
# `OVERCOMMIT_BY_FAMILY={"r8g":…}`. So a context line containing `{{IDENT}}` matches any value
# there, and everything else must match literally. That is stricter than `patch --fuzz`, which
# would ignore context it cannot match; here an unmatched line is a hard stop.


_PLACEHOLDER_RE = re.compile(r"\{\{[A-Za-z_][A-Za-z0-9_.-]*\}\}")


def _template_line_pattern(line: str) -> "re.Pattern":
    """A rendered-file matcher for one template line: literal, except {{IDENT}} -> any value."""
    out, last = [], 0
    for m in _PLACEHOLDER_RE.finditer(line):
        out.append(re.escape(line[last:m.start()]))
        out.append(r".*")
        last = m.end()
    out.append(re.escape(line[last:]))
    return re.compile("^" + "".join(out) + "$")


def _parse_single_hunk(diff_text: str) -> tuple[list[str], list[str]]:
    """(before_lines, after_lines) for a unified diff that must contain exactly one hunk.

    One hunk is asserted, not assumed: multi-hunk replay needs per-hunk offset tracking, and
    silently handling only the first hunk would install a partially-patched script.
    """
    lines = diff_text.splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("@@")]
    if len(starts) != 1:
        raise Fail(f"expected exactly one hunk in the diff, found {len(starts)}; "
                   "this operation refuses to replay a multi-hunk change")
    before, after = [], []
    for l in lines[starts[0] + 1:]:
        if l.startswith("@@") or l.startswith("diff ") or l.startswith("--- ") or l.startswith("+++ "):
            break
        if not l:
            before.append("")
            after.append("")
            continue
        tag, body = l[0], l[1:]
        if tag == " ":
            before.append(body)
            after.append(body)
        elif tag == "-":
            before.append(body)
        elif tag == "+":
            after.append(body)
        elif tag == "\\":
            continue          # "\ No newline at end of file"
        else:
            raise Fail(f"unparsable diff line: {l[:80]!r}")
    if not before or not after:
        raise Fail("diff hunk has an empty side")
    added = [l for l in after if l not in before]
    stray = [l for l in added if _PLACEHOLDER_RE.search(l)]
    if stray:
        raise Fail("the diff ADDS lines containing template placeholders "
                   f"({stray[0].strip()[:70]}…). Nothing in this apply path substitutes them — "
                   "only a CDK synth does — so the host would run a literal {{TOKEN}}.")
    return before, after


def _replay_template_hunk(rendered: str, diff_text: str) -> str:
    before, after = _parse_single_hunk(diff_text)
    pats = [_template_line_pattern(b) for b in before]
    lines = rendered.splitlines()
    hits = []
    for i in range(0, len(lines) - len(before) + 1):
        if all(p.match(lines[i + j]) for j, p in enumerate(pats)):
            hits.append(i)
    if len(hits) != 1:
        raise Fail(f"the hunk's before-image matches the live rendered script {len(hits)} time(s); "
                   "exactly one match is required. 0 means the live script is not the base this "
                   "diff was cut from; more than 1 means the anchor is ambiguous.")
    at = hits[0]
    # Carry each rendered value through: an unchanged context line keeps the LIVE text, never the
    # template text, or the replay would overwrite real values with {{PLACEHOLDER}}.
    live_ctx = {}
    for j, b in enumerate(before):
        live_ctx.setdefault(b, lines[at + j])
    rebuilt = [live_ctx.get(a, a) for a in after]
    out = lines[:at] + rebuilt + lines[at + len(before):]
    text = "\n".join(out)
    if rendered.endswith("\n"):
        text += "\n"
    if _PLACEHOLDER_RE.search("\n".join(rebuilt)):
        raise Fail("the rebuilt region still contains a template placeholder; refusing to upload")
    return text


def _host_asg_pin() -> tuple[str, str, str]:
    """(launch_template_id, pinned_version, shape) for the host ASG.

    `shape` is "direct" when the ASG carries a LaunchTemplate of its own, or "mip" when the pin
    lives inside a MixedInstancesPolicy. The two are repointed by DIFFERENT API shapes, and
    guessing one would silently no-op on the other — which is exactly how "promoted" can be
    reported while every new host still launches on the old version.
    """
    asg = env("HOST_ASG")
    g = (aws("autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names", asg,
             "--output", "json", parse_json=True) or {}).get("AutoScalingGroups", [])
    if len(g) != 1:
        raise Fail(f"{asg}: expected exactly one ASG, got {len(g)}")
    direct = g[0].get("LaunchTemplate") or {}
    mip = ((g[0].get("MixedInstancesPolicy") or {}).get("LaunchTemplate") or {}
           ).get("LaunchTemplateSpecification") or {}
    if direct.get("LaunchTemplateId") and mip.get("LaunchTemplateId"):
        raise Fail(f"{asg} declares both a direct LaunchTemplate and a MixedInstancesPolicy one; "
                   "refusing to choose")
    spec, shape = (direct, "direct") if direct.get("LaunchTemplateId") else (mip, "mip")
    if not spec.get("LaunchTemplateId"):
        raise Fail(f"{asg} does not pin a Launch Template by id")
    lt_id = spec["LaunchTemplateId"]
    ver = str(spec.get("Version", "") or "").strip()
    # MEASURED on the test fleet: openclaw-hosts-asg pins "$Default" while openclaw-edge-asg pins
    # "7". The two need different rollback anchors, so the shape is resolved here rather than
    # assumed anywhere downstream.
    if ver == "$Latest":
        raise Fail(
            f"{asg} pins $Latest, which has no stable anchor: a new Launch Template version becomes "
            "live for new instances the instant it is created, before anything can be verified. "
            "Point the ASG at $Default or at a version number before running this operation."
        )
    if ver in ("$Default", ""):
        # The ASG follows the template's default pointer, so moving that pointer IS the promote and
        # rollback is moving it back. Resolve the concrete version so every readback below compares
        # against the version actually in effect.
        concrete = str((aws("ec2", "describe-launch-templates", "--launch-template-ids", lt_id,
                            "--query", "LaunchTemplates[0].DefaultVersionNumber",
                            "--output", "text") or "").strip())
        if not concrete or concrete == "None":
            raise Fail(f"{lt_id} has no readable DefaultVersionNumber, so {asg}'s $Default pin "
                       "cannot be resolved to a concrete version")
        log(f"{asg} pins $Default -> currently version {concrete}; promoting means moving the "
            "template default, and rollback means moving it back")
        return lt_id, concrete, "default-pointer"
    return lt_id, ver, shape


def _repoint_host_asg(lt_id: str, version: str, shape: str) -> None:
    """Point the ASG at `version`, then read it back and assert it took.

    Three shapes, because the write differs and a wrong one is accepted by the API while changing
    nothing: `default-pointer` (the ASG follows $Default, so the TEMPLATE default is what moves),
    `direct`, and `mip` (the pin lives inside a MixedInstancesPolicy).
    """
    asg = env("HOST_ASG")
    if shape == "default-pointer":
        aws("ec2", "modify-launch-template", "--launch-template-id", lt_id,
            "--default-version", str(version))
        got_id, got_ver, got_shape = _host_asg_pin()
        assert_eq(got_id, lt_id, f"{asg} launch template id after moving the default")
        assert_eq(got_ver, str(version), f"{asg} effective version after moving the default")
        assert_eq(got_shape, "default-pointer", f"{asg} pin shape after moving the default")
        return
    if shape == "direct":
        aws("autoscaling", "update-auto-scaling-group", "--auto-scaling-group-name", asg,
            "--launch-template", f"LaunchTemplateId={lt_id},Version={version}")
    else:
        # A MixedInstancesPolicy update must carry the policy object; sending --launch-template
        # here is accepted by the API and changes nothing, which is the silent no-op to avoid.
        g = (aws("autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names", asg,
                 "--output", "json", parse_json=True) or {}).get("AutoScalingGroups", [])
        if len(g) != 1:
            raise Fail(f"{asg}: expected exactly one ASG on repoint, got {len(g)}")
        policy = json.loads(json.dumps(g[0].get("MixedInstancesPolicy") or {}))
        if not policy:
            raise Fail(f"{asg} lost its MixedInstancesPolicy between read and write")
        policy["LaunchTemplate"]["LaunchTemplateSpecification"]["Version"] = str(version)
        policy["LaunchTemplate"]["LaunchTemplateSpecification"]["LaunchTemplateId"] = lt_id
        policy["LaunchTemplate"]["LaunchTemplateSpecification"].pop("LaunchTemplateName", None)
        aws("autoscaling", "update-auto-scaling-group", "--auto-scaling-group-name", asg,
            "--mixed-instances-policy", json.dumps(policy))
    got_id, got_ver, got_shape = _host_asg_pin()
    assert_eq(got_id, lt_id, f"{asg} launch template id after repoint")
    assert_eq(got_ver, str(version), f"{asg} pinned launch template version after repoint")
    assert_eq(got_shape, shape, f"{asg} pin shape after repoint")


def _host_lt_id() -> tuple[str, str]:
    lt_id, ver, _shape = _host_asg_pin()
    return lt_id, ver


def _lt_userdata(lt_id: str, version: str) -> str:
    v = (aws("ec2", "describe-launch-template-versions", "--launch-template-id", lt_id,
             "--versions", version, "--output", "json", parse_json=True) or {})
    vs = v.get("LaunchTemplateVersions", [])
    if len(vs) != 1:
        raise Fail(f"{lt_id} v{version}: expected one version, got {len(vs)}")
    ud = (vs[0].get("LaunchTemplateData") or {}).get("UserData")
    if not ud:
        raise Fail(f"{lt_id} v{version} has no user data")
    return base64.b64decode(ud).decode("utf-8", "replace")


_BOOTSTRAP_KEY_RE = re.compile(r"deployment/bootstrap/host/([0-9a-f]{64})/init-host\.sh")


def _requested_sha(userdata: str) -> str:
    found = set(_BOOTSTRAP_KEY_RE.findall(userdata))
    if len(found) != 1:
        raise Fail(f"the live user data references {len(found)} distinct bootstrap digests; "
                   "exactly one is required")
    return found.pop()


def _hunk_after_lines(diff_text: str) -> tuple[list[str], list[str]]:
    """(added, removed) content lines of the single hunk, with template tokens stripped out.

    These are the AFTER-image fingerprint. Asserting on them is convergent: it holds the moment the
    change is in place and keeps holding, unlike replaying the diff, which by construction fails
    once the script is already patched.
    """
    before, after = _parse_single_hunk(diff_text)
    added = [l for l in after if l not in before]
    removed = [l for l in before if l not in after]
    # A line carrying a placeholder is rendered per-environment, so its literal text is not a
    # fingerprint. Drop it rather than assert on something that legitimately differs.
    added = [l for l in added if not _PLACEHOLDER_RE.search(l) and l.strip()]
    removed = [l for l in removed if not _PLACEHOLDER_RE.search(l) and l.strip()]
    if not added and not removed:
        raise Fail("the diff has no placeholder-free added or removed line, so there is no "
                   "after-image fingerprint to verify against")
    return added, removed


def op_host_init(mode: str) -> None:
    """MANUAL_CLI_REVIEW. plan computes and prints; a human runs the writes; verify asserts.

    apply and rollback deliberately refuse. The writes are a new content-addressed object, a new
    Launch Template version and an ASG repoint — fleet-level changes whose correctness depends on
    facts this tool cannot see (whether the pinned version's AMI/IAM/networking is still the one
    the fleet should run). Printing them and stopping is the honest boundary.
    """
    if mode in ("apply", "rollback"):
        # FIRST, before reading anything. A refusal that depends on a successful ASG read reports an
        # unrelated error in a degraded environment and hides that this operation is manual.
        raise Fail(
            f"host-init-bootstrap does not run {mode} automatically. It is MANUAL_CLI_REVIEW: the "
            "writes are a new bootstrap object, a new Launch Template version and an ASG repoint, "
            "and whether the pinned version is still the right base (AMI, IAM instance profile, "
            "security groups, subnets) is not something this tool can see. Run `plan` — it computes "
            "the digest, which can only come from this environment's own served script, prints the "
            "exact commands, and writes nothing. Execute them yourself, then run `verify`. Rollback "
            "is the printed counter-command: repoint the ASG back to the version it pins today "
            "(plan prints that id and version). The old prefix stays addressable because the "
            "deployment is prune=False / retain_on_delete=True, so nothing needs deleting."
        )

    diff_path = DIFF if DIFF is not None else Path("lib/init-host.sh.diff")
    if not diff_path.is_file():
        raise Fail(f"{diff_path} not found; the operation needs the template diff")
    bucket = env("ASSETS_BUCKET")
    lt_id, pinned_ver, shape = _host_asg_pin()
    pinned_ud = _lt_userdata(lt_id, pinned_ver)
    cur_sha = _requested_sha(pinned_ud)
    cur_key = f"deployment/bootstrap/host/{cur_sha}/init-host.sh"
    log(f"host ASG pins {lt_id} v{pinned_ver} ({shape} shape), which requests {cur_sha[:16]}…")

    with tempfile.TemporaryDirectory() as td:
        if mode == "verify":
            # After-image assertions only. No replay: replaying a diff onto an already-patched
            # script cannot match, so a verify built on replay could never pass after a successful
            # apply — it would report FAIL on a correctly applied change.
            served_key = f"deployment/bootstrap/host/{cur_sha}/init-host.sh"
            got = Path(td) / "served-init-host.sh"
            aws("s3", "cp", f"s3://{bucket}/{served_key}", str(got), "--only-show-errors")
            if sha256_file(got) != cur_sha:
                raise Fail(f"{served_key} hashes to {sha256_file(got)} but its own prefix says "
                           f"{cur_sha}; the served object and its key disagree")
            text = got.read_text()
            added, removed = _hunk_after_lines(diff_path.read_text())
            missing = [l for l in added if l not in text]
            leftover = [l for l in removed if l in text]
            if missing or leftover:
                raise Fail(
                    "the script the ASG-pinned version requests is NOT the patched one: "
                    f"{len(missing)} added line(s) absent (first: "
                    f"{(missing[0].strip()[:70] if missing else '-')!r}), "
                    f"{len(leftover)} removed line(s) still present (first: "
                    f"{(leftover[0].strip()[:70] if leftover else '-')!r})"
                )
            log(f"verified against the version the ASG PINS (v{pinned_ver}), not $Default: "
                f"{len(added)} added line(s) present, {len(removed)} removed line(s) gone, and the "
                "object's own sha256 equals the digest in its key and in the user data")
            return

        # plan — read-only
        live = Path(td) / "live-init-host.sh"
        aws("s3", "cp", f"s3://{bucket}/{cur_key}", str(live), "--only-show-errors")
        got = sha256_file(live)
        if got != cur_sha:
            raise Fail(f"{cur_key} hashes to {got}, but its own prefix says {cur_sha}. The live "
                       "object and the key it lives under disagree; stopping before computing "
                       "anything from it.")
        log("asserted the served object's sha256 equals the digest in its key and in the user data")

        rendered = live.read_text()
        patched = _replay_template_hunk(rendered, diff_path.read_text())
        new_sha = hashlib.sha256(patched.encode("utf-8")).hexdigest()
        new_key = f"deployment/bootstrap/host/{new_sha}/init-host.sh"
        if new_sha == cur_sha:
            log("PLAN: nothing to do — replaying the diff produces a byte-identical script, so the "
                "change is already what the fleet serves.")
            return

        WORK_DIR.mkdir(parents=True, exist_ok=True)
        keep = WORK_DIR / f"init-host.sh.rendered.{new_sha[:12]}"
        keep.write_text(patched)
        new_ud = pinned_ud.replace(cur_sha, new_sha)
        if new_ud == pinned_ud or new_ud.count(new_sha) != pinned_ud.count(cur_sha):
            raise Fail("substituting the digest in the pinned user data did not produce the same "
                       "occurrence count; refusing to print a command built on it")
        if "{{" in new_ud:
            raise Fail("the re-baked user data contains an unresolved template token")
        ud_path = WORK_DIR / f"userdata.{new_sha[:12]}.b64"
        ud_path.write_text(base64.b64encode(new_ud.encode("utf-8")).decode())

        print("")
        print("=" * 78)
        print("PLAN — nothing above this line wrote anything. Read every command, then run them.")
        print("=" * 78)
        print(f"  current: ASG pins {lt_id} v{pinned_ver} ({shape}), requesting {cur_sha}")
        print(f"  target : {new_sha}")
        print(f"  wrote locally (NOT uploaded, outside the kit):")
        print(f"      {keep}  ({len(patched)} bytes)")
        print(f"      {ud_path}  (base64 user data)")
        print("")
        print("1. upload the rendered script to its content-addressed key")
        print(f"   aws s3 cp {keep} s3://{bucket}/{new_key} --region {REGION}")
        print(f"   aws s3api head-object --bucket {bucket} --key {new_key} --region {REGION}")
        print("   # then confirm the object's own digest equals its key:")
        print(f"   aws s3 cp s3://{bucket}/{new_key} - --region {REGION} | shasum -a 256")
        print(f"   # must print {new_sha}")
        print("")
        print("2. cut a Launch Template version FROM THE PINNED ONE, changing only the user data")
        print(f"   aws ec2 create-launch-template-version --launch-template-id {lt_id} \\")
        print(f"     --source-version {pinned_ver} \\")
        print(f"     --launch-template-data \"{{\\\"UserData\\\":\\\"$(cat {ud_path})\\\"}}\" \\")
        print(f"     --region {REGION}")
        print("   # read the new version back and confirm ONLY the digest moved:")
        print(f"   aws ec2 describe-launch-template-versions --launch-template-id {lt_id} \\")
        print(f"     --versions <NEW> --region {REGION} \\")
        print("     --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \\")
        print("     | base64 -d | grep -c " + new_sha)
        print("")
        print("3. BEFORE repointing, compare the pinned version against the new one field by field.")
        print("   This is the step that cannot be automated safely: the pinned version may carry an")
        print("   AMI, IAM instance profile, security groups or subnets that are no longer what the")
        print("   fleet should launch with, and repointing would silently adopt them.")
        print(f"   aws ec2 describe-launch-template-versions --launch-template-id {lt_id} \\")
        print(f"     --versions {pinned_ver} <NEW> --region {REGION} --output json")
        print("")
        print("4. repoint the ASG (this is what new hosts read — the template default alone does")
        print("   NOT change what a version-pinned ASG launches)")
        if shape == "default-pointer":
            print("   # this ASG pins $Default, so moving the TEMPLATE default IS the promote:")
            print(f"   aws ec2 modify-launch-template --launch-template-id {lt_id} \\")
            print(f"     --default-version <NEW> --region {REGION}")
            print("   # confirm the effective version followed:")
            print(f"   aws ec2 describe-launch-templates --launch-template-ids {lt_id} \\")
            print(f"     --region {REGION} \\")
            print("     --query 'LaunchTemplates[0].DefaultVersionNumber' --output text")
        elif shape == "direct":
            print(f"   aws autoscaling update-auto-scaling-group --auto-scaling-group-name {env('HOST_ASG')} \\")
            print(f"     --launch-template LaunchTemplateId={lt_id},Version=<NEW> --region {REGION}")
        else:
            print("   # this ASG pins through a MixedInstancesPolicy: read the policy, change ONLY")
            print("   # LaunchTemplate.LaunchTemplateSpecification.Version, and send the whole object back.")
            print(f"   aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names {env('HOST_ASG')} \\")
            print(f"     --region {REGION} --query 'AutoScalingGroups[0].MixedInstancesPolicy' --output json > mip.json")
            print("   # edit mip.json, then:")
            print(f"   aws autoscaling update-auto-scaling-group --auto-scaling-group-name {env('HOST_ASG')} \\")
            print(f"     --mixed-instances-policy file://mip.json --region {REGION}")
        print("")
        print("5. STOP HERE. Do not issue an instance refresh. Running hosts keep serving; only")
        print("   newly launched hosts read the new script. Replacing the fleet is a separate call.")
        print("")
        print("6. verify (automated, convergent — safe to re-run):")
        print(f"   bash lib/apply-resource-ops.sh host-init-bootstrap verify {CLOSURE} {REGION} "
              f"{diff_path}")
        print("")
        _how = (" (move the template default back)" if shape == "default-pointer" else "")
        print(f"ROLLBACK is step 4 pointing back at version {pinned_ver}{_how}. Leave the object")
        print("in place: deleting it would break any host that already launched on the new version.")
        print("=" * 78)


OPS = {
    "host-init-bootstrap": op_host_init,
    "iam-edge-putmetricdata": op_iam,
    "lambda-api-code": op_lambda_code,
    "lambda-api-alias": op_lambda_alias,
    "ssm-deadline-params": op_ssm_deadlines,
    "s3-edge-bundle": op_s3_edge_bundle,
    "s3-obs-assets": op_s3_obs,
    "codebuild-golden-image": op_codebuild,
    "cw-drop-replication-lag-alarms": op_cw_drop_lag,
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("op", choices=sorted(OPS))
    # `plan` is read-only and exists for host-init-bootstrap, whose writes are fleet-level and
    # stay in human hands (MANUAL_CLI_REVIEW).
    ap.add_argument("mode", choices=["apply", "verify", "rollback", "gate", "plan"])
    ap.add_argument("closure")
    ap.add_argument("region")
    # host-init-bootstrap replays a TEMPLATE-level diff onto the live rendered script, so it needs
    # that file named on the command line (which is also how artifact_refs can inventory it).
    ap.add_argument("diff", nargs="?", default=None)
    a = ap.parse_args()
    DIFF = Path(a.diff) if a.diff else None
    # plan/verify only. apply and rollback must REACH the operation so it can print the manual
    # instructions; requiring a diff here made that refusal unreachable, and the shell front rejects
    # a diff for those modes — the two entry points disagreed.
    if a.op == "host-init-bootstrap" and a.mode in ("plan", "verify") and DIFF is None:
        sys.exit("host-init-bootstrap plan/verify requires the template diff path "
                 "(lib/init-host.sh.diff)")
    OP, MODE, REGION = a.op, a.mode, a.region
    CLOSURE = Path(a.closure)
    RECEIPT = os.environ.get("OC_RECEIPT_FILE", "cfn-verify-receipt.txt")
    RUN_ID = os.environ.get("OC_RUN_ID", "").strip()
    if not RUN_ID:
        sys.exit("OC_RUN_ID must be set to a value unique to this apply run, so a rollback "
                 "restores this run's state and never a stale file from an earlier one.")
    if not CLOSURE.is_dir():
        sys.exit(f"no closure directory {CLOSURE}")
    if a.mode == "gate" and a.op != "iam-edge-putmetricdata":
        sys.exit("gate mode exists only for iam-edge-putmetricdata")
    if a.mode == "plan" and a.op != "host-init-bootstrap":
        sys.exit("plan mode exists only for host-init-bootstrap")
    try:
        OPS[a.op](a.mode)
    except Fail as e:
        sys.exit(f"FATAL[{a.op}/{a.mode}]: {e}")
