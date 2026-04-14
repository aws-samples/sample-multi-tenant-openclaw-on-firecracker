"""Shared fixtures for all tests."""

import os
import json
import pytest
from unittest.mock import MagicMock

# Default env vars for unit tests (overridden by E2E via .env.deploy)
_DEFAULTS = {
    "TENANTS_TABLE": "openclaw-tenants",
    "HOSTS_TABLE": "openclaw-hosts",
    "ASSETS_BUCKET": "test-bucket",
    "ROOTFS_PREFIX": "deployment/rootfs",
    "BACKUP_PREFIX": "backups",
    "HOST_RESERVED_VCPU": "1",
    "HOST_RESERVED_MEM": "2048",
    "CPU_OVERCOMMIT_RATIO": "2.0",
    "MEM_OVERCOMMIT_RATIO": "1.5",
    "VM_DEFAULT_VCPU": "2",
    "VM_DEFAULT_MEM": "4096",
    "VM_DATA_DISK_MB": "8192",
    "VM_PORT_BASE": "18789",
    "VM_SUBNET_PREFIX": "172.16",
    "ASG_NAME": "openclaw-hosts-asg",
    "ALB_LISTENER_ARN": "arn:aws:elasticloadbalancing:us-east-1:123:listener/app/test/123/456",
    "VPC_ID": "vpc-test",
    "IDLE_TIMEOUT_MINUTES": "10",
    "AWS_DEFAULT_REGION": "us-east-1",
}
for k, v in _DEFAULTS.items():
    os.environ.setdefault(k, v)


def make_ddb_table():
    """Create a mock DynamoDB Table."""
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    table.get_item.return_value = {}
    table.put_item.return_value = {}
    table.update_item.return_value = {}
    table.meta.client.exceptions.ConditionalCheckFailedException = type("CCF", (Exception,), {})
    return table


def load_env_deploy():
    """Load .env.deploy for E2E tests. Returns dict or None if not found."""
    for path in ["../.env.deploy", ".env.deploy",
                  os.path.expanduser("~/Code/openclaw-firecracker-on-ec2/.env.deploy")]:
        if os.path.isfile(path):
            env = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
            return env
    return None
