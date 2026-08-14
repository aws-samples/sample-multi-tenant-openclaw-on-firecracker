# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrock as bedrock,
    aws_ssm as ssm,
)
from pathlib import Path


def build_network_vpc(self, ctx):
    """Build network_vpc resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    _api_cfg = getattr(ctx, "_api_cfg", None)
    sec_cfg = getattr(ctx, "sec_cfg", None)

    # 3 档:default_vpc(存量兼容,host 裸公网,不推荐)/ self_managed(自建 /20
    # + 3 公 + 3 私 + 3 NAT)/ imported(客户传 vpc_id + 6 subnet id)。
    # 生产走 self_managed(host 全落私有子网,守 AWS 暴露红线);切档=重建栈。
    # 同一个 ctx.vpc,避免调整 build_* 顺序破坏 guardrail 等后续依赖。

    # 老开关 api.private_api_enabled(布尔位)与新键 api.mode 的迁移。行为:
    #   · mode=edge    → 只建 EDGE(现状,不建 PRIVATE / VPCE)
    #   · mode=private → 建 PRIVATE + VPCE;EDGE 仍建但挂 deny-public resource policy
    #                   (D2 spec 的"EDGE 段 gate 掉"以 resource policy 断公网调用为
    #                    第一步落地——完全移除 EDGE 需重写 lambdas.py 里 ~150 处
    #                    api.root.add_resource/method,拆到 MR3.2 后续 MR)
    #   · mode=both    → 建 PRIVATE + VPCE + EDGE 并存(过渡态,与老 private_api_enabled=true 等价)
    # 冲突处理:mode 与 private_api_enabled 同显式设 → mode 优先 + 打警告;
    # 未设 mode 但设了 private_api_enabled → 派生(true→both, false→edge,向后兼容)。
    _mode_raw = str(_api_cfg.get("mode", "")).strip().lower() if _api_cfg else ""
    _legacy_priv = _api_cfg.get("private_api_enabled", None) if _api_cfg else None
    if _mode_raw:
        if _mode_raw not in ("edge", "private", "both"):
            raise ValueError(
                f"api.mode must be 'edge' | 'private' | 'both', got {_mode_raw!r}"
            )
        _api_mode = _mode_raw
        if _legacy_priv is True and _mode_raw == "edge":
            print(
                "[#212 api.mode] WARNING: api.mode=edge overrides legacy "
                "private_api_enabled=true (drop the legacy key)."
            )
    else:
        # 老开关派生:true → both;false/未设 → edge
        _api_mode = "both" if bool(_legacy_priv) else "edge"
    ctx._api_mode = _api_mode

    # 长期做法:把带外 apply-hardening.sh 建的 Guardrail 挪进 CDK 栈内,拿到 id 后
    # 写 SSM /openclaw/bedrock-guardrail-id,LiteLLM userdata 从 SSM 读(不硬编码
    # 账号特定的 id)。策略定义单一真相源仍是
    # deploy/runtime-config-export/bedrock-guardrail.json —— apply-hardening.sh
    # 和这里的 CfnGuardrail 都从这个 JSON 转换,保证两条路径策略一致。
    #
    # 迁移路径(默认 false 保存量兼容):
    #  ① 存量账号已经带外建过 Guardrail(id 已在 SSM 或硬编码) → 留 false,现状不变。
    #  ② 新账号 / 想统一走 IaC → config.yml 里设 security.guardrail_managed_by_stack: true,
    #    栈内建 Guardrail、写 SSM,LiteLLM userdata 从 SSM 读。同账号已有同名 Guardrail
    #    时 CFN 会冲突(create-guardrail 名字不唯一是异常),运营需先把带外那个改名或删了
    #    再切开关。切开关的运维笔记落 RUNBOOK.md,别静默切。
    _guardrail_ssm_param_name = "/openclaw/bedrock-guardrail-id"
    _guardrail_managed = sec_cfg.get("guardrail_managed_by_stack", False)
    if _guardrail_managed:
        from lib.guardrail_props import build_guardrail_kwargs, summary

        _gr_json = str(
            Path(__file__).resolve().parent.parent
            / "runtime-config-export"
            / "bedrock-guardrail.json"
        )
        _gr_kwargs = build_guardrail_kwargs(_gr_json)
        _gr_stats = summary(_gr_kwargs)
        print(
            f"[#80 guardrail] CfnGuardrail from {_gr_json}: "
            f"topics={_gr_stats['topics']} content_filters={_gr_stats['content_filters']} "
            f"words={_gr_stats['words']} pii={_gr_stats['pii_entities']} "
            f"regexes={_gr_stats['regexes']} grounding={_gr_stats['grounding_filters']}"
        )
        _guardrail = bedrock.CfnGuardrail(self, "OpenClawGuardrail", **_gr_kwargs)
        # id → SSM。LiteLLM userdata / apply-hardening 都可以从这里读,不再硬编码。
        ssm.StringParameter(
            self,
            "BedrockGuardrailIdParam",
            parameter_name=_guardrail_ssm_param_name,
            string_value=_guardrail.attr_guardrail_id,
            description="Bedrock Guardrail id created by stack (#80). "
            "LiteLLM userdata reads this at boot instead of hardcoded id.",
        )
        cdk.CfnOutput(
            self,
            "BedrockGuardrailId",
            value=_guardrail.attr_guardrail_id,
            description="Bedrock Guardrail id (#80 CfnGuardrail managed by stack).",
        )

    # --- Pack onto ctx ---
    ctx._guardrail_ssm_param_name = locals().get("_guardrail_ssm_param_name")
