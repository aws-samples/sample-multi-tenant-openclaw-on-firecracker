# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Issue #72 — migrate-vm.sh must surface the Firecracker error body on a
failed snapshot (no silent `curl -sf` exit 52) and must always resume the
source VM if the snapshot fails, so the async sweep's rollback assumption
(source only briefly paused) holds.

These are static source assertions on the shell script — they'd catch a
regression to the old bare `curl -sf ... /snapshot/create` that swallowed the
error and stranded the VM Paused. A live-behavior check needs a real
Firecracker host, so it lives in the e2e migrate script, not here.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).parent.parent / "deploy" / "userdata" / "migrate-vm.sh"


def _snapshot_block():
    """Return the text of the `snapshot)` case branch."""
    text = SCRIPT.read_text()
    start = text.index("snapshot)")
    end = text.index("restore)", start)
    return text[start:end]


def test_script_exists():
    assert SCRIPT.is_file()


def test_snapshot_uses_show_error_not_silent_fail():
    block = _snapshot_block()
    # The snapshot/create call must NOT use the silent `curl -sf` that produced
    # a bare exit 52. It should capture output with --show-error.
    assert "/snapshot/create" in block
    assert "--show-error" in block, "snapshot/create must surface curl errors"


def test_snapshot_captures_firecracker_body():
    block = _snapshot_block()
    # Must echo the captured Firecracker response on failure.
    assert "Firecracker said" in block or "firecracker said" in block.lower()
    # Must mention the balloon root cause so the SSM output is actionable.
    assert "balloon" in block.lower()


def test_snapshot_resumes_source_on_failure():
    block = _snapshot_block()
    # On snapshot failure the script must resume the source VM (the rollback
    # path assumes the VM was resumed) and then exit non-zero.
    assert "Resumed" in block
    assert "exit 52" in block


def test_snapshot_pause_failure_is_handled():
    block = _snapshot_block()
    # A failed pause must not silently `set -e` abort with no message.
    assert "pause failed" in block.lower()
