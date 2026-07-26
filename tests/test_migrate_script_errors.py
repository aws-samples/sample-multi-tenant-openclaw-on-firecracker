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


# ── Issue #72: balloon-aware snapshot + cold-migration modes ──

def _full():
    return SCRIPT.read_text()


def test_snapshot_quiesces_host_agent_when_balloon_on():
    block = _snapshot_block()
    # The balloon-aware branch must drop the migration sentinel (so host-agent
    # backs off) and clear it via a trap even on unexpected exit.
    assert "_migration_begin" in block and "_migration_end" in block
    assert "trap" in block
    # Gated on BALLOON_ENABLED so non-balloon VMs are unaffected.
    assert 'BALLOON_ENABLED' in block


def test_sentinel_helpers_defined():
    t = _full()
    assert 'SENTINEL="${VM_DIR}/.migrating"' in t
    assert "_migration_begin()" in t and "_migration_end()" in t
    # Must source platform.env to learn BALLOON_ENABLED on the host.
    assert "/etc/platform.env" in t


def test_cold_modes_exist():
    t = _full()
    assert "cold-dump)" in t, "cold-dump mode missing"
    assert "cold-restore)" in t, "cold-restore mode missing"
    # cold-dump must stop the VM before copying the data volume (quiescent copy).
    cold = t[t.index("cold-dump)"):t.index("cold-restore)")]
    assert "stop-vm.sh" in cold
    assert "data.ext4" in cold


def test_restore_uses_sentinel_against_recover_race():
    t = _full()
    restore = t[t.index("restore)"):t.index("cold-dump)")]
    # The target's host-agent would race _recover_vm against snapshot/load
    # unless the sentinel is set before vm.json lands.
    assert "SENTINEL" in restore


# ══════════════════════════════════════════════════════════════════════
# T3-1 P2b: live-restore must rebuild the guest network (incl. the IMDS
# DROP) and write the tenant's nginx conf, else the restored tenant is
# unreachable AND can steal host IMDS credentials.
# ══════════════════════════════════════════════════════════════════════


def _restore_block():
    """Text of the live `restore)` case branch (up to cold-dump)."""
    text = SCRIPT.read_text()
    start = text.index("restore)")
    end = text.index("cold-dump)", start)
    return text[start:end]


def test_restore_creates_tap_from_source_vm_num():
    b = _restore_block()
    # The tap MUST key off the SOURCE vm_num parsed from vm.json (the snapshot
    # baked in the source's guest network), NOT this invocation's target VM_NUM.
    assert 'SRC_VM_NUM=' in b and '"vm_num"' in b, \
        "restore must parse source vm_num from vm.json"
    assert "ip tuntap add dev" in b and "tap-vm${SRC_VM_NUM}" in b, \
        "restore must recreate tap-vm<src_vm_num>"


def test_restore_blocks_guest_to_imds():
    """SECURITY regression guard: omitting the IMDS DROP lets a restored tenant
    reach 169.254.169.254 and steal the host EC2 instance-profile creds."""
    b = _restore_block()
    assert "169.254.169.254 -j DROP" in b, "restore missing IMDSv4 DROP"
    assert "169.254.169.253 -j DROP" in b, "restore missing IMDS (.253) DROP"
    # The DROP must be INSERTED (top of FORWARD), i.e. before the ACCEPT rules.
    assert "iptables -I FORWARD 1 -i" in b, \
        "IMDS DROP must be -I FORWARD 1 (above the FORWARD ACCEPT)"


def test_restore_writes_nginx_conf():
    b = _restore_block()
    assert "/etc/nginx/conf.d/tenants/${TENANT}.conf" in b, \
        "restore must write the tenant nginx location (host-tg routing needs it)"
    assert "proxy_pass http://${R_GUEST_IP}:18789" in b, \
        "restore nginx conf must proxy to the source guest IP on :18789"
    assert "nginx -s reload" in b


def test_restore_imds_drop_precedes_masquerade():
    """Ordering: the FORWARD DROP must appear before the MASQUERADE rule in the
    script so guest→IMDS is blocked before any NAT could expose it."""
    b = _restore_block()
    drop_pos = b.index("169.254.169.254 -j DROP")
    masq_pos = b.index("POSTROUTING")
    assert drop_pos < masq_pos, "IMDS DROP must be set up before MASQUERADE"
