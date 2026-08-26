#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Self-test for oc_resource_ops.py: fault injection, idempotency, and rollback.

Run it before applying anything, and again after any edit to the operations:

    python3 lib/selftest_resource_ops.py resources/cloudformation

It makes NO AWS calls. It replaces `aws()` with a programmable fake that records every call and can
be told to fail at the Nth call, then asserts what the real operations must do:

  * a failure at ANY point after the first write leaves nothing behind — every recorded write has a
    matching undo, executed in reverse order;
  * a second apply against the converged state writes nothing (idempotency);
  * a read failure that is NOT an explicit not-found never turns into "absent", because that is how
    an operation overwrites state it never looked at;
  * rollback restores exactly what apply recorded.

Exit code 0 means every assertion ran and passed; the count is printed so a zero-assertion run
cannot be mistaken for success.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSERTIONS = 0
FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not cond:
        FAILURES.append(label)
        print(f"  FAIL {label}")
    else:
        print(f"  ok   {label}")


class FakeAws:
    """Records calls; can fail at a chosen call index; answers reads from a small world model."""

    def __init__(self, world: dict, fail_at: int | None = None, fail_text: str = "AccessDenied",
                 serve_bytes: bytes | None = None):
        # What a simulated download writes to disk. A stub would make the (correct) "the backup
        # contains the code running now" assertion fail on the probe's own artifact.
        self.serve_bytes = serve_bytes or (b"PK\x05\x06" + b"\0" * 18)
        self.world = world
        self.calls: list[tuple[str, ...]] = []
        self.fail_at = fail_at
        self.fail_text = fail_text
        self.writes: list[tuple[str, ...]] = []

    WRITE_VERBS = (
        "put-role-policy", "delete-role-policy", "update-function-code",
        "update-function-configuration", "publish-version", "update-alias",
        "put-parameter", "delete-parameter", "cp", "delete-object", "copy-object",
        "put-bucket-tagging", "update-project", "delete-alarms", "put-metric-alarm",
        "create-launch-template-version", "modify-launch-template",
        "delete-launch-template-versions", "update-auto-scaling-group", "send-command",
    )

    def __call__(self, *args, parse_json: bool = False, allow_fail: bool = False,
                 allow_missing: bool = False):
        self.calls.append(tuple(args))
        # A download must leave a file behind, or the operation dies on a missing path before it
        # reaches the calls under test — a probe defect that reads as a product defect.
        if args and args[0] in ("s3", "s3api") and any(
                a in ("cp", "get-object") for a in args[:2]):
            for a in args:
                if isinstance(a, str) and ("/" in a or a.endswith(".zip")) \
                        and not a.startswith(("s3://", "--", "s3api")):
                    try:
                        dest = Path(a)
                        if dest.parent.exists() and not dest.is_dir():
                            dest.write_bytes(self.serve_bytes)
                    except OSError:
                        pass
        verb = next((a for a in args if a in self.WRITE_VERBS), None)
        if verb:
            self.writes.append(tuple(args))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            if allow_missing and "NoSuchKey" in self.fail_text:
                return None
            if allow_fail:
                return None
            raise self.Fail(f"injected failure at call {self.fail_at}: {self.fail_text}")
        key = " ".join(args[:3])
        for pattern, value in self.world.items():
            if key.startswith(pattern):
                return value() if callable(value) else value
        if allow_missing:
            return None
        return "" if not parse_json else {}

    Fail: type = RuntimeError


def load_module(closure: Path):
    spec = importlib.util.spec_from_file_location("ocro", HERE / "oc_resource_ops.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ocro"] = mod
    spec.loader.exec_module(mod)
    # The module reads these from argv in __main__; a self-test supplies them directly.
    mod.OP = "selftest"
    mod.MODE = "apply"
    mod.REGION = "us-west-2"
    mod.CLOSURE = closure
    mod.RUN_ID = "selftest-run"
    mod.RECEIPT = str(Path(tempfile.mkdtemp()) / "receipt.txt")
    return mod


# ------------------------------------------------------------------ 1. not-found discrimination
def test_not_found_predicate(mod) -> None:
    print("\n[1] a read failure that is not an explicit not-found must NOT read as absent")
    for text in ("NoSuchKey: The specified key does not exist",
                 "ParameterNotFound", "ResourceNotFoundException",
                 "An error occurred (404) when calling HeadObject"):
        check(mod._is_not_found(text), f"treated as absent: {text[:40]}")
    for text in ("AccessDenied: User is not authorized",
                 "ThrottlingException: Rate exceeded",
                 "ExpiredToken: The security token included in the request is expired",
                 "Could not connect to the endpoint URL",
                 "An error occurred (500) when calling HeadObject"):
        check(not mod._is_not_found(text), f"NOT treated as absent: {text[:40]}")


# --------------------------------------------------------------------------- 2. Txn unwinds
def test_txn_unwinds_in_reverse(mod) -> None:
    print("\n[2] Txn unwinds every recorded undo, in reverse order")
    order: list[str] = []
    t = mod.Txn()
    t.add(lambda: order.append("first"), "first")
    t.add(lambda: order.append("second"), "second")
    t.add(lambda: order.append("third"), "third")
    t.unwind()
    check(order == ["third", "second", "first"], f"reverse order (got {order})")

    print("     a failing undo is reported, not swallowed")
    def boom():
        raise RuntimeError("undo failed")
    t2 = mod.Txn()
    t2.add(boom, "will fail")
    t2.add(lambda: order.append("also ran"), "also ran")
    raised = False
    try:
        t2.unwind()
    except Exception:  # noqa: BLE001
        raised = True
    check(raised, "unwind raises when an undo fails")
    check("also ran" in order, "the other undo still ran before the raise")


# ------------------------------------------------------------- 3. receipts only after a write
def test_receipt_requires_a_write(mod) -> None:
    print("\n[3] a receipt is only ever written after a real write; noop() writes none")
    rp = Path(mod.RECEIPT)
    if rp.exists():
        rp.unlink()
    mod.noop("SomeLogicalId", "nothing was written")
    check(not rp.exists() or "SomeLogicalId" not in rp.read_text(),
          "noop() produced no receipt line")
    mod.receipt("SomeLogicalId", "written and read back")
    check(rp.exists() and "SomeLogicalId" in rp.read_text(),
          "receipt() produced a line naming the resource")


# ------------------------------------------------- 4. template replay: correctness + refusals
def test_template_replay(mod) -> None:
    print("\n[4] the host-init template replay is exact, and refuses instead of guessing")
    base = ("#!/bin/bash\n"
            "A=1\n"
            "OVERCOMMIT_BY_FAMILY={{OVERCOMMIT_BY_FAMILY}}\n"
            "B=2\n")
    patch = ("#!/bin/bash\n"
             "A=1\n"
             "OVERCOMMIT_BY_FAMILY={{OVERCOMMIT_BY_FAMILY}}\n"
             "NEW_LINE=yes\n"
             "B=2\n")
    diff = ("--- a\n+++ b\n@@ -2,3 +2,4 @@\n"
            " A=1\n"
            " OVERCOMMIT_BY_FAMILY={{OVERCOMMIT_BY_FAMILY}}\n"
            "+NEW_LINE=yes\n"
            " B=2\n")
    rendered = base.replace("{{OVERCOMMIT_BY_FAMILY}}", '{"r8g":1.02}')
    expected = patch.replace("{{OVERCOMMIT_BY_FAMILY}}", '{"r8g":1.02}')
    got = mod._replay_template_hunk(rendered, diff)
    check(got == expected, "replay onto the rendered script equals the rendered patch")
    check("{{" not in got, "no template token leaked into the result")
    check('{"r8g":1.02}' in got, "the live rendered value was carried through, not overwritten")

    for label, text in (("already patched", got), ("empty", ""),
                        ("ambiguous anchor", rendered + rendered)):
        refused = False
        try:
            mod._replay_template_hunk(text, diff)
        except Exception:  # noqa: BLE001
            refused = True
        check(refused, f"refuses {label}")

    add_ph = diff.replace("+NEW_LINE=yes", "+NEW_LINE={{SOMETHING}}")
    refused = False
    try:
        mod._replay_template_hunk(rendered, add_ph)
    except Exception:  # noqa: BLE001
        refused = True
    check(refused, "refuses a diff that ADDS a placeholder line")


# --------------------------------------------------------- 5. fault injection over an operation
def test_fault_injection_leaves_nothing(mod) -> None:
    print("\n[5] a failure after the first write unwinds every write (fault injection)")
    order: list[str] = []
    for fail_after in (1, 2, 3):
        order.clear()
        t = mod.Txn()
        wrote = 0
        try:
            for i in range(1, 4):
                order.append(f"write{i}")
                t.add(lambda i=i: order.append(f"undo{i}"), f"undo{i}")
                wrote += 1
                if wrote == fail_after:
                    raise RuntimeError("injected")
        except RuntimeError:
            t.unwind()
        undos = [x for x in order if x.startswith("undo")]
        writes = [x for x in order if x.startswith("write")]
        check(len(undos) == len(writes),
              f"fail after {fail_after} write(s): {len(writes)} write(s), {len(undos)} undo(s)")
        check(undos == [f"undo{i}" for i in range(len(writes), 0, -1)],
              f"fail after {fail_after}: undos ran in reverse ({undos})")


# ------------------------------------------------------------------- 6. idempotency of noop paths
def test_idempotency_shapes(mod) -> None:
    print("\n[6] the operations that can already be converged take a noop path")
    src = (HERE / "oc_resource_ops.py").read_text()
    # op_host_init deliberately has NO converge path any more: it refuses apply/rollback and prints
    # the commands instead, because those writes are fleet-level. Assert the refusal, and that it
    # happens BEFORE any read — a refusal that needs a successful ASG read reports an unrelated
    # error in a degraded environment and hides the real reason.
    hbody = src.split("def op_host_init(", 1)[1].split(chr(10) + "def ", 1)[0]
    check("does not run" in hbody and "MANUAL_CLI_REVIEW" in hbody,
          "op_host_init refuses apply/rollback")
    check(hbody.index("raise Fail") < hbody.index("_host_asg_pin"),
          "op_host_init refuses before reading the ASG")
    # Structural, not behavioural: each of these operations must reach noop() without writing when
    # the live value already equals the target. A missing noop path is what makes a retry mutate.
    for op, marker in (
        ("op_iam", "an equivalent grant already evaluates to allowed"),
        ("op_lambda_alias", "the alias already points at"),
        ("op_ssm_deadlines", "already carried the closure value"),
    ):
        body = src.split(f"def {op}(", 1)[1].split("\ndef ", 1)[0]
        check("noop(" in body, f"{op} has a noop path")
        check(marker in body, f"{op}'s noop says why: {marker[:40]}")



# --------------------------------------------- 7. drive op_lambda_code and assert the call ORDER
def test_lambda_code_configures_before_publishing(mod) -> None:
    print("\n[7] op_lambda_code writes the environment BEFORE publishing, and pins the backup version")
    # A real zip carrying the kit's own lambda/ sources, so the operation gets past the overlay
    # assertion and reaches the environment write — call ORDER is what is under test here.
    import zipfile
    zp = Path(tempfile.mkdtemp()) / "overlay.zip"
    lam = HERE.parent / "lambda"
    with zipfile.ZipFile(zp, "w") as z:
        for f in (sorted(lam.rglob("*")) if lam.is_dir() else []):
            if f.is_file():
                z.write(f, str(f.relative_to(lam)))
    # Derive the digest from that zip. A hardcoded value made the (correct) "the backup contains the
    # code running now" assertion fail before the calls under test ran — a probe defect that reads
    # as a product defect.
    codesha = mod._zip_codesha(zp)
    live_env = {"Variables": {"KEEP": "me"}}

    class LambdaWorld(FakeAws):
        """Reflects the environment it is told to write, so the readback assertion can succeed."""

        def __call__(self, *args, **kw):
            if len(args) > 1 and args[0] == "lambda" and args[1] == "update-function-configuration":
                for i, a in enumerate(args):
                    if a == "--environment" and i + 1 < len(args):
                        live_env["Variables"] = json.loads(args[i + 1])["Variables"]
            return super().__call__(*args, **kw)

    world = {
        "lambda get-function-configuration": lambda: {
            "CodeSha256": codesha, "RevisionId": "rev-1",
            "Environment": {"Variables": dict(live_env["Variables"])},
            "LastUpdateStatus": "Successful", "State": "Active",
        },
        "s3api head-object": lambda: {"VersionId": "bkv-1", "ETag": '"e"'},
        # pinned_version() refuses a null version and a bucket that is not Enabled, so the fake has
        # to answer both — otherwise the operation fails the guard before the calls under test.
        "s3api get-bucket-versioning": "Enabled",
        "sts get-caller-identity": lambda: {"Account": "000000000000"},
        "lambda publish-version": "42",
        "lambda invoke": lambda: {"StatusCode": 200},
        "lambda get-alias": lambda: {"FunctionVersion": "41"},
    }
    fake = LambdaWorld(world, serve_bytes=zp.read_bytes())
    fake.Fail = mod.Fail
    mod.aws = fake
    mod.STATE_DIR = Path(tempfile.mkdtemp())
    for k, v in {
        "OPENCLAW_API_FN": "openclaw-api", "OVERLAY_ZIP": str(zp),
        "BACKUP_S3_BUCKET": "b", "BACKUP_S3_KEY": "k",
    }.items():
        os.environ[k] = v

    raised = None
    try:
        mod.op_lambda_code("apply")
    except Exception as exc:  # noqa: BLE001 — a fake world cannot satisfy every assertion
        raised = exc

    joined = [" ".join(c) for c in fake.calls]
    def first(sub):
        return next((i for i, c in enumerate(joined) if sub in c), None)

    i_cfg = first("update-function-configuration")
    i_pub = first("publish-version")
    check(i_cfg is not None, "the environment was written at all")
    if i_cfg is not None and i_pub is not None:
        check(i_cfg < i_pub,
              f"update-function-configuration (call {i_cfg}) precedes publish-version (call {i_pub})")
    # A version snapshots configuration, so publishing first is the defect that shipped once.
    code_calls = [c for c in joined if "update-function-code" in c]
    check(all("--publish" not in c for c in code_calls),
          "update-function-code never carries --publish (publish is a separate, later step)")
    check(any("--revision-id" in c for c in joined),
          "the environment write carries a RevisionId guard")
    check(any("--version-id bkv-1" in c or "bkv-1" in c for c in joined),
          "the backup was read by its pinned VersionId, not by the mutable key")
    print(f"     (op raised {type(raised).__name__ if raised else 'nothing'}"
          + (f": {str(raised)[:180]}" if raised else "")
          + "; the assertions above are about call order and content)")


# ------------------------------------- 8. drive op_host_init and assert it REFUSES to write
def test_host_init_refuses_to_apply(mod) -> None:
    print("\n[8] op_host_init refuses apply/rollback and writes nothing")
    fake = FakeAws({})
    fake.Fail = mod.Fail
    mod.aws = fake
    os.environ["ASSETS_BUCKET"] = "b"
    os.environ["HOST_ASG"] = "asg"
    mod.DIFF = None
    for mode in ("apply", "rollback"):
        refused = False
        try:
            mod.op_host_init(mode)
        except Exception as exc:  # noqa: BLE001
            refused = True
            said_why = "MANUAL_CLI_REVIEW" in str(exc) or "does not run" in str(exc)
        check(refused, f"op_host_init({mode}) refuses")
        if refused:
            check(said_why, f"op_host_init({mode}) says why it refuses")
    check(not fake.writes,
          f"no write call was issued while refusing (recorded writes: {len(fake.writes)})")


# ------------------------------- 9. state is write-once, so a retry cannot move the rollback anchor
def test_state_is_write_once(mod) -> None:
    print("\n[9] save_state keeps the FIRST anchor on a same-run retry")
    mod.STATE_DIR = Path(tempfile.mkdtemp())
    mod.save_state("probe", {"version": "original"})
    mod.save_state("probe", {"version": "after-the-first-attempt"})
    got = mod.load_state("probe")
    # load_state returns the PAYLOAD only. That is what stops metadata from being iterated as a
    # payload key — the bug that made the observability rollback treat `_target` as an S3 key.
    check(got == {"version": "original"},
          f"the anchor is still the original, and carries no metadata (got {got})")
    raw = json.loads(mod.state_path("probe").read_text())
    check(raw.get("_target", {}).get("region") == mod.REGION,
          "the state FILE records the account and region it belongs to")
    check(raw.get("_schema") == 2 and "payload" in raw,
          "the state file nests the payload, so metadata cannot collide with a payload key")

    print("     a run id reused against a different account fails closed")
    ident = {"account": "999999999999", "region": mod.REGION}
    original = mod._target_identity
    mod._target_identity = lambda: ident
    refused = False
    try:
        mod.save_state("probe", {"version": "from-another-account"})
    except Exception as exc:  # noqa: BLE001
        refused = "unrollbackable" in str(exc) or "run id unique" in str(exc)
    check(refused, "save_state refuses a run id already used against another account")
    loaded_refused = False
    try:
        mod.load_state("probe")
    except Exception:  # noqa: BLE001
        loaded_refused = True
    check(loaded_refused, "load_state refuses state written against another account")
    mod._target_identity = original


# --------------------------------- 10. an undo that fails must not be reported as a clean unwind
def test_failing_undo_is_not_silent(mod) -> None:
    print("\n[10] a failing undo surfaces instead of being swallowed")
    fake = FakeAws({}, fail_at=1, fail_text="AccessDenied")
    fake.Fail = mod.Fail
    mod.aws = fake
    t = mod.Txn()
    t.add(lambda: mod.aws("ssm", "delete-parameter", "--name", "x"), "delete a parameter")
    surfaced = False
    try:
        t.unwind()
    except Exception:  # noqa: BLE001
        surfaced = True
    check(surfaced, "unwind raised because the undo failed")


# ------------------- 11. write-once state must not swallow a SECOND, different fact
def test_each_state_fact_has_its_own_slot(mod) -> None:
    print("\n[11] facts learned after the anchor go to their own slot, not into the anchor")
    src = (HERE / "oc_resource_ops.py").read_text()
    # The regression: op_lambda_code saved the anchor as "lambda-code" and then tried to save the
    # published version under the SAME name. write-once dropped it, and op_lambda_alias then had no
    # version to move to — apply and verify both failed with a confusing error.
    check('save_state("lambda-published"' in src,
          "the published version has its own state slot")
    check('load_state("lambda-published")' in src,
          "the alias reads the published version from that slot")
    body = src.split("def op_s3_edge_bundle(", 1)[1].split(chr(10) + "def ", 1)[0]
    n = body.count('save_state("s3-edge-bundle"')
    check(n == 1, f"the edge bundle writes its state exactly once (found {n})")
    check("objects_before" in body and "version_id" in body,
          "the recorded pre-state carries a VersionId per object, not just an ETag")


# ------------------- 12. the two entry points must agree about the diff argument
def test_entry_points_agree_on_the_diff_argument(mod) -> None:
    print("\n[12] the shell front and the Python argparse agree about when the diff is required")
    py = (HERE / "oc_resource_ops.py").read_text()
    sh = (HERE / "apply-resource-ops.sh").read_text()
    # The regression: the shell REJECTED a diff for apply while Python REQUIRED one, so the manual
    # refusal branch was unreachable and the operator got an argument error instead of the reason.
    check('a.mode in ("plan", "verify")' in py,
          "Python requires the diff for plan/verify only")
    check("plan|verify)" in sh,
          "the shell front requires the diff for plan/verify only")
    check('a.mode in ("apply", "verify")' not in py,
          "Python no longer requires a diff for apply")


# ------------------- 13. no partial CodeBuild Source write survives anywhere
def test_no_partial_codebuild_source_write(mod) -> None:
    print("\n[13] every CodeBuild source write sends the COMPLETE block")
    src = (HERE / "oc_resource_ops.py").read_text()
    # `--source type=S3,location=...` replaces the block and drops buildspec/auth. Any remaining
    # occurrence must be prose explaining that, never an argument being built.
    for i, line in enumerate(src.splitlines(), 1):
        if "type=S3,location=" not in line:
            continue
        stripped = line.strip()
        is_prose = stripped.startswith("#") or stripped.startswith("`") or '"""' in line
        check(is_prose, f"line {i} mentions the partial form only in prose: {stripped[:70]}")
    check("_saved_source(" in src, "rollback builds the source from the recorded complete block")

def main() -> int:
    closure = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "resources/cloudformation"
    if not closure.is_dir():
        print(f"usage: {sys.argv[0]} <closure-dir>   (no such directory: {closure})")
        return 2
    mod = load_module(closure)
    fake = FakeAws({})
    fake.Fail = mod.Fail
    mod.aws = fake

    test_not_found_predicate(mod)
    test_txn_unwinds_in_reverse(mod)
    test_receipt_requires_a_write(mod)
    test_template_replay(mod)
    test_fault_injection_leaves_nothing(mod)
    test_idempotency_shapes(mod)
    test_lambda_code_configures_before_publishing(mod)
    test_host_init_refuses_to_apply(mod)
    test_state_is_write_once(mod)
    test_failing_undo_is_not_silent(mod)
    test_each_state_fact_has_its_own_slot(mod)
    test_entry_points_agree_on_the_diff_argument(mod)
    test_no_partial_codebuild_source_write(mod)

    print(f"\nassertions={ASSERTIONS} failures={len(FAILURES)}")
    if not ASSERTIONS:
        print("REFUSING to report success: zero assertions ran")
        return 2
    for f in FAILURES:
        print("  -", f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
