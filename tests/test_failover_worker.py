# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-2 worker: deploy/lambda/health_check/worker.py.

The worker consumes one SQS failover job, re-claims the tenant
(failover_queued/failover_recovering → failover_recovering), loads the target
host fresh, and runs handler._execute_failover. These tests stub the shared
handler pipeline (already covered by test_az_failover.py) and pin the worker's
claim/idempotency semantics.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

pytestmark = pytest.mark.unit


def _load_worker():
    """Load worker.py, which does `import handler`. Both are in the same asset
    dir; we import handler first under mocked AWS, alias it as 'handler' so the
    worker's top-level import resolves to the mocked module, then load worker."""
    mock_ddb = MagicMock()
    table_cache = {}

    def _table_factory(name):
        if name not in table_cache:
            table_cache[name] = make_ddb_table()
        return table_cache[name]

    mock_ddb.Table.side_effect = _table_factory
    mocks = {"ssm": MagicMock(), "sns": MagicMock(), "s3": MagicMock(),
             "elbv2": MagicMock(), "sqs": MagicMock()}

    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", side_effect=lambda svc: mocks.get(svc, MagicMock())):
        hspec = importlib.util.spec_from_file_location(
            "handler", "deploy/lambda/health_check/handler.py")
        hmod = importlib.util.module_from_spec(hspec)
        sys.modules["handler"] = hmod   # worker.py does `import handler`
        hspec.loader.exec_module(hmod)

        wspec = importlib.util.spec_from_file_location(
            "failover_worker", "deploy/lambda/health_check/worker.py")
        wmod = importlib.util.module_from_spec(wspec)
        sys.modules["failover_worker"] = wmod
        wspec.loader.exec_module(wmod)
    wmod._hc = hmod
    wmod._tables = table_cache
    return wmod


def _event(**job):
    job.setdefault("tenant_id", "t1")
    job.setdefault("source_az", "az-a")
    job.setdefault("source_host_id", "i-dead")
    job.setdefault("target_host_id", "i-ok")
    job.setdefault("target_vm_num", 3)
    job.setdefault("backup_key", "backups/t1/x.gz")
    return {"Records": [{"body": json.dumps(job)}]}


class TestWorkerClaim:
    def test_success_path_claims_then_executes(self):
        w = _load_worker()
        hc = w._hc
        hc.tenants_table.get_item = MagicMock(return_value={"Item": {
            "id": "t1", "status": "failover_queued", "host_id": "i-dead",
            "vcpu": 2, "mem_mb": 4096}})
        hc.tenants_table.update_item = MagicMock()  # claim succeeds
        hc.hosts_table.get_item = MagicMock(return_value={"Item": {
            "instance_id": "i-ok", "private_ip": "10.0.0.9", "az": "az-b"}})
        with patch.object(hc, "_execute_failover", return_value=True) as ex:
            w.lambda_handler(_event(), None)
        # Claim flipped to failover_recovering, then execute ran with the job's
        # target_vm_num + backup_key.
        claim = hc.tenants_table.update_item.call_args
        assert claim.kwargs["ExpressionAttributeValues"][":r"] == "failover_recovering"
        ex.assert_called_once()
        assert ex.call_args.kwargs["target_vm_num"] == 3
        assert ex.call_args.kwargs["backup_key"] == "backups/t1/x.gz"

    def test_recovering_redelivery_reclaims_and_proceeds(self):
        """A redelivered message (prior worker died) finds status=
        failover_recovering; the claim ConditionExpression accepts it."""
        w = _load_worker()
        hc = w._hc
        hc.tenants_table.get_item = MagicMock(return_value={"Item": {
            "id": "t1", "status": "failover_recovering", "host_id": "i-dead",
            "vcpu": 2, "mem_mb": 4096}})
        hc.tenants_table.update_item = MagicMock()
        hc.hosts_table.get_item = MagicMock(return_value={"Item": {
            "instance_id": "i-ok", "private_ip": "10.0.0.9"}})
        with patch.object(hc, "_execute_failover", return_value=True) as ex:
            w.lambda_handler(_event(), None)
        ex.assert_called_once()

    def test_running_message_is_silent_noop(self):
        """Crash-after-flip redelivery: tenant already running → re-claim fails
        the ConditionExpression → no execute, no SSM."""
        w = _load_worker()
        hc = w._hc
        hc.tenants_table.get_item = MagicMock(return_value={"Item": {
            "id": "t1", "status": "running", "host_id": "i-ok"}})
        exc = hc.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
        hc.tenants_table.update_item = MagicMock(
            side_effect=exc({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"))
        with patch.object(hc, "_execute_failover") as ex:
            w.lambda_handler(_event(), None)
        ex.assert_not_called()

    def test_unknown_tenant_drops_job(self):
        w = _load_worker()
        hc = w._hc
        hc.tenants_table.get_item = MagicMock(return_value={})
        with patch.object(hc, "_execute_failover") as ex:
            w.lambda_handler(_event(tenant_id="ghost"), None)
        ex.assert_not_called()

    def test_vanished_target_host_fails_tenant(self):
        w = _load_worker()
        hc = w._hc
        hc.tenants_table.get_item = MagicMock(return_value={"Item": {
            "id": "t1", "status": "failover_queued", "host_id": "i-dead"}})
        hc.tenants_table.update_item = MagicMock()
        hc.hosts_table.get_item = MagicMock(return_value={})  # target gone
        with patch.object(hc, "_execute_failover") as ex, \
             patch.object(hc, "_emit_audit"):
            w.lambda_handler(_event(), None)
        ex.assert_not_called()
        # Tenant flipped to failover_failed (last update_item call).
        last = hc.tenants_table.update_item.call_args
        assert last.kwargs["ExpressionAttributeValues"][":f"] == "failover_failed"
