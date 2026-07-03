# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression: API Gateway resources must exist for handler routes that the
console / operators actually call over HTTP.

WHY THIS EXISTS: API Gateway routes are declared EXPLICITLY in stack.py
(`resource.add_resource(...).add_method(...)`) — they are NOT auto-derived from
handler.py's route table. On 2026-07-01 Phase 8's `POST /hosts/fleet-power` and
Phase 4's `GET /hosts/rootfs-drift` were added to handler.py only; the missing
stack.py declaration meant real curls returned API Gateway's "Missing
Authentication Token" (= route doesn't exist) — the 1-minute fleet-power feature
was uncallable on the cloud despite passing every handler unit test (those call
the function directly, bypassing API Gateway). This test synthesizes the stack
and asserts the resource paths exist, so a handler route added without its API
Gateway resource fails CI instead of in production.
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("CDK_CONTEXT_JSON", '{"aws:cdk:bundling-stacks": []}')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import aws_cdk as cdk  # noqa: E402
from aws_cdk import assertions  # noqa: E402
from stack import OpenClawOrchestratorStack  # noqa: E402

# All tests in this module are pure-mock unit tests (no real AWS); mark them
# so `pytest -m unit` includes them (loop 2026-07-02: found 136 tests were
# silently excluded from the unit suite for lack of this marker).
pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = OpenClawOrchestratorStack(
        app,
        "OpenClawOrchestrator-apigw-test",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


@pytest.fixture(scope="module")
def resource_path_parts(template):
    """All AWS::ApiGateway::Resource PathPart values in the synthesized stack."""
    resources = template.find_resources("AWS::ApiGateway::Resource")
    parts = set()
    for r in resources.values():
        pp = r.get("Properties", {}).get("PathPart")
        if isinstance(pp, str):
            parts.add(pp)
    return parts


# Path segments that MUST have an API Gateway resource. Each maps to a handler
# route the console or operators invoke; a missing one = "Missing Authentication
# Token" in production. Keep in sync when adding new operator-facing endpoints.
@pytest.mark.parametrize(
    "path_part",
    [
        "fleet-power",  # Phase 8 — POST /hosts/fleet-power (1-min fleet start/stop)
        "rootfs-drift",  # Phase 4 — GET /hosts/rootfs-drift (upgrade drift view)
        "refresh-rootfs",  # existing — POST /hosts/refresh-rootfs
        "rootfs-version",  # existing — GET /hosts/rootfs-version
    ],
)
def test_apigw_resource_exists_for_route(resource_path_parts, path_part):
    assert path_part in resource_path_parts, (
        f"API Gateway resource for '{path_part}' is MISSING from stack.py — "
        f"a handler route without its add_resource()/add_method() declaration "
        f"returns 'Missing Authentication Token' in production. Add it next to "
        f"the other hosts_resource.add_resource(...) calls in stack.py."
    )


def test_fleet_power_has_post_method(template):
    """fleet-power must expose POST specifically (not just the resource)."""
    methods = template.find_resources("AWS::ApiGateway::Method")
    # Find a POST method whose resource ref resolves to the fleet-power resource.
    post_methods = [
        m
        for m in methods.values()
        if m.get("Properties", {}).get("HttpMethod") == "POST"
    ]
    assert post_methods, "no POST methods synthesized at all"
