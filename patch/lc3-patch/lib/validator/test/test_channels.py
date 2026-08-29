import base64
import hashlib
import io
import json
import shlex
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checks import CHECKS  # noqa: E402
from checks.channels import (  # noqa: E402
    ASSET_DIR,
    DISK_PATHS,
    MARKER_PATH,
    PLATFORM_ENV_PATH,
    SKILL_STATE_PATH,
    SSM_AGENT_CONFIG_PATH,
    _edge_bundle,
    _fetch_guards,
    _guard_satisfied,
    _obs_assets,
    _obs_landing,
    _sha,
    core_object_inventory,
)
from lib.awsread import (  # noqa: E402
    AwsReadError,
    assert_read_only_method,
)
from lib.discover import discover  # noqa: E402
from lib.result import report_document  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
SECRET = "SUPER_SECRET_SENTINEL_VALUE"
FC_ARCHIVES = {
    "aarch64": b"fixture-firecracker-aarch64",
    "x86_64": b"fixture-firecracker-x86_64",
}
PROVISION_SOURCE = """#!/bin/bash
FC_VER="${FC_VERSION:-v-fixture}"
_fc_expected_sha() {
  case "${FC_VER}:${ARCH}" in
    v-fixture:aarch64) printf '%s' ;;
    v-fixture:x86_64)  printf '%s' ;;
    *) return 0 ;;
  esac
}
""" % tuple(hashlib.sha256(FC_ARCHIVES[name]).hexdigest()
            for name in ("aarch64", "x86_64"))
#: The rendered host bootstrap object is content-addressed: the sha256 in its key is the sha256
#: of its bytes, and the launch-template user data verifies that before executing. The fixture
#: has to honour that or F2's digest comparison is testing a lie -- the earlier `"a" * 64`
#: placeholder key made every fixture bootstrap object permanently self-inconsistent.
HOST_BOOTSTRAP_BODY = (
    'MANIFEST_URL="s3://${ASSETS_BUCKET}/deployment/rootfs/manifest.json"\n'
).encode("utf-8")
HOST_BOOTSTRAP_SHA = hashlib.sha256(HOST_BOOTSTRAP_BODY).hexdigest()
MANIFEST = {
    "rootfs": "rootfs.gz",
    "data_template": "data.gz",
    "immutable": "immutable.gz",
    "version": "fixture-v1",
}


class FixtureContext:
    def __init__(self, bad=None, missing=False):
        self.repo = ROOT
        self.kit = ROOT / "patch" / "lc2-patch"
        self.gateway_ref = "fixture-gateway"
        self.region = "fixture-region"
        self.target_vms = 2
        self.offline = False
        self.bad = bad
        self.missing = missing
        self.env = {}
        self.manifest = {"paths": {}}
        self.host_lt_version = "$Latest" if bad == "F2" else "$Default"
        self.host_lt_shape = "mixed"
        self.edge_lt_version = "$Default"
        self.edge_lt_shape = "plain"
        self.host_bootstrap_shas = {1: HOST_BOOTSTRAP_SHA, 2: HOST_BOOTSTRAP_SHA}
        self.lambda_extra_members = {}
        self.lambda_package_entries = None
        self.missing_bootstrap_object = False
        self._discovery = self._make_discovery()
        self.aws = FixtureReader(self)
        self.inventory, self.inventory_gaps = core_object_inventory(self)
        self.aws.prepare()

    def _make_discovery(self):
        values = {
            "assets_bucket": "fixture-assets",
            "buckets": ["fixture-assets"],
            "host_asg": "fixture-host-asg",
            "edge_asg": "fixture-edge-asg",
            "host_lt": "lt-host-fixture",
            "edge_lt": "lt-edge-fixture",
            "lambda_functions": ["fixture-skills-function"],
            "lambda_logical_to_physical": {
                "SkillsFunction": "fixture-skills-function",
            },
            "rest_api_id": "fixture-api",
            "tables": ["fixture-table"],
            "redis_group": "fixture-redis",
            "logging_enabled": True,
        }
        if self.missing:
            return {
                "unresolved": [
                    {"coordinate": key, "reason": "fixture missing %s" % key}
                    for key in values
                ],
                "sources": {},
            }
        values.update({
            "unresolved": [],
            "sources": {key: "discovered" for key in values},
            "classifications": {
                "host_asg": {"classified_by": "logical_id"},
                "edge_asg": {"classified_by": "logical_id"},
                "host_lt": {"classified_by": "logical_id"},
                "edge_lt": {"classified_by": "logical_id"},
            },
        })
        return values

    def discovered(self):
        return self._discovery

    def get(self, dotted, default=None):
        value = self.env
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def git_bytes(self, ref, path):
        del ref
        if path == "deploy/userdata/provision-host.sh":
            return PROVISION_SOURCE.encode("utf-8")
        return (self.repo / path).read_bytes()

    def git_tree_paths(self, ref, prefix):
        del ref
        root = self.repo / prefix
        return [
            str(path.relative_to(self.repo))
            for path in root.rglob("*") if path.is_file()
        ]


class FixtureReader:
    def __init__(self, ctx):
        self.ctx = ctx
        self.calls = []
        self.available = True
        self._commands = {}
        self._command_number = 0
        self.s3_objects = {}
        self.host_paths = {}
        self.edge_paths = {}
        self.routes = []

    def prepare(self):
        bundle = _edge_bundle(self.ctx)
        for item in self.ctx.inventory:
            if item["channel"] in {"F3", "F4"}:
                self.s3_objects[item["key"]] = self.ctx.git_bytes(
                    self.ctx.gateway_ref, item["source_path"])
            elif item["channel"] == "F7":
                self.s3_objects[item["key"]] = bundle["bytes"]
            elif item["channel"] == "F8":
                architecture = item["key"].rsplit("-", 1)[-1].split(".", 1)[0]
                self.s3_objects[item["key"]] = FC_ARCHIVES[architecture]
        self.s3_objects[
            "deployment/bootstrap/host/%s/init-host.sh" % HOST_BOOTSTRAP_SHA
        ] = HOST_BOOTSTRAP_BODY
        self.s3_objects["deployment/rootfs/manifest.json"] = json.dumps(
            MANIFEST, sort_keys=True).encode("utf-8")

        for item in self.ctx.inventory:
            if item["channel"] == "F3" and item.get("landing_path"):
                self.host_paths[item["landing_path"]] = item["expected_sha256"]
        for item in _obs_assets(self.ctx):
            path, role = _obs_landing(item)
            if not path:
                continue
            digest = _sha(self.ctx.git_bytes(
                self.ctx.gateway_ref, item["source_path"]))
            target = self.edge_paths if role == "edge" else self.host_paths
            target[path] = digest
        self.routes = self._route_rows()

    def body_bytes(self, response, key="Body"):
        body = response.get(key)
        if body is None:
            return b""
        return body.read() if hasattr(body, "read") else bytes(body)

    def lambda_package(self, function_name, qualifier=None):
        response = self.call(
            "lambda", "get_function",
            FunctionName=function_name, Qualifier=qualifier)
        return bytes(response["CodeBytes"])

    def _route_rows(self):
        from checks.controlplane import _source_routes
        return _source_routes(self.ctx)

    def _zip(self):
        entries = self.ctx.lambda_package_entries
        if entries is None:
            data = self.ctx.git_bytes(
                self.ctx.gateway_ref, "deploy/lambda/skills/handler.py")
            if self.ctx.bad == "G2":
                data += b"\n# injected divergence\n"
            entries = {"handler.py": data}
        entries = dict(entries)
        entries.update(self.ctx.lambda_extra_members)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in sorted(entries.items()):
                archive.writestr(name, data)
        return buffer.getvalue()

    def _s3_bytes(self, key):
        data = self.s3_objects.get(key, b"fixture-object")
        if self.ctx.bad == "F3" and key == "deployment/scripts/launch-vm.sh":
            return data + b"\nF3 divergence\n"
        if (self.ctx.bad == "F4"
                and key == "deployment/observability/adot/adot-config.yaml"):
            return data + b"\nF4 divergence\n"
        if self.ctx.bad == "F8" and key.endswith("-aarch64.tgz"):
            return data + b"F8"
        if self.ctx.bad == "G4" and key == self.ctx.inventory[0]["key"]:
            return data + b"G4"
        return data

    def _user_data(self, kind, version=None):
        if kind == "host":
            digest = self.ctx.host_bootstrap_shas.get(
                int(version) if str(version).isdigit() else 2,
                HOST_BOOTSTRAP_SHA)
            return (
                "#!/bin/bash\n"
                "aws s3 cp s3://bucket/deployment/bootstrap/host/%s/init-host.sh /tmp/init\n"
                % digest
            )
        bundle = _edge_bundle(self.ctx)
        return (
            "#!/bin/bash\n"
            "aws s3 cp s3://bucket/deployment/bootstrap/edge/%s/%s /tmp/edge\n"
            % (bundle["sha256"], bundle["object_name"])
        )

    def _marker(self, instance_id):
        recipe = "fixture-recipe"
        if self.ctx.bad == "F1" and instance_id == "host-new":
            recipe = "diverged-recipe"
        return (
            "recipe_version=%s\n"
            "provisioned_arch=aarch64\n"
            "firecracker_version=v-fixture\n"
            "guest_kernel=vmlinux-fixture\n"
        ) % recipe

    def _skill_state(self, instance_id):
        versions = {
            "skills/alpha/SKILL.md": "version-alpha",
            "skills/beta/SKILL.md": "version-beta",
        }
        if self.ctx.bad == "F9" and instance_id == "host-new":
            versions["skills/beta/SKILL.md"] = "version-diverged"
        return json.dumps({
            "objects": {
                key: {"version_id": version}
                for key, version in versions.items()
            }
        })

    def _platform_env(self, instance_id):
        safe_value = "same"
        if self.ctx.bad == "F11" and instance_id == "host-new":
            safe_value = "different"
        return (
            "SAFE_KEY=%s\n"
            "LITELLM_SHARED_VKEY=%s\n"
            "INSTANCE_ID=%s\n"
        ) % (safe_value, SECRET, instance_id)

    def _ssm_config(self, instance_id):
        workers = 20
        if self.ctx.bad == "F10" and instance_id == "host-new":
            workers = 99
        return json.dumps({
            "Mds": {
                "CommandWorkersLimit": workers,
                "CommandWorkerBufferLimit": 10,
            }
        })

    def _hash_for_path(self, instance_id, path):
        paths = self.edge_paths if instance_id.startswith("edge-") else self.host_paths
        digest = paths.get(path)
        if path in DISK_PATHS:
            digest = _sha(path.encode("utf-8"))
            if self.ctx.bad == "F5" and instance_id == "host-new":
                digest = _sha((path + "-diverged").encode("utf-8"))
        elif path == ASSET_DIR + "/vmlinux":
            digest = _sha(b"fixture-kernel")
            if self.ctx.bad == "F6" and instance_id == "host-new":
                digest = _sha(b"fixture-kernel-diverged")
        elif path == "/opt/openclaw/baked/vmlinux":
            digest = _sha(b"fixture-kernel")
        elif path in ("/usr/local/bin/firecracker", "/usr/local/bin/jailer"):
            digest = _sha(path.encode("utf-8"))
            if (self.ctx.bad == "F8" and instance_id == "host-new"
                    and path.endswith("firecracker")):
                digest = _sha(b"installed-firecracker-diverged")
        return digest

    def _command_result(self, instance_id, command):
        if command.startswith("sha256sum "):
            rows = []
            for path in shlex.split(command)[1:]:
                digest = self._hash_for_path(instance_id, path)
                if digest:
                    rows.append("%s  %s" % (digest, path))
            status = "Success" if len(rows) == len(shlex.split(command)[1:]) else "Failed"
            return status, "\n".join(rows) + ("\n" if rows else "")
        if command == "cat " + MARKER_PATH:
            return "Success", self._marker(instance_id)
        if command == "cat " + ASSET_DIR + "/manifest.json":
            manifest = dict(MANIFEST)
            if self.ctx.bad == "F5" and instance_id == "host-new":
                manifest["version"] = "fixture-v2"
            return "Success", json.dumps(manifest)
        if command == "cat " + SKILL_STATE_PATH:
            return "Success", self._skill_state(instance_id)
        if command == "cat " + SSM_AGENT_CONFIG_PATH:
            return "Success", self._ssm_config(instance_id)
        if command == "cat " + PLATFORM_ENV_PATH:
            return "Success", self._platform_env(instance_id)
        if command == "cat /etc/fluent-bit/fluent-bit.conf":
            return "Success", "fixture rendered fluent bit\n"
        if command.startswith("stat "):
            if self.ctx.bad == "F7" and instance_id == "edge-one":
                return "Failed", ""
            return "Success", "present\n"
        return "Failed", ""

    def _api_export(self):
        paths = {}
        for path, method in self.routes:
            paths.setdefault(path, {})[method.lower()] = {
                "apiKeyRequired": True,
                "authorizationType": "NONE",
            }
        if self.ctx.bad == "G3":
            paths.get("/tenants", {}).pop("get", None)
        return {"paths": paths}

    def call(self, service, method, **kwargs):
        self.calls.append((service, method, kwargs))
        if service == "autoscaling" and method == "describe_auto_scaling_groups":
            name = kwargs["AutoScalingGroupNames"][0]
            if name == "fixture-host-asg":
                spec = {
                    "LaunchTemplateId": "lt-host-fixture",
                    "Version": self.ctx.host_lt_version,
                }
                asg = {
                    "AutoScalingGroupName": name,
                }
                if self.ctx.host_lt_shape == "mixed":
                    asg["MixedInstancesPolicy"] = {
                        "LaunchTemplate": {
                            "LaunchTemplateSpecification": spec,
                        },
                    }
                else:
                    asg["LaunchTemplate"] = spec
                return {"AutoScalingGroups": [asg]}
            spec = {
                "LaunchTemplateId": "lt-edge-fixture",
                "Version": self.ctx.edge_lt_version,
            }
            asg = {
                "AutoScalingGroupName": name,
                "Instances": [{"InstanceId": "edge-one"}],
            }
            if self.ctx.edge_lt_shape == "mixed":
                asg["MixedInstancesPolicy"] = {
                    "LaunchTemplate": {
                        "LaunchTemplateSpecification": spec,
                    },
                }
            else:
                asg["LaunchTemplate"] = spec
            return {"AutoScalingGroups": [asg]}
        if service == "ec2" and method == "describe_launch_templates":
            return {"LaunchTemplates": [{
                "LaunchTemplateId": kwargs["LaunchTemplateIds"][0],
                "DefaultVersionNumber": 2,
            }]}
        if service == "ec2" and method == "describe_launch_template_versions":
            lt_id = kwargs["LaunchTemplateId"]
            requested = kwargs.get("Versions") or []
            requested_version = requested[0] if requested else "2"
            version = (
                2 if requested_version in {"$Default", "$Latest"}
                else int(requested_version))
            kind = "edge" if lt_id == "lt-edge-fixture" else "host"
            raw = base64.b64encode(
                self._user_data(kind, version).encode("utf-8")).decode("ascii")
            return {"LaunchTemplateVersions": [{
                "VersionNumber": version,
                "LaunchTemplateData": {"UserData": raw},
            }]}
        if service == "ec2" and method == "describe_instances":
            if kwargs.get("InstanceIds"):
                return {"Reservations": [{"Instances": [
                    {"InstanceId": instance_id}
                    for instance_id in kwargs["InstanceIds"]
                ]}]}
            return {"Reservations": [{"Instances": [
                {"InstanceId": "host-old",
                 "LaunchTemplate": {
                     "LaunchTemplateId": "lt-host-fixture",
                     "Version": "1",
                 },
                 "LaunchTime": "2026-01-01T00:00:00+00:00",
                 "Architecture": "arm64"},
                {"InstanceId": "host-new",
                 "LaunchTemplate": {
                     "LaunchTemplateId": "lt-host-fixture",
                     "Version": "2",
                 },
                 "LaunchTime": "2026-02-01T00:00:00+00:00",
                 "Architecture": "arm64"},
            ]}]}
        if service == "ssm" and method == "send_command":
            self._command_number += 1
            command_id = "command-%s" % self._command_number
            self._commands[command_id] = (
                kwargs["InstanceIds"][0],
                kwargs["Parameters"]["commands"][0],
            )
            return {"Command": {"CommandId": command_id}}
        if service == "ssm" and method == "get_command_invocation":
            instance_id, command = self._commands[kwargs["CommandId"]]
            status, output = self._command_result(instance_id, command)
            return {"Status": status, "StandardOutputContent": output}
        if service == "ssm" and method == "describe_instance_information":
            return {"InstanceInformationList": [
                {"InstanceId": "host-old", "AgentVersion": "fixture-agent"},
                {"InstanceId": "host-new", "AgentVersion": "fixture-agent"},
            ]}
        if service == "ssm" and method == "get_parameter":
            # The spire-kit switch is off in this fixture, which init-host.sh expresses as
            # "get-parameter failed, so the variable is empty". Raising the not-found error is
            # the faithful shape and also exercises `_ssm_parameter_value`'s absent branch.
            #
            # This branch had to be added when `_SSM_PARAM_ASSIGN` gained `re.MULTILINE`: until
            # then the pattern matched nothing on the real init-host.sh, so F3 never reached an
            # SSM read at all and the stub's fail-closed `raise` was never hit. The three F3
            # tests going red on that flag is the receipt that this path was previously dead.
            raise AwsReadError("ParameterNotFound: %s" % kwargs["Name"])
        if service == "s3" and method == "head_object":
            key = kwargs["Key"]
            if (self.ctx.missing_bootstrap_object
                    and key.startswith("deployment/bootstrap/host/")):
                raise AwsReadError("NoSuchKey: %s" % key)
            return {"VersionId": "version-" + hashlib.sha256(
                key.encode("utf-8")).hexdigest()[:8]}
        if service == "s3" and method == "get_object":
            key = kwargs["Key"]
            return {
                "Body": io.BytesIO(self._s3_bytes(key)),
                "VersionId": "version-" + hashlib.sha256(
                    key.encode("utf-8")).hexdigest()[:8],
            }
        if service == "s3" and method == "list_objects_v2":
            return {"Contents": [
                {"Key": "skills/alpha/SKILL.md"},
                {"Key": "skills/beta/SKILL.md"},
            ]}
        if service == "s3" and method == "list_object_versions":
            return {"Versions": [
                {"Key": "skills/alpha/SKILL.md",
                 "VersionId": "version-alpha", "IsLatest": True},
                {"Key": "skills/beta/SKILL.md",
                 "VersionId": "version-beta", "IsLatest": True},
            ], "IsTruncated": False}
        if service == "s3" and method == "get_bucket_versioning":
            return {"Status": "Enabled"}
        if service == "lambda" and method == "get_function_configuration":
            if self.ctx.bad == "G1":
                raise AwsReadError(
                    "ResourceNotFoundException: SkillsFunction")
            return {"Runtime": "python3.12", "CodeSha256": "fixture-code"}
        if service == "lambda" and method == "get_function":
            return {
                "Configuration": {"CodeSha256": "fixture-code"},
                "CodeBytes": self._zip(),
            }
        if service == "lambda" and method == "list_aliases":
            return {"Aliases": [{"Name": "live", "FunctionVersion": "1"}]}
        if service == "apigateway" and method == "get_stages":
            return {"item": [{"stageName": "v1"}]}
        if service == "apigateway" and method == "get_export":
            return {"body": io.BytesIO(
                json.dumps(self._api_export()).encode("utf-8"))}
        raise AssertionError("unexpected AWS call: %s.%s" % (service, method))


CHECK_IDS = [
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
    "F9", "F10", "F11", "G1", "G2", "G3", "G4",
]

OFFENDING = {
    "F1": "host-new",
    "F2": "$Latest",
    "F3": "deployment/scripts/launch-vm.sh",
    "F4": "deployment/observability/adot/adot-config.yaml",
    "F5": "host-new:manifest.json",
    "F6": ASSET_DIR + "/vmlinux",
    "F7": "edge-one",
    "F8": "firecracker-v-fixture-aarch64.tgz",
    "F9": "host-new",
    "F10": "host-new",
    "F11": "host-new:SAFE_KEY",
    "G1": "SkillsFunction",
    "G2": "SkillsFunction:handler.py",
    "G3": "GET /tenants",
    "G4": "deployment/scripts/host-agent.py",
}


class ChannelCheckTests(unittest.TestCase):
    def run_check(self, check_id, bad=None, missing=False):
        ctx = FixtureContext(bad=bad, missing=missing)
        row = CHECKS[check_id][1](ctx)
        self.assertTrue(row.readings)
        self.assertTrue(row.remediation)
        return row, ctx

    def fetch_guards(self, init_host):
        class GatewaySource:
            gateway_ref = "fixture-gateway"

            def git_bytes(self, ref, path):
                self_ref = self.gateway_ref
                if ref != self_ref or path != "deploy/userdata/init-host.sh":
                    raise AssertionError("unexpected gateway source: %s:%s"
                                         % (ref, path))
                return init_host.encode("utf-8")

        return _fetch_guards(GatewaySource())

    def test_fetch_guards_skips_heredoc_bodies(self):
        guards = self.fetch_guards(
            """if [ "${EGRESS_MODE:-off}" = "deny" ]; then
_LLM_DERIVED=$(python3 - <<'PYEOF'
if not port:
if cidr_mode:
if not host:
PYEOF
)
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/oc-egress-chain.sh /usr/local/bin/
fi
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /usr/local/bin/
""")
        self.assertIsNone(guards["host-agent.py"])
        self.assertEqual(
            {"var": "EGRESS_MODE", "equals": "deny"},
            guards["oc-egress-chain.sh"])

    def test_fetch_guards_closes_single_line_if(self):
        guards = self.fetch_guards(
            """if [ "${EGRESS_MODE:-off}" = "deny" ]; then
if [ -w /x ]; then echo 0 > /x; fi
fi
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /usr/local/bin/
""")
        self.assertIsNone(guards["host-agent.py"])

    def test_fetch_guards_supports_all_heredoc_openers(self):
        for opener in ("<<'TAG'", '<<"TAG"', "<<TAG", "<<-TAG"):
            with self.subTest(opener=opener):
                terminator = "\tTAG" if opener == "<<-TAG" else "TAG"
                guards = self.fetch_guards(
                    """if [ "${EGRESS_MODE:-off}" = "deny" ]; then
cat %s
if [ "${INNER_GATE:-off}" = "on" ]; then
%s
fi
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /usr/local/bin/
""" % (opener, terminator))
                self.assertIsNone(guards["host-agent.py"])

    def test_fetch_guards_learns_ssm_parameter_below_the_first_line(self):
        """The SSM assignment must be found wherever it sits, not only on line 1.

        This is the fixture artefact that hid a dead feature: `_SSM_PARAM_ASSIGN` is
        `finditer`-ed over the whole joined script, so without `re.MULTILINE` its `^` anchors
        only at offset 0. Every earlier fixture happened to put the assignment first, so the
        unit suite was green while the real init-host.sh matched nothing and the spire-kit gate
        silently degraded to "unrecognised" -- i.e. its four files were reported missing on
        every correctly configured fleet. So the assignment here is deliberately preceded by
        unrelated shell, and the `--name` deliberately sits on a continuation line, which is
        also how the real script writes it.
        """
        guards = self.fetch_guards(
            """log "step: something unrelated"
ARCH=aarch64
_spire_enabled=$(aws ssm get-parameter --region ${REGION} \\
  --name /openclaw/spire-kit/enabled --query 'Parameter.Value' --output text 2>/dev/null || echo "")
if [ "$(printf '%s' "${_spire_enabled}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
for _f in spire-kit-setup.sh install.sh; do
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/spire-kit/${_f} /opt/openclaw/spire-kit/${_f}
done
fi
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /usr/local/bin/
""")
        self.assertEqual(
            {"ssm_parameter": "/openclaw/spire-kit/enabled", "equals": "true"},
            guards["spire-kit/spire-kit-setup.sh"])
        self.assertEqual(
            {"ssm_parameter": "/openclaw/spire-kit/enabled", "equals": "true"},
            guards["spire-kit/install.sh"])
        # The unconditional fetch after the block must stay unguarded, so that a fix which
        # simply stopped pushing guards could not pass this test.
        self.assertIsNone(guards["host-agent.py"])

    def test_fetch_guards_marks_the_fallback_arm(self):
        """`if ! aws s3 cp <primary>; then <secondary>` makes the secondary a fallback.

        Absence of a fallback is the healthy state, so it must not be reported as drift. The
        third assertion is the one that stops this from becoming a blanket excuse: the primary
        fetch, and any unconditional fetch after the block, stay unguarded.
        """
        guards = self.fetch_guards(
            """if ! aws s3 cp s3://${ASSETS_BUCKET}/deployment/observability/adot/adot-config.yaml /etc/c.yaml; then
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/adot-config.yaml /etc/c.yaml
fi
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /usr/local/bin/
""")
        self.assertEqual({"fallback": True}, guards["adot-config.yaml"])
        self.assertFalse(_guard_satisfied({"fallback": True}, {}))
        self.assertIsNone(guards["host-agent.py"])

    def test_f11_never_emits_raw_secret(self):
        row, _ctx = self.run_check("F11")
        document = json.dumps(report_document([row]), sort_keys=True)
        self.assertNotIn(SECRET, document)
        self.assertIn(
            hashlib.sha256(SECRET.encode("utf-8")).hexdigest()[:12],
            document)

    def test_f11_divergence_names_oldest_reference_and_both_hosts(self):
        row, _ctx = self.run_check("F11", bad="F11")
        self.assertEqual("host-old", row.readings["reference_instance_id"])
        self.assertEqual("oldest-launched", row.readings["reference_basis"])
        self.assertIn("host-old:SAFE_KEY", row.evidence)
        self.assertIn("host-new:SAFE_KEY", row.evidence)

    def test_discover_unresolved_propagates_to_check(self):
        class EmptyStackReader:
            def call(self, service, method, **kwargs):
                if method == "describe_stacks":
                    return {"Stacks": [{"StackName": kwargs["StackName"]}]}
                if method == "list_stack_resources":
                    return {"StackResourceSummaries": []}
                raise AssertionError("%s.%s" % (service, method))

        discovered = discover(
            EmptyStackReader(), "fixture-region", ["FixtureStack"])
        unresolved = {
            item["coordinate"] for item in discovered["unresolved"]}
        self.assertIn("assets_bucket", unresolved)

        ctx = FixtureContext()
        ctx._discovery = discovered
        row = CHECKS["F2"][1](ctx)
        self.assertEqual("INCONCLUSIVE", row.verdict)
        self.assertIn("no matching CloudFormation resource",
                      row.readings["reason"])

    def test_discover_classifies_opaque_launch_templates_by_logical_id(self):
        class StackReader:
            def call(self, service, method, **kwargs):
                if method == "describe_stacks":
                    return {"Stacks": [{"StackName": kwargs["StackName"]}]}
                if method == "list_stack_resources":
                    return {"StackResourceSummaries": [
                        {
                            "LogicalResourceId": "EdgeASG78BA88BD",
                            "PhysicalResourceId": "openclaw-edge-asg",
                            "ResourceType":
                                "AWS::AutoScaling::AutoScalingGroup",
                        },
                        {
                            "LogicalResourceId": "HostASG3A4B5C6D",
                            "PhysicalResourceId": "openclaw-hosts-asg",
                            "ResourceType":
                                "AWS::AutoScaling::AutoScalingGroup",
                        },
                        {
                            "LogicalResourceId":
                                "EdgeLaunchTemplateC24E33A2",
                            "PhysicalResourceId": "lt-edge-opaque",
                            "ResourceType": "AWS::EC2::LaunchTemplate",
                        },
                        {
                            "LogicalResourceId": "HostLT7E8F9A0B",
                            "PhysicalResourceId": "lt-host-opaque",
                            "ResourceType": "AWS::EC2::LaunchTemplate",
                        },
                        {
                            "LogicalResourceId": "GatewayApi",
                            "PhysicalResourceId": "opaque-rest-api",
                            "ResourceType": "AWS::ApiGateway::RestApi",
                        },
                    ]}
                raise AssertionError("%s.%s" % (service, method))

        discovered = discover(
            StackReader(), "fixture-region", ["FixtureStack"])
        self.assertEqual("lt-edge-opaque", discovered["edge_lt"])
        self.assertEqual("lt-host-opaque", discovered["host_lt"])
        self.assertEqual("opaque-rest-api", discovered["rest_api_id"])
        self.assertEqual(
            "logical_id",
            discovered["classifications"]["edge_lt"]["classified_by"])
        self.assertEqual(
            "logical_id",
            discovered["classifications"]["host_lt"]["classified_by"])

    def test_discover_falls_back_to_named_physical_id(self):
        class StackReader:
            def call(self, service, method, **kwargs):
                if method == "describe_stacks":
                    return {"Stacks": [{"StackName": kwargs["StackName"]}]}
                if method == "list_stack_resources":
                    return {"StackResourceSummaries": [{
                        "LogicalResourceId": "LaunchTemplateABC",
                        "PhysicalResourceId": "edge-template-name",
                        "ResourceType": "AWS::EC2::LaunchTemplate",
                    }]}
                raise AssertionError("%s.%s" % (service, method))

        discovered = discover(
            StackReader(), "fixture-region", ["FixtureStack"])
        self.assertEqual("edge-template-name", discovered["edge_lt"])
        self.assertEqual(
            "physical_id",
            discovered["classifications"]["edge_lt"]["classified_by"])

    def test_f2_default_mixed_policy_resolves_effective_version(self):
        row, _ctx = self.run_check("F2")
        self.assertEqual("PASS", row.verdict)
        self.assertEqual("$Default", row.readings["version_source"])
        self.assertEqual(2, row.readings["effective_version"])
        self.assertEqual("logical_id", row.readings["classified_by"])
        old = next(
            item for item in row.readings["instances"]
            if item["instance_id"] == "host-old")
        self.assertTrue(old["version_differs"])
        self.assertFalse(old["bootstrap_differs"])

    def test_f2_numeric_plain_policy_is_acceptable(self):
        ctx = FixtureContext()
        ctx.host_lt_version = "2"
        ctx.host_lt_shape = "plain"
        row = CHECKS["F2"][1](ctx)
        self.assertEqual("PASS", row.verdict)
        self.assertEqual("2", row.readings["version_source"])
        self.assertEqual(2, row.readings["effective_version"])

    def test_f2_latest_is_a_failure(self):
        row, _ctx = self.run_check("F2", bad="F2")
        self.assertEqual("FAIL", row.verdict)
        self.assertIn("host launch-template version $Latest", row.evidence)
        self.assertNotIn("was verified", row.summary)

    def test_f2_version_skew_fails_only_when_bootstrap_sha_differs(self):
        ctx = FixtureContext()
        ctx.host_bootstrap_shas[1] = "b" * 64
        row = CHECKS["F2"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertIn("host-old:bootstrap-sha", row.evidence)

    def test_f2_confirmed_missing_bootstrap_object_fails(self):
        ctx = FixtureContext()
        ctx.missing_bootstrap_object = True
        row = CHECKS["F2"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertIn(row.readings["key"], row.evidence)
        self.assertFalse(row.readings["object_present"])

    def test_f2_tampered_bootstrap_object_fails_even_though_the_key_is_unchanged(self):
        """Presence is not integrity: hash the bytes against the sha in the key.

        Found on a live fleet 2026-08-29 — an extra line appended to the rendered
        `init-host.sh` object left F2 (and F7, and G4 on the host side) reporting PASS, because
        the key is content-addressed so nothing about the key, the launch-template reference or
        `head_object` moves when only the bytes do. The user data sha-verifies before executing,
        so the real consequence is that every FUTURE host launch fails closed while the tool
        says green.
        """
        ctx = FixtureContext()
        key = "deployment/bootstrap/host/%s/init-host.sh" % HOST_BOOTSTRAP_SHA
        ctx.aws.s3_objects[key] = HOST_BOOTSTRAP_BODY + b"\n# tampered\n"
        row = CHECKS["F2"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertTrue(row.readings["object_present"])
        self.assertFalse(row.readings["object_digest_matches_key"])
        self.assertNotEqual(HOST_BOOTSTRAP_SHA, row.readings["object_sha256"])
        self.assertTrue(any("does not match the sha in its own key" in item
                            for item in row.evidence), row.evidence)

    def test_f7_tampered_bundle_object_fails_even_though_the_key_is_unchanged(self):
        """The edge half of the same pair. Kept as its own case rather than folded into the F2
        one: the two objects are produced by different channels, and a fix that only re-hashed
        the host object would otherwise still look complete."""
        ctx = FixtureContext()
        bundle = _edge_bundle(ctx)
        key = "deployment/bootstrap/edge/%s/%s" % (
            bundle["sha256"], bundle["object_name"])
        ctx.aws.s3_objects[key] = bundle["bytes"] + b"tampered"
        row = CHECKS["F7"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertTrue(row.readings["object_present"])
        self.assertFalse(row.readings["object_digest_matches_key"])
        self.assertTrue(any("does not match the sha in its own key" in item
                            for item in row.evidence), row.evidence)

    def test_f7_default_plain_policy_resolves_effective_version(self):
        row, _ctx = self.run_check("F7")
        self.assertEqual("PASS", row.verdict)
        self.assertEqual("$Default", row.readings["version_source"])
        self.assertEqual(2, row.readings["effective_version"])
        self.assertEqual("logical_id", row.readings["classified_by"])

    def test_f7_numeric_mixed_policy_is_acceptable(self):
        ctx = FixtureContext()
        ctx.edge_lt_version = "2"
        ctx.edge_lt_shape = "mixed"
        row = CHECKS["F7"][1](ctx)
        self.assertEqual("PASS", row.verdict)
        self.assertEqual("2", row.readings["version_source"])

    def test_f7_latest_is_a_failure(self):
        ctx = FixtureContext()
        ctx.edge_lt_version = "$Latest"
        row = CHECKS["F7"][1](ctx)
        self.assertEqual("FAIL", row.verdict)
        self.assertIn("edge launch-template version $Latest", row.evidence)

    def test_g2_package_only_members_are_neutral_and_bytecode_is_ignored(self):
        ctx = FixtureContext()
        ctx.lambda_extra_members = {
            "_cffi_backend.cpython-312-aarch64-linux-gnu.so": b"native",
            "bin/__pycache__/jp.cpython-312.pyc": b"bytecode",
            "bin/cffi-gen-src": b"generated",
            "bin/jp.py": b"vendored",
            "core/__pycache__/__init__.cpython-313.pyc": b"bytecode",
            "typing_extensions.py": b"vendored",
        }
        row = CHECKS["G2"][1](ctx)
        self.assertEqual("PASS", row.verdict)
        package = row.readings["functions"][0]["package"]
        self.assertIn("bin/jp.py", package["extra"])
        self.assertIn(
            "bin/__pycache__/jp.cpython-312.pyc",
            package["ignored_bytecode"])
        self.assertEqual([], package["failures"])

    def test_g2_cdk_singleton_provider_is_neutral(self):
        logical_id = "AWS679f53fac002430cb0da5b7982bd22872D164C4C"
        ctx = FixtureContext()
        ctx._discovery["lambda_logical_to_physical"] = {
            logical_id: "fixture-provider-function",
        }
        row = CHECKS["G2"][1](ctx)
        self.assertEqual("PASS", row.verdict)
        function = row.readings["functions"][0]
        self.assertEqual("cdk_provider", function["classification"])
        self.assertTrue(function["neutral"])
        self.assertIsNone(function["package_error"])

    def test_g2_unmatched_non_provider_is_unverified(self):
        ctx = FixtureContext()
        ctx._discovery["lambda_logical_to_physical"] = {
            "UnmatchedFunction": "fixture-unmatched-function",
        }
        ctx.lambda_package_entries = {"package-only.bin": b"package"}
        row = CHECKS["G2"][1](ctx)
        self.assertEqual("UNVERIFIED", row.verdict)
        self.assertIn("UnmatchedFunction:package", row.readings["unverified"])
        self.assertEqual(
            "unmatched",
            row.readings["functions"][0]["classification"])

    def test_every_used_method_is_read_only(self):
        calls = []
        for check_id in CHECK_IDS:
            _row, ctx = self.run_check(check_id)
            calls.extend(ctx.aws.calls)
        self.assertTrue(calls)
        for _service, method, _kwargs in calls:
            assert_read_only_method(method)


def _pass_case(check_id):
    def test(self):
        row, _ctx = self.run_check(check_id)
        self.assertEqual("PASS", row.verdict)
    return test


def _fail_case(check_id):
    def test(self):
        row, _ctx = self.run_check(check_id, bad=check_id)
        self.assertEqual("FAIL", row.verdict)
        self.assertIn(OFFENDING[check_id], json.dumps(row.to_dict(), sort_keys=True))
    return test


def _missing_case(check_id):
    def test(self):
        row, _ctx = self.run_check(check_id, missing=True)
        self.assertIn(row.verdict, {"INCONCLUSIVE", "UNVERIFIED"})
        self.assertNotEqual("PASS", row.verdict)
    return test


for _check_id in CHECK_IDS:
    setattr(
        ChannelCheckTests, "test_%s_pass" % _check_id.lower(),
        _pass_case(_check_id))
    setattr(
        ChannelCheckTests, "test_%s_fail" % _check_id.lower(),
        _fail_case(_check_id))
    setattr(
        ChannelCheckTests, "test_%s_missing_evidence" % _check_id.lower(),
        _missing_case(_check_id))


if __name__ == "__main__":
    unittest.main()
