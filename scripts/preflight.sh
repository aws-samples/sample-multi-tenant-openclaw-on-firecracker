#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Preflight checks for setup.sh — fail fast with a clear message instead of
# dying mid-deploy on a missing tool or bad region. Sourced by setup.sh with
# REGION + PROFILE already set. Run standalone: ./scripts/preflight.sh <region> <profile>
set -euo pipefail

REGION="${1:-${REGION:?preflight: REGION not set}}"
PROFILE="${2:-${PROFILE:?preflight: PROFILE not set}}"

fail() { echo "❌ preflight: $1" >&2; exit 1; }
ok()   { echo "  ✓ $1"; }

echo "→ Preflight checks (region=$REGION profile=$PROFILE)"

# Required CLIs. docker is mandatory since v1.5.0 — CDK bundles the api Lambda's
# cryptography wheel in a container; without it cdk deploy dies with an opaque
# bundling error rather than a clear cause.
for bin in aws python3 docker; do
  command -v "$bin" >/dev/null 2>&1 || fail "'$bin' not found on PATH."
done
# cdk is the Node CLI (npm `aws-cdk`), resolved from global PATH — it can't live
# in .venv (that's the Python side: `aws_cdk` lib, loaded by deploy/app.py).
command -v cdk >/dev/null 2>&1 \
  || fail "'cdk' (AWS CDK CLI) not found on PATH. It's a Node package — install with: npm i -g aws-cdk"
ok "required tools present (aws, cdk, python3, docker)"

# Docker daemon must be running, not just installed.
docker info >/dev/null 2>&1 || fail "Docker is installed but the daemon isn't reachable — start Docker and retry."
ok "docker daemon reachable"

# AWS credentials resolve for this profile.
CALLER=$(aws sts get-caller-identity --profile "$PROFILE" --region "$REGION" \
  --query 'Arn' --output text 2>/dev/null) \
  || fail "AWS credentials for profile '$PROFILE' don't resolve (aws sts get-caller-identity failed). Check ~/.aws or SSO login."
ok "AWS identity: $CALLER"

# CDK bootstrap present in this account/region (CDKToolkit stack). Without it
# cdk deploy fails late with 'This stack uses assets, please run cdk bootstrap'.
aws cloudformation describe-stacks --stack-name CDKToolkit \
  --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1 \
  || fail "CDK not bootstrapped in $REGION — run: cdk bootstrap aws://<account>/$REGION --profile $PROFILE"
ok "CDK bootstrapped in $REGION"

# .venv present AND its Python has aws_cdk. setup.sh runs `PATH=".venv/bin:$PATH"
# cdk deploy`, so the CDK CLI forks .venv/bin/python3 to run deploy/app.py — a
# .venv missing aws_cdk fails late with ModuleNotFoundError mid-deploy, not here.
[ -x .venv/bin/python3 ] || fail ".venv not found — create it and install deps (uv sync / pip install -r) before deploying."
.venv/bin/python3 -c "import aws_cdk" 2>/dev/null \
  || fail ".venv exists but 'aws_cdk' isn't installed in it — run: uv sync (or pip install -r requirements.txt)"
ok ".venv present with aws_cdk"

echo "✓ Preflight passed"
