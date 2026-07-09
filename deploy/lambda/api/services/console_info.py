"""services 层 · console_info:控制台只读端点 —— 备份清单 + AgentCore 工具清单。

handler-split #132 T1.8 —— 从 handler.py 逐字搬迁,行为零改动。
依赖方向:services → core(clients/utils),不反向 import handler。
这些是零鉴权的纯读聚合端点(鉴权在 lambda_handler 的 RBAC 前置闸完成)。
"""
import os

from core.clients import s3, tenants_table
from core.utils import _resp


def list_backups(tenant_id):
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{tenant_id}/")
    backups = []
    for obj in sorted(resp.get("Contents", []), key=lambda o: o["Key"], reverse=True):
        name = obj["Key"].rsplit("/", 1)[-1]
        backups.append(
            {
                "key": obj["Key"],
                "timestamp": name.replace(".gz", ""),
                "size_mb": round(obj["Size"] / 1048576, 1),
            }
        )
    return _resp(200, {"tenant_id": tenant_id, "backups": backups})


def list_all_backups():
    """List all backups across all tenants, left-joined with tenants table to mark orphans."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")

    # Build tenant_id → (name, exists) map from DDB (include soft-deleted for name resolution)
    tenants = tenants_table.scan().get("Items", [])
    tenant_info = {
        t["id"]: {"name": t.get("name", ""), "exists": t.get("status") != "deleted"}
        for t in tenants
    }

    # Paginate S3 list to avoid missing objects when > 1000 backups exist
    paginator = s3.get_paginator("list_objects_v2")
    backups = []
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            # Expect: {prefix}/{tenant_id}/{timestamp}.gz
            if len(parts) < 3 or not parts[-1].endswith(".gz"):
                continue
            src_tenant_id = parts[-2]
            timestamp = parts[-1][:-3]  # strip ".gz"
            info = tenant_info.get(src_tenant_id, {"name": None, "exists": False})
            backups.append(
                {
                    "tenant_id": src_tenant_id,
                    "tenant_name": info["name"],
                    "tenant_exists": info["exists"],
                    "timestamp": timestamp,
                    "size_bytes": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )

    backups.sort(key=lambda b: b["last_modified"], reverse=True)
    return _resp(200, backups)


def agentcore_status():
    enabled = os.environ.get("AGENTCORE_ENABLED", "false") == "true"
    gateway_url = os.environ.get("AGENTCORE_GATEWAY_URL", "")
    return _resp(
        200,
        {
            "enabled": enabled,
            "gateway_url": gateway_url if enabled else None,
        },
    )


# ════════════════════════════════════════════════════════════
# AgentCore tools listing (for console display)
# ════════════════════════════════════════════════════════════
#
# When AgentCore Gateway is enabled, three Lambda-backed MCP tools are
# registered (see deploy/stack.py — tools=hello/system_info/timestamp).
# The console wants to surface this list so operators can see what tools
# their VMs get for free without having to read the CDK code. The list is
# static (defined at deploy time), so the response is hard-coded here
# rather than calling out to bedrock-agentcore at request time — which
# would cost a control-plane API call per page load.
#
# If AgentCore is disabled, we return an empty list with a hint so the
# console can render an "AgentCore not enabled" placeholder.

_AGENTCORE_BUILTIN_TOOLS = [
    {
        "name": "hello",
        "description": "Say hello — test tool for verifying AgentCore Gateway connectivity",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name to greet"}},
        },
    },
    {
        "name": "system_info",
        "description": "Get Lambda runtime system information",
        "input_schema": {"type": "object"},
    },
    {
        "name": "timestamp",
        "description": "Get current UTC timestamp",
        "input_schema": {
            "type": "object",
            "properties": {"format": {"type": "string", "description": "iso or unix"}},
        },
    },
]


def agentcore_tools():
    """GET /agentcore/tools — list MCP tools registered with the Gateway.

    Today this is a static list (the tools are defined declaratively in
    stack.py at deploy time). A future PR can replace this with a live
    `bedrock-agentcore.list_targets()` call when the Gateway grows
    user-defined tools.
    """
    enabled = os.environ.get("AGENTCORE_ENABLED", "false") == "true"
    if not enabled:
        return _resp(200, {"enabled": False, "tools": []})
    return _resp(200, {"enabled": True, "tools": _AGENTCORE_BUILTIN_TOOLS})
