import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "patch" / "353-secret-ttl-plus-post315-rollup"
APPLY_LT = KIT / "lib" / "apply-lt.sh"
LT_USERDATA = KIT / "lib" / "lt-userdata.py"
INSTRUCTIONS = KIT / "APPLY-INSTRUCTIONS.md"


def _load_lt_userdata():
    spec = importlib.util.spec_from_file_location("lt_userdata", LT_USERDATA)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "text",
    [
        "echo '{{ json literal }}'\n",
        "echo '{{lower_case_app_token}}'\n",
        "echo 'left {{ only'\n",
        "echo 'right }} only'\n",
    ],
)
def test_placeholder_guard_allows_non_cdk_braces(text):
    _load_lt_userdata()._refuse_placeholders(text, "test")


@pytest.mark.parametrize("token", ["{{REGION}}", "{{HOST_2_SLOT}}"])
def test_placeholder_guard_rejects_unresolved_cdk_tokens(token):
    with pytest.raises(SystemExit) as exc:
        _load_lt_userdata()._refuse_placeholders(f"echo {token}\n", "test")
    assert exc.value.code == 2


def test_apply_instructions_match_script_interface():
    instructions = INSTRUCTIONS.read_text()
    script = APPLY_LT.read_text()

    assert "pull  $LT" not in instructions
    assert "push  $LT" not in instructions
    assert "rollback $LT" not in instructions
    assert "pull  $ASG $REGION" in instructions
    assert "promote $ASG $REGION" in instructions
    assert "verify  $ASG $REGION" in instructions
    assert "apply-lt.sh verify <asg> <region> [instance-id]" in script
    assert "| shasum -a 256" not in script
    assert "sha256sum | awk" in script
    assert "shasum -a 256 | awk" in script


def _write_fake_aws(path: Path, launch_version: str = "8"):
    path.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
case "$1 $2" in
  "autoscaling describe-auto-scaling-groups")
    printf '%s\\n' '{{"Instances":[{{"InstanceId":"i-new","LifecycleState":"InService","HealthStatus":"Healthy","LaunchTemplate":{{"LaunchTemplateId":"lt-123","Version":"{launch_version}"}}}}]}}'
    ;;
  "ssm describe-instance-information")
    printf '%s\\n' Online
    ;;
  "ssm send-command")
    printf '%s\\n' cmd-123
    ;;
  "ssm wait")
    exit 0
    ;;
  "ssm get-command-invocation")
    printf '%s\\n' '{{"Status":"Success","StandardOutputContent":"checks passed","StandardErrorContent":""}}'
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 99
    ;;
esac
"""
    )
    path.chmod(0o755)


def _run_verify(tmp_path: Path, launch_version: str = "8"):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_aws(fake_bin / "aws", launch_version)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "openclaw-hosts-asg.json").write_text(
        '{"lt_id":"lt-123","new_version":"8"}'
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "OC_APPLY_LT_STATE_DIR": str(state_dir),
    }
    return subprocess.run(
        [
            "bash",
            str(APPLY_LT),
            "verify",
            "openclaw-hosts-asg",
            "us-east-1",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_verify_checks_real_asg_replacement(tmp_path):
    result = _run_verify(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "signal 1/3 PASS" in result.stdout
    assert "signal 2/3 PASS" in result.stdout
    assert "signal 3/3 PASS" in result.stdout
    assert "boot verification passed" in result.stdout


def test_verify_rejects_host_on_wrong_lt_version(tmp_path):
    result = _run_verify(tmp_path, launch_version="7")

    assert result.returncode == 2
    assert "no healthy InService host on LT lt-123 v8 yet" in result.stderr
