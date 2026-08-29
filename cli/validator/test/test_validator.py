import base64
import hashlib
import io
import json
import os
import re
import sys
import pathlib
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from checks import CHECKS
from checks.kitproof import check_d8, check_d9
from lib.context import normalize_manifest  # noqa: E402
from lib import awsread  # noqa: E402
SCRIPT_BYTES = b"#!/bin/sh\nprintf ok\n"
SCRIPT_HASH = hashlib.sha256(SCRIPT_BYTES).hexdigest()

#: 自守卫扫描要排除的生成物目录。**只排除生成物,不排除源码** —— 见
#: `test_read_only_contract_scan_reaches_every_module` 钉住这条边界。
#:
#: 为什么必须排除 `__pycache__`:本文件把自己的断言针写成 `"invoke" + "_function"`、
#: `"A" + "KIA"`,好让针本身不出现在源码里。CPython 的常量折叠会把这些拼接在编译期
#: 折成字面量写进 `.pyc`,于是「扫整棵树」的自守卫在第二次运行时会扫到自己的字节码
#: 并报红 —— 假红,而且看起来像真的泄漏了坐标。判据只能是源码,不是编译产物。
_GENERATED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".venv", ".git"})


def _package_source_files():
    root = Path(__file__).resolve().parents[1]
    return sorted(p for p in root.rglob("*")
                  if p.is_file()
                  and not _GENERATED_DIRS.intersection(p.relative_to(root).parts))


def _package_source_text():
    return "\n".join(p.read_text(errors="replace") for p in _package_source_files())
class StubAws:
    def __init__(self):
        self.responses = {}
        self.calls = []
        self.available = True
    def set(self, service, method, value):
        self.responses[(service, method)] = value
    def call(self, service, method, **kwargs):
        self.calls.append((service, method, kwargs))
        value = self.responses.get((service, method), {})
        return value(**kwargs) if callable(value) else value
    def body_bytes(self, response, key="Body"):
        value = response.get(key, b"")
        return value.read() if hasattr(value, "read") else value
    def lambda_package(self, function_name, qualifier=None):
        return self.responses[("lambda", "package")]
class FakeContext:
    def __init__(self, root, bad=None):
        self.repo = Path(root)
        self.kit = self.repo / "patch" / "kit"
        self.gateway_ref = "GATEWAY_REF"
        self.region = "TEST_REGION"
        self.target_vms = 4
        self.offline = False
        self.aws = StubAws()
        self.bad = bad
        self.manifest = self._manifest()
        self.env = self._environment()
        self._defaults = self._source_defaults()
        self.git_commits = {"BASE_SHA", "PATCH_SHA"}
        self.other_manifests = []
        self._write_sources()
        self._set_aws()
        self._mutate()
    def _manifest(self):
        return {
            "id": "kit", "base_sha": "BASE_SHA", "patch_sha": "PATCH_SHA",
            "paths": {
                "deploy/userdata/launch-vm.sh": {
                    "artifact": "launch-vm.sh",
                    "patch_sha256": SCRIPT_HASH,
                    "operations": [{
                        "apply_cli": "aws s3api head-object prev VersionId; "
                                     "aws s3 cp source s3://$BUCKET/key",
                        "resource": "file:run.sh mode 0755",
                    }],
                }
            },
            "verifications": [{"action": "grep -c item file || true"}],
        }
    def _environment(self):
        live = {"sha256": SCRIPT_HASH, "mode": "0755", "executable": True}
        base = {
            "lambda_link": {
                "function": "FUNCTION_PLACEHOLDER", "serving_qualifier": "live",
                "dispatch_sqs_esm_binds": "$LATEST",
            },
            "asg": {"lt_id": "LT_PLACEHOLDER", "lt_version_pinned": "7"},
            "hosts": {"count": 2, "instance_ids": ["HOST_A", "HOST_B"]},
            "control_plane_api": {"id": "API_PLACEHOLDER",
                                  "deployed_stages": [{"stage": "STAGE_PLACEHOLDER"}]},
            "lt_bootstrap": {"source_path": "deploy/userdata/launch-vm.sh"},
            "live_files": {"deploy/userdata/launch-vm.sh": live},
            "scale": {
                "per_host_slots": 2, "target_tps": 2, "target_write_rate": 2,
                "deadline_budget": 9, "worst_single_seconds": 8,
                "table_name": "TABLE_PLACEHOLDER",
                "queue_url": "QUEUE_PLACEHOLDER",
            },
            "ssm": {
                "parameter_paths": ["/PARAM_PREFIX/"],
                "references": {"switch": [{"exists": True, "role": "primary"}]},
                "steady_state": {"switch": "go"},
            },
        }
        base.update(self._runtime_environment(live))
        return base
    def _runtime_environment(self, live):
        return {
            "iam": {
                "roles": [{
                    "name": "ROLE_PLACEHOLDER",
                    "source_arn": ":".join(["arn", "partition", "iam", "",
                                           "ACCOUNT_PLACEHOLDER", "role/ROLE_PLACEHOLDER"]),
                    "sources": ["deploy/lambda/api/core/clients.py"],
                }],
                "policy_documents": [{
                    "role": "ROLE_PLACEHOLDER",
                    "document": {"Statement": [{
                        "Effect": "Deny",
                        "Action": "dynamodb:GetItem",
                        "Resource": "*",
                        "Condition": {"ForAllValues:StringEquals": {
                            "dynamodb:LeadingKeys": ["PROTECTED_KEY"]
                        }},
                    }]},
                }],
            },
            "known_dimension_values": {"NodeId": ["NODE_PLACEHOLDER"]},
            "route_samples": [{"tenant_id": "TENANT_PLACEHOLDER",
                "control": {"host": "HOST_A", "port": "PORT_PLACEHOLDER"},
                "data": {"host": "HOST_A", "port": "PORT_PLACEHOLDER"},
            }],
            "replica_endpoints": [{
                "name": "reader-endpoint", "value": "ENDPOINT_PLACEHOLDER",
                "declared_role": "reader", "actual_role": "reader",
            }],
            "residue_observations": [{
                "kind": "tenant", "value": "none", "present": False
            }],
            "probes": [{"kind": "curl", "object_count": 1, "status": 200}],
        }
    def _source_defaults(self):
        return {"config": {"vm.host_launch_slots": 2},
            "env": {"SETTING": {"value": "expected", "source": "clients.py:1"}},
            "deadlines": {"create": 9}, "routes": [{"path": "/items", "method": "GET"}],
            "parameter_specs": {"switch": {"type": "str", "default": "go"}},
        }
    def _write_sources(self):
        files = {
            "deploy/userdata/launch-vm.sh": SCRIPT_BYTES.decode(),
            "deploy/lambda/api/core/clients.py":
                "import boto3\ns3 = boto3." "client('s3')\ns3.get_object()\n",
            "deploy/lambda/api/core/ssm_dispatch.py":
                "COMMAND = '/home/ubuntu/launch-vm.sh value'\n",
            "deploy/lambda/api/services/hosts.py":
                "def hosts(rows):\n return [r for r in rows "
                "if not str(r.get('instance_id','')).startswith('__')]\n",
            "deploy/lambda/api/services/errors.py":
                "def closed():\n raise RuntimeError('ASCII failure')\n",
            "patch/kit/launch-vm.sh": SCRIPT_BYTES.decode(),
            "patch/kit/lib/tool.sh": "#!/bin/bash\nprintf ok\n",
            "patch/push-marker-PATCH_SHA.md": "PATCH_SHA\n",
            "patch/manifest-PATCH_SHA.json": '{"source_commit":"PATCH_SHA"}\n',
        }
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    def _zip(self, missing=False):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("core/__init__.py", "")
            if not missing:
                archive.writestr("core/auth.py", "")
        return buf.getvalue()
    def _set_aws(self):
        cfg = {
            "CodeSha256": "CODE_SHA", "Environment": {"Variables": {"SETTING": "expected"}},
            "DeadLetterConfig": {"TargetArn": "DLQ_PLACEHOLDER"}, "Layers": [],
        }
        self.aws.set("lambda", "package", self._zip())
        self.aws.set("lambda", "get_function_configuration", cfg)
        self.aws.set("lambda", "list_aliases", {
            "Aliases": [{"Name": "live", "FunctionVersion": "3"}]})
        self.aws.set("lambda", "list_event_source_mappings",
                     {"EventSourceMappings": [{"BatchSize": 2, "ScalingConfig":
                                                {"MaximumConcurrency": 1}}]})
        user_data = base64.b64encode(
            (self.repo / "deploy/userdata/launch-vm.sh").read_bytes())
        self.aws.set("ec2", "describe_launch_template_versions",
                     {"LaunchTemplateVersions": [{"LaunchTemplateData":
                                                  {"UserData": user_data.decode()}}]})
        export = {"paths": {"/items": {"get": {}, "options": {
            "x-amazon-apigateway-auth": {"type": "NONE"}}}}}
        self.aws.set("apigateway", "get_export", {
            "body": io.BytesIO(json.dumps(export).encode())})
        self.aws.set("ssm", "get_parameters_by_path", {
            "Parameters": [{"Name": "/PARAM_PREFIX/switch", "Value": "go"}]})
        self.aws.set("sqs", "get_queue_attributes", {
            "Attributes": {"VisibilityTimeout": "9"}})
        self.aws.set("dynamodb", "describe_table", {
            "Table": {"BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}}})
        self.aws.set("iam", "simulate_principal_policy", lambda **kwargs: {
            "EvaluationResults": [{"EvalDecision":
                "explicitDeny" if kwargs.get("ContextEntries") else "allowed"}]})
        self.aws.set("logs", "filter_log_events", {"events": []})
        self.aws.set("lambda", "list_functions", {
            "Functions": [{"FunctionName": "FUNCTION_PLACEHOLDER"}]})
        self.aws.set("cloudwatch", "get_metric_statistics", {
            "Datapoints": [{"Sum": 1}]})
        self.aws.set("cloudwatch", "describe_alarms", {"MetricAlarms": [{
            "AlarmName": "ALARM_PLACEHOLDER", "StateValue": "OK",
            "Dimensions": [{"Name": "NodeId", "Value": "NODE_PLACEHOLDER"}],
        }]})
    def _mutate(self):
        if self.bad and self.bad[0] in "ABC":
            self._mutate_abc()
        else:
            self._mutate_de()
    def _mutate_abc(self):
        bad = self.bad
        if bad == "A1":
            self.env["live_files"]["deploy/userdata/launch-vm.sh"]["sha256"] = "BAD_HASH"
        elif bad == "A2":
            self.aws.set("lambda", "package", self._zip(True))
        elif bad == "A3":
            self.env["asg"]["lt_version_pinned"] = "$Default"
        elif bad == "A4":
            self.aws.set("apigateway", "get_export",
                         {"body": io.BytesIO(b'{"paths":{}}')})
        elif bad == "B2":
            self.env["scale"]["per_host_slots"] = 1
        elif bad == "B3":
            self.aws.set("ssm", "get_parameters_by_path",
                         {"Parameters": [{"Name": "/PARAM_PREFIX/switch", "Value": "stop"}]})
        elif bad == "B4":
            self.aws.set("sqs", "get_queue_attributes",
                         {"Attributes": {"VisibilityTimeout": "20"}})
        elif bad == "C1":
            self.aws.set("iam", "simulate_principal_policy",
                         {"EvaluationResults": [{"EvalDecision": "implicitDeny"}]})
        elif bad == "C2":
            self.aws.set("iam", "simulate_principal_policy",
                         {"EvaluationResults": [{"EvalDecision": "allowed"}]})
        elif bad == "C3":
            self.aws.set("logs", "filter_log_events",
                         {"events": [{"timestamp": 1, "message": "ERROR requestId=REQ"}]})
        elif bad == "C4":
            self.env["known_dimension_values"]["NodeId"] = []
    def _mutate_de(self):
        bad = self.bad
        if bad == "D1":
            self.git_commits.remove("PATCH_SHA")
        elif bad == "D2":
            self.other_manifests = [{"id": "new", "paths": {
                "deploy/userdata/launch-vm.sh": {"patch_sha256": SCRIPT_HASH}},
                "base_sha": "BASE_SHA", "patch_sha": "PATCH_SHA"}]
        elif bad == "D3":
            (self.repo / "patch/push-marker-PATCH_SHA.md").unlink()
        elif bad == "D4":
            (self.kit / "__pycache__").mkdir()
        elif bad == "D5":
            self.env["live_files"]["deploy/userdata/launch-vm.sh"]["executable"] = False
        elif bad == "D6":
            self.manifest["paths"]["deploy/userdata/launch-vm.sh"]["operations"][0][
                "apply_cli"] = "aws s3 cp source s3://$BUCKET/key"
        elif bad == "D7":
            (self.kit / "lib/tool.sh").write_text("mapfile values < input\n")
        elif bad == "E1":
            self.env["lambda_versions"] = {"$LATEST": {"CodeSha256": "NEW"},
                                           "3": {"CodeSha256": "OLD"}}
        elif bad == "E2":
            self.env["probes"][0]["object_count"] = 0
        elif bad == "E3":
            self.env["live_files"]["deploy/userdata/launch-vm.sh"]["sha256"] = "BAD_HASH"
        elif bad == "E4":
            self.env["route_samples"][0]["data"]["host"] = "HOST_B"
        elif bad in ("E5", "E6"):
            self.env["replica_endpoints"][0]["actual_role"] = "primary"
        elif bad == "E7":
            (self.repo / "deploy/lambda/api/services/errors.py").write_text(
                "def closed():\n raise RuntimeError('bad \\u4e8c')\n")
        elif bad == "E8":
            (self.repo / "deploy/lambda/api/services/hosts.py").write_text(
                "def hosts(hosts_table):\n return hosts_table.scan()\n")
        elif bad == "E9":
            self.env["residue_observations"][0]["present"] = True
    def get(self, dotted, default=None):
        value = self.env
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    def source_defaults(self):
        return self._defaults
    def manifest_entries(self):
        item = self.manifest["paths"]["deploy/userdata/launch-vm.sh"]
        return [{"path": "deploy/userdata/launch-vm.sh", "sha256": item["patch_sha256"],
                 "artifact": item["artifact"], "operations": item["operations"],
                 "mode": "0755"}]
    def manifests(self):
        return [self.manifest] + self.other_manifests
    def git_exists(self, sha):
        return sha in self.git_commits
    def git_is_ancestor(self, ancestor, descendant):
        return ancestor == descendant
    def git_bytes(self, ref, path):
        return (self.repo / path).read_bytes()
class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self.tmp.cleanup()
    def run_check(self, check_id, bad=False):
        ctx = FakeContext(self.tmp.name, check_id if bad else None)
        result = CHECKS[check_id][1](ctx)
        rows = result if isinstance(result, list) else [result]
        return rows
    def good(self, check_id):
        rows = self.run_check(check_id)
        self.assertTrue(rows)
        self.assertNotIn("FAIL", {row.verdict for row in rows})
        for row in rows:
            self.assertTrue(row.readings)
            self.assertTrue(row.remediation)
    def bad(self, check_id):
        rows = self.run_check(check_id, True)
        self.assertIn("FAIL", {row.verdict for row in rows})
    def test_b1(self):
        self.good("B1")
    def test_b1_mutant_undeclared(self):
        rows = self.run_check("B1")
        self.assertIn("classifications", rows[0].readings)
    def test_d2_byte_covered_but_different_hop_is_not_removable(self):
        # The branch that keeps a HISTORICAL kit alive. A kit whose artifacts are all covered by a
        # newer one is still needed when it served a DIFFERENT upgrade hop — customers sitting on the
        # older baseline apply it. Reporting it as removable would delete a live delivery path, which
        # is what an earlier revision of this check did to 311-post-266-rollup.
        ctx = FakeContext(self.tmp.name, None)
        ctx.other_manifests = [{
            "id": "newer-kit",
            "base_sha": "OTHER_BASE", "patch_sha": "OTHER_PATCH",
            "paths": {"deploy/userdata/launch-vm.sh": {"patch_sha256": SCRIPT_HASH}},
        }]
        row = CHECKS["D2"][1](ctx)
        self.assertEqual("PASS", row.verdict)
        self.assertEqual([], row.readings["obsolete"])
        covered = row.readings["byte_covered_other_hops"]
        self.assertTrue(covered, "the overlap must still be reported, just not as removable")
        self.assertEqual("newer-kit", covered[0]["covered_by"])

    def test_e1_missing_latest_is_inconclusive(self):
        ctx = FakeContext(self.tmp.name, None)
        ctx.env["lambda_versions"] = {"3": {"CodeSha256": "PUBLISHED_SHA"}}
        row = CHECKS["E1"][1](ctx)
        self.assertEqual("INCONCLUSIVE", row.verdict)
        self.assertEqual(0, row.readings["comparable_pairs"])
        self.assertFalse(row.readings["aliases"][0]["comparable"])
        self.assertIn("$LATEST", row.readings["aliases"][0]["not_comparable_reason"])

    def test_e1_comparable_divergence_still_fails(self):
        ctx = FakeContext(self.tmp.name, "E1")
        row = CHECKS["E1"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertEqual(1, row.readings["comparable_pairs"])

    def test_a3_templated_source_skips_byte_comparison(self):
        ctx = FakeContext(self.tmp.name, None)
        ctx.env["asg"]["lt_version_pinned"] = "$Default"
        source = ctx.repo / "deploy/userdata/launch-vm.sh"
        source.write_text("#!/bin/sh\nprintf '{{VALUE}}'\n")
        row = CHECKS["A3"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertIn("floating version", row.evidence)
        self.assertNotIn("user data differs from gateway source", row.evidence)
        self.assertIsNone(row.readings["source_match"])
        self.assertFalse(row.readings["source_comparable"]["comparable"])
        self.assertIn("template tokens", row.readings["source_comparable"]["reason"])

    def test_a3_token_free_source_mismatch_still_fails(self):
        ctx = FakeContext(self.tmp.name, None)
        source = ctx.repo / "deploy/userdata/launch-vm.sh"
        source.write_text("#!/bin/sh\nprintf changed\n")
        row = CHECKS["A3"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertIn("user data differs from gateway source", row.evidence)
        self.assertTrue(row.readings["source_comparable"]["comparable"])
        self.assertIsNone(row.readings["source_comparable"]["reason"])

    def test_e10(self):
        self.good("E10")
    def test_e10_mutant_annotations(self):
        rows = self.run_check("E10")
        self.assertEqual(5, rows[0].readings["known_count"])
    def test_missing_boto3_is_inconclusive(self):
        reader = awsread.AwsReader(region="TEST_REGION", client_factory=None)
        self.assertFalse(reader.available)
    def test_read_only_rejects_write_method(self):
        with self.assertRaises(awsread.ReadOnlyViolation):
            awsread.assert_read_only_method("put_item")
    def test_ssm_payload_allowlist(self):
        awsread.assert_read_only_payload(
            "ssm", "send_command", {"Parameters": {"commands": ["stat /tmp/PATH"]}})
        with self.assertRaises(awsread.ReadOnlyViolation):
            awsread.assert_read_only_payload(
                "ssm", "send_command", {"Parameters": {"commands": ["rm /tmp/PATH"]}})
    def test_read_only_contract(self):
        self.assertTrue(awsread.ALLOWED_EXACT.issubset(awsread.READ_ONLY_ALLOWLIST))
        text = _package_source_text()
        forbidden = ["invoke" + "_function", "invoke" + "_async"]
        self.assertFalse(any(item in text for item in forbidden))
        calls = set(re.findall(r'\.call\([^,]+,\s*["\']([a-z_0-9]+)', text))
        self.assertTrue(calls.issubset(awsread.READ_ONLY_ALLOWLIST))
    def test_no_duplicate_validator_copy_in_repo(self):
        """仓库只能有一份 validator。

        这条守卫来自一次真实的分叉:仓库里同时存在过 `patch/validator/` 与 `cli/validator/`,
        而**旧那份完全没有 F/G 两组** —— 没有 `checks/channels.py`、没有 `checks/controlplane.py`、
        没有 `lib/discover.py`、没有 `test/test_channels.py`,也就是 11 条数据面通道与控制面漂移
        检查一条都不在。两份并存时,改动落到哪一份取决于人当时打开了哪个目录;而缺 F/G 的那份
        跑出来照样是一片绿。

        判据刻意**不是**「`patch/validator/` 不存在」:那只挡住这一个具体路径,换个目录名就又漂了。
        判据是「整个仓库里 `oc-prelaunch-validate` 这个入口恰好只有一个,且它在 `cli/validator/`」。
        包搬家也要红 —— 那时必须同步改这条守卫和所有引用,而不是让引用悄悄变成死链。
        """
        repo = Path(__file__).resolve().parents[3]
        # `parents[3]` 只有在包位于 `<repo>/cli/validator/test/` 时才等于仓库根。包一搬家它就会
        # 指到仓库**外面**,于是下面的 os.walk 会去遍历一棵任意大的树(实测搬到仓库根之后这条用例
        # 从 0.02s 变成 13.8s,走的是仓库的父目录)。所以先证明推导出的根真的是本仓库,让「包搬了」
        # 快速失败在这里,而不是表现成一次莫名其妙的慢。
        self.assertTrue(
            (repo / "deploy").is_dir() and (repo / "patch").is_dir(),
            "%s 不像本仓库的根 —— `parents[3]` 的假设是包位于 <repo>/cli/validator/test/,"
            "包一旦搬家这个推导就会指到仓库外面。搬家时请同步修正这条守卫。" % repo)
        excluded = {".git", "__pycache__", ".venv", ".pytest_cache",
                    "node_modules", "cdk.out"}
        found = []
        for root, dirnames, filenames in os.walk(repo):
            dirnames[:] = [name for name in dirnames if name not in excluded]
            if "oc-prelaunch-validate" in filenames:
                found.append(Path(root) / "oc-prelaunch-validate")

        relative = sorted(path.relative_to(repo) for path in found)
        paths = "\n".join(path.as_posix() for path in relative) or "(none)"
        self.assertEqual(
            1, len(found),
            "Expected exactly one validator entry point in the repository; "
            "delete duplicate copies or restore cli/validator/oc-prelaunch-validate. "
            "Found:\n%s" % paths)
        entry_point = found[0]
        self.assertEqual(
            "validator", entry_point.parent.name,
            "The sole validator entry point must remain under cli/validator; "
            "update this guard and every repository reference if the package moves. "
            "Found:\n%s" % paths)
        self.assertEqual(
            "cli", entry_point.parent.parent.name,
            "The sole validator entry point must remain under cli/validator; "
            "update this guard and every repository reference if the package moves. "
            "Found:\n%s" % paths)
    def test_read_only_contract_scan_reaches_every_module(self):
        """守住上面两条扫描的分母:排除清单只该挡生成物,不该挡源码。

        排除是按目录名做的,一旦有人把某个真模块塞进被排除的目录,上面两条会静默变成
        扫不到它 —— 通过依旧是绿的,覆盖却少了。这里把分母本身钉住。
        """
        root = Path(__file__).resolve().parents[1]
        tracked = {p.relative_to(root).as_posix()
                   for p in _package_source_files()}
        for expected in ("oc-prelaunch-validate", "lib/awsread.py",
                         "lib/context.py", "lib/discover.py",
                         "checks/__init__.py", "checks/channels.py",
                         "checks/controlplane.py", "test/test_validator.py",
                         "test/test_channels.py"):
            self.assertIn(expected, tracked)
        self.assertFalse([name for name in tracked if "__pycache__" in name])
    def test_coordinate_self_guard(self):
        text = _package_source_text()
        suffix = "." + "amazon" + "aws" + "." + "com"
        needles = [
            re.compile(r"(?<!\d)\d{12}(?!\d)"),
            re.compile("A" + "KIA"),
            re.compile(r"\bi-[0-9a-f]{8,}\b"),
            re.compile(r"\blt-[0-9a-f]{8,}\b"),
            re.compile(r"\b(?:us|ap|eu|ca|sa|me|af)-[a-z]+-\d\b"),
        ]
        self.assertFalse(any(pattern.search(text) for pattern in needles))
        self.assertNotIn(suffix, text)


MUTANTS = [
    ("A1", "live_hash"), ("A2", "missing_core_file"),
    ("A3", "floating_version"), ("A4", "missing_route"),
    ("B2", "host_capacity"), ("B3", "switch_state"), ("B4", "visibility"),
    ("C1", "under_grant"), ("C2", "mixed_batch"), ("C3", "error_log"),
    ("C4", "missing_dimension"), ("D1", "missing_commit"),
    ("D2", "superseded_kit"), ("D3", "marker_missing"), ("D4", "bytecode"),
    ("D5", "not_executable"), ("D6", "no_anchor"), ("D7", "bash4"),
    ("E1", "alias_diverged"), ("E2", "empty_probe"), ("E3", "live_drift"),
    ("E4", "route_mismatch"), ("E5", "role_assumption"),
    ("E6", "name_semantics"), ("E7", "non_ascii"),
    ("E8", "synthetic_rows"), ("E9", "residue"),
]


def _case(check_id, mutant):
    def test(self):
        self.bad(check_id) if mutant else self.good(check_id)
    return test


for _check_id, _mutant_name in MUTANTS:
    setattr(ValidatorTests, "test_" + _check_id.lower(), _case(_check_id, False))
    setattr(ValidatorTests, "test_%s_mutant_%s" %
            (_check_id.lower(), _mutant_name), _case(_check_id, True))


class _TinyCtx:
    """The smallest context D8/D9 read: a repo, a kit payload, a manifest, an injected observation."""

    def __init__(self, root, manifest=None, env=None, offline=True):
        self.repo = pathlib.Path(root)
        self.kit = self.repo / "kit"
        self.offline = offline
        self.manifest = manifest or {"paths": {}}
        self.env = env or {}
        self.aws = StubAws()

    def get(self, dotted, default=None):
        node = self.env
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def manifest_entries(self):
        return normalize_manifest(self.manifest)


def _write(path, body=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


class DanglingReferenceTests(unittest.TestCase):
    """D8 — a companion a shipped file names must itself be shipped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        # A repository that HAS the companions. A reference the repository cannot satisfy is a
        # machine-provided file, not a packaging miss, so the repository copy is what makes the
        # reference actionable.
        for role in ("host", "edge"):
            _write(self.root / "deploy/edge/fluent-bit" / role / "add_timestamp.lua", "-- filter\n")
            _write(self.root / "deploy/edge/fluent-bit" / role / "parsers.conf", "[PARSER]\n")
        _write(self.root / "deploy/userdata/route_ops.py", "PORT_RANGE_LOW = 1\n")
        _write(self.root / "deploy/userdata/host-agent.service", "[Unit]\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _kit(self, *, host_filter=True, edge_filter=True, route_ops=True):
        kit = self.root / "kit"
        # The host conf names its filter bare; the edge conf names the runtime path. Both shapes are
        # real and they resolve differently, which is why both are here.
        _write(kit / "host-scripts/edge/fluent-bit/host/fluent-bit.conf",
               "[FILTER]\n    Name lua\n    script add_timestamp.lua\n"
               "    Parsers_File parsers.conf\n")
        _write(kit / "host-scripts/edge/fluent-bit/edge/fluent-bit.conf",
               "[FILTER]\n    Name lua\n    script /etc/fluent-bit/add_timestamp.lua\n"
               "    Parsers_File /etc/fluent-bit/parsers.conf\n")
        for role, present in (("host", host_filter), ("edge", edge_filter)):
            _write(kit / "host-scripts/edge/fluent-bit" / role / "parsers.conf", "[PARSER]\n")
            if present:
                _write(kit / "host-scripts/edge/fluent-bit" / role / "add_timestamp.lua", "-- f\n")
        _write(kit / "host-scripts/host-agent.py.patched",
               "import os\nimport json\nimport route_ops\n")
        if route_ops:
            _write(kit / "host-scripts/route_ops.py", "PORT_RANGE_LOW = 1\n")
        return kit

    def _run(self, **kwargs):
        self._kit(**kwargs)
        return check_d8(_TinyCtx(self.root))

    def test_complete_payload_passes(self):
        row = self._run()
        self.assertEqual(row.verdict, "PASS")
        self.assertEqual(row.readings["dangling"], {})

    def test_missing_bare_named_filter_fails(self):
        row = self._run(host_filter=False)
        self.assertEqual(row.verdict, "FAIL")
        self.assertIn("add_timestamp.lua", row.readings["dangling"])

    def test_missing_absolute_named_filter_fails(self):
        # The mutant that survived two revisions: the edge conf names /etc/fluent-bit/…, and matching
        # the basename anywhere let the HOST copy answer for the missing edge one.
        row = self._run(edge_filter=False)
        self.assertEqual(row.verdict, "FAIL")
        self.assertTrue([k for k in row.readings["dangling"] if "add_timestamp.lua" in k])

    def test_missing_imported_module_fails(self):
        row = self._run(route_ops=False)
        self.assertEqual(row.verdict, "FAIL")
        self.assertIn("route_ops.py", row.readings["dangling"])

    def test_stdlib_imports_are_not_companions(self):
        row = self._run()
        self.assertEqual(row.verdict, "PASS")   # `import os` / `import json` must not be reported

    def test_unit_dependency_name_is_not_a_missing_file(self):
        # `After=…host-agent.service` is a unit systemd resolves itself. Reporting it made a correct
        # payload read as broken, and a check that cries wolf gets switched off.
        kit = self._kit()
        _write(kit / "launch-template/init-host.sh.patched",
               "cat > /etc/systemd/system/x.service <<EOF\n"
               "After=network.target host-agent.service\nEOF\n"
               "systemctl is-active host-agent.service\n")
        row = check_d8(_TinyCtx(self.root))
        self.assertEqual(row.verdict, "PASS")

    def test_self_fetched_companion_need_not_be_co_located(self):
        # init-host.sh downloads the unit into /opt/openclaw/spire-kit/, so the payload mirrors the
        # delivery layout, not the referencing file's directory.
        kit = self._kit()
        _write(self.root / "deploy/userdata/spire-kit/spire-join-broker.service", "[Unit]\n")
        _write(kit / "launch-template/init-host.sh.patched",
               'for _f in install.sh spire-join-broker.service; do\n'
               '  aws s3 cp s3://$B/deployment/scripts/spire-kit/${_f} $D/${_f}\n'
               'done\n')
        _write(kit / "host-scripts/spire-kit/spire-join-broker.service", "[Unit]\n")
        row = check_d8(_TinyCtx(self.root))
        self.assertEqual(row.verdict, "PASS")
        kit_unit = kit / "host-scripts/spire-kit/spire-join-broker.service"
        kit_unit.unlink()
        self.assertEqual(check_d8(_TinyCtx(self.root)).verdict, "FAIL")


class FutureMachineSourceTests(unittest.TestCase):
    """D9 — what a new machine downloads must match what the running machines were fixed to."""

    KEY = "deployment/scripts/host-agent.py"
    WANT = "a" * 64
    OLD = "b" * 64

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        _write(self.root / "deploy/userdata/init-host.sh",
               "aws s3 cp s3://$B/deployment/scripts/host-agent.py /opt/openclaw/host-agent.py\n")
        _write(self.root / "setup.sh",
               "aws s3 cp x s3://$B/deployment/scripts/oc-egress-sim.py\n")
        self.manifest = {"paths": {
            "deploy/userdata/host-agent.py": {"artifact": "host-agent.py.patched",
                                              "patch_sha256": self.WANT},
        }}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, live=None):
        env = {"live_files": live} if live is not None else {}
        return check_d9(_TinyCtx(self.root, manifest=self.manifest, env=env))

    def test_matching_object_passes(self):
        self.assertEqual(self._run({self.KEY: self.WANT}).verdict, "PASS")

    def test_stale_object_fails(self):
        # The real shape: 21 hosts hot-fixed, the prefix a new host reads still at pre-patch bytes.
        row = self._run({self.KEY: self.OLD})
        self.assertEqual(row.verdict, "FAIL")
        self.assertEqual(row.readings["stale"], [self.KEY])

    def test_no_observation_refuses_rather_than_passes(self):
        self.assertEqual(self._run().verdict, "INCONCLUSIVE")

    def test_discovery_reads_setup_sh_too(self):
        # Reading only init-host.sh silently shrank the check by one object. A check that covers less
        # than the step it guards is the failure this group exists to catch.
        self.manifest["paths"]["deploy/userdata/oc-egress-sim.py"] = {
            "artifact": "oc-egress-sim.py", "patch_sha256": self.WANT}
        row = self._run({self.KEY: self.WANT,
                         "deployment/scripts/oc-egress-sim.py": self.WANT})
        self.assertEqual(row.readings["inspected"], 2)


if __name__ == "__main__":
    unittest.main()
