# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Go-live A2 regression guard: the chat frontend must NOT route a user to any
hardcoded / shared default tenant.

History: a `window.OC_DEFAULT_TENANT = "htest-v13"` fallback once routed every
user with no node of their own into the same shared node — a cross-tenant
breach. It was removed (commit 52e58a5). This test fails loudly if anyone
reintroduces a non-empty default tenant or a hardcoded `htest-*` node id in the
production chat path, so the bomb can't come back silently.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHAT_HTML = ROOT / "console" / "chat" / "index.html"


@pytest.mark.unit
class TestNoHardcodedTenant:
    def test_default_tenant_is_empty(self):
        """OC_DEFAULT_TENANT must be assigned the empty string, never a real id."""
        src = CHAT_HTML.read_text()
        # find the assignment(s); allow only `= ""` (empty)
        assigns = re.findall(r"""OC_DEFAULT_TENANT\s*=\s*(['"])(.*?)\1""", src)
        assert assigns, "OC_DEFAULT_TENANT assignment not found — did the var move?"
        for _q, val in assigns:
            assert val == "", (
                f"OC_DEFAULT_TENANT must be empty (go-live A2: no shared default "
                f"tenant), got {val!r}. A non-empty default routes node-less users "
                f"to a shared node — a cross-tenant breach."
            )

    def test_no_hardcoded_htest_node_in_routing(self):
        """No `htest-*` literal should appear as a routed tenant/id outside comments."""
        for raw in CHAT_HTML.read_text().splitlines():
            line = raw.strip()
            if line.startswith("//") or line.startswith("*") or line.startswith("<!--"):
                continue  # comments may mention the history for context
            # flag a literal htest id used as a value (id:/tenant:/= "htest-..")
            if re.search(r"""['"]htest-[a-z0-9]+['"]""", line):
                pytest.fail(
                    f"hardcoded htest-* node id in a non-comment line (go-live A2 "
                    f"forbids hardcoded routing targets): {line[:120]}"
                )

    def test_active_node_not_synthesized_from_default(self):
        """The dead 'synthesize active from defTenant' pattern must stay gone."""
        src = CHAT_HTML.read_text()
        # the old bomb built: active = { id: defTenant, tenant: defTenant, ... }
        assert "id: defTenant" not in src and "tenant: defTenant" not in src, (
            "found a synthesized active node from defTenant — the removed "
            "default-tenant fallback bomb (go-live A2) must not return."
        )
