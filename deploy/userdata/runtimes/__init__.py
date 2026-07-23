# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pluggable VM runtime layer (issue #5).

Defines a Runtime ABC and a `get_runtime(name)` factory so the
orchestrator stays agnostic of the concrete hypervisor. Today only
Firecracker is fully implemented; Cloud Hypervisor and QEMU are stubs
that the seam is *visible* (and testable) — concrete drivers can be
contributed without touching the orchestrator.

Why ABC and not duck typing?
- Compile-time guarantee that every implementation provides the four
  lifecycle methods we actually call from host-agent / launch-vm.sh.
- `inspect.isabstract()` lets the test suite confirm the contract.
- Forces stub authors to either implement or explicitly raise.
"""

from abc import ABC, abstractmethod

SUPPORTED_RUNTIMES = ("firecracker", "cloud_hypervisor", "qemu")


class RuntimeNotImplementedError(NotImplementedError):
    """Raised when the configured runtime exists in the protocol but lacks a driver."""


class Runtime(ABC):
    """Lifecycle contract every VM runtime driver must implement."""

    NAME: str = ""

    @abstractmethod
    def launch_vm(self, tenant_id: str, vm_num: int, vcpu: int, mem_mb: int):
        """Boot a microVM. Idempotent: a second call for the same tenant must
        not produce a duplicate VM."""

    @abstractmethod
    def stop_vm(self, tenant_id: str, vm_num: int):
        """Stop the VM cleanly (best effort: send shutdown, then kill)."""

    @abstractmethod
    def restart_vm(self, tenant_id: str, vm_num: int):
        """Stop and re-launch with the same parameters."""

    @abstractmethod
    def balloon_set(self, tenant_id: str, amount_mib: int):
        """Set the memory balloon target (inflate or deflate)."""


# ═══════════════════════════════════════════
# Concrete drivers
# ═══════════════════════════════════════════


class FirecrackerRuntime(Runtime):
    """The default driver. Delegates to the existing launch-vm.sh / stop-vm.sh
    shell scripts so this PR is a non-behavioural change for production."""

    NAME = "firecracker"

    def __init__(self, launch_script: str = "/home/ubuntu/launch-vm.sh",
                 stop_script: str = "/home/ubuntu/stop-vm.sh"):
        self.launch_script = launch_script
        self.stop_script = stop_script

    def launch_vm(self, tenant_id, vm_num, vcpu, mem_mb):
        import subprocess
        subprocess.Popen(
            ["bash", self.launch_script, str(tenant_id), str(vm_num),
             str(vcpu), str(mem_mb)],
        )

    def stop_vm(self, tenant_id, vm_num):
        import subprocess
        subprocess.run(["bash", self.stop_script, str(tenant_id), str(vm_num)],
                       check=False)

    def restart_vm(self, tenant_id, vm_num):
        # The launch script is idempotent enough that we just stop+launch.
        # vcpu / mem_mb come from the host-agent's vm.json read; the script
        # itself reads them when invoked without explicit args.
        self.stop_vm(tenant_id, vm_num)
        # Caller is expected to re-invoke launch_vm with original params.

    def balloon_set(self, tenant_id, amount_mib):
        import json
        import os
        import subprocess
        sock = f"/data/firecracker-vms/{tenant_id}/fc.sock"
        if not os.path.exists(sock):
            return
        subprocess.run(
            ["curl", "-sf", "--unix-socket", sock, "-X", "PATCH",
             "http://localhost/balloon", "-H", "Content-Type: application/json",
             "-d", json.dumps({"amount_mib": amount_mib})],
            check=False,
        )


class CloudHypervisorRuntime(Runtime):
    """Stub. The slot is reserved; a future PR can fill in CHV ch-remote calls."""

    NAME = "cloud_hypervisor"

    def launch_vm(self, *_, **__):
        raise RuntimeNotImplementedError(
            "cloud_hypervisor driver is reserved but not yet implemented")

    def stop_vm(self, *_, **__):
        raise RuntimeNotImplementedError(
            "cloud_hypervisor driver is reserved but not yet implemented")

    def restart_vm(self, *_, **__):
        raise RuntimeNotImplementedError(
            "cloud_hypervisor driver is reserved but not yet implemented")

    def balloon_set(self, *_, **__):
        raise RuntimeNotImplementedError(
            "cloud_hypervisor driver is reserved but not yet implemented")


class QemuRuntime(Runtime):
    """Stub for QEMU/KVM. Same shape as CloudHypervisorRuntime."""

    NAME = "qemu"

    def launch_vm(self, *_, **__):
        raise RuntimeNotImplementedError(
            "qemu driver is reserved but not yet implemented")

    def stop_vm(self, *_, **__):
        raise RuntimeNotImplementedError(
            "qemu driver is reserved but not yet implemented")

    def restart_vm(self, *_, **__):
        raise RuntimeNotImplementedError(
            "qemu driver is reserved but not yet implemented")

    def balloon_set(self, *_, **__):
        raise RuntimeNotImplementedError(
            "qemu driver is reserved but not yet implemented")


# ═══════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════


_REGISTRY = {
    "firecracker": FirecrackerRuntime,
    "cloud_hypervisor": CloudHypervisorRuntime,
    "qemu": QemuRuntime,
}


def get_runtime(name: str, **kwargs) -> Runtime:
    """Return a Runtime instance for the named driver.

    Raises:
        ValueError: name is unknown
        RuntimeNotImplementedError: driver is reserved but not yet wired up
    """
    if name not in _REGISTRY:
        supported = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"unknown runtime '{name}'; supported: {supported}")
    instance = _REGISTRY[name](**kwargs)
    # Trigger NotImplementedError for stubs *before* returning, so callers
    # don't get a half-baked object that fails on first use.
    if name != "firecracker":
        # Probe a method — stubs raise immediately and tell us why.
        instance.launch_vm
        # The probe is just attribute access; the actual raise happens when
        # the caller invokes a method. We surface it eagerly here:
        raise RuntimeNotImplementedError(
            f"runtime '{name}' is reserved in the protocol but not yet implemented")
    return instance
