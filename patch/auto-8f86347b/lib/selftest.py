#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Self-test for apply.py. Makes NO AWS calls: every call is served by a programmable
fake, so this runs anywhere and fails on a logic change rather than on an environment.

What it is for: the previous kit shipped an operation that could not run in ANY environment because
nothing exercised the function that refused it. Every assertion here drives a real `op_*` function
and checks the ORDER and CONTENT of the calls it makes.

Usage: python3 lib/selftest.py
"""
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
ASSERTIONS = 0
FAILURES = []


def check(cond, label):
    global ASSERTIONS
    ASSERTIONS += 1
    if cond:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}")


def load_module():
    spec = importlib.util.spec_from_file_location("apply", HERE / "apply.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.RUN_ID = "selftest"
    m.REGION = "us-west-2"
    m.OP, m.MODE = "selftest", "selftest"
    m.RECEIPT = ""
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    return m


class FakeAws:
    """Serves AWS calls from a table, records every call, and can fail on demand."""

    def __init__(self, world, fail_at=None):
        self.world = world
        self.calls = []
        self.fail_at = fail_at or {}
        self.files = {}          # s3 key -> bytes, for get-object materialisation

    def key_for(self, args):
        return " ".join(a for a in args[:2])

    def __call__(self, *args, parse_json=False, allow_missing=False):
        self.calls.append(list(args))
        k = self.key_for(args)
        if k in self.fail_at:
            raise self.fail_at[k]
        # get-object writes a local file; the destination is the last positional before flags
        if args[:2] == ("s3api", "get-object"):
            dest = next((a for a in args if "/" in str(a) and not str(a).startswith("--")
                         and Path(a).parent.exists() and not str(a).startswith("s3://")), None)
            key = args[args.index("--key") + 1] if "--key" in args else ""
            body = self.files.get(key)
            if body is None:
                if allow_missing:
                    return None
                raise self.world.get("_notfound", Exception("NoSuchKey"))
            if dest:
                Path(dest).write_bytes(body)
            return {} if parse_json else ""
        v = self.world.get(k, self.world.get(" ".join(str(a) for a in args[:3])))
        if callable(v):
            v = v(*args)
        if v is None and allow_missing:
            return None
        return v


def test_kit_is_consistent_with_itself(m):
    print("\n[1] the kit's shipped files match the sha256 its manifest records")
    man = json.loads((KIT / "manifest.json").read_text())
    for rel, src in ((m._lambda_paths()[1], m._lambda_paths()[0]),):
        p = KIT / rel
        check(p.is_file(), f"[1] the kit ships {rel}")
        if not p.is_file():
            continue
        want = man["paths"][src]["patch_sha256"]
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        check(got == want, f"[1] {rel} == manifest patch_sha256 for {src}")
    # A tampered copy must be refused, or the assertion is decoration.
    tmp = Path(tempfile.mkdtemp()) / "manifest.json"
    bad = json.loads(json.dumps(man))
    bad["paths"][m._lambda_paths()[0]]["patch_sha256"] = "0" * 64
    tmp.write_text(json.dumps(bad))
    real = m.MANIFEST
    m.MANIFEST = tmp
    try:
        m.shipped_file(m._lambda_paths()[1], m._lambda_paths()[0])
        check(False, "[1] a manifest/file mismatch is refused")
    except m.Fail as exc:
        check("inconsistent with itself" in str(exc), "[1] a manifest/file mismatch is refused")
    finally:
        m.MANIFEST = real


def test_overlay_replaces_exactly_one_entry(m):
    print("\n[2] the overlay replaces ONE entry and keeps the package's entry set")
    src = KIT / m._lambda_paths()[1]
    td = Path(tempfile.mkdtemp())
    live = td / "live.zip"
    # A package shaped like the real one: root-level handler.py, nested core/ and services/.
    others = {"handler.py": b"# handler\n", "core/__init__.py": b"", "core/auth.py": b"# auth\n",
              "services/__init__.py": b"", "services/host_service.py": b"# host\n",
              m._lambda_paths()[2]: b"# the OLD egress service\n"}
    with zipfile.ZipFile(live, "w") as z:
        for k, v in others.items():
            z.writestr(k, v)
    live_sha = m.zip_codesha(live)

    fake = FakeAws({
        "lambda get-function": "https://example.invalid/live.zip",
        "lambda get-function-configuration": {"CodeSha256": live_sha, "RevisionId": "r1"},
    })
    m.aws = fake
    # Stand in for curl by pre-placing the file where build_overlay downloads it.
    dest = td / "overlay.zip"
    real_run = m.subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "curl":
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(live.read_bytes())

            class R:
                returncode = 0
                stderr = ""
            return R()
        return real_run(cmd, *a, **kw)

    m.subprocess.run = fake_run
    try:
        got_live, got_new = m.build_overlay("fn", dest)
        check(got_live == live_sha, "[2] the download is asserted against the declared CodeSha256")
        zc = zipfile.ZipFile(dest)
        check([i.filename for i in zc.infolist()] == list(others),
              "[2] the entry set is unchanged (order included)")
        differ = [n for n in others
                  if hashlib.sha256(zipfile.ZipFile(live).read(n)).hexdigest()
                  != hashlib.sha256(zc.read(n)).hexdigest()]
        check(differ == [m._lambda_paths()[2]], f"[2] exactly the shipped file differs (got {differ})")
        check(hashlib.sha256(zc.read(m._lambda_paths()[2])).hexdigest()
              == hashlib.sha256(src.read_bytes()).hexdigest(),
              "[2] the replaced entry is this kit's bytes")
        check(got_new != got_live, "[2] the new digest differs from the live one")

        # A package that does not contain the target path must be refused by NAME, so a wrong
        # function or a changed layout is a clear error rather than a silent add.
        thin = td / "thin.zip"
        with zipfile.ZipFile(thin, "w") as z:
            z.writestr("handler.py", b"# only handler\n")

        def fake_run2(cmd, *a, **kw):
            if cmd and cmd[0] == "curl":
                Path(cmd[cmd.index("-o") + 1]).write_bytes(thin.read_bytes())

                class R:
                    returncode = 0
                    stderr = ""
                return R()
            return real_run(cmd, *a, **kw)

        m.subprocess.run = fake_run2
        fake.world["lambda get-function-configuration"] = {"CodeSha256": m.zip_codesha(thin)}
        try:
            m.build_overlay("fn", td / "o2.zip")
            check(False, "[2] a package without the target path is refused")
        except m.Fail as exc:
            check("not in the live package" in str(exc),
                  "[2] a package without the target path is refused, naming the path")

        # A download that does not hash to the declared CodeSha256 must be refused: patching a stale
        # package would install the wrong dependencies alongside the right source.
        fake.world["lambda get-function-configuration"] = {"CodeSha256": "not-the-download"}
        m.subprocess.run = fake_run
        try:
            m.build_overlay("fn", td / "o3.zip")
            check(False, "[2] a stale download is refused")
        except m.Fail as exc:
            check("not the deployed one" in str(exc), "[2] a stale download is refused")
    finally:
        m.subprocess.run = real_run


def test_lambda_publishes_last_and_unwinds(m):
    print("\n[3] apply order is code -> read back -> invoke -> publish -> alias, and it unwinds")
    src = KIT / m._lambda_paths()[1]
    td = Path(tempfile.mkdtemp())
    live = td / "live.zip"
    with zipfile.ZipFile(live, "w") as z:
        z.writestr("handler.py", b"# handler\n")
        z.writestr(m._lambda_paths()[2], b"# the OLD egress service\n")
    live_sha = m.zip_codesha(live)
    # The digest the overlay will produce, computed the same way the operation does.
    overlay_probe = td / "probe.zip"
    zin = zipfile.ZipFile(live)
    with zipfile.ZipFile(overlay_probe, "w", zipfile.ZIP_DEFLATED) as zo:
        for info in zin.infolist():
            data = src.read_bytes() if info.filename == m._lambda_paths()[2] else zin.read(info)
            ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            ni.external_attr, ni.compress_type = info.external_attr, zipfile.ZIP_DEFLATED
            zo.writestr(ni, data)
    want_sha = m.zip_codesha(overlay_probe)

    state = {"code": live_sha}

    def conf(*a):
        return {"CodeSha256": state["code"], "RevisionId": "r1"}

    fake = FakeAws({
        "lambda get-function": "https://example.invalid/live.zip",
        "lambda get-function-configuration": conf,
        "lambda update-function-code": lambda *a: state.update(code=want_sha) or "",
        "lambda wait": "",
        "lambda invoke": "None",
        "lambda publish-version": "77",
        # The fake must REFLECT the write, or the operation's own readback assertion fails and the
        # test proves nothing about the call order it is meant to check.
        "lambda update-alias": lambda *a: state.update(
            alias=a[a.index("--function-version") + 1]) or "",
        "lambda get-alias": lambda *a: state.get("alias", "59"),
        "s3api head-object": {"VersionId": "v-backup"},
        "s3api get-bucket-versioning": "Enabled",
        "sts get-caller-identity": {"Account": "111111111111"},
        "s3 cp": "",
    })
    fake.files["backups/api.zip"] = live.read_bytes()
    m.aws = fake
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-3"
    real_run = m.subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "curl":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(live.read_bytes())

            class R:
                returncode = 0
                stderr = ""
            return R()
        return real_run(cmd, *a, **kw)

    m.subprocess.run = fake_run
    os.environ.update({"OPENCLAW_API_FN": "openclaw-api", "BACKUP_S3_BUCKET": "b",
                       "BACKUP_S3_KEY": "backups/api.zip", "OPENCLAW_API_ALIAS": "live"})
    try:
        m.op_lambda("apply")
        seq = [" ".join(c[:2]) for c in fake.calls]
        def idx(name):
            return next((i for i, s in enumerate(seq) if s == name), -1)
        check(idx("lambda update-function-code") < idx("lambda invoke") < idx("lambda publish-version"),
              "[3] code is written, then invoked, and only then published")
        check(idx("lambda publish-version") < idx("lambda update-alias"),
              "[3] the alias moves AFTER the version exists")
        check(idx("s3api get-object") < idx("lambda update-function-code"),
              "[3] the backup is downloaded and checked BEFORE the code is replaced")
        pub = next(c for c in fake.calls if c[:2] == ["lambda", "publish-version"])
        check("--code-sha256" in pub and pub[pub.index("--code-sha256") + 1] == want_sha,
              "[3] publish-version pins the digest it expects")
        st = m.load_state("lambda")
        check(st["backup_version_id"] == "v-backup",
              "[3] the backup is pinned by version id, not by the mutable key")
        check(st["alias_before"] == "59", "[3] the alias's previous target is recorded")
        check(m.load_state("lambda-published")["published_version"] == "77",
              "[3] the published version gets its own state slot")
    finally:
        m.subprocess.run = real_run

    # Fault injection: a failure AFTER the code was written must restore the code, delete the
    # version, and move the alias back — in reverse order.
    state["code"] = live_sha
    # Inject on the alias WRITE, not on the read: the read that records `alias_before` happens
    # before the transaction exists, so failing there leaves nothing to unwind and the assertions
    # below would be vacuous.
    fake2 = FakeAws({**fake.world},
                    fail_at={"lambda update-alias": m.Fail("injected write failure")})
    fake2.files = dict(fake.files)
    fake2.world["lambda update-function-code"] = lambda *a: state.update(code=want_sha) or ""
    fake2.world["lambda publish-version"] = "78"
    m.aws = fake2
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-3b"
    m.subprocess.run = fake_run
    try:
        try:
            m.op_lambda("apply")
            check(False, "[3] an injected failure propagates")
        except m.Fail:
            check(True, "[3] an injected failure propagates")
        undone = [" ".join(c[:2]) for c in fake2.calls]
        check("lambda delete-function" in undone,
              "[3] the unwind deletes the version this attempt published")
        restores = [c for c in fake2.calls if c[:2] == ["lambda", "update-function-code"]
                    and "--s3-object-version" in c]
        check(bool(restores), "[3] the unwind restores $LATEST from the pinned backup version")
    finally:
        m.subprocess.run = real_run


def test_edge_prefix_is_discovered_not_assumed(m):
    print("\n[4] the edge bundle prefix comes from the launch template the ASG pins")
    sha = "a" * 64
    ud = f"#!/bin/bash\naws s3 cp s3://bucket/deployment/bootstrap/edge/{sha}/install-edge.sh .\n"
    packed = base64.b64encode(gzip.compress(ud.encode())).decode()

    def lt_versions(*a):
        return packed

    base_world = {
        "autoscaling describe-auto-scaling-groups": {
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "Version": "3"}},
        "ec2 describe-launch-template-versions": lt_versions,
        "sts get-caller-identity": {"Account": "111111111111"},
    }
    os.environ["EDGE_ASG"] = "openclaw-edge-asg"
    m.aws = FakeAws(base_world)
    check(m.discover_edge_prefix() == f"deployment/bootstrap/edge/{sha}",
          "[4] the prefix is read out of the gzip+base64 user data")

    # $Latest must be refused: the version served changes the instant a new one is created.
    w = {**base_world, "autoscaling describe-auto-scaling-groups": {
        "LaunchTemplate": {"LaunchTemplateId": "lt-1", "Version": "$Latest"}}}
    m.aws = FakeAws(w)
    try:
        m.discover_edge_prefix()
        check(False, "[4] a $Latest pin is refused")
    except m.Fail as exc:
        check("$Latest" in str(exc), "[4] a $Latest pin is refused")

    # $Default must be RESOLVED, not refused — a real fleet pins it.
    w2 = {**base_world,
          "autoscaling describe-auto-scaling-groups": {
              "LaunchTemplate": {"LaunchTemplateId": "lt-1", "Version": "$Default"}},
          "ec2 describe-launch-templates": "7"}
    m.aws = FakeAws(w2)
    check(m.discover_edge_prefix() == f"deployment/bootstrap/edge/{sha}",
          "[4] a $Default pin is resolved to the default version number")

    # Two different prefixes in the user data is ambiguous and must refuse rather than pick one.
    ud2 = ud + f"aws s3 cp s3://bucket/deployment/bootstrap/edge/{'b'*64}/x.sh .\n"
    w3 = {**base_world,
          "ec2 describe-launch-template-versions":
              lambda *a: base64.b64encode(gzip.compress(ud2.encode())).decode()}
    m.aws = FakeAws(w3)
    try:
        m.discover_edge_prefix()
        check(False, "[4] two candidate prefixes are refused")
    except m.Fail as exc:
        check("refusing to guess" in str(exc), "[4] two candidate prefixes are refused")

    # No prefix at all must also refuse — an empty result is not "nothing to do".
    w4 = {**base_world,
          "ec2 describe-launch-template-versions":
              lambda *a: base64.b64encode(gzip.compress(b"#!/bin/bash\necho nothing\n")).decode()}
    m.aws = FakeAws(w4)
    try:
        m.discover_edge_prefix()
        check(False, "[4] zero candidate prefixes are refused")
    except m.Fail as exc:
        check("0 edge bundle prefix" in str(exc), "[4] zero candidate prefixes are refused")


def test_edge_apply_pins_and_unwinds(m):
    print("\n[5] the edge upload records the previous version and can put it back")
    sha = "c" * 64
    ud = f"aws s3 cp s3://b/deployment/bootstrap/edge/{sha}/install-edge.sh .\n"
    key = f"deployment/bootstrap/edge/{sha}/{m._edge_paths()[2]}"
    world = {
        "autoscaling describe-auto-scaling-groups": {
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "Version": "3"}},
        "ec2 describe-launch-template-versions":
            lambda *a: base64.b64encode(gzip.compress(ud.encode())).decode(),
        "s3api head-object": {"VersionId": "v-old", "ETag": '"old"'},
        "s3api get-bucket-versioning": "Enabled",
        "sts get-caller-identity": {"Account": "111111111111"},
        "s3 cp": "",
        "s3api copy-object": "",
    }
    shipped = (KIT / m._edge_paths()[1]).read_bytes()
    os.environ.update({"ASSETS_BUCKET": "b", "EDGE_ASG": "openclaw-edge-asg"})

    # Happy path: an object exists with different bytes, gets overwritten, and reads back correct.
    fake = FakeAws(world)
    fake.files[key] = b"# the OLD integration script\n"

    def cp(*a):
        fake.files[key] = shipped
        return ""

    fake.world["s3 cp"] = cp
    m.aws = fake
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-5"
    m.op_edge("apply")
    st = m.load_state("edge")
    check(st["version_before"] == "v-old",
          "[5] the previous version id is recorded before the overwrite")
    check(st["key"] == key, "[5] the key is built from the DISCOVERED prefix")
    order = [" ".join(c[:2]) for c in fake.calls]
    check(order.index("s3api head-object") < order.index("s3 cp"),
          "[5] the previous state is read before anything is written")
    check("s3api get-object" in order[order.index("s3 cp"):],
          "[5] the upload is read back, not assumed")

    # Idempotency: applying again writes nothing and says so.
    fake2 = FakeAws({**world})
    fake2.files[key] = shipped
    m.aws = fake2
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-5b"
    m.op_edge("apply")
    check(not any(c[:2] == ["s3", "cp"] for c in fake2.calls),
          "[5] a second apply writes nothing when the bytes already match")

    # An unversioned bucket must refuse: the unwind could not put the previous bytes back.
    fake3 = FakeAws({**world, "s3api get-bucket-versioning": "Suspended"})
    fake3.files[key] = b"# old\n"
    m.aws = fake3
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-5c"
    try:
        m.op_edge("apply")
        check(False, "[5] a bucket without versioning is refused")
    except m.Fail as exc:
        check("versioning" in str(exc), "[5] a bucket without versioning is refused")
    check(not any(c[:2] == ["s3", "cp"] for c in fake3.calls),
          "[5] that refusal happened BEFORE any write")

    # Readback mismatch must unwind by restoring the recorded version.
    fake4 = FakeAws({**world})
    fake4.files[key] = b"# old\n"
    fake4.world["s3 cp"] = lambda *a: fake4.files.__setitem__(key, b"# WRONG bytes\n") or ""
    m.aws = fake4
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-5d"
    try:
        m.op_edge("apply")
        check(False, "[5] a readback mismatch fails")
    except m.Fail as exc:
        check("readback hashes to" in str(exc), "[5] a readback mismatch fails")
    check(any(c[:2] == ["s3api", "copy-object"] for c in fake4.calls),
          "[5] the unwind restores the recorded version after a bad readback")


def test_verify_reads_the_deployed_artifact(m):
    print("\n[6] verify reads the deployed artifact; it never replays the apply")
    src = (KIT / m._lambda_paths()[1]).read_bytes()
    td = Path(tempfile.mkdtemp())
    pkg = td / "deployed.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("handler.py", b"# handler\n")
        z.writestr(m._lambda_paths()[2], src)
    world = {
        "lambda get-function": "https://example.invalid/x.zip",
        "lambda invoke": "None",
        "sts get-caller-identity": {"Account": "111111111111"},
    }
    fake = FakeAws(world)
    m.aws = fake
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-6"
    os.environ["OPENCLAW_API_FN"] = "openclaw-api"
    real_run = m.subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "curl":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(pkg.read_bytes())

            class R:
                returncode = 0
                stderr = ""
            return R()
        return real_run(cmd, *a, **kw)

    m.subprocess.run = fake_run
    try:
        m.op_lambda("verify")
        check(True, "[6] verify passes when the deployed file is this kit's file")
        check(not any(c[:2] == ["lambda", "update-function-code"] for c in fake.calls),
              "[6] verify wrote nothing")

        # The wrong bytes deployed must FAIL, and say so — not pass because the function is healthy.
        bad = td / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("handler.py", b"# handler\n")
            z.writestr(m._lambda_paths()[2], b"# the OLD egress service\n")

        def fake_run2(cmd, *a, **kw):
            if cmd and cmd[0] == "curl":
                Path(cmd[cmd.index("-o") + 1]).write_bytes(bad.read_bytes())

                class R:
                    returncode = 0
                    stderr = ""
                return R()
            return real_run(cmd, *a, **kw)

        m.subprocess.run = fake_run2
        m.aws = FakeAws(world)
        try:
            m.op_lambda("verify")
            check(False, "[6] verify fails when the deployed file is the OLD one")
        except m.Fail as exc:
            check("is NOT applied" in str(exc),
                  "[6] verify fails when the deployed file is the OLD one, and says so")
    finally:
        m.subprocess.run = real_run


def test_state_is_write_once_and_target_bound(m):
    print("\n[7] state is write-once, atomic, and bound to the account and region")
    m.aws = FakeAws({"sts get-caller-identity": {"Account": "111111111111"}})
    m.WORK_DIR = Path(tempfile.mkdtemp())
    m.STATE_DIR = m.WORK_DIR / "state"
    m.RUN_ID = "sel-7"
    m.save_state("x", {"first": True})
    m.save_state("x", {"second": True})
    check(m.load_state("x") == {"first": True},
          "[7] a second write under the same name keeps the FIRST anchor")
    p = m.state_path("x")
    check(json.loads(p.read_text())["payload"] == {"first": True},
          "[7] the payload is nested, so metadata cannot be mistaken for data")
    check(oct(p.stat().st_mode)[-3:] == "600", "[7] the state file is not world-readable")
    # Loading against a different account must refuse.
    m.aws = FakeAws({"sts get-caller-identity": {"Account": "222222222222"}})
    try:
        m.load_state("x")
        check(False, "[7] state written for another account is refused")
    except m.Fail as exc:
        check("another" in str(exc), "[7] state written for another account is refused")
    # A missing state file must refuse rather than silently do nothing.
    m.aws = FakeAws({"sts get-caller-identity": {"Account": "111111111111"}})
    try:
        m.load_state("never-written")
        check(False, "[7] a missing state file is refused")
    except m.Fail as exc:
        check("no state for this run" in str(exc), "[7] a missing state file is refused")


def test_failing_undo_is_not_silent(m):
    print("\n[8] a transaction that cannot undo itself says so instead of reporting success")
    done = []
    t = m.Txn()
    t.add(lambda: done.append("first"), "first")
    t.add(lambda: (_ for _ in ()).throw(m.Fail("cannot undo")), "second")
    try:
        t.unwind()
        check(False, "[8] a failing undo raises")
    except m.Fail as exc:
        check("UNWIND INCOMPLETE" in str(exc), "[8] a failing undo raises and names the state")
    check(done == ["first"], "[8] the remaining undos still run (reverse order, best effort)")
    order = []
    t2 = m.Txn()
    t2.add(lambda: order.append(1), "one")
    t2.add(lambda: order.append(2), "two")
    t2.unwind()
    check(order == [2, 1], "[8] undos run in reverse registration order")


def test_pinned_version_rejects_unrestorable(m):
    print("\n[9] a version id that cannot be restored is refused")
    m.aws = FakeAws({"s3api get-bucket-versioning": "Enabled"})
    check(m.pinned_version("b", "k", "v1", "thing") == "v1", "[9] a real version id passes")
    for bad in ("null", "", None, "None"):
        try:
            m.pinned_version("b", "k", bad, "thing")
            check(False, f"[9] version {bad!r} is refused")
        except m.Fail:
            check(True, f"[9] version {bad!r} is refused")
    m.aws = FakeAws({"s3api get-bucket-versioning": "Suspended"})
    try:
        m.pinned_version("b", "k", "v1", "thing")
        check(False, "[9] a suspended bucket is refused even with a version id")
    except m.Fail as exc:
        check("not Enabled" in str(exc), "[9] a suspended bucket is refused even with a version id")


def test_region_is_always_explicit(m):
    print("\n[10] every AWS call is region-pinned")
    src = (HERE / "apply.py").read_text()
    body = src.split("def aws(")[1].split("\ndef ")[0]
    check('"--region", REGION' in body, "[10] aws() appends --region on every call")
    check('if "--region" not in args' in body,
          "[10] an explicitly-passed region is not duplicated")


def main() -> int:
    m = load_module()
    test_kit_is_consistent_with_itself(m)
    test_overlay_replaces_exactly_one_entry(m)
    test_lambda_publishes_last_and_unwinds(m)
    test_verify_reads_the_deployed_artifact(m)
    test_state_is_write_once_and_target_bound(m)
    test_failing_undo_is_not_silent(m)
    test_pinned_version_rejects_unrestorable(m)
    test_region_is_always_explicit(m)
    print("GROUPS NOT RUN (this kit ships no artifact of that kind): "
          + ", ".join(['test_edge_prefix_is_discovered_not_assumed', 'test_edge_apply_pins_and_unwinds']))
    print(f"\nassertions={ASSERTIONS} failures={len(FAILURES)}")
    if not ASSERTIONS:
        print("REFUSING to report success: zero assertions ran")
        return 2
    for f in FAILURES:
        print("  -", f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
