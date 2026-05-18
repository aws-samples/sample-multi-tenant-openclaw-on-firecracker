# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for pluggable VM runtime (issue #5).

The platform currently runs Firecracker, but the design wants to leave a
clean seam for Cloud Hypervisor / QEMU later. This PR adds a Runtime
Abstract Base Class and a factory `get_runtime(name)` so future
implementations can plug in without touching the orchestrator.

Tests assert:
- The ABC declares the four lifecycle methods we need.
- FirecrackerRuntime concretely implements all of them.
- `get_runtime("firecracker")` returns a usable instance.
- `get_runtime("cloud_hypervisor")` and `get_runtime("qemu")` raise
  RuntimeNotImplementedError (the slot exists, the wiring doesn't).
- `get_runtime("unknown")` raises ValueError with the supported names.
"""

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """Import deploy.userdata.runtimes as a package, mocking nothing."""
    pkg_dir = ROOT / "deploy" / "userdata" / "runtimes"
    spec = importlib.util.spec_from_file_location(
        "ocruntime", str(pkg_dir / "__init__.py"),
        submodule_search_locations=[str(pkg_dir)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ocruntime"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
class TestABCInterface:
    def test_runtime_abc_declares_lifecycle_methods(self):
        rt = _load()
        for name in ("launch_vm", "stop_vm", "restart_vm", "balloon_set"):
            assert hasattr(rt.Runtime, name), f"Runtime ABC missing {name}"
            assert getattr(rt.Runtime, name).__isabstractmethod__, \
                f"{name} should be @abstractmethod"

    def test_cannot_instantiate_abc_directly(self):
        rt = _load()
        with pytest.raises(TypeError):
            rt.Runtime()


@pytest.mark.unit
class TestFirecrackerRuntime:
    def test_implements_all_methods(self):
        rt = _load()
        fc = rt.FirecrackerRuntime()
        # All abstract methods are concretely overridden
        for name in ("launch_vm", "stop_vm", "restart_vm", "balloon_set"):
            method = getattr(fc, name)
            assert callable(method)
            # Not abstract anymore
            assert not getattr(method, "__isabstractmethod__", False)

    def test_name_attribute(self):
        rt = _load()
        assert rt.FirecrackerRuntime.NAME == "firecracker"


@pytest.mark.unit
class TestFactory:
    def test_get_firecracker(self):
        rt = _load()
        instance = rt.get_runtime("firecracker")
        assert isinstance(instance, rt.FirecrackerRuntime)

    def test_get_cloud_hypervisor_raises_not_implemented(self):
        rt = _load()
        with pytest.raises(rt.RuntimeNotImplementedError):
            rt.get_runtime("cloud_hypervisor")

    def test_get_qemu_raises_not_implemented(self):
        rt = _load()
        with pytest.raises(rt.RuntimeNotImplementedError):
            rt.get_runtime("qemu")

    def test_unknown_runtime_raises_value_error(self):
        rt = _load()
        with pytest.raises(ValueError) as e:
            rt.get_runtime("xyz")
        # Error message lists supported names so users can self-correct
        msg = str(e.value)
        assert "firecracker" in msg

    def test_supported_list_includes_all_three(self):
        rt = _load()
        names = set(rt.SUPPORTED_RUNTIMES)
        assert {"firecracker", "cloud_hypervisor", "qemu"}.issubset(names)


@pytest.mark.unit
class TestConfigSchema:
    def test_config_example_has_runtime_key(self):
        cfg_text = (ROOT / "config.yml.example").read_text()
        assert "runtime:" in cfg_text, \
            "config.yml.example must declare a runtime field"
        # Default must be firecracker (only fully-implemented one)
        assert "firecracker" in cfg_text
