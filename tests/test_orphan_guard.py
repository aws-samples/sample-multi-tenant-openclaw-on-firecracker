# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for host-agent _write_ddb orphan-row guard (loop 2026-07-02).

Bug: the health-refresh update_item calls in _write_ddb had no
attribute_exists(id) condition, so when a tenant's main record was already
deleted (control-plane DELETE) but its Firecracker process still lingered on
the host, the next health poll would UPSERT a ghost row carrying only health
fields — {id, vm_health, app_health, last_health_check, metrics} — with no
status / host / capacity. A real run accumulated 1254 such orphans across
loop iterations; they pollute scan --select COUNT (COUNT counts them, distinct
status does not) though they don't affect scheduling (which filters on status).

Fix: every health-refresh update_item now carries
ConditionExpression="attribute_exists(id)" so a vanished tenant fails the
write cleanly (caught + logged) instead of resurrecting a ghost.
"""

import importlib.util
import sys
from unittest.mock import patch, MagicMock

import pytest


# Import host-agent.py with mocked SDK (mirror test_dead_zone_recovery.py).
_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with (
    patch("boto3.resource", return_value=_mock_ddb),
    patch("boto3.client", return_value=_mock_ssm),
):
    _mock_ddb.Table.side_effect = lambda name: MagicMock()
    spec = importlib.util.spec_from_file_location(
        "host_agent_orphan", "deploy/userdata/host-agent.py"
    )
    agent = importlib.util.module_from_spec(spec)
    sys.modules["host_agent_orphan"] = agent
    spec.loader.exec_module(agent)


def _make_table():
    """A mock DDB table whose update_item raises ConditionalCheckFailed the way
    boto3 does when attribute_exists(id) fails (tenant record is gone)."""
    table = MagicMock()

    class _CCF(Exception):
        pass

    table.meta.client.exceptions.ConditionalCheckFailedException = _CCF

    def _raise_ccf(*a, **k):
        raise _CCF("The conditional request failed")

    table.update_item.side_effect = _raise_ccf
    return table


@pytest.mark.unit
class TestOrphanGuard:
    def test_down_vm_refresh_carries_attribute_exists(self):
        """A down VM's health refresh must carry attribute_exists(id) so a
        deleted tenant is not resurrected as a health-only orphan."""
        table = _make_table()
        with (
            patch.object(agent, "TENANTS_TABLE", "openclaw-tenants"),
            patch.object(agent, "_get_ddb") as gddb,
        ):
            gddb.return_value.Table.return_value = table
            # vm_health != "up" → takes the plain health-refresh branch.
            results = {
                "ghost-1": {
                    "vm_health": "down",
                    "app_health": "down",
                    "guest_ip": "172.16.1.9",
                }
            }
            # Must not raise even though update_item throws CCF (tenant gone).
            agent._write_ddb(results)

        assert table.update_item.called
        kw = table.update_item.call_args.kwargs
        assert kw.get("ConditionExpression") == "attribute_exists(id)", (
            "down-VM health refresh must guard with attribute_exists(id) "
            "to avoid upserting an orphan row for a deleted tenant"
        )

    def test_existing_tenant_down_refresh_writes(self):
        """When the tenant still exists (update_item succeeds), the refresh
        writes vm_health/app_health as before — guard is transparent."""
        table = MagicMock()
        table.meta.client.exceptions.ConditionalCheckFailedException = type(
            "_CCF", (Exception,), {}
        )
        table.update_item.return_value = {}
        with (
            patch.object(agent, "TENANTS_TABLE", "openclaw-tenants"),
            patch.object(agent, "_get_ddb") as gddb,
        ):
            gddb.return_value.Table.return_value = table
            results = {
                "t-live": {
                    "vm_health": "down",
                    "app_health": "down",
                    "guest_ip": "172.16.1.5",
                }
            }
            agent._write_ddb(results)

        kw = table.update_item.call_args.kwargs
        assert kw["Key"] == {"id": "t-live"}
        assert kw.get("ConditionExpression") == "attribute_exists(id)"
        assert kw["ExpressionAttributeValues"][":vh"] == "down"
