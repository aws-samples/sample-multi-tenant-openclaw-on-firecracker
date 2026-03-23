"""AgentCore Gateway tool: hello-world + system info.
Registered as MCP tool via Gateway, available to all OpenClaw agents."""

import json
import os
import platform
from datetime import datetime, timezone


def lambda_handler(event, context):
    """Handle MCP tool calls from AgentCore Gateway."""
    tool_name = event.get("toolName", event.get("name", ""))
    args = event.get("arguments", event.get("input", {}))

    tools = {
        "hello": handle_hello,
        "system_info": handle_system_info,
        "timestamp": handle_timestamp,
    }

    handler = tools.get(tool_name)
    if not handler:
        return {"error": f"Unknown tool: {tool_name}", "available": list(tools.keys())}

    return handler(args)


def handle_hello(args):
    name = args.get("name", "World")
    return {"message": f"Hello, {name}! This response comes from AgentCore Gateway → Lambda."}


def handle_system_info(args):
    return {
        "runtime": "AWS Lambda",
        "python": platform.python_version(),
        "region": os.environ.get("AWS_REGION", "unknown"),
        "function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "unknown"),
    }


def handle_timestamp(args):
    fmt = args.get("format", "iso")
    now = datetime.now(timezone.utc)
    if fmt == "unix":
        return {"timestamp": int(now.timestamp())}
    return {"timestamp": now.isoformat()}
