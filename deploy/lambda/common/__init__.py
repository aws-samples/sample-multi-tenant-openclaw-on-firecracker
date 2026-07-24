# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared helpers for the OpenClaw Lambdas (T3-3).

Every control-plane Lambda (api, health_check, scaler, backup) had drifting
copies of the same primitives — DDB scan pagination, capacity math, SSM
command helpers, audit-row writers, ALB rule management. This package holds one
authoritative copy of each so a fix (or a bug) lives in exactly one place.

Design rule: functions here take their boto3 client/table as an explicit first
argument and never hold module-level AWS clients. Each Lambda keeps its own
module-level clients (which the test suite monkeypatches) and passes them in via
thin same-name wrappers, so no existing test patch point moves.

The package is copied into each Lambda's asset bundle at synth time (see
stack.py `_stage_lambda_asset`), so at runtime `from common import ...` resolves
next to the handler; tests add deploy/lambda to sys.path (see conftest.py).
"""
