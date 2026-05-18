#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Bring up the local-dev stack and seed LocalStack with the DDB tables
# the orchestrator expects (issue #24).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then cp .env.example .env; fi
export $(grep -v '^#' .env | xargs)

docker compose up -d localstack host-agent
echo "→ waiting for LocalStack to become healthy..."
for _ in $(seq 1 30); do
  curl -sf "${AWS_ENDPOINT_URL}/_localstack/health" >/dev/null && break
  sleep 2
done

# Seed the orchestrator tables.
aws --endpoint-url "$AWS_ENDPOINT_URL" dynamodb create-table \
  --table-name "$TENANTS_TABLE" \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST 2>/dev/null || true

aws --endpoint-url "$AWS_ENDPOINT_URL" dynamodb create-table \
  --table-name "$HOSTS_TABLE" \
  --attribute-definitions AttributeName=instance_id,AttributeType=S \
  --key-schema AttributeName=instance_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST 2>/dev/null || true

aws --endpoint-url "$AWS_ENDPOINT_URL" s3 mb "s3://$ASSETS_BUCKET" 2>/dev/null || true

echo "✓ local-dev stack is up"
echo "  • LocalStack:    http://localhost:4566"
echo "  • host-agent:    http://localhost:8899/health  /metrics:9090"
echo
echo "Run a one-shot Lambda invocation:"
echo "  docker compose --profile full run --rm api-lambda"
echo
echo "Tear down:  ./stop.sh"
