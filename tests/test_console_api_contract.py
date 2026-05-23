# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract tests between console/index.html and the API Lambda.

The console is a thin Alpine.js client over the API. Several constants are
duplicated between the two — most notably the tenant-name regex (issue
#1.2.6) and the RBAC role list (issue #14). When the API tightens
validation but the console doesn't follow, users get cryptic 400s with no
inline hint; when the console rejects values the API would accept, users
hit a wall before the API even sees them.

These tests are cheap regex-greps against the rendered HTML, not a full
JavaScript parse — but they're enough to fail loudly on a typo or a
forgotten cross-update. Run as part of `pytest -m unit`.
"""

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "console" / "index.html").read_text()


# ─────────────────────────────────────────────
# Load handler with mocked SDK so we can read its module-level constants.
# ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def handler():
    _mock_ddb = MagicMock()
    _mock_ssm = MagicMock()
    with patch("boto3.resource", return_value=_mock_ddb), \
         patch("boto3.client", return_value=_mock_ssm):
        spec = importlib.util.spec_from_file_location(
            "contract_handler", str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
        h = importlib.util.module_from_spec(spec)
        sys.modules["contract_handler"] = h
        spec.loader.exec_module(h)
        return h


# ═════════════════════════════════════════════
# Tenant name validation
# ═════════════════════════════════════════════


@pytest.mark.unit
class TestNameRegexParity:
    """The console mirrors API _NAME_RE for live validation in the create modal.

    A divergence means either:
    - the console accepts a name the API will reject (user submits, gets 400)
    - the console rejects a name the API would accept (user blocked unnecessarily)

    Both are bad UX; this test is the canary.
    """

    def test_console_has_a_name_regex(self):
        # The console embeds the regex in app() get nameError. Match the
        # actual `/.../.test(...)` line to avoid scraping comments.
        # Console literal: /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/
        # Inside a JS char class `-` is unescaped at end/start, so the literal
        # in the file is exactly that — no backslash before the dash.
        m = re.search(
            r"/\^\[a-z0-9\]\(\[a-z0-9-\]\{0,30\}\[a-z0-9\]\)\?\$/",
            INDEX_HTML,
        )
        assert m, "console/index.html missing a /^[a-z0-9](...)?$/ regex literal"

    def test_console_regex_matches_api_regex(self, handler):
        api_pattern = handler._NAME_RE.pattern
        # Console's literal form is the same string between the slashes.
        assert re.search(re.escape(api_pattern), INDEX_HTML), (
            f"console regex literal does not contain the API _NAME_RE "
            f"pattern {api_pattern!r}; one was updated without the other"
        )

    def test_max_length_in_sync(self, handler):
        # API rejects > 32 chars. Console caps the input + checks length > 32.
        # Look for both the maxlength attribute and the length check JS.
        assert 'maxlength="32"' in INDEX_HTML, "create modal name input missing maxlength=32"
        assert "length > 32" in INDEX_HTML, "console missing the > 32 character length guard"


# ═════════════════════════════════════════════
# RBAC role list
# ═════════════════════════════════════════════


@pytest.mark.unit
class TestRoleHierarchyParity:
    """API and console both rank cognito groups admin > operator > viewer.

    If someone introduces a new role on either side without the other,
    permissions silently break.
    """

    def test_api_role_rank_keys(self, handler):
        assert set(handler._ROLE_RANK.keys()) == {"viewer", "operator", "admin"}

    def test_console_has_same_role_keys(self):
        # Console role() has `var rank = { viewer: 0, operator: 1, admin: 2 };`
        # We grep for each key. (A future change that renames a role on one
        # side without the other will show up here.)
        for role in ("viewer", "operator", "admin"):
            assert re.search(rf"{role}\s*:\s*[0-2]", INDEX_HTML), (
                f"console role-rank table missing {role!r}; API and console "
                f"role lists have diverged"
            )


# ═════════════════════════════════════════════
# Tab navigation matches loaders
# ═════════════════════════════════════════════


@pytest.mark.unit
class TestTabsHaveLoaders:
    """Each tab declared in the nav-tabs strip has its `page='X'` and a
    matching `<div x-show="page==='X'">` content section. A typo in either
    half silently leaves the tab dead."""

    @pytest.fixture
    def declared_pages(self):
        # class="nav-tab" ... @click="page='tenants'" → tenants, app, monitoring, ...
        return set(re.findall(r"page=['\"]([a-z]+)['\"]", INDEX_HTML))

    def test_all_tabs_have_content(self, declared_pages):
        # The set of declared pages should be exactly:
        # tenants, app, monitoring, backups, settings  (post-1.2.8).
        assert declared_pages >= {
            "tenants", "app", "monitoring", "backups", "settings",
        }, f"missing one or more expected tabs: declared={declared_pages}"

    def test_each_page_has_x_show_block(self, declared_pages):
        for page in declared_pages:
            assert re.search(rf'x-show="page===\'{re.escape(page)}\'"', INDEX_HTML), (
                f"tab {page!r} declared in nav-tab strip but no matching "
                f"<div x-show=\"page==='{page}'\"> content block"
            )


# ═════════════════════════════════════════════
# /system/info & /agentcore/tools wired
# ═════════════════════════════════════════════


@pytest.mark.unit
class TestNewEndpointsWired:
    """The 1.2.8 console depends on /system/info and /agentcore/tools.
    If someone removes either route from the handler without dropping the
    console reference, the UI silently goes blank — these tests will fail
    first."""

    def test_system_info_route_in_handler(self, handler):
        ev = {
            "httpMethod": "GET", "resource": "/system/info",
            "pathParameters": {}, "headers": {},
        }
        resp = handler.lambda_handler(ev, None)
        assert resp["statusCode"] == 200

    def test_agentcore_tools_route_in_handler(self, handler):
        ev = {
            "httpMethod": "GET", "resource": "/agentcore/tools",
            "pathParameters": {}, "headers": {},
        }
        resp = handler.lambda_handler(ev, None)
        assert resp["statusCode"] == 200

    def test_console_calls_system_info(self):
        assert re.search(r"this\.api\(['\"]GET['\"],\s*['\"]system/info['\"]", INDEX_HTML)

    def test_console_calls_agentcore_tools(self):
        assert re.search(r"this\.api\(['\"]GET['\"],\s*['\"]agentcore/tools['\"]", INDEX_HTML)

    def test_migrate_endpoint_used_by_console(self):
        """Console's migrate modal posts to /tenants/{id}/migrate."""
        assert "tenants/' + id + '/migrate'" in INDEX_HTML or \
               "tenants/' + id + '/migrate" in INDEX_HTML
