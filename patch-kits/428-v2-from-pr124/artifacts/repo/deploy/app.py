#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os, sys

sys.path.insert(0, os.path.dirname(__file__))
import yaml
from pathlib import Path
import aws_cdk as cdk
from stack import OpenClawOrchestratorStack
from stacks.image import OpenClawImageStack
from stacks.host_image import OpenClawHostImageStack

app = cdk.App()
region = app.node.try_get_context("region") or "us-east-1"
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=region,
)
orchestrator = OpenClawOrchestratorStack(app, "OpenClawOrchestrator", env=env)

# Golden-image bake in its own stack so a build failure can't roll back the
# orchestrator. Dependency is one-way: OpenClawImage depends on Orchestrator
# (needs the assets bucket to exist first) — Orchestrator NEVER depends on the
# image stack, so a bad build only touches OpenClawImage. _gsuffix mirrors
# stack.py: bare names for the original ap-southeast-1, region-suffixed elsewhere.
_cfg = yaml.safe_load((Path(__file__).parent.parent / "config.yml").read_text())
_gsuffix = "" if region == "ap-southeast-1" else f"-{region}"
image_stack = OpenClawImageStack(
    app, "OpenClawImage", cfg=_cfg, gsuffix=_gsuffix, env=env
)
image_stack.add_dependency(orchestrator)

# Host golden AMI (#389 v2 block 2). Own stack for the same reason as OpenClawImage: a
# failed bake must not roll back the control plane. NO dependency in either direction —
# it needs nothing from the orchestrator (the AWSTOE component carries its scripts
# inline), and the orchestrator must not wait on it, because the LaunchTemplate reads the
# AMI id through resolve:ssm at launch rather than at deploy. Deploy order is therefore
# free: bake first and the ASG picks the image up on its next scale-out.
OpenClawHostImageStack(app, "OpenClawHostImage", cfg=_cfg, gsuffix=_gsuffix, env=env)
app.synth()
