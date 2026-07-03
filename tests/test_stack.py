# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for CDK stack synthesized resources.

These tests use aws_cdk.assertions.Template to verify the synthesized
CloudFormation template contains the expected properties — no AWS calls,
no actual deployment.
"""

import os
import sys
import pytest

# Ensure deploy/ is importable so we can import stack.py
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "deploy"))

import aws_cdk as cdk
from aws_cdk import assertions

from stack import OpenClawOrchestratorStack  # noqa: E402


@pytest.fixture(scope="module")
def synthesized_template():
    """Synthesize the stack once and return a Template for assertions."""
    app = cdk.App()
    stack = OpenClawOrchestratorStack(
        app, "OpenClawOrchestrator-test",
        env=cdk.Environment(account="123456789012", region="ap-northeast-1"),
    )
    return assertions.Template.from_stack(stack)


# ═══════════════════════════════════════════
# Issue #6 — EBS encryption at rest
# ═══════════════════════════════════════════


class TestEbsEncryption:
    """Tenant data is stored on the host EBS data volume; it must be encrypted at rest."""

    @pytest.mark.unit
    def test_data_volume_is_encrypted(self, synthesized_template):
        """The /dev/sdf data volume in the host LaunchTemplate must have Encrypted: true."""
        # Find the LaunchTemplate and assert its data volume is encrypted.
        # The root volume (/dev/sda1) does not need encryption (no tenant data).
        synthesized_template.has_resource_properties(
            "AWS::EC2::LaunchTemplate",
            {
                "LaunchTemplateData": assertions.Match.object_like({
                    "BlockDeviceMappings": assertions.Match.array_with([
                        assertions.Match.object_like({
                            "DeviceName": "/dev/sdf",
                            "Ebs": assertions.Match.object_like({
                                "Encrypted": True,
                            }),
                        }),
                    ]),
                }),
            },
        )

    @pytest.mark.unit
    def test_data_volume_uses_gp3(self, synthesized_template):
        """Regression: ensure encryption did not change the volume type from GP3."""
        synthesized_template.has_resource_properties(
            "AWS::EC2::LaunchTemplate",
            {
                "LaunchTemplateData": assertions.Match.object_like({
                    "BlockDeviceMappings": assertions.Match.array_with([
                        assertions.Match.object_like({
                            "DeviceName": "/dev/sdf",
                            "Ebs": assertions.Match.object_like({
                                "VolumeType": "gp3",
                            }),
                        }),
                    ]),
                }),
            },
        )

    @pytest.mark.unit
    @pytest.mark.regression
    def test_data_volume_size_unchanged(self, synthesized_template):
        """Regression: data volume size still comes from config.yml host.data_volume_gb."""
        # Read the configured size from config.yml so the test stays in sync with config.
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(ROOT) / "config.yml").read_text())
        expected_size = cfg["host"]["data_volume_gb"]

        synthesized_template.has_resource_properties(
            "AWS::EC2::LaunchTemplate",
            {
                "LaunchTemplateData": assertions.Match.object_like({
                    "BlockDeviceMappings": assertions.Match.array_with([
                        assertions.Match.object_like({
                            "DeviceName": "/dev/sdf",
                            "Ebs": assertions.Match.object_like({
                                "VolumeSize": expected_size,
                            }),
                        }),
                    ]),
                }),
            },
        )
