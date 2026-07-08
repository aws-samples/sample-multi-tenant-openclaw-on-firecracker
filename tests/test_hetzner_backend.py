# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static checks for the single-host Hetzner backend."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(path):
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_hetzner_installer_preflights_kvm_and_installs_local_service():
    text = _read(ROOT / "scripts" / "hetzner" / "install.sh")

    assert "/dev/kvm" in text
    assert "swarmclaw-firecracker-api.service" in text
    assert "deploy/hetzner/local-api.py" in text
    assert "/data/swarmclaw-firecracker" in text
    assert "HETZNER_LOCAL=1" in text
    assert "--kernel-path" in text
    assert "--kernel-url" in text


@pytest.mark.unit
def test_hetzner_installer_does_not_require_aws_cli_or_s3():
    text = _read(ROOT / "scripts" / "hetzner" / "install.sh")

    assert "awscli" not in text
    assert "aws s3" not in text
    assert "s3.amazonaws.com" not in text
    assert "ASSETS_BUCKET=" in text


@pytest.mark.unit
def test_local_api_imports_without_aws_dependencies():
    path = ROOT / "deploy" / "hetzner" / "local-api.py"
    spec = importlib.util.spec_from_file_location("hetzner_local_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.HOST_PORT_BASE == 18789
    assert mod.APP_PORT == 3456
    assert hasattr(mod, "Handler")


@pytest.mark.unit
def test_launch_vm_supports_local_assets_and_templates():
    text = _read(ROOT / "deploy" / "userdata" / "launch-vm.sh")

    assert 'FIRECRACKER_ASSETS="${FIRECRACKER_ASSETS:-/data/firecracker-assets}"' in text
    assert 'LOCAL_TEMPLATES_DIR="${LOCAL_TEMPLATES_DIR:-/data/swarmclaw-firecracker/templates}"' in text
    assert '${LOCAL_TEMPLATES_DIR}/${CONFIG_TEMPLATE}/.env.local' in text
    assert '${VM_DIR}/access-key' in text


@pytest.mark.unit
def test_build_rootfs_has_local_output_mode():
    text = _read(ROOT / "build-rootfs.sh")

    assert 'HETZNER_LOCAL="${HETZNER_LOCAL:-0}"' in text
    assert "LOCAL_OUTPUT_DIR" in text
    assert "LOCAL_INSTALL_DIR" in text
    assert "swarmclaw-rootfs.ext4" in text
