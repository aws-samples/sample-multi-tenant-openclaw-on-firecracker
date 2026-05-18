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

ROOT = Path(__file__).resolve().parent.parent


def _synth(arch="x86_64", instance_type=None):
    import yaml
    cfg_path = ROOT / "config.yml"
    original = cfg_path.read_text()
    cfg = yaml.safe_load(original)
    cfg.setdefault("host", {})["arch"] = arch
    if instance_type is not None:
        cfg["host"]["instance_type"] = instance_type
    cfg_path.write_text(yaml.safe_dump(cfg))
    try:
        sys.modules.pop("deploy.stack", None)
        spec = importlib.util.spec_from_file_location(
            "deploy.stack", ROOT / "deploy" / "stack.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["deploy.stack"] = mod
        spec.loader.exec_module(mod)
        import aws_cdk as cdk
        from aws_cdk import assertions
        app = cdk.App()
        stack = mod.OpenClawOrchestratorStack(app, "Test",
            env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
        return assertions.Template.from_stack(stack)
    finally:
        cfg_path.write_text(original)


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
        """arch=arm64 with no explicit instance_type → m8g.xlarge."""
        # Pop the explicit instance_type to test defaulting
        import yaml
        cfg_path = ROOT / "config.yml"
        original = cfg_path.read_text()
        cfg = yaml.safe_load(original)
        cfg.setdefault("host", {})["arch"] = "arm64"
        cfg["host"].pop("instance_type", None)
        cfg_path.write_text(yaml.safe_dump(cfg))
        try:
            sys.modules.pop("deploy.stack", None)
            spec = importlib.util.spec_from_file_location(
                "deploy.stack", ROOT / "deploy" / "stack.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["deploy.stack"] = mod
            spec.loader.exec_module(mod)
            import aws_cdk as cdk
            from aws_cdk import assertions
            app = cdk.App()
            stack = mod.OpenClawOrchestratorStack(app, "Test",
                env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
            tpl = assertions.Template.from_stack(stack)
            lts = tpl.find_resources("AWS::EC2::LaunchTemplate")
            assert lts
            for _, res in lts.items():
                itype = res["Properties"]["LaunchTemplateData"].get("InstanceType", "")
                assert "g." in itype, \
                    f"arm64 should pick a Graviton (g) family, got {itype}"
        finally:
            cfg_path.write_text(original)

    def test_x86_keeps_intel_family(self):
        """arch=x86_64 default keeps an Intel/AMD family."""
        # Reset to default
        import yaml
        cfg_path = ROOT / "config.yml"
        original = cfg_path.read_text()
        cfg = yaml.safe_load(original)
        cfg.setdefault("host", {})["arch"] = "x86_64"
        cfg["host"].pop("instance_type", None)
        cfg_path.write_text(yaml.safe_dump(cfg))
        try:
            sys.modules.pop("deploy.stack", None)
            spec = importlib.util.spec_from_file_location(
                "deploy.stack", ROOT / "deploy" / "stack.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["deploy.stack"] = mod
            spec.loader.exec_module(mod)
            import aws_cdk as cdk
            from aws_cdk import assertions
            app = cdk.App()
            stack = mod.OpenClawOrchestratorStack(app, "Test",
                env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
            tpl = assertions.Template.from_stack(stack)
            lts = tpl.find_resources("AWS::EC2::LaunchTemplate")
            assert lts
            for _, res in lts.items():
                itype = res["Properties"]["LaunchTemplateData"].get("InstanceType", "")
                # Intel families: m8i / c8i / r8i / m6i / m7i etc.
                assert ("i." in itype or "n." in itype or "a." in itype), \
                    f"x86_64 should pick an Intel/AMD family, got {itype}"
        finally:
            cfg_path.write_text(original)


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
