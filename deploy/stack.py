# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import yaml
import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy,
)
from constructs import Construct
from pathlib import Path

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yml").read_text())

# --- Domain modules (issue #87 mechanical split) ---
from types import SimpleNamespace as _NS
from stacks.storage import build_storage
from stacks.lambdas import build_lambdas
from stacks.compute import build_compute
from stacks.network_vpc import build_network_vpc
from stacks.litellm import build_litellm
from stacks.ha_edge import build_ha_edge
from stacks.auth import build_auth
from stacks.observability import build_observability
from stacks.outputs import build_outputs
from stacks.alarms import build_alarms
from stacks.tenant_query_rollout import validate_tenant_query_config


class OpenClawOrchestratorStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── Multi-region naming suffix ──
        # S3 bucket names, IAM instance-profile names, and Cognito domain prefixes
        # are GLOBALLY unique (not per-region). Deploying the same account into a
        # second region collides on these. Append the region to make them distinct,
        # EXCEPT for the original ap-southeast-1 deployment which keeps its bare
        # names so an existing prod stack there is never disturbed by a redeploy.
        _deploy_region = self.node.try_get_context("region") or "us-east-1"
        self._gsuffix = (
            "" if _deploy_region == "ap-southeast-1" else f"-{_deploy_region}"
        )

        # Removal policy: when True, stateful resources (DDB tables, backup/audit
        # buckets) are RETAIN + WORM Object Lock for data safety; when False they
        # are DESTROY + auto-delete so "delete the stack and redeploy" converges
        # cleanly (RETAIN residue otherwise collides on the next create / races the
        # S3 name release). Driven by config `deploy.protect_stateful_resources`;
        # when unset it falls back to the legacy default (ap-southeast-1 = prod =
        # protected) so existing deployers are unaffected. A rebuildable test region
        # (incl. a test ap-southeast-1) sets it false to allow teardown+rebuild.
        _protect = (CFG.get("deploy") or {}).get("protect_stateful_resources")
        _is_prod_region = (
            bool(_protect)
            if _protect is not None
            else _deploy_region == "ap-southeast-1"
        )
        self._stateful_removal = (
            RemovalPolicy.RETAIN if _is_prod_region else RemovalPolicy.DESTROY
        )
        self._auto_delete = not _is_prod_region

        # ╔══════════════════════════════════════════════════════════════════╗
        # ║ 三包归属导航(配合 engineering/03-collaboration/OWNERS.md)         ║
        # ║ 本 __init__ 顺序构建,资源用局部变量交叉引用,暂不物理拆分(拆 con-  ║
        # ║ struct 会改 logical ID → 删库风险,见 work-items/WI-001)。改动时    ║
        # ║ 按下方 [包X] 标记认领自己的段,别动别的包的段;跨段枢纽变量(api_fn/ ║
        # ║ assets_bucket/vpc/host_role/alb 等)改动走 SHARED-FILES-PROTOCOL。  ║
        # ║   [包C 控制面+工程化] DDB/S3/Lambda/API GW/CodeBuild/Cognito/Outputs ║
        # ║   [包B 隔离安全]       HostRole/监控/ASG/userdata/DNS-FW/Wazuh/AgentCore║
        # ║   [包A 数据面]         ALB/CloudFront/CORS                          ║
        # ╚══════════════════════════════════════════════════════════════════╝

        # ╓─── [包C 控制面+工程化] owner=C ── 数据/Lambda/API 控制面 ───────────╖

        # --- Domain resource construction (issue #87 mechanical split) ---
        ctx = _NS()
        ctx.CFG = CFG
        ctx._deploy_region = _deploy_region
        ctx._is_prod_region = _is_prod_region

        validate_tenant_query_config(CFG)
        build_storage(self, ctx)
        build_lambdas(self, ctx)
        build_compute(self, ctx)
        build_network_vpc(self, ctx)
        build_litellm(self, ctx)
        build_ha_edge(self, ctx)
        build_auth(self, ctx)
        # #220 (R9): business alarm set — runs after every other build_* so
        # ctx has every Lambda/api/queue/table needed for CloudWatch metrics.
        build_alarms(self, ctx)
        build_observability(self, ctx)
        build_outputs(self, ctx)
