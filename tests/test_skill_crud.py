# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Console skills CRUD (issue #63, v1.4.1).

Covers PUT/GET/DELETE /skills/{name} in deploy/lambda/api/handler.py.
The list endpoint (GET /skills) still lives in skills/handler.py and
is exercised separately.
"""

import datetime
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Loader — fresh handler with mocked S3
# ---------------------------------------------------------------------------

def _load_api_handler(s3_mock=None):
    """Re-import api/handler.py with a controllable S3 client."""
    sys.modules.pop("api_handler", None)
    mock_ddb = MagicMock()
    mock_ssm = MagicMock()
    mock_s3 = s3_mock or MagicMock()
    # Wire up the NoSuchKey exception class boto3 normally provides via
    # a service model — our handler catches this explicitly.

    class _NoSuchKey(Exception):
        pass

    mock_s3.exceptions = MagicMock()
    mock_s3.exceptions.NoSuchKey = _NoSuchKey
    mock_asg = MagicMock()
    mock_elbv2 = MagicMock()
    mock_ddb.Table.side_effect = lambda name: make_ddb_table()

    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client") as mc:
        mc.side_effect = lambda svc, **kw: {
            "ssm": mock_ssm, "s3": mock_s3, "autoscaling": mock_asg,
            "elbv2": mock_elbv2,
        }.get(svc, MagicMock())
        spec = importlib.util.spec_from_file_location(
            "api_handler", str(ROOT / "deploy/lambda/api/handler.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["api_handler"] = mod
        spec.loader.exec_module(mod)
    return mod, mock_s3


# ---------------------------------------------------------------------------
# read_skill — GET /skills/{name}
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReadSkill:
    def test_returns_content_for_existing_skill(self):
        s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b"# My Skill\n\nDescription"
        s3.get_object.return_value = {
            "Body": body,
            "ContentLength": 24,
            "LastModified": datetime.datetime(2026, 5, 28, tzinfo=datetime.timezone.utc),
        }
        api, _ = _load_api_handler(s3_mock=s3)
        resp = api.read_skill("my-skill")
        assert resp["statusCode"] == 200
        out = json.loads(resp["body"])
        assert out["name"] == "my-skill"
        assert out["content"] == "# My Skill\n\nDescription"
        assert out["last_modified"].startswith("2026-05-28")

    def test_returns_404_for_missing_skill(self):
        s3 = MagicMock()
        api, _ = _load_api_handler(s3_mock=s3)
        # Raise the *same* exception class the handler will catch
        s3.get_object.side_effect = s3.exceptions.NoSuchKey()
        resp = api.read_skill("ghost")
        assert resp["statusCode"] == 404

    def test_rejects_invalid_name(self):
        api, _ = _load_api_handler()
        resp = api.read_skill("Has Spaces")
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# update_skill — PUT /skills/{name}
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUpdateSkill:
    def test_creates_new_skill_returns_201(self):
        s3 = MagicMock()
        # head_object raises → skill is new
        s3.head_object.side_effect = Exception("not found")
        api, _ = _load_api_handler(s3_mock=s3)
        body = json.dumps({"content": "# New Skill\n\nBody"})
        resp = api.update_skill("new-skill", body)
        assert resp["statusCode"] == 201
        out = json.loads(resp["body"])
        assert out["created"] is True
        s3.put_object.assert_called_once()
        kwargs = s3.put_object.call_args.kwargs
        assert kwargs["Key"] == "skills/new-skill/SKILL.md"
        assert kwargs["ContentType"].startswith("text/markdown")

    def test_replaces_existing_skill_returns_200(self):
        s3 = MagicMock()
        s3.head_object.return_value = {}  # exists
        api, _ = _load_api_handler(s3_mock=s3)
        body = json.dumps({"content": "# Updated\n\nNew body"})
        resp = api.update_skill("existing", body)
        assert resp["statusCode"] == 200
        out = json.loads(resp["body"])
        assert out["created"] is False

    def test_rejects_content_without_h1(self):
        api, _ = _load_api_handler()
        body = json.dumps({"content": "Just plain text\nNo heading"})
        resp = api.update_skill("foo", body)
        assert resp["statusCode"] == 400
        assert "# Title" in json.loads(resp["body"])["error"]

    def test_rejects_empty_content(self):
        api, _ = _load_api_handler()
        resp = api.update_skill("foo", json.dumps({"content": ""}))
        assert resp["statusCode"] == 400

    def test_rejects_oversized_content(self):
        api, _ = _load_api_handler()
        # _SKILL_MAX_BYTES = 256 KiB; 300 KiB definitely over
        big = "# Title\n\n" + ("x" * (300 * 1024))
        resp = api.update_skill("foo", json.dumps({"content": big}))
        assert resp["statusCode"] == 400
        assert "exceeds" in json.loads(resp["body"])["error"].lower()

    def test_rejects_invalid_name(self):
        api, _ = _load_api_handler()
        resp = api.update_skill("Has Spaces", json.dumps({"content": "# T"}))
        assert resp["statusCode"] == 400

    def test_rejects_invalid_json_body(self):
        api, _ = _load_api_handler()
        resp = api.update_skill("foo", "not-json")
        assert resp["statusCode"] == 400

    def test_accepts_h1_after_blank_lines(self):
        s3 = MagicMock()
        s3.head_object.side_effect = Exception("new")
        api, _ = _load_api_handler(s3_mock=s3)
        body = json.dumps({"content": "\n\n# Title\n\nBody"})
        resp = api.update_skill("foo", body)
        assert resp["statusCode"] == 201


# ---------------------------------------------------------------------------
# delete_skill — DELETE /skills/{name}
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeleteSkill:
    def test_deletes_all_objects_under_prefix(self):
        s3 = MagicMock()
        # Paginator → one page with 3 objects (SKILL.md + 2 images)
        page = {"Contents": [
            {"Key": "skills/myskill/SKILL.md"},
            {"Key": "skills/myskill/diagram.png"},
            {"Key": "skills/myskill/notes.md"},
        ]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        s3.get_paginator.return_value = paginator
        api, _ = _load_api_handler(s3_mock=s3)
        resp = api.delete_skill("myskill")
        assert resp["statusCode"] == 200
        out = json.loads(resp["body"])
        assert out["deleted"] == 3
        s3.delete_objects.assert_called_once()
        # Confirm all 3 keys were targeted
        sent = s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert {o["Key"] for o in sent} == {
            "skills/myskill/SKILL.md",
            "skills/myskill/diagram.png",
            "skills/myskill/notes.md",
        }

    def test_returns_404_when_prefix_empty(self):
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": []}]
        s3.get_paginator.return_value = paginator
        api, _ = _load_api_handler(s3_mock=s3)
        resp = api.delete_skill("ghost")
        assert resp["statusCode"] == 404
        s3.delete_objects.assert_not_called()

    def test_rejects_invalid_name(self):
        api, _ = _load_api_handler()
        resp = api.delete_skill("Has Spaces")
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# RBAC wiring — viewer can read skills, but not write
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSkillCRUDRouting:
    """Confirm the dispatcher gate so viewer can't accidentally PUT/DELETE."""

    def test_get_skill_open_to_viewer(self):
        api, _ = _load_api_handler()
        # _VIEWER_OK contains GET /skills/{name}
        assert ("GET", "/skills/{name}") in api._VIEWER_OK

    def test_put_and_delete_not_in_viewer_ok(self):
        api, _ = _load_api_handler()
        assert ("PUT", "/skills/{name}") not in api._VIEWER_OK
        assert ("DELETE", "/skills/{name}") not in api._VIEWER_OK
