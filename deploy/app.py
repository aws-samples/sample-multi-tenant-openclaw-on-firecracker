#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import aws_cdk as cdk
from stack import OpenClawOrchestratorStack

app = cdk.App()
region = app.node.try_get_context("region") or "us-east-1"
OpenClawOrchestratorStack(app, "OpenClawOrchestrator",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=region,
    ),
)
app.synth()
