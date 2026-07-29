#!/usr/bin/env python3
"""Preflight the tenant-query GSI rollout against the deployed DynamoDB table."""

import argparse
from pathlib import Path
import sys

import boto3
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

from stacks.tenant_query_rollout import validate_live_rollout  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--table", default="openclaw-tenants")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    session = (
        boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    )
    ddb = session.client("dynamodb", region_name=args.region)
    try:
        table = ddb.describe_table(TableName=args.table)["Table"]
    except ddb.exceptions.ResourceNotFoundException:
        table = {"GlobalSecondaryIndexes": []}
    missing = validate_live_rollout(cfg, table.get("GlobalSecondaryIndexes", []))
    if missing:
        print(f"OK: this deployment creates {next(iter(missing))}")
    else:
        print("OK: no tenant-query GSI creation required")


if __name__ == "__main__":
    main()
