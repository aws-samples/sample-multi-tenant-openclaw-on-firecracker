# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-4 Phase 0 guardrail: CloudFormation logical-ID stability.

The upcoming stack.py refactor cuts the 1600-line __init__ into ~20 private
_build_* methods. That MUST NOT change any resource's CloudFormation logical ID
— a changed logical ID makes CFN DELETE the old resource and CREATE a new one,
which for a DynamoDB table or the ALB would be a data-loss / downtime event on
the next `cdk deploy`.

This test synthesizes the current stack and compares its {logical_id: type} map
against a checked-in snapshot (tests/fixtures/stack_logical_ids.json). Any
add / remove / retype fails with an explicit diff. Regenerate the fixture ONLY
when a change intentionally alters resources (and call it out in review):

    uv run python tests/fixtures/gen_logical_ids.py
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "stack_logical_ids.json"


@pytest.mark.unit
@pytest.mark.regression
class TestLogicalIdStability:
    @pytest.fixture(scope="class")
    def synthesized(self):
        import sys

        import yaml
        sys.path.insert(0, str(ROOT / "deploy"))
        import aws_cdk as cdk
        from aws_cdk import assertions
        if "stack" in sys.modules:
            del sys.modules["stack"]
        import stack as stack_mod
        # Synthesize against the CANONICAL config (config.yml.example), the same
        # basis the checked-in fixture was generated from. Otherwise a
        # developer's local config.yml (features enabled) yields a different
        # resource set and the comparison is meaningless / machine-dependent.
        stack_mod.CFG = yaml.safe_load(
            (ROOT / "config.yml.example").read_text())
        app = cdk.App()
        s = stack_mod.OpenClawOrchestratorStack(
            app, "OpenClawOrchestrator",
            env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
        tmpl = assertions.Template.from_stack(s).to_json()
        return {lid: r["Type"] for lid, r in tmpl.get("Resources", {}).items()}

    def test_fixture_exists(self):
        assert FIXTURE.is_file(), (
            "tests/fixtures/stack_logical_ids.json missing — regenerate with "
            "tests/fixtures/gen_logical_ids.py")

    def test_logical_ids_unchanged(self, synthesized):
        expected = json.loads(FIXTURE.read_text())
        cur_ids, exp_ids = set(synthesized), set(expected)
        added = sorted(cur_ids - exp_ids)
        removed = sorted(exp_ids - cur_ids)
        retyped = sorted(
            f"{lid}: {expected[lid]} → {synthesized[lid]}"
            for lid in cur_ids & exp_ids if synthesized[lid] != expected[lid])
        msg = []
        if added:
            msg.append(f"ADDED logical IDs (would create new resources): {added}")
        if removed:
            msg.append(f"REMOVED logical IDs (would DELETE resources — data loss "
                       f"risk on deploy): {removed}")
        if retyped:
            msg.append(f"RETYPED (replace resource): {retyped}")
        assert not (added or removed or retyped), (
            "stack synthesis drifted from the logical-ID snapshot. If this is an "
            "intentional resource change, regenerate the fixture "
            "(tests/fixtures/gen_logical_ids.py) and call it out in review; if "
            "this is the T3-4 _build_* refactor, the split changed a construct "
            "path and WILL replace resources — fix the construct id/scope.\n"
            + "\n".join(msg))

    def test_resource_count_matches(self, synthesized):
        expected = json.loads(FIXTURE.read_text())
        assert len(synthesized) == len(expected), (
            f"resource count changed: {len(expected)} → {len(synthesized)}")
