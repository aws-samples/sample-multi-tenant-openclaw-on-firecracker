import json
import os
import re
import subprocess
from pathlib import Path

from lib.awsread import AwsReader
from lib import srcdefaults


HASH_KEYS = ("patch_sha256", "patch_hash", "sha256")
MODE_RE = re.compile(r"(?:install\s+-m\s+|mode\s+)(0?[0-7]{3,4})")


class ContextError(ValueError):
    pass


def _load_json(path, required=True):
    if not path:
        return {}
    target = Path(path)
    if not target.exists():
        if required:
            raise ContextError("JSON file not found: %s" % target)
        return {}
    try:
        return json.loads(target.read_text())
    except (OSError, ValueError) as error:
        raise ContextError("cannot read %s: %s" % (target, error))


def _hash_of(item):
    for key in HASH_KEYS:
        if item.get(key):
            return str(item[key])
    return None


def _mode_of(item):
    values = [item.get("mode"), item.get("install_mode")]
    for operation in item.get("operations") or []:
        values.extend([operation.get("resource"), operation.get("apply_cli")])
    for value in values:
        text = str(value or "").strip()
        if re.fullmatch(r"0?[0-7]{3,4}", text):
            return text.zfill(4)
        match = MODE_RE.search(text)
        if match:
            return match.group(1).zfill(4)
    return None


def normalize_manifest(manifest):
    entries = []
    for path, item in (manifest.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        entries.append({
            "path": path,
            "sha256": _hash_of(item),
            "artifact": item.get("artifact"),
            "operations": list(item.get("operations") or []),
            "mode": _mode_of(item),
        })
    for section in (manifest.get("replaces") or {}).values():
        if not isinstance(section, dict):
            continue
        for item in section.values():
            if not isinstance(item, dict) or not item.get("source"):
                continue
            source = str(item["source"]).rstrip("/")
            entries.append({
                "path": source,
                "sha256": _hash_of(item),
                "artifact": item.get("artifact"),
                "operations": [],
                "mode": _mode_of(item),
            })
    return entries


class ValidationContext:
    def __init__(self, args, aws=None):
        self.repo = Path(args.repo).resolve()
        self.kit = Path(args.kit).resolve()
        self.gateway_ref = args.gateway_ref
        self.region = args.region
        self.target_vms = args.target_vms
        self.offline = args.offline
        self.manifest = _load_json(self.kit / "manifest.json")
        self.env = _load_json(args.environment_json, required=False)
        self.aws = aws or AwsReader(region=self.region)
        self._defaults = None
        self._manifests = None

    def get(self, dotted, default=None):
        value = self.env
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def source_defaults(self):
        if self._defaults is None:
            self._defaults = srcdefaults.collect(self.repo)
        return self._defaults

    def manifest_entries(self):
        return normalize_manifest(self.manifest)

    def manifests(self):
        if self._manifests is not None:
            return self._manifests
        found = []
        for path in sorted((self.repo / "patch").glob("*/manifest.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            data["_manifest_path"] = str(path.relative_to(self.repo))
            found.append(data)
        self._manifests = found
        return found

    def _git(self, args, check=True):
        command = ["git", "-C", str(self.repo)] + list(args)
        return subprocess.run(
            command, check=check, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def git_exists(self, sha):
        if not sha:
            return False
        return self._git(["cat-file", "-e", str(sha) + "^{commit}"],
                         check=False).returncode == 0

    def git_is_ancestor(self, ancestor, descendant):
        if not ancestor or not descendant:
            return False
        return self._git(
            ["merge-base", "--is-ancestor", str(ancestor), str(descendant)],
            check=False,
        ).returncode == 0

    def git_bytes(self, ref, path):
        result = self._git(["show", "%s:%s" % (ref, path)])
        return result.stdout

    def git_tree_paths(self, ref, prefix):
        result = self._git(["ls-tree", "-r", "--name-only", ref, "--", prefix])
        return result.stdout.decode("utf-8", "replace").splitlines()


def add_arguments(parser):
    parser.add_argument("--kit", required=True)
    parser.add_argument("--gateway-ref", default="origin/gateway")
    parser.add_argument("--environment-json")
    parser.add_argument("--region")
    parser.add_argument("--target-vms", type=int)
    parser.add_argument("--report", required=True)
    parser.add_argument("--group")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--repo", default=os.getcwd())
