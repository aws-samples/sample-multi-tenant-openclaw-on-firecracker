# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for the SwarmClaw guest runtime adaptation."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(path):
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_config_declares_distinct_host_and_guest_ports():
    cfg = _read(ROOT / "config.yml.example")

    assert "gateway_port_base: 18789" in cfg
    assert "app_port: 3456" in cfg


@pytest.mark.unit
def test_build_rootfs_installs_and_runs_swarmclaw():
    text = _read(ROOT / "build-rootfs.sh")

    assert "npm install -g @swarmclawai/swarmclaw" in text
    assert "swarmclaw.service" in text
    assert "swarmclaw server --port" in text
    assert "swarmclaw-rootfs-${VERSION}.ext4.gz" in text


@pytest.mark.unit
def test_launch_vm_uses_swarmclaw_app_port_and_state_dir():
    text = _read(ROOT / "deploy" / "userdata" / "launch-vm.sh")

    assert 'APP_PORT="${VM_APP_PORT:-3456}"' in text
    assert 'ROOTFS="${FIRECRACKER_ASSETS}/swarmclaw-rootfs.ext4"' in text
    assert "SWARMCLAW_HOME=/home/agent/.swarmclaw" in text
    assert "templates/swarmclaw/${CONFIG_TEMPLATE}/.env.local" in text
    assert "proxy_pass http://${GUEST_IP}:${APP_PORT}" in text


@pytest.mark.unit
def test_api_dnat_targets_guest_app_port_not_host_port_base():
    text = _read(ROOT / "deploy" / "lambda" / "api" / "handler.py")

    assert "VM_APP_PORT" in text
    assert "to-destination {guest_ip}:{VM_APP_PORT}" in text
    assert "to-destination {guest_ip}:{VM_PORT_BASE}" not in text


@pytest.mark.unit
def test_host_agent_probes_swarmclaw_and_reads_access_key():
    text = _read(ROOT / "deploy" / "userdata" / "host-agent.py")

    assert 'APP_PORT = int(os.environ.get("VM_APP_PORT"' in text
    assert "f\"http://{guest_ip}:{APP_PORT}/\"" in text
    assert "SWARMCLAW_ACCESS_KEY" in text
    assert ".swarmclaw/.env.local" in text
