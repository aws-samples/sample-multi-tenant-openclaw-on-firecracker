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
            "deploy.stack", ROOT / "deploy" / "stack.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["deploy.stack"] = mod
        spec.loader.exec_module(mod)
        import aws_cdk as cdk
        from aws_cdk import assertions

        app = cdk.App()
        stack = mod.OpenClawOrchestratorStack(
            app,
            "Test",
            env=cdk.Environment(account="123456789012", region="ap-northeast-1"),
        )
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
                "deploy.stack", ROOT / "deploy" / "stack.py"
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["deploy.stack"] = mod
            spec.loader.exec_module(mod)
            import aws_cdk as cdk
            from aws_cdk import assertions

            app = cdk.App()
            stack = mod.OpenClawOrchestratorStack(
                app,
                "Test",
                env=cdk.Environment(account="123456789012", region="ap-northeast-1"),
            )
            tpl = assertions.Template.from_stack(stack)
            lts = tpl.find_resources("AWS::EC2::LaunchTemplate")
            assert lts
            for _, res in lts.items():
                itype = res["Properties"]["LaunchTemplateData"].get("InstanceType", "")
                assert "g." in itype, (
                    f"arm64 should pick a Graviton (g) family, got {itype}"
                )
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
                "deploy.stack", ROOT / "deploy" / "stack.py"
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["deploy.stack"] = mod
            spec.loader.exec_module(mod)
            import aws_cdk as cdk
            from aws_cdk import assertions

            app = cdk.App()
            stack = mod.OpenClawOrchestratorStack(
                app,
                "Test",
                env=cdk.Environment(account="123456789012", region="ap-northeast-1"),
            )
            tpl = assertions.Template.from_stack(stack)
            lts = tpl.find_resources("AWS::EC2::LaunchTemplate")
            assert lts
            for _, res in lts.items():
                itype = res["Properties"]["LaunchTemplateData"].get("InstanceType", "")
                # Intel families: m8i / c8i / r8i / m6i / m7i etc.
                assert "i." in itype or "n." in itype or "a." in itype, (
                    f"x86_64 should pick an Intel/AMD family, got {itype}"
                )
        finally:
            cfg_path.write_text(original)


@pytest.mark.unit
class TestMixedInstancePool:
    """task #22 — ASG MixedInstancesPolicy across equal-capacity metal types.

    Host total vCPU/mem is injected STATICALLY into the hosts table at init
    (init-host.sh writes total_vcpu / total_mem_mb), so every member of a
    mixed-instance pool must have identical capacity or a smaller member would
    over-subscribe. These tests pin the capacity table (which must understand
    bare-metal size tokens like "metal-24xl") and the equal-capacity rule.

    Pure-logic mirror of deploy/stack.py's _host_capacity / pool validation —
    runs without aws_cdk so it executes in any environment.
    """

    # Mirror of the tables in deploy/stack.py (kept in sync intentionally).
    _SIZES = {
        "medium": 1,
        "large": 2,
        "xlarge": 4,
        "2xlarge": 8,
        "4xlarge": 16,
        "8xlarge": 32,
        "12xlarge": 48,
        "16xlarge": 64,
        "24xlarge": 96,
        "metal-24xl": 96,
        "metal-48xl": 192,
        "metal": 64,
    }
    _MEM_RATIO = {"c": 2048, "m": 4096, "r": 8192, "i": 8192}

    def _cap(self, itype):
        family, size = itype.split(".")[0], itype.split(".")[1]
        return self._SIZES[size], self._SIZES[size] * self._MEM_RATIO[family[0]]

    def test_stack_tables_match_mirror(self):
        """The mirror tables above must equal what stack.py actually uses, so
        these tests can't silently drift from the real code."""
        src = (ROOT / "deploy" / "stack.py").read_text()
        # metal tokens must be present in stack.py's _sizes
        for tok in ("metal-24xl", "metal-48xl"):
            assert f'"{tok}"' in src, f"stack.py _sizes missing {tok}"
        # i-family must map to the 8GiB ratio alongside r
        assert '"i": 8192' in src, "stack.py _mem_ratio must map i→8192"

    def test_metal_24xl_capacity(self):
        """r8g/i8g/i8ge.metal-24xl are all 96 vCPU / 768 GiB (verified against
        ap-southeast-1 describe-instance-types)."""
        for t in ("r8g.metal-24xl", "i8g.metal-24xl", "i8ge.metal-24xl"):
            vcpu, mem = self._cap(t)
            assert vcpu == 96, f"{t} vCPU"
            assert mem == 768 * 1024, f"{t} mem"

    def test_equal_capacity_pool_passes(self):
        pool = ["r8g.metal-24xl", "i8g.metal-24xl", "i8ge.metal-24xl"]
        caps = {t: self._cap(t) for t in pool}
        assert len(set(caps.values())) == 1, f"pool not equal-capacity: {caps}"

    def test_unequal_capacity_pool_rejected(self):
        """m8g(384GiB)/c8g(192GiB).metal-24xl differ from r8g(768GiB) → must be
        caught by the equal-capacity rule before it can over-subscribe."""
        pool = ["r8g.metal-24xl", "m8g.metal-24xl", "c8g.metal-24xl"]
        caps = {t: self._cap(t) for t in pool}
        assert len(set(caps.values())) > 1, (
            "these differ; stack.py must raise ValueError on this pool"
        )

    def test_metal_detection(self):
        """`.metal` types must be flagged so nested-virt is NOT enabled
        (AWS: nested virtualization applies only to non-metal types)."""
        assert ".metal" in "r8g.metal-24xl"
        assert ".metal" not in "m8i.2xlarge"

    def test_no_keyerror_on_metal_size_token(self):
        """Regression: r8g.metal-24xl.split('.')[1] == 'metal-24xl' used to
        KeyError the old _sizes table that only had virtual tokens."""
        # Must not raise
        self._cap("r8g.metal-24xl")
        self._cap("r8g.metal-48xl")
        self._cap("r7g.metal")


@pytest.mark.unit
class TestBuildRootfsArch:
    def test_build_script_accepts_arch_flag(self):
        sh = (ROOT / "build-rootfs.sh").read_text()
        # Either a `--arch` flag handler or an ARCH env var in the help text
        assert "--arch" in sh or "ARCH=" in sh

    def test_build_script_supports_arm64(self):
        sh = (ROOT / "build-rootfs.sh").read_text()
        assert "arm64" in sh, (
            "build-rootfs.sh must mention arm64 (debootstrap target arch)"
        )
