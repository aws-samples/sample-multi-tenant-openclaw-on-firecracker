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
