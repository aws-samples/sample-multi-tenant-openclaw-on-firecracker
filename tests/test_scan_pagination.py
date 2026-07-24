# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T2-6/T2-7: DynamoDB scans must paginate (no silent 1MB truncation) and the
pinned-host lookup must use get_item, not a full-table scan.

The bug this guards: a bare Table.scan() returns only the first ~1MB page, so
past ~500-1000 tenants the health watchdog, TTL sweep, scheduler, and backups
silently stop seeing rows. _scan_all follows LastEvaluatedKey.
"""

import importlib.util
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "scanpg_handler", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["scanpg_handler"] = api
    spec.loader.exec_module(api)

pytestmark = pytest.mark.unit


class TestScanAll:
    def test_follows_last_evaluated_key_across_pages(self):
        table = MagicMock()
        # Page 1 has a cursor; page 2 does not → loop stops after 2 calls.
        table.scan.side_effect = [
            {"Items": [{"id": "a"}, {"id": "b"}], "LastEvaluatedKey": {"id": "b"}},
            {"Items": [{"id": "c"}]},
        ]
        items = api._scan_all(table)
        assert [i["id"] for i in items] == ["a", "b", "c"], "page 2 not read"
        assert table.scan.call_count == 2
        # The 2nd call must carry ExclusiveStartKey = the prior cursor.
        assert table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"id": "b"}

    def test_forwards_filter_kwargs_to_every_page(self):
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"id": "x"}},
            {"Items": []},
        ]
        api._scan_all(table, FilterExpression="#s = :r",
                      ExpressionAttributeValues={":r": "running"})
        for call in table.scan.call_args_list:
            assert call.kwargs["FilterExpression"] == "#s = :r"
            assert call.kwargs["ExpressionAttributeValues"] == {":r": "running"}

    def test_single_page_no_cursor(self):
        table = MagicMock()
        table.scan.return_value = {"Items": [{"id": "only"}]}
        assert api._scan_all(table) == [{"id": "only"}]
        assert table.scan.call_count == 1


class TestListTenantsPaginates:
    def test_list_tenants_reads_beyond_page_one(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.side_effect = [
            {"Items": [{"id": "t1", "status": "running"}],
             "LastEvaluatedKey": {"id": "t1"}},
            {"Items": [{"id": "t2", "status": "running"}]},
        ]
        resp = api.list_tenants()
        import json
        ids = {t["id"] for t in json.loads(resp["body"])}
        assert ids == {"t1", "t2"}, "list_tenants truncated at page 1"


class TestGetSpecificHostUsesGetItem:
    def test_uses_get_item_not_scan(self):
        api.hosts_table = make_ddb_table()
        api.CPU_OVERCOMMIT_RATIO = 2.0
        api.MEM_OVERCOMMIT_RATIO = 1.5
        api.MAX_VMS_PER_HOST = 0
        api.hosts_table.get_item.return_value = {"Item": {
            "instance_id": "i-9", "status": "active", "total_vcpu": 8,
            "total_mem_mb": 16384, "used_vcpu": 0, "used_mem_mb": 0, "vm_count": 0}}
        h = api._get_specific_host_with_capacity("i-9", 2, 4096)
        assert h and h["instance_id"] == "i-9"
        api.hosts_table.get_item.assert_called_once_with(Key={"instance_id": "i-9"})
        api.hosts_table.scan.assert_not_called()

    def test_missing_host_returns_none(self):
        api.hosts_table = make_ddb_table()
        api.hosts_table.get_item.return_value = {}
        assert api._get_specific_host_with_capacity("i-gone", 1, 1024) is None

    def test_wrong_status_returns_none(self):
        api.hosts_table = make_ddb_table()
        api.hosts_table.get_item.return_value = {"Item": {
            "instance_id": "i-9", "status": "draining", "total_vcpu": 8,
            "total_mem_mb": 16384, "used_vcpu": 0, "used_mem_mb": 0, "vm_count": 0}}
        assert api._get_specific_host_with_capacity("i-9", 1, 1024) is None
