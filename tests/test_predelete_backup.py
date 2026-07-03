# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Go-live B2: snapshot-before-destroy. Deleting a tenant with keep_data=false
does an irreversible rm -rf of its data disk. These tests verify a SYNCHRONOUS
backup runs first and the delete ABORTS if the backup fails (fail-closed),
unless the caller explicitly opts out with ?skip_backup=true.
"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock, patch

from conftest import make_ddb_table
import pytest

# All tests in this module are pure-mock unit tests (no real AWS); mark them
# so `pytest -m unit` includes them (loop 2026-07-02: found 136 tests were
# silently excluded from the unit suite for lack of this marker).
pytestmark = pytest.mark.unit

_mock_ddb = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: MagicMock()
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_predelete", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_predelete"] = api
    spec.loader.exec_module(api)


def _tenant(**over):
    t = {
        "id": "t1",
        "owner_id": "api-key",
        "status": "running",
        "host_id": "i-host",
        "vm_num": 1,
        "vcpu": 2,
        "mem_mb": 4096,
        "host_port": 18789,
        "guest_ip": "172.16.0.2",
    }
    t.update(over)
    return t


class TestPreDeleteBackup:
    def _setup(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _tenant()}
        api.hosts_table = make_ddb_table()
        api.hosts_table.update_item.return_value = {
            "Attributes": {"used_vcpu": 0, "used_mem_mb": 0, "vm_count": 0}
        }

    def test_destroy_backs_up_first_sync(self):
        self._setup()
        invoked = {}

        def _fake_lambda_client(*a, **k):
            m = MagicMock()

            def _invoke(**kw):
                invoked.update(kw)
                return {"StatusCode": 200}

            m.invoke.side_effect = _invoke
            return m

        with (
            patch.object(api, "RBAC_ENABLED", False),
            patch.object(api.boto3, "client", side_effect=_fake_lambda_client),
            patch.object(api, "_ssm_run", lambda *a, **k: None),
            patch.object(api, "_remove_alb_rule", lambda *a, **k: None),
        ):
            r = api.delete_tenant("t1", {"keep_data": "false"}, {"headers": {}})
        # backup was invoked SYNCHRONOUSLY before the destroy proceeded
        assert invoked.get("InvocationType") == "RequestResponse"
        assert r["statusCode"] in (200, 202)

    def test_destroy_aborts_when_backup_fails(self):
        self._setup()

        def _fake_lambda_client(*a, **k):
            m = MagicMock()
            # backup reports a FunctionError → must abort the destroy
            m.invoke.return_value = {"StatusCode": 200, "FunctionError": "Unhandled"}
            return m

        rm_calls = []
        with (
            patch.object(api, "RBAC_ENABLED", False),
            patch.object(api.boto3, "client", side_effect=_fake_lambda_client),
            patch.object(api, "_ssm_run", lambda *a, **k: rm_calls.append(a)),
            patch.object(api, "_remove_alb_rule", lambda *a, **k: None),
        ):
            r = api.delete_tenant("t1", {"keep_data": "false"}, {"headers": {}})
        assert r["statusCode"] == 502, "must fail closed when pre-delete backup fails"
        # crucially, no rm -rf was issued (destroy aborted before VM teardown)
        assert not any("rm -rf" in str(c) for c in rm_calls)

    def test_skip_backup_opt_out_allows_destroy(self):
        self._setup()
        invoked = {}

        def _fake_lambda_client(*a, **k):
            m = MagicMock()
            m.invoke.side_effect = lambda **kw: (
                invoked.update(kw) or {"StatusCode": 200}
            )
            return m

        with (
            patch.object(api, "RBAC_ENABLED", False),
            patch.object(api.boto3, "client", side_effect=_fake_lambda_client),
            patch.object(api, "_ssm_run", lambda *a, **k: None),
            patch.object(api, "_remove_alb_rule", lambda *a, **k: None),
        ):
            r = api.delete_tenant(
                "t1", {"keep_data": "false", "skip_backup": "true"}, {"headers": {}}
            )
        # explicit opt-out → no pre-delete backup invoked, destroy proceeds
        assert invoked == {} or invoked.get("InvocationType") != "RequestResponse"
        assert r["statusCode"] in (200, 202)

    def test_keep_data_does_not_require_backup(self):
        self._setup()
        invoked = {}

        def _fake_lambda_client(*a, **k):
            m = MagicMock()
            m.invoke.side_effect = lambda **kw: (
                invoked.update(kw) or {"StatusCode": 200}
            )
            return m

        with (
            patch.object(api, "RBAC_ENABLED", False),
            patch.object(api.boto3, "client", side_effect=_fake_lambda_client),
            patch.object(api, "_ssm_run", lambda *a, **k: None),
            patch.object(api, "_remove_alb_rule", lambda *a, **k: None),
        ):
            r = api.delete_tenant("t1", {"keep_data": "true"}, {"headers": {}})
        # keep_data=true keeps the disk → no forced pre-delete backup
        assert invoked.get("InvocationType") != "RequestResponse"
        assert r["statusCode"] in (200, 202)
