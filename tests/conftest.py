# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared fixtures for all tests."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    """Ensure a config.yml exists for the test session.

    config.yml is .gitignored (only config.yml.example is tracked), and
    deploy/stack.py reads config.yml at import time. On a clean checkout / CI
    it's absent, so every stack-synth test would die with FileNotFoundError at
    import. Materialize it from config.yml.example for the session, and remove
    the copy on teardown so we don't leave an untracked file behind. A
    developer's real config.yml is left untouched.
    """
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    repo_root = _os.path.dirname(here)
    cfg = _os.path.join(repo_root, "config.yml")
    example = _os.path.join(repo_root, "config.yml.example")
    if not _os.path.exists(cfg) and _os.path.exists(example):
        import shutil
        shutil.copyfile(example, cfg)
        config._oc_created_config_yml = cfg


def pytest_unconfigure(config):
    import os as _os
    created = getattr(config, "_oc_created_config_yml", None)
    if created and _os.path.exists(created):
        _os.remove(created)


def pytest_collection_modifyitems(config, items):
    """Skip e2e tests by default — they hit a real deployed AWS API.

    pytest markers are just labels; registering `e2e` in pyproject.toml does
    NOT deselect it. Without this hook a plain `pytest` run executes every
    e2e test against whatever `.env.deploy` points at, which fails (e.g. a
    viewer-role API key 403s the write-path tests) instead of skipping.

    e2e tests run only when explicitly opted in:
      - `OPENCLAW_E2E=1 pytest ...`               (CI / operator env), or
      - `pytest tests/test_e2e.py -m e2e -v`      (the documented invocation;
        the `-m e2e` selector puts "e2e" in the mark expression).
    """
    if os.environ.get("OPENCLAW_E2E") == "1":
        return
    if "e2e" in (config.option.markexpr or ""):
        return
    skip_e2e = pytest.mark.skip(
        reason="e2e tests need OPENCLAW_E2E=1 or `-m e2e` (they call a real AWS API)"
    )
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


@pytest.fixture(autouse=True)
def _fast_clock(request):
    """Make sleep-based polling loops finish instantly in unit tests.

    SSM sync-wait paths (e.g. _wait_ssm_done) sleep between polls until a
    real-wall-clock deadline. We make sleep() a no-op that instead advances a
    virtual offset added to time.time(), so a loop that sleeps reaches its
    deadline immediately, while code that never sleeps (JWT exp/iat checks,
    JWKS cache TTL) still sees the real clock. E2E tests keep the real clock.
    """
    if request.node.get_closest_marker("e2e"):
        yield
        return

    import time as _time
    real_time = _time.time
    state = {"offset": 0.0}

    def fake_sleep(secs=0, *_args, **_kwargs):
        state["offset"] += float(secs or 0)

    def fake_time():
        return real_time() + state["offset"]

    with patch("time.sleep", fake_sleep), patch("time.time", fake_time):
        yield

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


def make_ddb_table():
    """Create a mock DynamoDB Table."""
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    table.get_item.return_value = {}
    table.put_item.return_value = {}
    table.update_item.return_value = {}
    table.meta.client.exceptions.ConditionalCheckFailedException = type("CCF", (Exception,), {})
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
