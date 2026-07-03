# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for seamless rolling image refresh (task #21).

The scaler force-rotates every tenant onto the current golden image every
REFRESH_INTERVAL_HOURS by REBUILD (backup → relaunch on a new-image host →
repoint → drop old), never by hot-patching a live VM. These tests pin the
pure decision predicate `_should_refresh_image` and the new-image host picker
`_find_new_image_host`, which are the load-bearing logic; the SSM/DDB side
effects are exercised via mocks.
"""

import importlib.util
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_cli = MagicMock()

with (
    patch("boto3.resource", return_value=_mock_ddb),
    patch("boto3.client", return_value=_mock_cli),
):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "ir_handler", "deploy/lambda/scaler/handler.py"
    )
    ir = importlib.util.module_from_spec(spec)
    sys.modules["ir_handler"] = ir
    spec.loader.exec_module(ir)


NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _ago_h(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


@pytest.mark.unit
class TestShouldRefreshImage:
    """Pure predicate: does this tenant need rolling onto the new image now?"""

    def test_empty_golden_never_refreshes(self):
        t = {"status": "running", "rootfs_version": "v4", "last_refresh": _ago_h(50)}
        assert ir._should_refresh_image(t, "", NOW, 48) is False

    def test_already_on_golden_skips(self):
        t = {"status": "running", "image_version": "v5", "last_refresh": _ago_h(50)}
        assert ir._should_refresh_image(t, "v5", NOW, 48) is False

    def test_behind_and_overdue_refreshes(self):
        t = {"status": "running", "image_version": "v4", "last_refresh": _ago_h(50)}
        assert ir._should_refresh_image(t, "v5", NOW, 48) is True

    def test_behind_but_within_interval_skips(self):
        t = {"status": "running", "image_version": "v4", "last_refresh": _ago_h(10)}
        assert ir._should_refresh_image(t, "v5", NOW, 48) is False

    @pytest.mark.parametrize(
        "status", ["stopped", "deleted", "failed", "creating", "migrating"]
    )
    def test_non_running_status_skips(self, status):
        t = {"status": status, "image_version": "v4", "last_refresh": _ago_h(50)}
        assert ir._should_refresh_image(t, "v5", NOW, 48) is False

    def test_already_mid_refresh_skips(self):
        t = {
            "status": "running",
            "image_version": "v4",
            "last_refresh": _ago_h(50),
            "image_refresh_phase": "backup",
        }
        assert ir._should_refresh_image(t, "v5", NOW, 48) is False

    def test_behind_no_timestamp_refreshes(self):
        """Predates the last_refresh field but version is behind → eligible."""
        t = {"status": "running", "rootfs_version": "v4"}
        assert ir._should_refresh_image(t, "v5", NOW, 48) is True

    def test_fallback_to_rootfs_version(self):
        """No image_version → fall back to the rootfs_version it was born on."""
        t = {"status": "running", "rootfs_version": "v4", "created_at": _ago_h(50)}
        assert ir._should_refresh_image(t, "v5", NOW, 48) is True

    def test_malformed_timestamp_treated_eligible(self):
        t = {"status": "running", "image_version": "v4", "last_refresh": "not-a-date"}
        assert ir._should_refresh_image(t, "v5", NOW, 48) is True


@pytest.mark.unit
class TestFindNewImageHost:
    """Pick an active host already on the new image with capacity, excluding
    the source host. Must never return a host still on the old image."""

    def _hosts(self, items):
        tbl = make_ddb_table()
        tbl.scan.return_value = {"Items": items}
        return tbl

    def test_picks_new_image_host_with_capacity(self):
        ir.hosts_table = self._hosts(
            [
                {
                    "instance_id": "i-new",
                    "status": "active",
                    "rootfs_version": "v5",
                    "total_vcpu": 96,
                    "used_vcpu": 0,
                    "total_mem_mb": 786432,
                    "used_mem_mb": 0,
                    "private_ip": "172.16.0.9",
                },
            ]
        )
        h = ir._find_new_image_host("v5", 2, 4096, exclude_host_id="i-old")
        assert h is not None and h["instance_id"] == "i-new"

    def test_rejects_old_image_host(self):
        ir.hosts_table = self._hosts(
            [
                {
                    "instance_id": "i-old2",
                    "status": "active",
                    "rootfs_version": "v4",
                    "total_vcpu": 96,
                    "used_vcpu": 0,
                    "total_mem_mb": 786432,
                    "used_mem_mb": 0,
                },
            ]
        )
        assert ir._find_new_image_host("v5", 2, 4096, exclude_host_id="i-old") is None

    def test_excludes_source_host(self):
        ir.hosts_table = self._hosts(
            [
                {
                    "instance_id": "i-src",
                    "status": "active",
                    "rootfs_version": "v5",
                    "total_vcpu": 96,
                    "used_vcpu": 0,
                    "total_mem_mb": 786432,
                    "used_mem_mb": 0,
                },
            ]
        )
        assert ir._find_new_image_host("v5", 2, 4096, exclude_host_id="i-src") is None

    def test_rejects_insufficient_capacity(self):
        ir.hosts_table = self._hosts(
            [
                {
                    "instance_id": "i-full",
                    "status": "active",
                    "rootfs_version": "v5",
                    "total_vcpu": 96,
                    "used_vcpu": 95,
                    "total_mem_mb": 786432,
                    "used_mem_mb": 786000,
                },
            ]
        )
        assert ir._find_new_image_host("v5", 4, 8192, exclude_host_id="i-old") is None

    def test_prefers_emptiest_host(self):
        ir.hosts_table = self._hosts(
            [
                {
                    "instance_id": "i-busy",
                    "status": "active",
                    "rootfs_version": "v5",
                    "total_vcpu": 96,
                    "used_vcpu": 80,
                    "total_mem_mb": 786432,
                    "used_mem_mb": 600000,
                },
                {
                    "instance_id": "i-empty",
                    "status": "active",
                    "rootfs_version": "v5",
                    "total_vcpu": 96,
                    "used_vcpu": 4,
                    "total_mem_mb": 786432,
                    "used_mem_mb": 8192,
                },
            ]
        )
        h = ir._find_new_image_host("v5", 2, 4096, exclude_host_id="i-old")
        assert h["instance_id"] == "i-empty"


@pytest.mark.unit
class TestReconcileGating:
    """Refresh must be a no-op when manifest unreadable (fail safe) and when
    no new-image host exists yet (defer, don't crash)."""

    def test_no_manifest_is_noop(self):
        ir.tenants_table = make_ddb_table()
        with patch.object(ir, "_current_golden_version", return_value=""):
            ir._reconcile_image_refresh()  # must not raise
        ir.tenants_table.update_item.assert_not_called()

    def test_eligible_but_no_target_defers(self):
        tbl = make_ddb_table()
        tbl.scan.return_value = {
            "Items": [
                {
                    "id": "t1",
                    "status": "running",
                    "image_version": "v4",
                    "last_refresh": _ago_h(50),
                    "host_id": "i-old",
                    "vcpu": 2,
                    "mem_mb": 4096,
                },
            ]
        }
        ir.tenants_table = tbl
        ir.hosts_table = make_ddb_table()
        ir.hosts_table.scan.return_value = {"Items": []}  # no new-image host
        with patch.object(ir, "_current_golden_version", return_value="v5"):
            ir._reconcile_image_refresh()
        # deferred: tenant not marked mid-refresh
        tbl.update_item.assert_not_called()
