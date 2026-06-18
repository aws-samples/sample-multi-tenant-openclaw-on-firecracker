# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static regression test for init-host.sh lifecycle-hook safety (issue #73).

init-host.sh runs under ``set -e`` as ASG launch user-data. 
The DDB self-register step used to have no error tolerance and ran *before* the explicit complete-lifecycle-action call, 
so a transient ``put-item`` failure (common when a batch scale-out throttles DDB writes) aborted the script before the hook was settled — leaving the instance stuck ``MidLifecycleAction`` until the 600s timeout. 
Several hosts in a burst launch could hang this way.

The fix:
  1. An EXIT trap always settles the hook (CONTINUE on success, ABANDON on
     failure), so no init failure can leave an instance hanging.
  2. The DDB register retries, so a transient throttle doesn't fail init.

These assertions are pure static analysis — no AWS, no shell execution.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INIT_HOST_SH = ROOT / "deploy" / "userdata" / "init-host.sh"


@pytest.fixture(scope="module")
def init_text():
    return INIT_HOST_SH.read_text()


@pytest.mark.regression
def test_installs_exit_trap_completing_the_hook(init_text):
    """An EXIT trap must call complete-lifecycle-action, so any failure path
    still settles the ASG hook instead of hanging until timeout."""
    assert re.search(r"trap\s+\w+\s+EXIT", init_text), (
        "init-host.sh must install an EXIT trap so the lifecycle hook is "
        "always settled (issue #73)"
    )
    # The trap handler must actually complete the lifecycle action.
    assert "complete-lifecycle-action" in init_text, (
        "the EXIT trap must call complete-lifecycle-action"
    )


@pytest.mark.regression
def test_trap_abandons_on_failure(init_text):
    """On a non-zero exit the hook must be ABANDONed (ASG replaces the host
    immediately) rather than CONTINUEd or left to time out."""
    assert "ABANDON" in init_text, (
        "init-host.sh must ABANDON the lifecycle hook on failure so a broken "
        "host is replaced promptly instead of hanging MidLifecycleAction"
    )
    assert "CONTINUE" in init_text, "successful init must CONTINUE the hook"


@pytest.mark.regression
def test_ddb_register_retries(init_text):
    """The DDB self-register must retry — a single throttled put-item under
    concurrent launch must not fail the whole init."""
    assert "put-item" in init_text, "init-host.sh must register the host via put-item"
    # The register must sit inside a retry loop with break-on-success.
    assert re.search(r"for\s+\w+\s+in\s+\$\(seq[^\n]+\)\s*;?\s*do", init_text), (
        "init-host.sh register should be wrapped in a retry loop (issue #73)"
    )
    assert re.search(r"put-item.*&&\s*\{?\s*_registered=1", init_text, re.DOTALL), (
        "the DDB put-item register must set a success flag and break inside a "
        "retry loop so a transient throttle is retried rather than aborting init"
    )


@pytest.mark.regression
def test_register_failure_exits_nonzero(init_text):
    """If all register attempts fail, init must exit non-zero so the EXIT trap
    ABANDONs. An unregistered host that CONTINUEs is invisible to the scheduler
    (never gets tenants) — worse than being replaced (issue #73 review finding)."""
    # After the retry loop, a guard must exit 1 when registration never succeeded.
    assert re.search(r'_registered.*-eq\s*1.*\|\|.*exit\s+1', init_text, re.DOTALL), (
        "init-host.sh must `exit 1` when DDB registration fails all attempts, "
        "so the host ABANDONs instead of CONTINUEing unregistered"
    )


@pytest.mark.regression
def test_no_unguarded_complete_outside_trap(init_text):
    """The old explicit Step-6 complete call (after register, reachable only on
    success) should be gone — the trap owns completion now. Guard against a
    regression that reintroduces a complete-lifecycle call that a failed
    register would skip."""
    # complete-lifecycle-action should appear only within the trap function,
    # not as a standalone post-register step. We assert it appears exactly once.
    assert init_text.count("complete-lifecycle-action") == 1, (
        "complete-lifecycle-action should appear once (inside the EXIT trap); "
        "a second standalone call would be skipped when init fails early"
    )


@pytest.mark.regression
def test_firecracker_version_is_pinned_not_latest(init_text):
    """init-host.sh must NOT resolve the Firecracker version from `latest` (#74).
    `latest` bumped to v1.16.0 whose CI guest-kernel assets didn't exist yet,
    404ing step3b and bricking every new host. The version must be pinned."""
    # No reliance on the GitHub `latest` redirect to pick FC_VER.
    assert not re.search(r"FC_VER=.*releases/latest", init_text), (
        "init-host.sh resolves Firecracker from `latest` — pin FC_VER instead (#74)"
    )
    # A concrete pinned version must be present.
    assert re.search(r'FC_VER="?\$\{FC_VERSION:-v\d+\.\d+\.\d+\}', init_text) \
        or re.search(r'FC_VER="?v\d+\.\d+\.\d+', init_text), (
        "init-host.sh must pin a concrete Firecracker version (e.g. v1.15.1)"
    )
