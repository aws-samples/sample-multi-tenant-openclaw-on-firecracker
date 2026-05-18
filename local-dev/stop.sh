#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail
cd "$(dirname "$0")"
docker compose down -v
echo "✓ local-dev stack torn down"
