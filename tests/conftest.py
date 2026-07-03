# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared fixtures for all tests."""

import os
import json
import pytest
from unittest.mock import MagicMock

# Default env vars for unit tests (overridden by E2E via .env.deploy)
_DEFAULTS = {
    "TENANTS_TABLE": "openclaw-tenants",
    "HOSTS_TABLE": "openclaw-hosts",
    "GROUPS_TABLE": "openclaw-groups",
    "ASSETS_BUCKET": "test-bucket",
    "ROOTFS_PREFIX": "deployment/rootfs",
    "BACKUP_PREFIX": "backups",
    "HOST_RESERVED_VCPU": "1",
    "HOST_RESERVED_MEM": "2048",
    "CPU_OVERCOMMIT_RATIO": "2.0",
    "MEM_OVERCOMMIT_RATIO": "1.5",
    "VM_DEFAULT_VCPU": "2",
    "VM_DEFAULT_MEM": "4096",
    "VM_DATA_DISK_MB": "8192",
    "VM_PORT_BASE": "18789",
    "VM_SUBNET_PREFIX": "172.16",
    "ASG_NAME": "openclaw-hosts-asg",
    "ALB_LISTENER_ARN": "arn:aws:elasticloadbalancing:us-east-1:123:listener/app/test/123/456",
    "VPC_ID": "vpc-test",
    "IDLE_TIMEOUT_MINUTES": "10",
    "AWS_DEFAULT_REGION": "us-east-1",
}
for k, v in _DEFAULTS.items():
    os.environ.setdefault(k, v)

# Skip CDK asset bundling during synth-based tests. The stack's Lambda functions
# use Code.from_asset(..., bundling=...) which shells out to docker (the
# public.ecr.aws/sam/build-python3.12 image) to pip-install requirements. That
# makes every CDK assertion test depend on a running docker daemon + network,
# turns a sub-second template assertion into a ~minute pip install, and hangs the
# whole `pytest -m unit` run in CI / on contributor machines without docker.
#
# `aws:cdk:bundling-stacks: []` is CDK's official switch to skip bundling for all
# stacks: synth still produces the full CloudFormation template (so every
# Template.from_stack assertion still works), it just stamps a placeholder asset
# instead of running the bundler. Set via CDK_CONTEXT_JSON at import time so it is
# in place before any test module constructs cdk.App(). An explicit env value
# (e.g. an E2E run that genuinely needs real assets) is preserved.
if "CDK_CONTEXT_JSON" not in os.environ:
    os.environ["CDK_CONTEXT_JSON"] = json.dumps({"aws:cdk:bundling-stacks": []})


@pytest.fixture(autouse=True)
def _fast_clock(request):
    """Make polling loops terminate instantly in unit/integration tests.

    The control plane polls SSM / health endpoints with the shape:

        deadline = time.time() + timeout_sec      # e.g. 90 or 120
        while time.time() < deadline:
            time.sleep(poll_sec)
            ... check a mocked client ...

    Under unit tests the client is a MagicMock that may never return a terminal
    status (e.g. a test overrides get_command_invocation to "Failed" without the
    VERIFIED_HTTP_200 stdout the verify-gate waits for). With a real clock that
    loop blocks for the full timeout; if a test ALSO mocks time.sleep to a no-op
    it becomes a busy-loop that pegs a core until the wall-clock deadline. Either
    way `pytest -m unit` hangs.

    Fix at one place instead of per-test: advance a fake monotonic clock by the
    sleep amount on every time.sleep() call, and have time.time() read it. A
    `while time.time() < deadline: time.sleep(poll_sec)` loop then reaches its
    deadline in a handful of iterations, instantly, deterministically — the loop
    still runs its real logic, it just can't wall-clock-hang. E2E tests keep the
    real clock (they talk to real services where timing matters).
    """
    if request.node.get_closest_marker("e2e"):
        yield
        return
    import time as _t
    from unittest.mock import patch

    real_time = _t.time
    state = {"now": real_time()}

    # time.time() auto-advances a little on every read. This is the key: even if
    # a test patches time.sleep to a bare no-op (several failover tests do), the
    # `while time.time() < deadline` poll loops still converge — each deadline
    # check nudges the clock forward, so the loop reaches its timeout in a fixed
    # number of iterations instead of busy-looping forever on a frozen clock.
    def fake_time():
        state["now"] += 0.25
        return state["now"]

    def fake_sleep(secs=0, *a, **k):
        try:
            state["now"] += float(secs) if secs else 0.0
        except (TypeError, ValueError):
            pass

    with patch.object(_t, "sleep", fake_sleep), patch.object(_t, "time", fake_time):
        yield


def make_ddb_table():
    """Create a mock DynamoDB Table."""
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    table.get_item.return_value = {}
    table.put_item.return_value = {}
    # update_item must return an Attributes map: the host-slot allocator
    # (handler.py _claim_vm_slot) uses ReturnValues="UPDATED_NEW" and reads
    # r["Attributes"]["next_vm_num"] to derive the claimed vm_num. An empty {}
    # made every create_tenant-path test KeyError. Default to a post-increment
    # next_vm_num=2 → claimed slot = 1 (the common single-VM case). Tests that
    # need a specific slot override table.update_item.return_value themselves.
    table.update_item.return_value = {"Attributes": {"next_vm_num": 2}}
    table.meta.client.exceptions.ConditionalCheckFailedException = type(
        "CCF", (Exception,), {}
    )
    return table


def load_env_deploy():
    """Load .env.deploy for E2E tests. Returns dict or None if not found.

    Searches relative to the test file's parent (the repo root) and the
    current working directory — covers `pytest` invoked from anywhere
    inside the project. Avoids any hard-coded absolute path so the suite
    runs cleanly in CI / on contributor machines / inside containers.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    candidates = [
        os.path.join(repo_root, ".env.deploy"),
        os.path.join(os.getcwd(), ".env.deploy"),
        os.path.join(os.getcwd(), "..", ".env.deploy"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            env = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
            return env
    return None
