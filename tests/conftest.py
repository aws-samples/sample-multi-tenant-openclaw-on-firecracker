# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared fixtures for all tests."""

import json
import os
import pathlib
import sys
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
    import sys as _sys
    here = _os.path.dirname(_os.path.abspath(__file__))
    repo_root = _os.path.dirname(here)
    # T3-3: handlers are loaded by file path (importlib.spec_from_file_location)
    # and now do `from common import ...`. Put deploy/lambda on sys.path so that
    # import resolves in-tree (at deploy runtime `common/` is copied next to the
    # handler by stack._stage_lambda_asset, so no path hack is needed there).
    lambda_dir = _os.path.join(repo_root, "deploy", "lambda")
    if lambda_dir not in _sys.path:
        _sys.path.insert(0, lambda_dir)
    # T3-4 Phase 2: the api handler does `from domains.common import ...`; the
    # domains/ package sits inside deploy/lambda/api (copied to the Lambda task
    # root at deploy time). Put that dir on sys.path so importlib-loaded handler
    # tests resolve it too.
    api_dir = _os.path.join(lambda_dir, "api")
    if api_dir not in _sys.path:
        _sys.path.insert(0, api_dir)
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


def synth_stack(mutate=None, *, base="config.yml.example", stack_id="Test",
                account="123456789012", region="ap-northeast-1"):
    """Synth the CDK stack against a THROWAWAY config and return its Template.

    `mutate(cfg)` may edit the parsed config dict in place before synth.

    Why this exists
    ---------------
    Five test modules used to do: read repo `config.yml` → overwrite it →
    synth → restore in `finally`. Three problems that bit us for real:

    1. Two overlapping pytest processes clobbered each other's config.yml —
       reproduced as 24 spurious failures.
    2. The base was the DEVELOPER's config.yml, so a locally-enabled feature
       silently became the assertion baseline (a feature-enabled config once
       baked 206 CFN resources and broke CI on the logical-ID snapshot).
    3. A hard kill between write and restore left the repo config corrupted.

    Passing the config via `OPENCLAW_CONFIG` (see deploy/stack._config_path)
    touches no repo file, so the suite is safe to run concurrently, and
    `_normalize_config` still runs over the injected config — which the
    alternative of assigning `stack_mod.CFG` after import would skip, silently
    disabling the routing.mode/overcommit guardrails under test.
    """
    import importlib.util
    import tempfile

    import yaml

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((repo_root / base).read_text())
    if mutate:
        mutate(cfg)

    with tempfile.TemporaryDirectory() as td:
        cfg_file = pathlib.Path(td) / "config.yml"
        cfg_file.write_text(yaml.safe_dump(cfg))
        prev = os.environ.get("OPENCLAW_CONFIG")
        os.environ["OPENCLAW_CONFIG"] = str(cfg_file)
        try:
            # Re-exec stack.py so its module-level CFG picks up our config.
            for name in ("deploy.stack", "deploy", "stack"):
                sys.modules.pop(name, None)
            spec = importlib.util.spec_from_file_location(
                "deploy.stack", repo_root / "deploy" / "stack.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["deploy.stack"] = mod
            spec.loader.exec_module(mod)

            import aws_cdk as cdk
            from aws_cdk import assertions
            app = cdk.App()
            stack = mod.OpenClawOrchestratorStack(
                app, stack_id,
                env=cdk.Environment(account=account, region=region))
            return assertions.Template.from_stack(stack)
        finally:
            if prev is None:
                os.environ.pop("OPENCLAW_CONFIG", None)
            else:
                os.environ["OPENCLAW_CONFIG"] = prev
            # Leave no half-initialised module behind for the next test.
            sys.modules.pop("deploy.stack", None)


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
