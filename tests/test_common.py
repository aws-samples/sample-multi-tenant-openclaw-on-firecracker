# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the shared deploy/lambda/common/ package (T3-3, Phase 1).

Covers ddb.scan_all pagination and the capacity math that is now the single
source of truth for both the API scheduler and AZ-failover placement.
"""

from unittest.mock import MagicMock

import pytest
from common import capacity, ddb

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════════════
# ddb.scan_all — pagination
# ══════════════════════════════════════════════════════════════════════


class TestScanAll:
    def test_follows_last_evaluated_key_across_pages(self):
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [{"id": "a"}], "LastEvaluatedKey": {"id": "a"}},
            {"Items": [{"id": "b"}], "LastEvaluatedKey": {"id": "b"}},
            {"Items": [{"id": "c"}]},  # no LEK → last page
        ]
        out = ddb.scan_all(table)
        assert [i["id"] for i in out] == ["a", "b", "c"]
        assert table.scan.call_count == 3

    def test_forwards_kwargs_to_every_page_no_start_key_on_first(self):
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"id": "x"}},
            {"Items": []},
        ]
        ddb.scan_all(table, FilterExpression="#s = :r",
                     ExpressionAttributeNames={"#s": "status"})
        first, second = table.scan.call_args_list
        assert "ExclusiveStartKey" not in first.kwargs
        assert first.kwargs["FilterExpression"] == "#s = :r"
        # Page 2 carries the cursor AND the same filter kwargs.
        assert second.kwargs["ExclusiveStartKey"] == {"id": "x"}
        assert second.kwargs["FilterExpression"] == "#s = :r"

    def test_single_page(self):
        table = MagicMock()
        table.scan.return_value = {"Items": [{"id": "only"}]}
        assert ddb.scan_all(table) == [{"id": "only"}]


# ══════════════════════════════════════════════════════════════════════
# capacity — allocatable / host_free / host_fits / rank_hosts
# ══════════════════════════════════════════════════════════════════════


def _host(iid="i-1", total_vcpu=8, total_mem_mb=16384,
          used_vcpu=0, used_mem_mb=0, vm_count=0):
    return {"instance_id": iid, "total_vcpu": total_vcpu,
            "total_mem_mb": total_mem_mb, "used_vcpu": used_vcpu,
            "used_mem_mb": used_mem_mb, "vm_count": vm_count}


class TestCapacityMath:
    def test_allocatable_applies_ratios_with_int_truncation(self):
        h = _host(total_vcpu=8, total_mem_mb=1000)
        assert capacity.allocatable(h, 1.5, 1.5) == (12, 1500)
        # int() truncates, matching historical API math (8*1.3=10.4→10).
        assert capacity.allocatable(h, 1.3, 1.0) == (10, 1000)

    def test_host_free_subtracts_used_defaulting_zero(self):
        h = _host(total_vcpu=8, used_vcpu=3, total_mem_mb=16384, used_mem_mb=4096)
        assert capacity.host_free(h, 1.0, 1.0) == (5, 12288)
        # Missing used_* fields default to 0.
        bare = {"instance_id": "i", "total_vcpu": 4, "total_mem_mb": 2048}
        assert capacity.host_free(bare, 1.0, 1.0) == (4, 2048)

    def test_host_fits_checks_both_dims(self):
        h = _host(total_vcpu=4, total_mem_mb=4096, used_vcpu=2, used_mem_mb=2048)
        # free = (2, 2048)
        assert capacity.host_fits(h, 2, 2048, 1.0, 1.0) is True
        assert capacity.host_fits(h, 3, 2048, 1.0, 1.0) is False   # vcpu short
        assert capacity.host_fits(h, 2, 2049, 1.0, 1.0) is False   # mem short

    def test_host_fits_enforces_max_vms_when_set(self):
        h = _host(vm_count=3)
        assert capacity.host_fits(h, 1, 1024, 1.0, 1.0, max_vms=0) is True   # 0 = no cap
        assert capacity.host_fits(h, 1, 1024, 1.0, 1.0, max_vms=5) is True
        assert capacity.host_fits(h, 1, 1024, 1.0, 1.0, max_vms=3) is False  # at cap

    def test_rank_hosts_spreads_most_free_first(self):
        a = _host("i-a", used_vcpu=6)   # free 2
        b = _host("i-b", used_vcpu=1)   # free 7  ← most free
        c = _host("i-c", used_vcpu=4)   # free 4
        ranked = capacity.rank_hosts([a, b, c], 1, 1024, cpu_ratio=1.0, mem_ratio=1.0)
        assert [h["instance_id"] for h in ranked] == ["i-b", "i-c", "i-a"]

    def test_rank_hosts_deterministic_instance_id_tiebreak(self):
        a = _host("i-z", used_vcpu=2)
        b = _host("i-a", used_vcpu=2)   # same free → lower id wins
        ranked = capacity.rank_hosts([a, b], 1, 1024, cpu_ratio=1.0, mem_ratio=1.0)
        assert [h["instance_id"] for h in ranked] == ["i-a", "i-z"]

    def test_rank_hosts_excludes_and_filters_unfit(self):
        a = _host("i-a", used_vcpu=0)
        full = _host("i-full", used_vcpu=8)   # free 0 → unfit
        ranked = capacity.rank_hosts([a, full], 1, 1024, cpu_ratio=1.0, mem_ratio=1.0,
                                     exclude_ids={"i-a"})
        assert ranked == []  # a excluded, full doesn't fit
