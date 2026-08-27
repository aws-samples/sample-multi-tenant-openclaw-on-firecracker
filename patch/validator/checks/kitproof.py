import ast
import hashlib
import re
from pathlib import Path

from lib.context import normalize_manifest
from lib.result import finding
def _missing(check_id, subject, error="nothing to inspect"):
    return finding(
        check_id, "INCONCLUSIVE", "%s was not inspectable." % subject,
        {"inspected": 0, "error": str(error)},
        remediation="Complete the kit declaration and retry.",
    )
def check_d1(ctx):
    values = {"base_sha": ctx.manifest.get("base_sha"),
              "patch_sha": ctx.manifest.get("patch_sha")}
    readings, missing = {}, []
    for key, value in values.items():
        exists = ctx.git_exists(value)
        readings[key] = {"value": value, "resolves": exists}
        if not exists:
            missing.append("%s does not resolve" % key)
    verdict = "FAIL" if missing else ("UNVERIFIED" if not all(values.values()) else "PASS")
    return finding(
        "D1", verdict, "Declared commits were probed with git cat-file.",
        {"inspected": len(values), "commits": readings}, missing,
        "Publish both declared commits to the gateway lineage and update the manifest.",
    )
def _gateway_hash(ctx, path):
    try:
        return hashlib.sha256(ctx.git_bytes(ctx.gateway_ref, path)).hexdigest()
    except (OSError, RuntimeError):
        return None
def _hop_contained(ctx, older, newer):
    old_base, old_patch = older.get("base_sha"), older.get("patch_sha")
    new_base, new_patch = newer.get("base_sha"), newer.get("patch_sha")
    if not all((old_base, old_patch, new_base, new_patch)):
        return False
    return (ctx.git_is_ancestor(new_base, old_base) and
            ctx.git_is_ancestor(old_patch, new_patch))
def check_d2(ctx):
    manifests = ctx.manifests()
    kits = []
    for manifest in manifests:
        entries = {item["path"]: item["sha256"] for item in normalize_manifest(manifest)
                   if item.get("sha256") and not item["path"].startswith("patch/")}
        if entries:
            kits.append({"id": manifest.get("id") or manifest.get("_manifest_path"),
                         "base_sha": manifest.get("base_sha"),
                         "patch_sha": manifest.get("patch_sha"),
                         "entries": entries})
    if not kits:
        return _missing("D2", "kit products")
    obsolete, informational = [], []
    for older in kits:
        for newer in kits:
            if older is newer or not set(older["entries"]).issubset(newer["entries"]):
                continue
            covered = True
            for path, old_hash in older["entries"].items():
                new_hash = newer["entries"][path]
                gateway = _gateway_hash(ctx, path)
                if new_hash != old_hash and new_hash != gateway:
                    covered = False
                    break
            if covered:
                row = {"kit": older["id"], "covered_by": newer["id"]}
                if _hop_contained(ctx, older, newer):
                    obsolete.append({"remove": older["id"],
                                     "covered_by": newer["id"]})
                    break
                informational.append(row)
    return finding(
        "D2", "FAIL" if obsolete else "PASS",
        "Kit outputs and declared upgrade windows were compared.",
        {"inspected": len(kits), "obsolete": obsolete,
         "byte_covered_other_hops": informational,
         "kits": [{"id": item["id"], "paths": len(item["entries"])} for item in kits]},
        ["%s is fully covered by %s" % (item["remove"], item["covered_by"])
         for item in obsolete],
        "Remove fully superseded kits so customers see one authoritative delivery.",
    )
def check_d3(ctx):
    sha = str(ctx.manifest.get("patch_sha") or "")
    patch_dir = Path(ctx.repo) / "patch"
    markers = list(patch_dir.glob("push-marker-*"))
    logs = list(patch_dir.glob("manifest-*"))
    marker_by_slug = {path.stem[len("push-marker-"):]: path for path in markers}
    log_by_slug = {path.stem[len("manifest-"):]: path for path in logs}
    slugs = set(marker_by_slug).union(log_by_slug)
    slug = max(slugs) if slugs else None
    marker = marker_by_slug.get(slug)
    log = log_by_slug.get(slug)
    marker_has_sha = False
    if marker:
        try:
            marker_has_sha = bool(sha) and sha in marker.read_text()
        except OSError:
            marker_has_sha = False
    failures = []
    if not marker or not marker_has_sha:
        failures.append("push marker missing or does not reference patch_sha")
    if not log:
        failures.append("manifest log missing")
    return finding(
        "D3", "FAIL" if failures else "PASS",
        "The newest publish marker and manifest log were paired by filename slug.",
        {"inspected": len(markers) + len(logs), "patch_sha": sha,
         "slug": slug,
         "marker": str(marker.relative_to(ctx.repo)) if marker else None,
         "manifest_log": str(log.relative_to(ctx.repo)) if log else None,
         "marker_references_patch_sha": marker_has_sha},
        failures, "Add both records for the newest slug and anchor the marker to patch_sha.",
    )
def check_d4(ctx):
    residues = []
    inspected = 0
    for path in Path(ctx.kit).rglob("*"):
        inspected += 1
        name = path.name
        if name == "__pycache__" or name.startswith("._"):
            residues.append(str(path.relative_to(ctx.kit)))
        if path.is_dir() and (name.startswith(".oc-apply-") or name.endswith(".state")):
            residues.append(str(path.relative_to(ctx.kit)))
    if not inspected:
        return _missing("D4", "kit tree")
    return finding(
        "D4", "FAIL" if residues else "PASS",
        "The kit tree was scanned for generated state and bytecode.",
        {"inspected": inspected, "residues": residues}, residues,
        "Delete generated state, bytecode caches, and metadata files from the kit.",
    )


def _direct_scripts(ctx):
    root = Path(ctx.repo) / "deploy/lambda/api"
    scripts = set()
    pattern = re.compile(
        r"(?:^|&&\s+)(/(?:home/ubuntu|opt/openclaw)/"
        r"[A-Za-z0-9_./-]+\.(?:sh|py))(?=\s)", re.MULTILINE
    )
    for path in root.rglob("*.py"):
        try:
            text = path.read_text()
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        if (path.name != "ssm_dispatch.py" and
                "AWS-RunShellScript" not in text and "ssm_dispatch" not in text):
            continue
        values = []
        for node in ast.walk(tree):
            names = ([target.id for target in node.targets
                      if isinstance(target, ast.Name)]
                     if isinstance(node, ast.Assign) else [])
            if any("cmd" in name.lower() or "command" in name.lower() or
                   name.lower() == "script" for name in names):
                values.append(node.value)
            if (isinstance(node, ast.Call) and len(node.args) > 1 and
                    getattr(node.func, "attr", "") in ("_ssm_run", "_ssm_send")):
                values.append(node.args[1])
            if (path.name == "ssm_dispatch.py" and isinstance(node, ast.Return)
                    and node.value is not None):
                values.append(node.value)
        joined = [node for value in values for node in ast.walk(value)
                  if isinstance(node, ast.JoinedStr)]
        joined_parts = {id(part) for node in joined for part in node.values}
        texts = ["".join(part.value if isinstance(part, ast.Constant) else "{}"
                         for part in node.values) for node in joined]
        texts.extend(node.value for value in values for node in ast.walk(value)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and id(node) not in joined_parts)
        for command_path in pattern.findall("\n".join(texts)):
            if command_path.startswith("/home/ubuntu/"):
                relative = command_path[len("/home/ubuntu/"):]
            else:
                relative = command_path[len("/opt/openclaw/"):]
            scripts.add("deploy/userdata/" + relative)
    return scripts


def check_d5(ctx):
    scripts = _direct_scripts(ctx)
    entries = {item["path"]: item for item in ctx.manifest_entries()
               if not item["path"].startswith("patch/")}
    live = ctx.get("live_files", {}) or {}
    rows, failures = [], []
    for script in sorted(scripts):
        item = entries.get(script)
        if not item:
            continue
        observed = live.get(item["path"], {})
        mode_ok = item.get("mode") == "0755"
        live_ok = observed.get("executable")
        if not mode_ok or live_ok is False:
            failures.append(script)
        rows.append({"script": script, "path": item["path"],
                     "manifest_mode": item.get("mode"),
                     "live_executable": live_ok,
                     "direct_execution": True})
    if not rows:
        return _missing("D5", "directly executed kit scripts")
    verdict = "FAIL" if failures else (
        "UNVERIFIED" if any(row["live_executable"] is None for row in rows) else "PASS")
    return finding(
        "D5", verdict, "Directly executed scripts were checked for executable delivery.",
        {"inspected": len(rows), "scripts": rows}, failures,
        "Declare mode 0755 and ensure the deployed file is executable.",
    )


def check_d6(ctx):
    writes, failures = [], []
    write_pattern = re.compile(
        r"\baws\s+s3\s+(?:cp|sync|mv)\s+\S+\s+[\"']?s3://|"
        r"\baws\s+s3api\s+put-object\b"
    )
    for item in ctx.manifest_entries():
        for operation in item.get("operations") or []:
            text = str(operation.get("apply_cli") or "")
            match = write_pattern.search(text)
            if not match:
                continue
            write_at = match.start()
            head_at = max(text.find("head-object"), text.find("head_object"))
            version_at = text.find("VersionId")
            ok = 0 <= head_at < write_at and 0 <= version_at < write_at
            if not ok:
                failures.append(item["path"])
            writes.append({"path": item["path"], "anchor_before_write": ok,
                           "apply_cli": operation.get("apply_cli")})
    if not writes:
        return _missing("D6", "S3 write operations")
    return finding(
        "D6", "FAIL" if failures else "PASS",
        "S3 writes were checked for a preceding version rollback anchor.",
        {"inspected": len(writes), "writes": writes}, failures,
        "Read and record the previous VersionId before every S3 write.",
    )


def check_d7(ctx):
    files = list((Path(ctx.kit) / "lib").glob("*.sh"))
    if not files:
        return _missing("D7", "kit shell libraries")
    problems = []
    for path in files:
        try:
            text = path.read_text()
        except OSError as error:
            problems.append("%s: %s" % (path.name, error))
            continue
        if re.search(r"\bmapfile\b|declare\s+-A\b", text):
            problems.append(path.name)
    return finding(
        "D7", "FAIL" if problems else "PASS",
        "Shell libraries were scanned for bash-4-only constructs.",
        {"inspected": len(files), "problems": problems}, problems,
        "Replace mapfile and associative arrays with bash 3.2-compatible code.",
    )

# --------------------------------------------------------------------- D8, D9
# Reference shapes that name a companion file. Each is anchored on the syntax that makes the
# token a reference rather than prose.
_REF_PATTERNS = (
    ("fluent-bit script", r"^\s*script\s+(\S+)", (".lua",)),
    ("fluent-bit parser file", r"^\s*[Pp]arsers_[Ff]ile\s+(\S+)", (".conf",)),
    ("include", r"^\s*@INCLUDE\s+(\S+)", None),
    ("python import", r"^\s*(?:import|from)\s+([a-z_][a-z0-9_]*)", None),
    ("lua require", r"""require\s*\(?\s*['\"]([\w./-]+)['\"]""", None),
    ("shell source", r"^\s*(?:\.|source)\s+(\S+)", None),
    ("key material", r"(\S+\.(?:gpg|asc|pem|crt))", None),
    ("systemd unit", r"(\S+\.service)\b", None),
)
# `xxx.service` is a UNIT name far more often than a file path. systemd resolves a unit from its own
# directories, so `After=`/`Wants=`/`systemctl <verb>` lines reference nothing the kit could ship.
_UNIT_NAME_CONTEXT = re.compile(
    r"(?:systemctl|systemd-\w+|After=|Before=|Requires=|Requisite=|Wants=|BindsTo=|PartOf=|"
    r"WantedBy=|RequiredBy=|Conflicts=|JoinsNamespaceOf=|unit\s*=)", re.IGNORECASE)
# What makes it a file: something puts it somewhere or pulls it from somewhere.
_FILE_CONTEXT = re.compile(
    r"(?:\binstall\b|\bcp\b|\bmv\b|\bln\b|s3\s+cp|s3://|_s3_get|\bcurl\b|\bwget\b|"
    r"\btar\b|\bfor\s+\w+\s+in\b|\$\{[A-Za-z_]\w*\}/|\$[A-Za-z_]\w*/)")
# How a file arrives when it does not sit beside its referencing file.
_FETCH_CONTEXT = re.compile(r"(?:s3\s+cp|s3://|_s3_get|\bcurl\b|\bwget\b|\binstall\b|\bcp\b)")
# Names that are never a shipped companion: the standard library and the third-party wheels the
# runtime already has. Keeping this list explicit is what stops D8 from reporting `import json`.
_NOT_A_COMPANION = frozenset("""
os sys re json time base64 hashlib subprocess pathlib typing datetime logging socket threading
collections functools itertools math random shutil signal tempfile traceback uuid urllib http
ipaddress boto3 botocore yaml requests jwt cryptography decimal enum copy argparse glob gzip io
struct textwrap unittest warnings queue concurrent contextlib dataclasses errno fcntl getpass grp
pwd stat string csv abc operator secrets zlib zipfile binascii bisect ast importlib inspect
platform types weakref html email hmac select shlex sqlite3 pickle configparser difflib
""".split())
_REF_SUFFIXES = (".conf", ".lua", ".sh", ".py", ".service", ".yaml", ".yml")


def _unpackaged(name):
    """The name a machine sees, with the kit's packaging suffix removed."""
    for suffix in (".patched", ".diff"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _shipped_index(kit):
    """(names, relative paths) the kit ships, both as a machine would see them.

    Two indexes because the two questions are different. A same-directory reference must be
    satisfied by a file in THAT directory; only an absolute path — which names a host location the
    kit cannot mirror — falls back to "anywhere in the payload".
    """
    names, rels = set(), set()
    for path in kit.rglob("*"):
        if not path.is_file():
            continue
        name = _unpackaged(path.name)
        names.add(name)
        names.add(Path(name).stem)
        rels.add(str(path.relative_to(kit).parent / name))
    return names, rels


def _is_fetched_by(text, base):
    """Does the referencing file itself download or install `base`?

    Then the payload mirrors the delivery layout rather than the referencing file's directory, and
    demanding co-location would report a correct kit as broken.
    """
    for line in text.splitlines():
        if base in line and _FETCH_CONTEXT.search(line):
            return True
    return False


def _reference_satisfied(ref, referencing_rel, names, rels, text=""):
    """Is `ref`, as written inside `referencing_rel`, satisfied by the kit payload?

    Default resolution is relative to the referencing file's own directory, because that is how a
    conf naming a sibling filter, a module import, a `require` and a `source` all resolve. Matching
    the basename anywhere in the payload is what let a mutant through: `add_timestamp.lua` ships
    under both host/ and edge/, so deleting the host copy stayed green on the edge one while a host
    booting Fluent Bit would have died.

    Two cases legitimately escape co-location, and both are read off the input rather than assumed:
    an absolute path names a host location the kit cannot mirror, and a companion the referencing
    file fetches for itself lands wherever that fetch puts it.
    """
    base = Path(ref).name
    here = Path(referencing_rel).parent

    if _is_fetched_by(text, base):
        # The referencing file pulls this companion itself, so it lands where the fetch puts it and
        # the payload mirrors the delivery layout rather than this directory.
        return base in names or Path(base).stem in names

    if str(here / base) in rels:
        return True

    if ref.startswith("/"):
        # An absolute runtime path. The question the payload can answer is whether this companion is
        # organised per role directory: if a SIBLING of the referencing file's directory carries the
        # basename, then each role directory is meant to carry its own copy and this one's absence is
        # the defect. Counting copies instead does not work — deleting one leaves a single copy, which
        # then looks like a payload that never organised it per directory, and the mutant survives.
        holders = {str(Path(r).parent) for r in rels if Path(r).name == base}
        sibling_holders = {d for d in holders
                           if d != str(here) and str(Path(d).parent) == str(here.parent)}
        if sibling_holders:
            return False
        # Otherwise try the path's own trailing directory before the bare basename, so
        # `/home/ubuntu/lib/cred-inject.sh` is answered by `…/lib/cred-inject.sh` and not by any
        # file that happens to share the name.
        tail = "/".join(Path(ref).parts[-2:])
        if any(r == tail or r.endswith("/" + tail) for r in rels):
            return True
        return base in names or Path(base).stem in names

    # A reference may also be written relative to the payload root rather than to the file.
    return str(Path(ref)) in rels


def check_d8(ctx):
    """Every companion a shipped file names must itself be shipped.

    A kit is derived from a git diff, and a diff is not a closure: a file that did not change never
    enters the kit even when a file that DID change now depends on it. That is not a hypothetical.
    A fluent-bit.conf was renamed to call add_timestamp.lua, the conf entered the kit and the lua did
    not, and because the installer syncs the whole S3 prefix, the new conf landed beside the OLD lua
    and Fluent Bit refused to start — which ABANDONs every host that boots after it.

    The kit's own coverage checks could not see this. They compare the payload's prefixes to the
    deployment's prefixes, so a payload holding 3 of 9 files satisfies every prefix and passes. What
    is missing is not a prefix; it is a reference.
    """
    kit = ctx.kit
    shipped, shipped_rels = _shipped_index(kit)
    if not shipped:
        return _missing("D8", "kit payload")
    # Resolve against the repository at the gateway ref, because a reference that names nothing in
    # the repository is a runtime-provided file (a distro package, a generated unit) and not a
    # packaging miss. Only a reference the repository CAN satisfy is a file the kit should carry.
    repo_index = {}
    for path in ctx.repo.rglob("*"):
        if path.is_file() and ".git/" not in str(path):
            repo_index.setdefault(path.name, []).append(str(path.relative_to(ctx.repo)))

    dangling, scanned = {}, 0
    for path in sorted(kit.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in _REF_SUFFIXES and not path.name.endswith(".patched"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        rel = str(path.relative_to(kit))
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            # A commented-out reference is not a reference — except in fluent-bit conf, where `#`
            # begins a comment but `script` lines are what we are after.
            if stripped.startswith("#") and "script" not in line:
                continue
            for label, pattern, suffixes in _REF_PATTERNS:
                for match in re.finditer(pattern, line, re.MULTILINE):
                    ref = match.group(1).strip("\"'")
                    base = Path(ref).name
                    if suffixes and not base.endswith(suffixes):
                        continue
                    if label == "systemd unit" and (
                            _UNIT_NAME_CONTEXT.search(line) or not _FILE_CONTEXT.search(line)):
                        continue      # a unit name, which systemd resolves; not a shipped file
                    if label == "python import":
                        root = ref.split(".")[0]
                        if root in _NOT_A_COMPANION or base in _NOT_A_COMPANION:
                            continue
                        candidates = repo_index.get(base + ".py", [])
                        if not candidates:
                            continue      # not a repository module, so third party
                        ref = base + ".py"   # `import x` names the file x.py
                        base = ref
                    else:
                        candidates = repo_index.get(base, [])
                        if not candidates:
                            continue      # provided by the machine, not by this repository
                    if _reference_satisfied(ref, rel, shipped, shipped_rels, text):
                        continue
                    row = dangling.setdefault(ref, {"kind": label, "repo_paths": candidates[:3],
                                                    "referenced_by": []})
                    if len(row["referenced_by"]) < 3:
                        row["referenced_by"].append("%s:%d" % (rel, lineno))

    return finding(
        "D8", "FAIL" if dangling else "PASS",
        "Shipped files were scanned for companions the kit does not carry.",
        {"inspected": scanned, "shipped_names": len(shipped), "dangling": dangling},
        ["%s [%s] is referenced by %s and exists at %s but the kit does not ship it"
         % (ref, row["kind"], row["referenced_by"][0], row["repo_paths"][0])
         for ref, row in sorted(dangling.items())],
        "Add each dangling companion to the kit payload and declare it in the manifest. Uploading a "
        "changed file into a prefix an installer syncs wholesale requires the whole reference "
        "closure, not only the files the diff touched.",
    )


# The prefix a booting machine downloads its runtime scripts from, and the manifest paths that
# supply them. Read from the repository rather than hardcoded: init-host.sh is what actually
# builds a host.
_FUTURE_PREFIX = "deployment/scripts/"


def _future_machine_keys(ctx):
    """{s3 key -> (manifest path, expected sha256)} for host scripts this kit replaces."""
    # Both files, because neither alone names the whole prefix: setup.sh is what UPLOADS every
    # object and init-host.sh is what DOWNLOADS the subset a host needs at boot. Reading only
    # init-host.sh silently shrank the check by one object, and a check that quietly covers less
    # than the step it guards is the failure mode this whole group exists to catch.
    referenced = set()
    for candidate in ("deploy/userdata/init-host.sh", "setup.sh"):
        path = ctx.repo / candidate
        if path.is_file():
            referenced |= set(re.findall(r"deployment/scripts/([A-Za-z0-9_./-]+)",
                                         path.read_text(encoding="utf-8", errors="replace")))
    if not referenced:
        return {}
    mapped = {}
    for item in ctx.manifest_entries():
        path, sha = item.get("path"), item.get("sha256")
        if not path or not sha or not path.startswith("deploy/userdata/"):
            continue
        rel = path[len("deploy/userdata/"):]
        if rel in referenced:
            mapped[_FUTURE_PREFIX + rel] = (path, sha)
    return mapped


def check_d9(ctx):
    """What a NEW machine downloads must match what the running machines were hot-fixed to.

    Fixing the running fleet and fixing the future-machine source are two different writes, and the
    kit used to describe the second one without shipping a command for it. The result on a real
    apply: 21 hosts hot-fixed to the new host-agent.py, `deployment/scripts/host-agent.py` still at
    its pre-patch bytes, and the next replacement host silently booting the old code. Nothing failed;
    the fleet just stopped being one fleet.

    This check reads the objects. It cannot be satisfied by a claim that the step ran.
    """
    expected = _future_machine_keys(ctx)
    if not expected:
        return _missing("D9", "future-machine host scripts")
    bucket = ctx.get("assets_bucket") or ctx.get("s3.assets_bucket")
    live_files = ctx.get("live_files") or {}
    rows, stale, unread = {}, [], []

    for key, (path, want) in sorted(expected.items()):
        got, source = None, None
        # An injected observation is allowed to stand in for the S3 read, so the check is runnable
        # offline and testable — but it must say which leg it used.
        if key in live_files:
            got, source = live_files[key], "injected observation"
        elif bucket and not ctx.offline:
            try:
                response = ctx.aws.call("s3", "get_object", Bucket=bucket, Key=key)
                got = hashlib.sha256(ctx.aws.body_bytes(response)).hexdigest()
                source = "s3://%s/%s" % (bucket, key)
            except Exception as error:            # noqa: BLE001 - reported, never swallowed
                source = "unreadable: %s" % error
        rows[key] = {"manifest_path": path, "expected_sha256": want,
                     "live_sha256": got, "read_from": source}
        if got is None:
            unread.append(key)
        elif got != want:
            stale.append(key)

    if stale:
        verdict = "FAIL"
    elif unread and len(unread) == len(rows):
        verdict = "INCONCLUSIVE"
    elif unread:
        verdict = "UNVERIFIED"
    else:
        verdict = "PASS"
    evidence = ["%s serves %s but this kit ships %s — a machine launched now boots the old file"
                % (key, (rows[key]["live_sha256"] or "?")[:12], rows[key]["expected_sha256"][:12])
                for key in stale]
    evidence += ["%s could not be read (%s)" % (key, rows[key]["read_from"]) for key in unread]
    return finding(
        "D9", verdict,
        "The future-machine source prefix was compared to the bytes this kit ships.",
        {"inspected": len(rows), "prefix": _FUTURE_PREFIX, "bucket": bucket,
         "stale": stale, "unread": unread, "objects": rows},
        evidence,
        "Run the kit's Step 3a to promote each replaced script into %s, then prove it by launching "
        "one replacement machine and hashing its on-disk copies against a hot-fixed sibling. A "
        "bucket read proves the upload; only a new machine proves the boot path consumes it."
        % _FUTURE_PREFIX,
    )
