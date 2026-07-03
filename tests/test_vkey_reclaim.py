# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Go-live C: delete-time LiteLLM vkey reclaim. A per-tenant vkey must be revoked
(POST /key/delete) when the tenant is deleted, else it lingers in LiteLLM after
the tenant is gone — a credential + budget leak that accumulates over churn.
Revoke is best-effort (delete proceeds either way) and never logs the key value.
"""

import importlib.util
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
        "api_handler_vkey", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_vkey"] = api
    spec.loader.exec_module(api)


class TestRevokeHelper:
    def test_revoke_no_vkey_is_false(self):
        assert api._revoke_tenant_vkey("") is False
        assert api._revoke_tenant_vkey(None) is False

    def test_revoke_no_master_key_is_false(self):
        with patch.object(api, "_get_litellm_master_key", return_value=""):
            assert api._revoke_tenant_vkey("sk-abc") is False

    def test_revoke_calls_key_delete(self):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return _Resp()

        # NOTE (loop 2026-07-02): the base URL is resolved via the
        # _get_litellm_base_url() helper (env/SSM + cache), NOT a module-level
        # LITELLM_BASE_URL attribute — patching the latter raised AttributeError
        # ("module does not have attribute LITELLM_BASE_URL"). This test had been
        # silently broken and excluded from `pytest -m unit` for lack of the
        # unit marker; adding the marker surfaced it. Patch the actual helper.
        with (
            patch.object(api, "_get_litellm_master_key", return_value="sk-master"),
            patch.object(
                api, "_get_litellm_base_url", return_value="http://litellm:4000"
            ),
            patch.object(api.urllib.request, "urlopen", _fake_urlopen),
        ):
            ok = api._revoke_tenant_vkey("sk-tenant-1")
        assert ok is True
        assert captured["url"].endswith("/key/delete")
        assert b"sk-tenant-1" in captured["data"]


class TestDeleteReclaimsVkey:
    def _setup(self, vkey="sk-tenant-1"):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {
            "Item": {
                "id": "t1",
                "owner_id": "api-key",
                "status": "running",
                "host_id": "i-host",
                "vm_num": 1,
                "vcpu": 2,
                "mem_mb": 4096,
                "host_port": 18789,
                "guest_ip": "172.16.0.2",
                "litellm_vkey": vkey,
            }
        }
        api.hosts_table = make_ddb_table()
        api.hosts_table.update_item.return_value = {
            "Attributes": {"used_vcpu": 0, "used_mem_mb": 0, "vm_count": 0}
        }

    def test_delete_revokes_vkey_and_clears_field(self):
        self._setup()
        revoked = {}
        update_exprs = []

        def _fake_revoke(vk):
            revoked["vk"] = vk
            return True

        def _capture_update(**kw):
            update_exprs.append(kw.get("UpdateExpression", ""))
            return {"Attributes": {"vm_count": 0}}

        api.tenants_table.update_item.side_effect = _capture_update
        with (
            patch.object(api, "RBAC_ENABLED", False),
            patch.object(api, "_revoke_tenant_vkey", side_effect=_fake_revoke),
            patch.object(api, "_ssm_run", lambda *a, **k: None),
            patch.object(api, "_remove_alb_rule", lambda *a, **k: None),
        ):
            # keep_data=true so we skip the pre-delete backup branch and focus on vkey
            r = api.delete_tenant("t1", {"keep_data": "true"}, {"headers": {}})
        assert r["statusCode"] in (200, 202)
        assert revoked.get("vk") == "sk-tenant-1", "delete must revoke the tenant vkey"
        # the soft-delete update also REMOVEs litellm_vkey from the record
        assert any("REMOVE litellm_vkey" in e for e in update_exprs)

    def test_delete_proceeds_even_if_revoke_fails(self):
        self._setup()
        with (
            patch.object(api, "RBAC_ENABLED", False),
            patch.object(api, "_revoke_tenant_vkey", return_value=False),
            patch.object(api, "_ssm_run", lambda *a, **k: None),
            patch.object(api, "_remove_alb_rule", lambda *a, **k: None),
        ):
            r = api.delete_tenant("t1", {"keep_data": "true"}, {"headers": {}})
        # revoke failure is non-fatal — delete still succeeds
        assert r["statusCode"] in (200, 202)
