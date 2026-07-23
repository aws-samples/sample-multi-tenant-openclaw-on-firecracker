# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""moto-backed DynamoDB contract tests (#audit-5).

The 667 offline tests mock DynamoDB with a bare MagicMock (conftest.make_ddb_table),
which happily ACCEPTS syntactically-invalid DynamoDB expressions — that is
exactly how the 1.5.8 ConditionExpression-arithmetic bug ("used_vcpu + :v <=
:cap") shipped and could only be caught live.

These tests run the real handler code against moto's DynamoDB backend, which
parses and rejects invalid expressions the same way AWS does. They (a) prove the
current _reserve_host_slot expression is valid against a real engine, and (b)
pin the guard: arithmetic in a ConditionExpression raises, so a regression to
the old form fails offline instead of in production.
"""

import importlib.util
import sys

import boto3
import pytest
from botocore.exceptions import ClientError

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

pytestmark = pytest.mark.unit

REGION = "us-east-1"


def _make_tables():
    """Create the tenants + hosts tables in the (moto) backend."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName="openclaw-tenants",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="openclaw-hosts",
        KeySchema=[{"AttributeName": "instance_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "instance_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _load_handler():
    """Import the API handler fresh so its module-level boto3 resource binds to
    the active moto backend."""
    import os
    os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
    os.environ.setdefault("HOSTS_TABLE", "openclaw-hosts")
    os.environ.setdefault("ASSETS_BUCKET", "test")
    os.environ.setdefault("ROOTFS_PREFIX", "deployment/rootfs")
    sys.modules.pop("moto_api_handler", None)
    spec = importlib.util.spec_from_file_location(
        "moto_api_handler", "deploy/lambda/api/handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["moto_api_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


def _host_item(iid="i-1", total_vcpu=15, used_vcpu=0, total_mem_mb=32768,
               used_mem_mb=0, vm_count=0, next_vm_num=1):
    return {"instance_id": iid, "total_vcpu": total_vcpu, "used_vcpu": used_vcpu,
            "total_mem_mb": total_mem_mb, "used_mem_mb": used_mem_mb,
            "vm_count": vm_count, "status": "active", "next_vm_num": next_vm_num}


class TestReserveHostSlotAgainstRealDdb:
    @mock_aws
    def test_reserve_succeeds_and_returns_unique_vm_num(self):
        _make_tables()
        api = _load_handler()
        api.CPU_OVERCOMMIT_RATIO = 2.0
        api.MEM_OVERCOMMIT_RATIO = 1.5
        api.MAX_VMS_PER_HOST = 0
        h = _host_item(next_vm_num=7)
        api.hosts_table.put_item(Item=h)
        # This is the assertion the MagicMock suite CANNOT make: the real DDB
        # engine parses the UpdateExpression + ConditionExpression. If either
        # contained arithmetic (the 1.5.8 bug) this raises ValidationException.
        vm_num = api._reserve_host_slot(h, 1, 2048)
        assert vm_num == 7
        row = api.hosts_table.get_item(Key={"instance_id": "i-1"}).get("Item")
        assert int(row["used_vcpu"]) == 1
        assert int(row["next_vm_num"]) == 8

    @mock_aws
    def test_reserve_refused_when_full(self):
        _make_tables()
        api = _load_handler()
        api.CPU_OVERCOMMIT_RATIO = 2.0
        api.MEM_OVERCOMMIT_RATIO = 1.5
        api.MAX_VMS_PER_HOST = 0
        # 15 * 2.0 = 30 allocatable; used 30 → no room for even 1 more.
        h = _host_item(used_vcpu=30)
        api.hosts_table.put_item(Item=h)
        assert api._reserve_host_slot(h, 1, 2048) is None


class TestConditionExpressionArithmeticIsRejected:
    """Guard: the 1.5.8-class bug. Real DynamoDB (via moto) rejects arithmetic
    in a ConditionExpression — so if _reserve_host_slot ever regresses to
    'used_vcpu + :v <= :cap', a moto test fails offline instead of in prod."""

    @mock_aws
    def test_arithmetic_in_condition_expression_raises(self):
        _make_tables()
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.Table("openclaw-hosts")
        t.put_item(Item=_host_item())
        # Real AWS raises ValidationException; moto's expression parser raises
        # ValueError. Either way the invalid arithmetic is REJECTED at the
        # engine layer — which is the whole point (a bare MagicMock accepts it).
        with pytest.raises((ClientError, ValueError)) as ei:
            t.update_item(
                Key={"instance_id": "i-1"},
                UpdateExpression="SET used_vcpu = used_vcpu + :v",
                ConditionExpression="used_vcpu + :v <= :cap",  # invalid on real DDB
                ExpressionAttributeValues={":v": 1, ":cap": 30},
            )
        if isinstance(ei.value, ClientError):
            assert ei.value.response["Error"]["Code"] == "ValidationException"
