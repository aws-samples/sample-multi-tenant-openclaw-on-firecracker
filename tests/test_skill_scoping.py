# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for per-tenant / per-group skill distribution (issue #62, v1.4.0).

The semantics being tested:

   effective_skills(tenant) =
     - None (broadcast all)        if no tenant.skills AND no tenant.group
     - tenant.skills               if no group set
     - group.skills                if no tenant.skills set
     - tenant.skills ∪ group.skills if both set
     - tenant.skills (group dropped) if group is set but doesn't exist in DDB
     - None (broadcast)            if union ends up empty (don't lock-out)

Also covers: launch-vm.sh's $SCOPED_SKILLS parsing, the new groups CRUD
endpoints, and the api/handler.py POST /tenants validation of the new
`skills` and `group` fields.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Module loader (api/handler.py with mocked AWS SDK)
# ---------------------------------------------------------------------------

def _load_api_handler():
    """Re-import api/handler.py with fresh mocks for tests that need a clean
    tenants_table / groups_table state."""
    sys.modules.pop("api_handler", None)
    mock_ddb = MagicMock()
    mock_ssm = MagicMock()
    mock_s3 = MagicMock()
    mock_asg = MagicMock()
    mock_elbv2 = MagicMock()
    tenants = make_ddb_table()
    hosts = make_ddb_table()
    groups = make_ddb_table()
    audit = make_ddb_table()

    def _table(name):
        if "tenant" in name.lower():
            return tenants
        if "host" in name.lower():
            return hosts
        if "group" in name.lower():
            return groups
        if "audit" in name.lower():
            return audit
        return make_ddb_table()

    mock_ddb.Table.side_effect = _table

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
    return mod, tenants, hosts, groups


# ---------------------------------------------------------------------------
# 1. Effective-skill resolution semantics — the core 6 scenarios from the issue
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestResolveEffectiveSkills:
    def setup_method(self):
        self.api, self.tenants, self.hosts, self.groups = _load_api_handler()

    def test_empty_tenant_returns_none_for_broadcast(self):
        """No skills, no group → None = broadcast all (legacy v1.3.x behavior)."""
        tenant = {"id": "t1", "name": "t1"}
        assert self.api._resolve_effective_skills(tenant) is None

    def test_single_skill_returns_that_skill(self):
        tenant = {"id": "t1", "skills": ["web-search"]}
        assert self.api._resolve_effective_skills(tenant) == ["web-search"]

    def test_group_only_returns_group_skills(self):
        self.groups.get_item.return_value = {
            "Item": {"name": "team-sre", "skills": ["k8s", "monitoring"]}
        }
        tenant = {"id": "t1", "group": "team-sre"}
        result = self.api._resolve_effective_skills(tenant)
        assert result == ["k8s", "monitoring"]

    def test_tenant_only_returns_tenant_skills(self):
        # Group not set — group lookup not even attempted.
        tenant = {"id": "t1", "skills": ["a", "b"]}
        assert self.api._resolve_effective_skills(tenant) == ["a", "b"]
        self.groups.get_item.assert_not_called()

    def test_both_returns_union_sorted(self):
        self.groups.get_item.return_value = {
            "Item": {"name": "team-sre", "skills": ["k8s", "web-search"]}
        }
        tenant = {"id": "t1", "skills": ["code-review", "web-search"], "group": "team-sre"}
        # web-search appears in both — result is deduplicated
        result = self.api._resolve_effective_skills(tenant)
        assert result == ["code-review", "k8s", "web-search"]

    def test_unknown_group_silently_dropped(self):
        """Group lookup returns no Item → tenant.skills used alone, no exception."""
        self.groups.get_item.return_value = {"Item": None}
        tenant = {"id": "t1", "skills": ["a"], "group": "nonexistent"}
        assert self.api._resolve_effective_skills(tenant) == ["a"]

    def test_empty_union_falls_back_to_broadcast(self):
        """Edge case: explicit empty skills + empty group → don't lock out."""
        self.groups.get_item.return_value = {"Item": {"skills": []}}
        tenant = {"id": "t1", "skills": [], "group": "team-empty"}
        assert self.api._resolve_effective_skills(tenant) is None

    def test_group_lookup_exception_doesnt_raise(self):
        """DDB error during group resolution → fall back to tenant.skills only."""
        self.groups.get_item.side_effect = Exception("simulated DDB outage")
        tenant = {"id": "t1", "skills": ["a"], "group": "team-x"}
        # Should not raise; group simply gets dropped from union.
        assert self.api._resolve_effective_skills(tenant) == ["a"]


# ---------------------------------------------------------------------------
# 2. Groups CRUD endpoints
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGroupsCRUD:
    def setup_method(self):
        self.api, self.tenants, self.hosts, self.groups = _load_api_handler()

    def test_create_group_writes_item_with_201(self):
        # ConditionExpression succeeds → no exception
        self.groups.put_item.return_value = {}
        body = json.dumps({"name": "team-sre", "skills": ["k8s", "monitoring"], "description": "SRE team"})
        resp = self.api.create_group(body)
        assert resp["statusCode"] == 201
        out = json.loads(resp["body"])
        assert out["name"] == "team-sre"
        assert out["skills"] == ["k8s", "monitoring"]
        assert out["description"] == "SRE team"

    def test_create_group_rejects_invalid_name(self):
        resp = self.api.create_group(json.dumps({"name": "Has Spaces"}))
        assert resp["statusCode"] == 400

    def test_create_group_rejects_missing_name(self):
        resp = self.api.create_group(json.dumps({"skills": ["a"]}))
        assert resp["statusCode"] == 400

    def test_create_group_rejects_non_string_skills(self):
        resp = self.api.create_group(json.dumps({"name": "team-x", "skills": [1, 2, 3]}))
        assert resp["statusCode"] == 400

    def test_create_group_409_on_duplicate(self):
        ccf = self.groups.meta.client.exceptions.ConditionalCheckFailedException
        self.groups.put_item.side_effect = ccf("dup")
        resp = self.api.create_group(json.dumps({"name": "team-existing"}))
        assert resp["statusCode"] == 409

    def test_list_groups_returns_items(self):
        self.groups.scan.return_value = {"Items": [
            {"name": "team-sre", "skills": ["a"]},
            {"name": "team-ml", "skills": ["b"]},
        ]}
        resp = self.api.list_groups()
        assert resp["statusCode"] == 200
        assert len(json.loads(resp["body"])["groups"]) == 2

    def test_add_skill_to_group_idempotent(self):
        self.groups.get_item.return_value = {"Item": {"name": "team-x", "skills": ["a", "b"]}}
        # add a skill that's already there → no duplication
        resp = self.api.add_skill_to_group("team-x", json.dumps({"skill": "a"}))
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["skills"] == ["a", "b"]  # unchanged

    def test_add_skill_to_group_appends_new(self):
        self.groups.get_item.return_value = {"Item": {"name": "team-x", "skills": ["a"]}}
        resp = self.api.add_skill_to_group("team-x", json.dumps({"skill": "c"}))
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["skills"] == ["a", "c"]

    def test_add_skill_to_unknown_group_404(self):
        self.groups.get_item.return_value = {"Item": None}
        resp = self.api.add_skill_to_group("nonexistent", json.dumps({"skill": "a"}))
        assert resp["statusCode"] == 404

    def test_remove_skill_from_group(self):
        self.groups.get_item.return_value = {"Item": {"name": "team-x", "skills": ["a", "b", "c"]}}
        resp = self.api.remove_skill_from_group("team-x", "b")
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["skills"] == ["a", "c"]

    def test_remove_nonexistent_skill_is_noop(self):
        self.groups.get_item.return_value = {"Item": {"name": "team-x", "skills": ["a"]}}
        resp = self.api.remove_skill_from_group("team-x", "nonexistent")
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["skills"] == ["a"]


# ---------------------------------------------------------------------------
# 3. POST /tenants validation of new skills + group fields
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCreateTenantSkillsValidation:
    def setup_method(self):
        self.api, self.tenants, self.hosts, self.groups = _load_api_handler()

    def test_skills_must_be_list_of_strings(self):
        resp = self.api.create_tenant(json.dumps({"name": "agent-1", "skills": "not-a-list"}))
        assert resp["statusCode"] == 400
        resp = self.api.create_tenant(json.dumps({"name": "agent-1", "skills": [1, 2]}))
        assert resp["statusCode"] == 400

    def test_group_must_match_dns_label(self):
        resp = self.api.create_tenant(json.dumps({"name": "agent-1", "group": "Has Spaces"}))
        assert resp["statusCode"] == 400

    def test_unknown_group_rejected_at_create(self):
        """We surface unknown groups at create time, not at launch — saves
        operators from typing 'team-srz' and never noticing skills don't flow."""
        self.groups.get_item.return_value = {"Item": None}
        resp = self.api.create_tenant(json.dumps({"name": "agent-1", "group": "nonexistent"}))
        assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# 4. launch-vm.sh argument schema
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLaunchVmShArgs:
    """launch-vm.sh accepts SCOPED_SKILLS as the 7th positional arg."""

    def setup_method(self):
        self.text = (ROOT / "deploy/userdata/launch-vm.sh").read_text()

    def test_seventh_positional_arg_is_scoped_skills(self):
        assert 'SCOPED_SKILLS="${7:-}"' in self.text

    def test_empty_scoped_skills_keeps_broadcast_branch(self):
        """The conditional in launch-vm.sh: empty or '*' → cp -r ${SHARED_SKILLS}/*"""
        assert 'if [ -z "${SCOPED_SKILLS}" ] || [ "${SCOPED_SKILLS}" = "*" ]' in self.text

    def test_comma_split_iterates_skill_list(self):
        """Non-empty SCOPED_SKILLS → IFS=',' read into an array, loop and cp each."""
        assert "IFS=',' read -ra SKILL_LIST" in self.text

    def test_unknown_skill_subdir_is_logged_not_fatal(self):
        """Missing skill subdir should log a 'skipped unknown skill' line, not exit."""
        assert "skipped unknown skill" in self.text


# ---------------------------------------------------------------------------
# 5. _launch_vm passes scoped_skills into launch-vm.sh command
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLaunchVmCommandWiring:
    def setup_method(self):
        self.api, _, _, _ = _load_api_handler()

    def _capture_cmd(self, **kwargs):
        """Call _launch_vm and return the SSM command string that would be sent."""
        sent = {}
        original = self.api._ssm_send

        def capture(instance_id, command, timeout=None):
            sent["cmd"] = command
            return None

        self.api._ssm_send = capture
        try:
            self.api._launch_vm("i-host", "tenant-x", 1, 2, 4096, "172.16.1.2", 18789, **kwargs)
        finally:
            self.api._ssm_send = original
        return sent.get("cmd", "")

    def test_no_skills_passes_quoted_empty_placeholder(self):
        """scoped_skills=None → 7th positional arg is "" (literal quoted empty)."""
        cmd = self._capture_cmd(scoped_skills=None)
        # 7 positional args after launch-vm.sh: tenant_id vm_num vcpu mem_mb tpl restore skills
        # Empty placeholders are quoted "" so positional order stays correct.
        assert "tenant-x 1 2 4096" in cmd
        assert '"" "" ""' in cmd  # tpl, restore, skills all empty

    def test_skill_list_passes_comma_separated(self):
        cmd = self._capture_cmd(scoped_skills=["web-search", "code-review"])
        assert "web-search,code-review" in cmd

    def test_template_and_skills_both_present(self):
        cmd = self._capture_cmd(config_template="claude-sonnet", scoped_skills=["a"])
        assert "claude-sonnet" in cmd
        assert " a " in cmd or cmd.endswith(" a")  # skill arg is " a " or " a &&"
