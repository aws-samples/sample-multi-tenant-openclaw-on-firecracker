# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-4 Phase 0 guardrail: api handler routing/facade contract.

The upcoming refactor splits the 2800-line api handler into a thin facade +
domain modules (domains/tenants.py, hosts.py, ...). Through all of that, the
Lambda's OBSERVABLE contract must not change:

  * the exact set of (METHOD, resource) routes the facade dispatches;
  * every route resolving to a real, callable handler;
  * lambda_handler still routing an event to the right handler, 404-ing unknown
    paths, running RBAC AFTER routing, and auditing mutations.

This test pins that contract against the CURRENT monolith, so any domain move
that drops a route, renames a handler the routes dict references, or breaks the
late-binding wiring (a domain calling back into a facade symbol) fails loudly —
BEFORE it reaches a deploy. Internals (which module a function lives in) are
deliberately NOT asserted; only the contract is.
"""

import importlib.util
import sys
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────
# Load the handler with mocked AWS (mirrors test_api.py).
# ─────────────────────────────────────────────


def _load():
    mock_ddb = MagicMock()
    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", return_value=MagicMock()):
        mock_ddb.Table.side_effect = lambda name: MagicMock()
        spec = importlib.util.spec_from_file_location(
            "facade_handler", "deploy/lambda/api/handler.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["facade_handler"] = mod
        spec.loader.exec_module(mod)
        return mod


api = _load()


# The routing contract as of T3-4 Phase 0. If a route is intentionally added or
# removed, update this set in the SAME commit and call it out in review — the
# point is that a refactor can't change it silently.
EXPECTED_ROUTES = {
    ("GET", "/tenants"),
    ("POST", "/tenants"),
    ("GET", "/tenants/{id}"),
    ("DELETE", "/tenants/{id}"),
    ("POST", "/tenants/{id}/{action}"),
    ("GET", "/tenants/{id}/{action}"),
    ("GET", "/backups"),
    ("POST", "/batch/tenants"),
    ("GET", "/hosts"),
    ("POST", "/hosts"),
    ("POST", "/hosts/refresh-rootfs"),
    ("GET", "/hosts/rootfs-version"),
    ("GET", "/agentcore/status"),
    ("GET", "/agentcore/tools"),
    ("GET", "/system/info"),
    ("GET", "/audit-log"),
    ("DELETE", "/hosts/{instance_id}"),
    ("POST", "/hosts/{instance_id}/drain"),
    ("POST", "/failover/{az}"),
    ("GET", "/groups"),
    ("POST", "/groups"),
    ("POST", "/groups/{name}/skills"),
    ("DELETE", "/groups/{name}/skills/{skill}"),
    ("GET", "/skills/{name}"),
    ("PUT", "/skills/{name}"),
    ("DELETE", "/skills/{name}"),
}


@pytest.mark.unit
@pytest.mark.regression
class TestRoutingContract:
    """Pin the exact route set the facade dispatches.

    We assert this from the source's routes dict (a static, deterministic
    read) rather than by live-dispatching every handler — driving the real
    handlers with a stub event risks a blocking SSM/retry loop, and would test
    handler internals rather than the routing contract this guardrail is about.
    The registration set + each key's handler resolving is what must survive
    the domain-extraction refactor.
    """

    def _routes_block(self):
        src = open("deploy/lambda/api/handler.py").read()
        return src[src.index("routes = {"):src.index("handler = routes.get")]

    def test_route_set_matches_contract(self):
        import re
        block = self._routes_block()
        found = set(re.findall(
            r'\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*"(/[^"]*)"\s*\)\s*:', block))
        assert found == EXPECTED_ROUTES, (
            "routes dict drifted from the contract.\n"
            f"  added:   {sorted(found - EXPECTED_ROUTES)}\n"
            f"  removed: {sorted(EXPECTED_ROUTES - found)}\n"
            "Update EXPECTED_ROUTES in the SAME commit and call it out in review "
            "— a domain-extraction refactor must not change the route set.")

    def test_every_route_handler_symbol_exists(self):
        """Each route dispatches to a facade symbol (either directly or inside a
        `lambda: name(...)`). Every referenced name must resolve on the module,
        so a domain move can't leave a route pointing at a vanished handler."""
        import re
        block = self._routes_block()
        # Names invoked as bare handlers or inside the lambda bodies.
        called = set(re.findall(r'\b([a-z_][a-z0-9_]*)\s*\(', block))
        called |= set(re.findall(r':\s*([a-z_][a-z0-9_]*)\s*,', block))  # bare refs
        # Only assert on the domain handlers the routes reference (skip locals
        # like event.get / path_params access).
        handlers = {n for n in called if n.islower()
                    and not n.startswith("event") and n not in {
                        "get", "lambda", "path_params"}}
        # Spot-check the load-bearing ones are real module attributes.
        for name in ("list_tenants", "create_tenant", "tenant_action",
                     "delete_tenant", "list_hosts", "register_host",
                     "drain_host", "trigger_failover", "list_groups",
                     "read_skill", "system_info", "list_all_backups"):
            assert name in handlers, f"route no longer references {name}"
            assert hasattr(api, name), f"facade missing route handler {name}"

    def test_unknown_route_404s(self):
        with patch.object(api, "_rbac_check", return_value=None):
            resp = api.lambda_handler({
                "httpMethod": "GET", "resource": "/no/such/route",
                "pathParameters": {}, "headers": {},
            }, None)
        assert resp["statusCode"] == 404


@pytest.mark.unit
@pytest.mark.regression
class TestFacadeLateBinding:
    """The refactor's domain modules call back into facade symbols (clients,
    env constants, shared helpers). These must exist as module attributes so
    the ~30 tests that monkeypatch e.g. api.tenants_table / api.ssm keep
    working, and so a domain's `_CTX.tenants_table` resolves at runtime."""

    @pytest.mark.parametrize("name", [
        # clients + tables (monkeypatch points across the suite)
        "ssm", "s3", "sns", "ddb", "tenants_table", "hosts_table",
        # env-derived config the handlers read
        "CPU_OVERCOMMIT_RATIO", "MEM_OVERCOMMIT_RATIO", "MAX_VMS_PER_HOST",
        "RBAC_ENABLED", "AUDIT_TTL_DAYS",
        # core shared helpers + dispatch
        "lambda_handler", "_resp", "_scan_all", "_host_fits", "_find_host",
        "_rbac_check", "_audit_write",
        # a representative handler from each domain that the routes reference
        "list_tenants", "create_tenant", "tenant_action", "delete_tenant",
        "list_hosts", "register_host", "drain_host", "trigger_failover",
        "list_groups", "read_skill", "system_info", "_list_audit_log",
        "process_pending", "cleanup_terminated_host",
    ])
    def test_facade_exposes_symbol(self, name):
        assert hasattr(api, name), (
            f"facade no longer exposes `{name}` — a domain move must re-export "
            f"it on the facade (a wrapper def or `from domains.x import {name}`) "
            f"or ~30 monkeypatching tests + runtime late-binding break")
