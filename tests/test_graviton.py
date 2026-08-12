# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Graviton (ARM64) support (issue #19).

Today the stack hard-codes m8i.xlarge (Intel x86_64). Graviton 4 hosts
(m8g.xlarge / c8g.xlarge) are typically 20% cheaper for equivalent
performance — and Firecracker has supported aarch64 for years.

We make architecture explicit and switchable:

1. config.yml.example declares `host.arch` (`x86_64` | `arm64`).
2. The CDK stack picks an Ubuntu AMI matching the arch.
3. The CDK stack picks an InstanceType in the right family when the
   user leaves it unset (m8i for x86, m8g for arm).
4. build-rootfs.sh accepts `--arch` and forwards it to debootstrap.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from conftest import synth_stack

ROOT = Path(__file__).resolve().parent.parent


def _synth(arch="x86_64", instance_type=None, drop_instance_type=False):
    """Synth with host.arch overridden. Never touches the repo's config.yml —
    see conftest.synth_stack for why that mattered."""
    def mutate(cfg):
        cfg.setdefault("host", {})["arch"] = arch
        if instance_type is not None:
            cfg["host"]["instance_type"] = instance_type
        if drop_instance_type:
            cfg["host"].pop("instance_type", None)
    return synth_stack(mutate)


@pytest.mark.unit
class TestConfigSchema:
    def test_example_documents_arch_field(self):
        text = (ROOT / "config.yml.example").read_text()
        assert "arch:" in text, "config.yml.example must declare host.arch"
        # Both architectures should be mentioned in comments
        assert "x86_64" in text and "arm64" in text


@pytest.mark.unit
class TestInstanceTypeDefaulting:
    def test_arm64_picks_graviton_family(self):
        """arch=arm64 with no explicit instance_type → a Graviton (g) family."""
        tpl = _synth(arch="arm64", drop_instance_type=True)
        lts = tpl.find_resources("AWS::EC2::LaunchTemplate")
        assert lts
        for _, res in lts.items():
            itype = res["Properties"]["LaunchTemplateData"].get("InstanceType", "")
            assert "g." in itype, \
                f"arm64 should pick a Graviton (g) family, got {itype}"

    def test_x86_keeps_intel_family(self):
        """arch=x86_64 default keeps an Intel/AMD family."""
        tpl = _synth(arch="x86_64", drop_instance_type=True)
        lts = tpl.find_resources("AWS::EC2::LaunchTemplate")
        assert lts
        for _, res in lts.items():
            itype = res["Properties"]["LaunchTemplateData"].get("InstanceType", "")
            # Intel families: m8i / c8i / r8i / m6i / m7i etc.
            assert ("i." in itype or "n." in itype or "a." in itype), \
                f"x86_64 should pick an Intel/AMD family, got {itype}"


@pytest.mark.unit
class TestBuildRootfsArch:
    def test_build_script_accepts_arch_flag(self):
        sh = (ROOT / "build-rootfs.sh").read_text()
        # Either a `--arch` flag handler or an ARCH env var in the help text
        assert "--arch" in sh or "ARCH=" in sh

    def test_build_script_supports_arm64(self):
        sh = (ROOT / "build-rootfs.sh").read_text()
        assert "arm64" in sh, \
            "build-rootfs.sh must mention arm64 (debootstrap target arch)"
