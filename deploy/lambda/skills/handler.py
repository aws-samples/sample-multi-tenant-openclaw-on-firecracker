# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Skills Lambda — manage shared skills in S3.

1.4.0 (#62): GET /skills now optionally accepts ?tenant=<id> to filter
the returned skill list to only the skills that tenant would receive
at launch time (per-tenant + group-resolved). Without ?tenant the
endpoint returns the full broadcast catalog (legacy v1.3.x behavior),
which the operator console uses for the "Skills library" view.
"""

import json
import os

import boto3

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")

BUCKET = os.environ.get("ASSETS_BUCKET", "")
PREFIX = "skills/"

# Optional tables — present only on 1.4.0+ deployments.
_tenants_table_name = os.environ.get("TENANTS_TABLE", "")
_groups_table_name = os.environ.get("GROUPS_TABLE", "")
tenants_table = ddb.Table(_tenants_table_name) if _tenants_table_name else None
groups_table = ddb.Table(_groups_table_name) if _groups_table_name else None


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "GET" and path == "/skills":
        qs = event.get("queryStringParameters") or {}
        return list_skills(tenant_filter=qs.get("tenant"))

    return _resp(404, {"error": "not found"})


def list_skills(tenant_filter=None):
    """List all skills from S3 skills/ prefix.

    If tenant_filter is provided, restrict the result to that tenant's
    effective skill set (tenant.skills ∪ group.skills). Tenants without
    any scoping configured see the full catalog (broadcast = legacy).
    Unknown tenants → 404.
    """
    try:
        # Compute the allow-list (None = no filtering, return everything)
        allow_list = None
        if tenant_filter and tenants_table is not None:
            tenant = tenants_table.get_item(Key={"id": tenant_filter}).get("Item")
            if not tenant:
                return _resp(404, {"error": f"tenant not found: {tenant_filter}"})
            allow_list = _resolve_effective_skills(tenant)

        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX, Delimiter="/")
        skills = []
        for cp in resp.get("CommonPrefixes", []):
            name = cp["Prefix"].replace(PREFIX, "").rstrip("/")
            if not name:
                continue
            # Apply per-tenant scoping
            if allow_list is not None and name not in allow_list:
                continue
            desc = _read_skill_description(name)
            skills.append({"id": name, "name": name, "description": desc})
        out = {"skills": skills}
        if tenant_filter:
            out["tenant"] = tenant_filter
            out["scope"] = "broadcast" if allow_list is None else "scoped"
        return _resp(200, out)
    except Exception as e:
        return _resp(500, {"error": str(e)})


def _resolve_effective_skills(tenant_item):
    """Same semantics as api/handler.py::_resolve_effective_skills.
    Kept inline so we don't need to share Lambda layers between the two functions.
    Returns None for "broadcast all", or a sorted list of skill names."""
    if not tenant_item:
        return None
    tenant_skills = tenant_item.get("skills") or []
    group_name = (tenant_item.get("group") or "").strip()
    if not tenant_skills and not group_name:
        return None
    effective = set(s for s in tenant_skills if s)
    if group_name and groups_table is not None:
        try:
            grp = groups_table.get_item(Key={"name": group_name}).get("Item") or {}
            for s in (grp.get("skills") or []):
                if s:
                    effective.add(s)
        except Exception:
            pass
    return sorted(effective) if effective else None


def _read_skill_description(name):
    """Read description from SKILL.md YAML frontmatter."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}{name}/SKILL.md")
        content = obj["Body"].read(4096).decode("utf-8", errors="ignore")
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                frontmatter = content[3:end]
                in_desc = False
                desc_lines = []
                for line in frontmatter.splitlines():
                    if line.strip().startswith("description:"):
                        val = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if val and val != "|" and val != ">":
                            return val
                        in_desc = True
                        continue
                    if in_desc:
                        if line.startswith("  "):
                            desc_lines.append(line.strip())
                        else:
                            break
                if desc_lines:
                    return desc_lines[0]
    except Exception:
        pass
    return ""


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }
