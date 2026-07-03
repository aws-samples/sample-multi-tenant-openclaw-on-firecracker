# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for issue #93 — control-plane API standardization.

Covers the additive, backward-compatible fields and idempotency added to
create_tenant, validated against the api-design-review checklist:

  - security nested Map (F4 naming, invariant enforcement)
  - image_id field (default + validation)
  - client_token → deterministic id (C1 idempotency)
  - conditional put → 409 on same-id replay (C2/J2/C4)
  - structured error code (E1)
  - backward compatibility: omitting every #93 field == pre-#93 behavior (A1/A2)

Same import seam as test_config_template_injection.py / test_api.py.
"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
_mock_s3 = MagicMock()
_mock_asg = MagicMock()
_mock_elbv2 = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm,
        "s3": _mock_s3,
        "autoscaling": _mock_asg,
        "elbv2": _mock_elbv2,
    }.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_i93", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_i93"] = api
    spec.loader.exec_module(api)


class TestSecurityFieldValidation:
    """_validate_security — F4 naming + the encrypted/key invariant."""

    @pytest.mark.unit
    def test_absent_is_empty_and_ok(self):
        # A2: missing security == pre-#93 world (no encryption config), not an error.
        clean, err = api._validate_security(None)
        assert err is None and clean == {}

    @pytest.mark.unit
    def test_minimal_encrypted_platform_managed(self):
        clean, err = api._validate_security({"storage_encrypted": True})
        assert err is None
        assert clean["storage_encrypted"] is True
        assert clean["encryption_type"] == "platform_managed"

    @pytest.mark.unit
    def test_valid_full_config(self):
        clean, err = api._validate_security(
            {
                "storage_encrypted": True,
                "encryption_type": "tenant_cmk",
                "kms_key_arn": "arn:aws:kms:ap-southeast-1:123456789012:key/abc",
                "cert_arn": "arn:aws:acm:ap-southeast-1:123456789012:certificate/xyz",
                "secret_ref": "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:foo-AbCdEf",
            }
        )
        assert err is None
        assert clean["encryption_type"] == "tenant_cmk"
        assert clean["kms_key_arn"].startswith("arn:aws:kms:")

    @pytest.mark.unit
    def test_bare_kms_id_rejected(self):
        # Must be a full ARN, not a bare id/alias (cross-account resolves wrong key).
        _, err = api._validate_security(
            {"storage_encrypted": True, "kms_key_arn": "alias/my-key"}
        )
        assert err and "ARN" in err

    @pytest.mark.unit
    def test_invariant_key_without_encryption_rejected(self):
        _, err = api._validate_security(
            {
                "storage_encrypted": False,
                "kms_key_arn": "arn:aws:kms:ap-southeast-1:123456789012:key/abc",
            }
        )
        assert err and "storage_encrypted is false" in err

    @pytest.mark.unit
    def test_invariant_cmk_requires_key(self):
        _, err = api._validate_security(
            {"storage_encrypted": True, "encryption_type": "tenant_cmk"}
        )
        assert err and "requires kms_key_arn" in err

    @pytest.mark.unit
    def test_bad_encryption_type_rejected(self):
        _, err = api._validate_security(
            {"storage_encrypted": True, "encryption_type": "magic"}
        )
        assert err and "encryption_type" in err

    @pytest.mark.unit
    def test_non_dict_rejected(self):
        _, err = api._validate_security("nope")
        assert err


class TestGenIdIdempotency:
    """_gen_id — C1: client_token makes id deterministic; absence stays random."""

    @pytest.mark.unit
    def test_same_token_same_id(self):
        a = api._gen_id("acme", "tok-123", "owner-1")
        b = api._gen_id("acme", "tok-123", "owner-1")
        assert a == b, "same client_token must yield the same id (idempotent)"

    @pytest.mark.unit
    def test_different_token_different_id(self):
        a = api._gen_id("acme", "tok-1", "owner-1")
        b = api._gen_id("acme", "tok-2", "owner-1")
        assert a != b

    @pytest.mark.unit
    def test_no_token_is_random(self):
        # Legacy behavior preserved: no token → time-seeded, fresh each call.
        a = api._gen_id("acme")
        b = api._gen_id("acme")
        assert a != b

    @pytest.mark.unit
    def test_token_id_is_name_independent(self):
        # #95 ADV-C-002: the WHOLE id on the token path must NOT depend on name,
        # else the same token + different name → different primary key → the
        # conditional put can't catch the double-open. This is the core fix.
        a = api._gen_id("acme", "same-token", "owner-1")
        b = api._gen_id("totally-different-name", "same-token", "owner-1")
        assert a == b, "same (owner, token) must collide on id regardless of name"

    @pytest.mark.unit
    def test_token_id_is_owner_scoped(self):
        # #95 ADV-C-011: fold owner into the seed so one owner's token can't
        # collide with — or probe the existence of — another owner's tenant.
        a = api._gen_id("acme", "shared-token", "owner-1")
        b = api._gen_id("acme", "shared-token", "owner-2")
        assert a != b, "same token across owners must NOT collide (no 409 oracle)"

    @pytest.mark.unit
    def test_id_shape(self):
        # No-token path keeps the legacy name-xxxx shape (4-char hash).
        legacy = api._gen_id("acme")
        assert legacy.startswith("acme-")
        assert len(legacy.split("-")[-1]) == 4
        # Token path is an opaque, name-independent key: t-<16 hex>.
        tok = api._gen_id("acme", "tok", "owner-1")
        assert tok.startswith("t-")
        assert len(tok) == 18  # "t-" + 16 hex chars (64-bit)


class TestCreateTenantValidation:
    """create_tenant edge validation for the new fields, with structured code."""

    @pytest.mark.unit
    def test_bad_image_id_rejected_with_code(self):
        resp = api.create_tenant(json.dumps({"name": "t1", "image_id": "bad slug!"}))
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    def test_non_numeric_vcpu_rejected_not_500(self):
        # #95 对抗测试 ADV-J-003:vcpu="lots" 修前抛 ValueError→500 泄内部报错。
        # 修后应 400 code=VALIDATION,不是 500。
        resp = api.create_tenant(json.dumps({"name": "t1", "vcpu": "lots"}))
        assert resp["statusCode"] == 400, f"应 400 非 500,得 {resp['statusCode']}"
        assert json.loads(resp["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "field,val", [("vcpu", -4), ("mem_mb", -1024), ("vcpu", 0), ("data_disk_mb", 0)]
    )
    def test_non_positive_capacity_rejected(self, field, val):
        # #95 对抗测试 ADV-J-004:负数/0 容量修前被接受(202),会击穿配额账本。
        # 修后应 400 拒绝。
        resp = api.create_tenant(json.dumps({"name": "t1", field: val}))
        assert resp["statusCode"] == 400, (
            f"{field}={val} 应被拒,得 {resp['statusCode']}"
        )
        assert json.loads(resp["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    def test_bad_security_rejected_with_code(self):
        resp = api.create_tenant(
            json.dumps(
                {
                    "name": "t1",
                    "security": {"storage_encrypted": True, "kms_key_arn": "alias/x"},
                }
            )
        )
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    def test_bad_client_token_rejected(self):
        resp = api.create_tenant(
            json.dumps({"name": "t1", "client_token": "x"})  # too short (<4)
        )
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "token",
        [
            "tok\n\ten",  # ADV-C-006: embedded control chars (log poisoning / SSM injection)
            "tok\x00en",  # ADV-C-006: NUL byte
            "令牌🔑令牌🔑",  # ADV-C-004: non-ASCII in a deterministic seed
            "a" * 129,  # ADV-C-005: over the 128 ceiling
            "has space",  # space is not a valid idempotency-key char
        ],
    )
    def test_malformed_client_token_rejected(self, token):
        resp = api.create_tenant(json.dumps({"name": "t1", "client_token": token}))
        assert resp["statusCode"] == 400, f"token={token!r} should be rejected"
        assert json.loads(resp["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["true", "false", "1", 1, 0, "yes"])
    def test_chat_endpoint_enabled_must_be_bool(self, bad):
        # #95 ADV-C-017: bool("false") is True, so a string used to silently OPEN
        # the deviceAuth-bypassing chat endpoint. Only a real JSON boolean is ok.
        resp = api.create_tenant(
            json.dumps({"name": "t1", "chat_endpoint_enabled": bad})
        )
        assert resp["statusCode"] == 400, f"chat_endpoint_enabled={bad!r} should 400"
        assert json.loads(resp["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    def test_chat_endpoint_enabled_real_bool_ok(self):
        # A real boolean is accepted (not rejected by the new type guard).
        api.tenants_table = make_ddb_table()
        resp = api.create_tenant(
            json.dumps({"name": "t1", "chat_endpoint_enabled": True})
        )
        assert resp["statusCode"] != 400

    @pytest.mark.unit
    def test_backward_compatible_no_new_fields(self):
        # A1/A2: a pre-#93 body (none of image_id/security/client_token) must not
        # be rejected by the new validation.
        api.tenants_table = make_ddb_table()
        resp = api.create_tenant(json.dumps({"name": "legacy"}))
        assert resp["statusCode"] != 400


class TestConflictOn409:
    """C2/C4: a same-id put that fails the conditional check → 409 CONFLICT."""

    @pytest.mark.unit
    def test_duplicate_put_returns_409(self):
        t = make_ddb_table()
        # Simulate "id already exists": put_item raises the conditional-check error.
        t.put_item.side_effect = (
            t.meta.client.exceptions.ConditionalCheckFailedException()
        )
        api.tenants_table = t
        # No host capacity → pending branch → hits the conditional put first.
        with (
            patch.object(api, "_find_host", return_value=None),
            patch.object(api, "_scale_out", return_value=None),
        ):
            resp = api.create_tenant(
                json.dumps({"name": "dup", "client_token": "same-token"})
            )
        assert resp["statusCode"] == 409
        assert json.loads(resp["body"]).get("code") == "CONFLICT"


class TestPaginationValidation:
    """#95 D-series — malformed ?limit / ?next_token must 400, not silently degrade.

    D5/E1/E2: a bad client input fails loud with a structured code instead of
    being coerced into a valid default (which hides the client bug and, for a
    tampered cursor, traps a paging client on page 1 forever)."""

    @pytest.mark.unit
    def test_parse_limit_absent_is_default(self):
        # Backward compatible: no ?limit → the default page size, no error.
        val, err = api._parse_limit({})
        assert err is None and val == api._USER_PAGE_DEFAULT
        val, err = api._parse_limit(None)
        assert err is None and val == api._USER_PAGE_DEFAULT

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["-1", "0", "abc", "1.5", "-999", " ", "lots"])
    def test_parse_limit_malformed_rejected(self, bad):
        # ADV-D-01/03/04/05: negative, zero, non-integer, fractional → 400.
        val, err = api._parse_limit({"limit": bad})
        assert val is None, f"limit={bad!r} must not yield a usable value"
        assert err is not None and err["statusCode"] == 400
        assert json.loads(err["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    def test_parse_limit_over_ceiling_is_clamped(self):
        # ADV-D-02: a positive over-ceiling limit is VALID, just capped (not 400).
        val, err = api._parse_limit({"limit": "999999999"})
        assert err is None and val == api._USER_PAGE_MAX

    @pytest.mark.unit
    def test_parse_limit_valid_passthrough(self):
        val, err = api._parse_limit({"limit": "25"})
        assert err is None and val == 25

    @pytest.mark.unit
    def test_parse_next_token_absent_is_first_page(self):
        key, err = api._parse_next_token(None)
        assert err is None and key is None
        key, err = api._parse_next_token("")
        assert err is None and key is None

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "bad",
        [
            "eyJpZCI6ICJmb28tYWJjZCJ9XXXX",  # ADV-D-06: tampered base64 tail
            "!!!not-base64!!!",  # ADV-D-07: not base64 at all
            "garbage==",  # ADV-D-11: garbage cursor
        ],
    )
    def test_parse_next_token_garbage_rejected(self, bad):
        key, err = api._parse_next_token(bad)
        assert key is None, f"token={bad!r} must not decode to a usable key"
        assert err is not None and err["statusCode"] == 400
        assert json.loads(err["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    def test_parse_next_token_valid_json_wrong_shape_rejected(self):
        # ADV-D-10: base64 of a JSON array (not an object) → reject, not reset.
        import base64 as _b64

        tok = _b64.urlsafe_b64encode(b"[1,2,3]").decode()
        key, err = api._parse_next_token(tok)
        assert key is None
        assert err is not None and err["statusCode"] == 400

    @pytest.mark.unit
    def test_parse_next_token_wellformed_cursor_decodes(self):
        # A cursor we ourselves emitted (base64 of an object) decodes cleanly.
        # ADV-D-08/09: a structurally valid but foreign cursor still decodes here;
        # cross-owner access is denied downstream by the owner_id filter, not here.
        tok = api._encode_next_token({"id": "some-tenant-abcd"})
        key, err = api._parse_next_token(tok)
        assert err is None
        assert key == {"id": "some-tenant-abcd"}

    @pytest.mark.unit
    def test_list_tenants_rejects_bad_limit(self):
        # Integration: the 400 surfaces through the real list_tenants entrypoint.
        api.tenants_table = make_ddb_table()
        resp = api.list_tenants(query_params={"limit": "-1"})
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"]).get("code") == "VALIDATION"

    @pytest.mark.unit
    def test_list_tenants_rejects_bad_next_token(self):
        api.tenants_table = make_ddb_table()
        resp = api.list_tenants(query_params={"next_token": "!!!garbage!!!"})
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"]).get("code") == "VALIDATION"
