# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""host-agent's control/metrics listener must stay off the public internet.

What went wrong
---------------
host-agent served `/health` — which enumerates EVERY tenant on the host, with
ids, health state and per-VM metrics — from `0.0.0.0:8899`, and init-host.sh
reverse-proxied it as `/agent/health` on the *public* `:80` server block. That
block is what the ALB (and in single-domain mode, CloudFront) forwards to, so
the fleet's tenant inventory was one unauthenticated GET away.

Both halves have to stay fixed, hence two independent guards: binding to
loopback alone would still leak through a re-added proxy, and removing the
proxy alone would still leave the port open to anything that can route to the
host's private IP (which includes guest VMs — their egress is SNAT'd through
the host, see the cross-tenant isolation tests).

These are source-level assertions on purpose: the failure mode is a config
line reappearing during a refactor, which no amount of runtime mocking of the
handler would notice.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "deploy" / "userdata" / "host-agent.py"
INIT_HOST = ROOT / "deploy" / "userdata" / "init-host.sh"

pytestmark = pytest.mark.unit


class TestAgentBindsLoopback:
    def test_httpserver_does_not_bind_all_interfaces(self):
        src = AGENT.read_text()
        assert '"0.0.0.0"' not in src and "'0.0.0.0'" not in src, (
            "host-agent must not bind 0.0.0.0 — /health lists every tenant on "
            "the host")

    def test_bind_default_is_loopback(self):
        src = AGENT.read_text()
        m = re.search(
            r"BIND_HOST\s*=\s*os\.environ\.get\(\s*['\"]OC_AGENT_BIND['\"]\s*,"
            r"\s*['\"](?P<default>[^'\"]+)['\"]",
            src)
        assert m, "expected BIND_HOST to come from OC_AGENT_BIND with a default"
        assert m.group("default") == "127.0.0.1", (
            f"default bind must be loopback, got {m.group('default')!r}")

    def test_server_actually_uses_bind_host(self):
        """Guard against the constant existing but not being wired up."""
        src = AGENT.read_text()
        assert re.search(r"HTTPServer\(\s*\(\s*BIND_HOST\s*,\s*PORT\s*\)", src), (
            "HTTPServer must bind (BIND_HOST, PORT)")

    def test_adot_still_scrapes_loopback(self):
        """The one legitimate consumer must keep working after the change."""
        adot = (ROOT / "deploy" / "userdata" / "adot-config.yaml").read_text()
        assert "127.0.0.1:8899" in adot, (
            "ADOT scrapes host-agent over loopback; if this target changed, "
            "binding 127.0.0.1 would break metrics collection")


class TestNoPublicAgentProxy:
    @staticmethod
    def _public_server_block():
        """Extract the `listen 80` server block from init-host.sh."""
        text = INIT_HOST.read_text()
        blocks = re.findall(r"server\s*\{(.*?)\n\}", text, re.S)
        pub = [b for b in blocks if re.search(r"listen\s+80\b", b)]
        assert pub, "could not find the `listen 80` server block in init-host.sh"
        return pub[0]

    def test_public_block_has_no_agent_proxy(self):
        assert "/agent/health" not in self._public_server_block(), (
            "the public :80 block must not proxy host-agent — it exposes every "
            "tenant's health detail to the internet via the ALB")

    def test_nothing_proxies_to_8899(self):
        """Any proxy_pass to the agent port is a leak regardless of its path."""
        text = INIT_HOST.read_text()
        offenders = [ln.strip() for ln in text.splitlines()
                     if "proxy_pass" in ln and "8899" in ln]
        assert offenders == [], (
            f"host-agent's port must not be reverse-proxied: {offenders}")

    def test_public_block_still_serves_tenants(self):
        """Removing the proxy must not have taken the tenant routes with it."""
        block = self._public_server_block()
        assert "conf.d/tenants/*.conf" in block
        assert "conf.d/tenant-peers/*.conf" in block
        assert "/health" in block, "ALB target-group health check needs /health"
