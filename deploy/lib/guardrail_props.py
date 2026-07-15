# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""把 deploy/runtime-config-export/bedrock-guardrail.json(get-guardrail dump)
转成 CDK CfnGuardrail 的构造 kwargs——纯字典变换,不依赖 CDK/boto3,便于 pytest -m unit。

apply-hardening.sh 已经做过一次同样的 create-guardrail 入参转换(xxxPolicy → xxxPolicyConfig,
内层数组键加 Config 后缀,去只读字段),这里把那份逻辑抽出来一是可测,二是 stack.py
构造 CfnGuardrail 时用同一份真相源(拿不出「apply-hardening 和 CDK 内容不一致」的漂移)。

产出 dict 约定(踩过 JSII 反序列化的坑):
- **顶层键**用 snake_case(`topic_policy_config`)—— CDK Python 层通过 `**kwargs` 展开时
  会做 snake→camel 转换,这是 Python API 边界。
- **嵌套 dict 内部**用 camelCase(`topicsConfig` / `filtersConfig`)—— nested dict 走 jsii
  反序列化,jsii schema 只认 camelCase;传 snake_case 会 `Missing required properties`。

用法:

    kwargs = build_guardrail_kwargs(json_path)
    bedrock.CfnGuardrail(scope, "OpenClawGuardrail", **kwargs)

设计取舍:
- 只处理已经在 export 里出现的字段(topic/content/word/sensitive-info/contextual-grounding)。
  未来 export 新增字段时先在 test_issue80_guardrail_props.py 里加断言,再在 build 里加映射,
  保证「JSON 里有值但 CDK 建的 guardrail 缺一半策略」这类静默丢失被测试卡住。
- tier(tierName=CLASSIC)保留在 topic/content 两处(export 有就带,没有就跳)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def _topics(policy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """topicPolicy → topic_policy_config(顶层 snake), 内嵌 topicsConfig(camel)。"""
    if not policy or not policy.get("topics"):
        return None
    topics = [
        {
            "name": t["name"],
            "definition": t["definition"],
            "examples": t.get("examples", []) or [],
            "type": t.get("type", "DENY"),
        }
        for t in policy["topics"]
    ]
    out: Dict[str, Any] = {"topicsConfig": topics}
    tier = policy.get("tier")
    if tier and tier.get("tierName"):
        out["topicsTierConfig"] = {"tierName": tier["tierName"]}
    return out


def _content(policy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """contentPolicy → content_policy_config(顶层 snake), 内嵌 filtersConfig(camel)。"""
    if not policy or not policy.get("filters"):
        return None
    filters = [
        {
            "type": f["type"],
            "inputStrength": f["inputStrength"],
            "outputStrength": f["outputStrength"],
        }
        for f in policy["filters"]
    ]
    out: Dict[str, Any] = {"filtersConfig": filters}
    tier = policy.get("tier")
    if tier and tier.get("tierName"):
        out["contentFiltersTierConfig"] = {"tierName": tier["tierName"]}
    return out


def _words(policy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """wordPolicy → word_policy_config(顶层 snake), 内嵌 wordsConfig/managedWordListsConfig(camel)。"""
    if not policy:
        return None
    out: Dict[str, Any] = {}
    if policy.get("words"):
        out["wordsConfig"] = [{"text": w["text"]} for w in policy["words"]]
    if policy.get("managedWordLists"):
        out["managedWordListsConfig"] = [
            {"type": m["type"]} for m in policy["managedWordLists"]
        ]
    return out or None


def _pii(policy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """sensitiveInformationPolicy → sensitive_information_policy_config(顶层 snake)。"""
    if not policy:
        return None
    out: Dict[str, Any] = {}
    if policy.get("piiEntities"):
        out["piiEntitiesConfig"] = [
            {"type": p["type"], "action": p["action"]} for p in policy["piiEntities"]
        ]
    if policy.get("regexes"):
        out["regexesConfig"] = [
            {
                "name": r["name"],
                "description": r.get("description", "") or "",
                "pattern": r["pattern"],
                "action": r["action"],
            }
            for r in policy["regexes"]
        ]
    return out or None


def _grounding(policy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """contextualGroundingPolicy → contextual_grounding_policy_config(顶层 snake)。"""
    if not policy or not policy.get("filters"):
        return None
    return {
        "filtersConfig": [
            {"type": f["type"], "threshold": f["threshold"]} for f in policy["filters"]
        ]
    }


def build_guardrail_kwargs(json_path: str | Path) -> Dict[str, Any]:
    """读 bedrock-guardrail.json,产出 CfnGuardrail 构造 kwargs。

    必填(CFN 硬要求): name / blocked_input_messaging / blocked_outputs_messaging。
    可选: 各 policy 段(缺就不传,CfnGuardrail 里都是 Optional)。
    """
    path = Path(json_path)
    if not path.is_file():
        raise FileNotFoundError(f"guardrail export not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        d = json.load(f)

    # description 有 200 char CFN 上限;apply-hardening.sh 同款截断。
    desc = (d.get("description") or "")[:200]
    kwargs: Dict[str, Any] = {
        "name": d["name"],
        "description": desc,
        "blocked_input_messaging": d.get("blockedInputMessaging") or "Blocked.",
        "blocked_outputs_messaging": d.get("blockedOutputsMessaging") or "Blocked.",
    }
    tp = _topics(d.get("topicPolicy"))
    if tp is not None:
        kwargs["topic_policy_config"] = tp
    cp = _content(d.get("contentPolicy"))
    if cp is not None:
        kwargs["content_policy_config"] = cp
    wp = _words(d.get("wordPolicy"))
    if wp is not None:
        kwargs["word_policy_config"] = wp
    pp = _pii(d.get("sensitiveInformationPolicy"))
    if pp is not None:
        kwargs["sensitive_information_policy_config"] = pp
    gp = _grounding(d.get("contextualGroundingPolicy"))
    if gp is not None:
        kwargs["contextual_grounding_policy_config"] = gp
    return kwargs


def summary(kwargs: Dict[str, Any]) -> Dict[str, int]:
    """给 stack.py synth 时打日志用——统计有多少 topic/filter/regex/pii,
    改 JSON 时 CDK 输出直接对得上 export,防「静默丢一半策略」。"""

    def _len(k: str, sub: str) -> int:
        p = kwargs.get(k) or {}
        return len(p.get(sub, []) or [])

    return {
        "topics": _len("topic_policy_config", "topicsConfig"),
        "content_filters": _len("content_policy_config", "filtersConfig"),
        "words": _len("word_policy_config", "wordsConfig"),
        "managed_word_lists": _len("word_policy_config", "managedWordListsConfig"),
        "pii_entities": _len(
            "sensitive_information_policy_config", "piiEntitiesConfig"
        ),
        "regexes": _len("sensitive_information_policy_config", "regexesConfig"),
        "grounding_filters": _len(
            "contextual_grounding_policy_config", "filtersConfig"
        ),
    }
