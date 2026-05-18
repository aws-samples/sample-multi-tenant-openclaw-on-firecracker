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
