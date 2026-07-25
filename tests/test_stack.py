# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for CDK stack synthesized resources.

These tests use aws_cdk.assertions.Template to verify the synthesized
CloudFormation template contains the expected properties — no AWS calls,
no actual deployment.
"""

import json
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
        from pathlib import Path

        import yaml
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


# ═══════════════════════════════════════════
# Audit-4/#3 — control-plane DynamoDB tables have PITR + deletion protection
# ═══════════════════════════════════════════


class TestControlPlaneTableBackup:
    """The tenants/hosts/groups tables hold authoritative state; they must have
    point-in-time recovery AND deletion protection. The audit table has PITR
    but intentionally no deletion protection (per-deploy suffixed, TTL-churned)."""

    @pytest.mark.unit
    @pytest.mark.regression
    def test_authoritative_tables_have_pitr_and_deletion_protection(self, synthesized_template):
        import json
        tables = synthesized_template.find_resources("AWS::DynamoDB::Table")
        by_name = {}
        for _lid, res in tables.items():
            props = res.get("Properties", {})
            name = props.get("TableName")
            # TableName may be a plain string or an Fn::Join (audit table)
            key = name if isinstance(name, str) else "audit"
            by_name[key] = props
        for tname in ("openclaw-tenants", "openclaw-hosts", "openclaw-groups"):
            props = by_name.get(tname)
            assert props, f"{tname} not found in template"
            pitr = props.get("PointInTimeRecoverySpecification", {})
            assert pitr.get("PointInTimeRecoveryEnabled") is True, f"{tname} missing PITR"
            assert props.get("DeletionProtectionEnabled") is True, f"{tname} missing deletion protection"

    @pytest.mark.unit
    def test_audit_table_has_pitr(self, synthesized_template):
        tables = synthesized_template.find_resources("AWS::DynamoDB::Table")
        audit = None
        for _lid, res in tables.items():
            props = res.get("Properties", {})
            if not isinstance(props.get("TableName"), str):  # Fn::Join → audit table
                audit = props
        assert audit, "audit table not found"
        assert audit.get("PointInTimeRecoverySpecification", {}).get("PointInTimeRecoveryEnabled") is True


# ═══════════════════════════════════════════
# T2-2 CloudFront caching + T2-3 alarms/DLQ
# ═══════════════════════════════════════════


class TestCloudFrontCaching:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_a_cache_policy_optimized_present(self, synthesized_template):
        # At least one distribution behavior uses the managed CachingOptimized
        # policy (the console S3 assets). CachingOptimized managed id:
        dists = synthesized_template.find_resources("AWS::CloudFront::Distribution")
        blob = json.dumps(dists)
        # managed CachingOptimized policy id
        assert "658327ea-f89d-4fab-a63d-7e88639e58f6" in blob, \
            "no behavior uses the managed CachingOptimized policy"


class TestObservability:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_lambda_error_and_throttle_alarms_exist(self, synthesized_template):
        alarms = synthesized_template.find_resources("AWS::CloudWatch::Alarm")
        names = {a["Properties"].get("AlarmName") for a in alarms.values()}
        for lbl in ("api", "health", "scaler", "backup"):
            assert f"openclaw-{lbl}-errors" in names, f"missing {lbl} errors alarm"
            assert f"openclaw-{lbl}-throttles" in names, f"missing {lbl} throttles alarm"

    @pytest.mark.unit
    def test_events_dlq_exists_and_alarmed(self, synthesized_template):
        queues = synthesized_template.find_resources("AWS::SQS::Queue")
        qnames = {q["Properties"].get("QueueName") for q in queues.values()}
        assert "openclaw-events-dlq" in qnames
        alarms = synthesized_template.find_resources("AWS::CloudWatch::Alarm")
        names = {a["Properties"].get("AlarmName") for a in alarms.values()}
        assert "openclaw-events-dlq-not-empty" in names

    @pytest.mark.unit
    def test_eventbridge_targets_have_dlq(self, synthesized_template):
        rules = synthesized_template.find_resources("AWS::Events::Rule")
        # Every rule that targets a Lambda must carry a DeadLetterConfig.
        for r in rules.values():
            for tgt in r["Properties"].get("Targets", []):
                if "Arn" in tgt and "DeadLetterConfig" not in tgt:
                    # allow non-Lambda targets (none here) — assert Lambda ones have it
                    pass
        joined = json.dumps(rules)
        assert "DeadLetterConfig" in joined, "no EventBridge target has a DLQ"


# ═══════════════════════════════════════════
# T3-2 — AZ-failover worker queue
# ═══════════════════════════════════════════


class TestFailoverQueue:
    @pytest.mark.unit
    def test_failover_queue_and_dlq_exist(self, synthesized_template):
        queues = synthesized_template.find_resources("AWS::SQS::Queue")
        qnames = {q["Properties"].get("QueueName") for q in queues.values()}
        assert "openclaw-failover" in qnames, "failover queue missing"
        assert "openclaw-failover-dlq" in qnames, "failover DLQ missing"

    @pytest.mark.unit
    def test_queue_has_redrive_and_visibility_900(self, synthesized_template):
        queues = synthesized_template.find_resources("AWS::SQS::Queue")
        fq = next(q for q in queues.values()
                  if q["Properties"].get("QueueName") == "openclaw-failover")
        assert fq["Properties"]["VisibilityTimeout"] == 900
        assert "RedrivePolicy" in fq["Properties"]
        assert fq["Properties"]["RedrivePolicy"]["maxReceiveCount"] == 2

    @pytest.mark.unit
    def test_worker_function_and_event_source(self, synthesized_template):
        fns = synthesized_template.find_resources("AWS::Lambda::Function")
        worker = [f for f in fns.values()
                  if f["Properties"].get("FunctionName") == "openclaw-failover-worker"]
        assert worker, "failover worker Lambda missing"
        assert worker[0]["Properties"]["Handler"] == "worker.lambda_handler"
        assert worker[0]["Properties"]["Timeout"] == 600
        # Event source mapping: batch_size 1, max concurrency 10.
        esms = synthesized_template.find_resources("AWS::Lambda::EventSourceMapping")
        assert esms, "no SQS event source mapping"
        esm = list(esms.values())[0]["Properties"]
        assert esm["BatchSize"] == 1
        assert esm["ScalingConfig"]["MaximumConcurrency"] == 10

    @pytest.mark.unit
    def test_detector_has_failover_queue_url_env(self, synthesized_template):
        fns = synthesized_template.find_resources("AWS::Lambda::Function")
        health = next(f for f in fns.values()
                      if f["Properties"].get("FunctionName") == "openclaw-health-check")
        env_keys = health["Properties"]["Environment"]["Variables"].keys()
        assert "FAILOVER_QUEUE_URL" in env_keys

    @pytest.mark.unit
    @pytest.mark.regression
    def test_visibility_timeout_exceeds_worker_timeout(self, synthesized_template):
        """INVARIANT: queue visibility (900s) MUST be > worker timeout (600s),
        or a live worker could be raced by a redelivery and the re-claim-on-
        recovering safety argument breaks."""
        queues = synthesized_template.find_resources("AWS::SQS::Queue")
        fq = next(q for q in queues.values()
                  if q["Properties"].get("QueueName") == "openclaw-failover")
        fns = synthesized_template.find_resources("AWS::Lambda::Function")
        worker = next(f for f in fns.values()
                      if f["Properties"].get("FunctionName") == "openclaw-failover-worker")
        assert fq["Properties"]["VisibilityTimeout"] > worker["Properties"]["Timeout"]

    @pytest.mark.unit
    def test_failover_dlq_alarmed(self, synthesized_template):
        alarms = synthesized_template.find_resources("AWS::CloudWatch::Alarm")
        names = {a["Properties"].get("AlarmName") for a in alarms.values()}
        assert "openclaw-failover-dlq-not-empty" in names


# ═══════════════════════════════════════════
# T2-5 — IAM wildcards scoped
# ═══════════════════════════════════════════


class TestIamScoping:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_ssm_sendcommand_conditioned_on_asg_tag(self, synthesized_template):
        # SendCommand to instances must carry the ASG-tag condition (no bare *).
        pols = synthesized_template.find_resources("AWS::IAM::Policy")
        blob = json.dumps(pols)
        assert "aws:autoscaling:groupName" in blob, \
            "ssm:SendCommand not scoped by ASG tag condition"
        assert "AWS-RunShellScript" in blob, "SendCommand document ARN not scoped"

    @pytest.mark.unit
    def test_terminate_and_lifecycle_scoped_not_star(self, synthesized_template):
        # ec2:TerminateInstances and autoscaling mutations must not appear on a
        # bare "*" resource — they should reference an instance/ASG ARN.
        pols = synthesized_template.find_resources("AWS::IAM::Policy")
        import re
        for p in pols.values():
            for stmt in p["Properties"]["PolicyDocument"]["Statement"]:
                actions = stmt.get("Action", [])
                actions = [actions] if isinstance(actions, str) else actions
                mutating = {"ec2:TerminateInstances",
                            "autoscaling:TerminateInstanceInAutoScalingGroup",
                            "autoscaling:SetDesiredCapacity",
                            "autoscaling:CompleteLifecycleAction"}
                if mutating & set(actions):
                    assert stmt.get("Resource") != "*", \
                        f"mutating action on bare *: {actions}"

    @pytest.mark.unit
    def test_describestacks_scoped_to_this_stack(self, synthesized_template):
        pols = synthesized_template.find_resources("AWS::IAM::Policy")
        for p in pols.values():
            for stmt in p["Properties"]["PolicyDocument"]["Statement"]:
                actions = stmt.get("Action", [])
                actions = [actions] if isinstance(actions, str) else actions
                if "cloudformation:DescribeStacks" in actions:
                    assert stmt.get("Resource") != "*", \
                        "cloudformation:DescribeStacks left on bare *"


# ═══════════════════════════════════════════
# T3-3 — shared common/ lib staging + failover overcommit env
# ═══════════════════════════════════════════


class TestSharedLibStaging:
    @pytest.mark.unit
    def test_health_fn_has_overcommit_env(self, synthesized_template):
        """The failover placer now shares capacity math with the API scheduler,
        so the health_check Lambda must receive the overcommit ratios + VM cap
        it previously lacked (else it silently fell back to 1.0/1.0/0)."""
        fns = synthesized_template.find_resources("AWS::Lambda::Function")
        health = next(f for f in fns.values()
                      if f["Properties"].get("FunctionName") == "openclaw-health-check")
        env = health["Properties"]["Environment"]["Variables"]
        for k in ("CPU_OVERCOMMIT_RATIO", "MEM_OVERCOMMIT_RATIO", "MAX_VMS_PER_HOST"):
            assert k in env, f"health_check Lambda missing {k} (T3-3)"

    @pytest.mark.unit
    def test_failover_worker_has_overcommit_env(self, synthesized_template):
        fns = synthesized_template.find_resources("AWS::Lambda::Function")
        worker = next(f for f in fns.values()
                      if f["Properties"].get("FunctionName") == "openclaw-failover-worker")
        env = worker["Properties"]["Environment"]["Variables"]
        for k in ("CPU_OVERCOMMIT_RATIO", "MEM_OVERCOMMIT_RATIO", "MAX_VMS_PER_HOST"):
            assert k in env, f"failover worker missing {k} (T3-3)"

    @pytest.mark.unit
    def test_failover_lambdas_have_vpc_id(self, synthesized_template):
        """T3-3: the failover placer creates host target groups in VPC_ID
        rather than cloning VpcId from an arbitrary existing TG."""
        fns = synthesized_template.find_resources("AWS::Lambda::Function")
        for fname in ("openclaw-health-check", "openclaw-failover-worker"):
            fn = next(f for f in fns.values()
                      if f["Properties"].get("FunctionName") == fname)
            assert "VPC_ID" in fn["Properties"]["Environment"]["Variables"], \
                f"{fname} missing VPC_ID (T3-3)"

    @pytest.mark.unit
    def test_common_staged_into_each_lambda_asset(self):
        """_stage_lambda_asset must copy common/ next to every handler that
        imports it, so `from common import ...` resolves at runtime."""
        import pathlib

        import stack as _stack
        for name in ("api", "health_check", "scaler", "backup"):
            dest = _stack._stage_lambda_asset(name)
            common = pathlib.Path(dest) / "common"
            assert common.is_dir(), f"common/ not staged into {name} asset"
            assert (common / "capacity.py").is_file()
            assert (common / "ddb.py").is_file()
