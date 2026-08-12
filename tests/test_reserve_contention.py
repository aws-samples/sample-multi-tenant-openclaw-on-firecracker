# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A lost reservation race must not be mistaken for an exhausted fleet.

The bug
-------
`_find_host` is deterministic ("least-loaded first" — the issue-#77 spread
fix), so every concurrent create aimed at the SAME host and they all collided
on that host's conditional UpdateItem. `create_tenant` then treated the
collision exactly like "no capacity anywhere": it parked the tenant in
`pending` and called `_scale_out()`. Two users creating a tenant in the same
instant could therefore provision a bare-metal host (minutes of lead time,
roughly $4k/month) purely because their writes raced.

The fix has two halves and both are tested here:
  * placement picks randomly among the top-K least-loaded fitting hosts, so
    concurrent creates rarely aim at the same host at all;
  * a refused reservation retries on the next-best candidate, and scale-out
    happens only when every candidate refuses.

Pinned placements (clone_from / preferred_host_id) must NOT retry elsewhere —
that would silently violate the caller's explicit intent — so that is asserted
too.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.unit


def _load_api():
    mock_ddb = MagicMock()
    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", return_value=MagicMock()):
        mock_ddb.Table.side_effect = lambda name: MagicMock()
        spec = importlib.util.spec_from_file_location(
            "api_reserve", str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["api_reserve"] = mod
        spec.loader.exec_module(mod)
        return mod


api = _load_api()


def _host(iid, free_vcpu=8, free_mem=32768):
    """A host row with enough headroom to fit the default tenant shape."""
    return {
        "instance_id": iid,
        "status": "active",
        "total_vcpu": free_vcpu, "used_vcpu": 0,
        "total_mem_mb": free_mem, "used_mem_mb": 0,
        "vm_count": 0, "next_vm_num": 1,
        "private_ip": "10.0.0.9", "az": "ap-northeast-1a",
    }


class TestPickHostsForCreate:
    """Placement must stay inside the least-loaded band but stop herding."""

    def _hosts(self, n):
        # Descending free vCPU so rank order is i-0, i-1, ... i-(n-1).
        return [_host(f"i-{k}", free_vcpu=64 - k) for k in range(n)]

    def test_returns_every_fitting_host(self):
        api.hosts_table = MagicMock()
        api.hosts_table.scan.return_value = {"Items": self._hosts(5)}
        picked = api._pick_hosts_for_create(1, 1024)
        assert {h["instance_id"] for h in picked} == {f"i-{k}" for k in range(5)}

    def test_single_fitting_host_is_unchanged(self):
        api.hosts_table = MagicMock()
        api.hosts_table.scan.return_value = {"Items": self._hosts(1)}
        assert [h["instance_id"] for h in api._pick_hosts_for_create(1, 1024)] \
            == ["i-0"]

    def test_first_choice_varies_across_calls(self):
        """The anti-herding property: not always the same head.

        Uses a fixed seed so the test is deterministic while still exercising
        the shuffle — a regression to `ranked[0]` makes the set collapse to one.
        """
        import random
        api.hosts_table = MagicMock()
        api.hosts_table.scan.return_value = {"Items": self._hosts(8)}
        random.seed(1234)
        firsts = {api._pick_hosts_for_create(1, 1024)[0]["instance_id"]
                  for _ in range(40)}
        assert len(firsts) > 1, (
            "every create picked the same host — concurrent creates will all "
            f"collide on it (got {firsts})")

    def test_shuffle_is_confined_to_top_k(self):
        """Hosts outside the top-K band must never jump to the front."""
        import random
        api.hosts_table = MagicMock()
        api.hosts_table.scan.return_value = {"Items": self._hosts(20)}
        top_k = api.HOST_PICK_TOP_K
        random.seed(99)
        for _ in range(30):
            head = api._pick_hosts_for_create(1, 1024)[0]["instance_id"]
            rank = int(head.split("-")[1])
            assert rank < top_k, (
                f"picked rank {rank}, outside the top-{top_k} least-loaded band")

    def test_no_fitting_host_returns_empty(self):
        api.hosts_table = MagicMock()
        api.hosts_table.scan.return_value = {"Items": []}
        assert api._pick_hosts_for_create(1, 1024) == []


class TestContentionDoesNotScaleOut:
    """The expensive half: a refused reservation must retry, not scale out."""

    def setup_method(self):
        api.hosts_table = MagicMock()
        api.tenants_table = MagicMock()
        api.tenants_table.get_item.return_value = {}
        api.tenants_table.scan.return_value = {"Items": []}
        api.hosts_table.scan.return_value = {
            "Items": [_host(f"i-{k}", free_vcpu=64 - k) for k in range(4)]}

    def _create(self):
        return api.create_tenant({"name": "acme", "vcpu": 1, "mem_mb": 1024})

    def test_retries_next_candidate_on_contention(self):
        """First host refuses, second accepts → no scale-out, tenant runs."""
        calls = []

        def reserve(host, vcpu, mem):
            calls.append(host["instance_id"])
            return None if len(calls) == 1 else 7   # lose once, then win

        with patch.object(api, "_reserve_host_slot", side_effect=reserve), \
             patch.object(api, "_launch_vm"), \
             patch.object(api, "_publish_event"), \
             patch.object(api, "_scale_out") as scale, \
             patch.object(api, "_audit_write"):
            resp = self._create()

        assert len(calls) == 2, f"expected a retry on another host, got {calls}"
        assert calls[0] != calls[1], "retried on the SAME host"
        scale.assert_not_called()
        assert resp["statusCode"] == 201
        import json
        assert json.loads(resp["body"])["status"] != "pending"

    def test_scales_out_only_when_all_candidates_refuse(self):
        with patch.object(api, "_reserve_host_slot", return_value=None), \
             patch.object(api, "_launch_vm"), \
             patch.object(api, "_publish_event"), \
             patch.object(api, "_scale_out") as scale, \
             patch.object(api, "_audit_write"):
            resp = self._create()

        scale.assert_called_once()
        import json
        assert json.loads(resp["body"])["status"] == "pending"

    def test_attempts_are_bounded(self):
        """Don't hammer DynamoDB — the retry budget is finite."""
        calls = []

        def reserve(host, vcpu, mem):
            calls.append(host["instance_id"])
            return None

        with patch.object(api, "_reserve_host_slot", side_effect=reserve), \
             patch.object(api, "_launch_vm"), \
             patch.object(api, "_publish_event"), \
             patch.object(api, "_scale_out"), \
             patch.object(api, "_audit_write"):
            self._create()

        assert len(calls) <= api.HOST_RESERVE_ATTEMPTS, (
            f"made {len(calls)} reservation attempts, budget is "
            f"{api.HOST_RESERVE_ATTEMPTS}")

    def test_pinned_host_does_not_retry_elsewhere(self):
        """preferred_host_id means THAT host — never silently place elsewhere."""
        api.hosts_table.get_item.return_value = {"Item": _host("i-pinned")}
        calls = []

        def reserve(host, vcpu, mem):
            calls.append(host["instance_id"])
            return None

        with patch.object(api, "_reserve_host_slot", side_effect=reserve), \
             patch.object(api, "_launch_vm"), \
             patch.object(api, "_publish_event"), \
             patch.object(api, "_scale_out"), \
             patch.object(api, "_audit_write"):
            api.create_tenant({"name": "acme", "vcpu": 1, "mem_mb": 1024,
                               "preferred_host_id": "i-pinned"})

        assert calls == ["i-pinned"], (
            f"pinned create must attempt only the pinned host, got {calls}")
