# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the Terraform deployment module (issue #18).

We don't run `terraform plan` (no provider creds, no terraform binary
guarantee) — but we *can* verify:

1. The module exists at the expected location.
2. The required files (main.tf, variables.tf, outputs.tf, README.md)
   are present and non-empty.
3. The HCL declares the canonical resources we promised users:
   2 dynamodb tables, 1 s3 bucket, 1 lambda, 1 api gateway.
4. README documents the CDK ↔ Terraform mapping.

Resource counting is regex-based (no hcl parser); good enough for a
sanity check. A future PR can add `terraform validate` as a CI step.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TF_DIR = ROOT / "terraform"


@pytest.mark.unit
class TestModuleLayout:
    def test_terraform_dir_exists(self):
        assert TF_DIR.is_dir(), "terraform/ module directory missing"

    @pytest.mark.parametrize("filename", ["main.tf", "variables.tf", "outputs.tf", "README.md"])
    def test_required_file_exists(self, filename):
        f = TF_DIR / filename
        assert f.is_file(), f"terraform/{filename} missing"
        assert f.stat().st_size > 0, f"terraform/{filename} is empty"


@pytest.mark.unit
class TestResourceCoverage:
    def _all_tf(self):
        return "\n".join((p.read_text() for p in TF_DIR.glob("*.tf")))

    def test_two_dynamodb_tables(self):
        text = self._all_tf()
        # match `resource "aws_dynamodb_table" "..."`
        matches = re.findall(r'resource\s+"aws_dynamodb_table"\s+"', text)
        assert len(matches) >= 2, \
            f"expected ≥2 aws_dynamodb_table (tenants + hosts), found {len(matches)}"

    def test_assets_bucket(self):
        text = self._all_tf()
        assert re.search(r'resource\s+"aws_s3_bucket"\s+', text), \
            "missing aws_s3_bucket resource"

    def test_api_lambda_function(self):
        text = self._all_tf()
        assert re.search(r'resource\s+"aws_lambda_function"\s+', text), \
            "missing aws_lambda_function resource"

    def test_api_gateway(self):
        text = self._all_tf()
        # Either v1 (aws_api_gateway_rest_api) or v2 (aws_apigatewayv2_api)
        assert (re.search(r'resource\s+"aws_api_gateway_rest_api"\s+', text) or
                re.search(r'resource\s+"aws_apigatewayv2_api"\s+', text)), \
            "missing API Gateway resource"

    def test_iam_role_for_lambda(self):
        text = self._all_tf()
        assert re.search(r'resource\s+"aws_iam_role"\s+', text), \
            "missing aws_iam_role for Lambda execution"


@pytest.mark.unit
class TestVariables:
    def test_region_variable(self):
        text = (TF_DIR / "variables.tf").read_text()
        assert 'variable "region"' in text or 'variable "aws_region"' in text


@pytest.mark.unit
class TestOutputs:
    def test_api_url_output(self):
        text = (TF_DIR / "outputs.tf").read_text()
        # Must expose an api_url output for parity with CDK
        assert "api_url" in text.lower()


@pytest.mark.unit
class TestReadmeQuality:
    def test_readme_has_quick_start(self):
        text = (TF_DIR / "README.md").read_text().lower()
        assert "terraform init" in text and "terraform apply" in text

    def test_readme_documents_cdk_mapping(self):
        text = (TF_DIR / "README.md").read_text().lower()
        # Must explain that this is parity with the CDK stack
        assert "cdk" in text


# ══════════════════════════════════════════════════════════════════════
# T3-6: env-var parity between the api handler and the Terraform path
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnvParity:
    """The api handler silently disables features when an env var is unset
    (GROUPS_TABLE→no /groups, AUDIT_TABLE→no audit, RBAC_ENABLED→RBAC off, ...).
    The TF path used to set only 9 of ~42 vars. This is the drift guard: every
    os.environ name the handler reads MUST be provided by main.tf — as a static
    local, resource-derived in the Lambda block, or an explicit CDK-only var
    (features this minimal module doesn't create the backing resource for)."""

    HANDLER = ROOT / "deploy" / "lambda" / "api" / "handler.py"

    # Injected by the Lambda runtime — never set by IaC.
    _RUNTIME_PROVIDED = {"AWS_REGION", "AWS_DEFAULT_REGION"}

    # Resource-derived and CDK-only: this minimal TF module doesn't create the
    # backing resource (Cognito, ALB, VPC wiring, AgentCore, AMP/AMG, the
    # sibling Lambdas). Documented as intentionally absent in README; operators
    # supply them via api_env_overrides. Keep in sync with the README list.
    _CDK_ONLY = {
        "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID",
        "ALB_LISTENER_ARN", "VPC_ID",
        "AGENTCORE_ENABLED", "AGENTCORE_GATEWAY_URL",
        "AMP_REMOTE_WRITE_URL", "GRAFANA_WORKSPACE_URL",
        "HEALTH_CHECK_FUNCTION", "BACKUP_FUNCTION",
        "NOTIFICATIONS_TOPIC_ARN", "PROJECT_VERSION",
    }

    def _handler_env_names(self):
        src = self.HANDLER.read_text()
        return set(re.findall(
            r'os\.environ(?:\.get)?\(\s*["\']([A-Z_][A-Z0-9_]*)["\']', src))

    def _tf_provided_names(self):
        text = "\n".join(p.read_text() for p in TF_DIR.glob("*.tf"))
        # Any `NAME = ...` inside a local/env map (uppercase snake keys).
        return set(re.findall(r'^\s*([A-Z_][A-Z0-9_]*)\s*=', text, re.MULTILINE))

    def test_every_handler_env_is_provided_or_documented(self):
        read = self._handler_env_names()
        provided = self._tf_provided_names()
        missing = read - provided - self._RUNTIME_PROVIDED - self._CDK_ONLY
        assert not missing, (
            "api handler reads env vars the Terraform path neither sets nor "
            f"documents as CDK-only: {sorted(missing)}. Add them to "
            "local.api_env_static / the Lambda env block, or (if resource-"
            "derived) to the _CDK_ONLY allowlist + terraform/README.md.")

    def test_feature_gating_vars_present(self):
        """The specific vars whose absence silently disabled features."""
        provided = self._tf_provided_names()
        for name in ("GROUPS_TABLE", "AUDIT_TABLE", "AUDIT_TTL_DAYS",
                     "RBAC_ENABLED", "CONSOLE_AUTH_ENABLED", "VM_DATA_DISK_MB"):
            assert name in provided, f"TF path missing feature-gating {name}"

    def test_groups_and_audit_tables_exist(self):
        text = "\n".join(p.read_text() for p in TF_DIR.glob("*.tf"))
        matches = re.findall(r'resource\s+"aws_dynamodb_table"\s+"(\w+)"', text)
        assert "groups" in matches and "audit" in matches, \
            f"missing groups/audit DDB tables; found {matches}"
        # Audit table must carry a TTL block so rows auto-prune.
        assert re.search(r'ttl\s*\{[^}]*attribute_name\s*=\s*"expires_ttl"',
                         text, re.DOTALL), "audit table missing TTL on expires_ttl"

    def test_vm_data_disk_default_matches_config(self):
        """The headline drift: handler fallback 2048 vs config 8192. TF must
        pin the config value, not inherit the handler default."""
        vs = (TF_DIR / "variables.tf").read_text()
        m = re.search(r'variable\s+"vm_data_disk_mb"\s*\{.*?default\s*=\s*(\d+)',
                      vs, re.DOTALL)
        assert m and int(m.group(1)) == 8192, \
            "vm_data_disk_mb must default to 8192 (config parity), not 2048"
