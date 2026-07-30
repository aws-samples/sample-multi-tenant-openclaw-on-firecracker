# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""routes/skills_groups — skill/group CRUD 端点(handler-split #132)。

路由层:参数解析+调数据源+响应包装。函数体逐字搬。依赖 core.clients(groups_table/s3)+
core.utils(_resp/_NAME_RE)。专属常量 _SKILL_NAME_RE/_SKILL_MAX_BYTES 随迁。
"""
import json
import re
import os
from core.clients import groups_table, s3
from core.utils import _resp, _NAME_RE

def list_groups():
    """GET /groups — list all groups."""
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured (1.3.x deployment?)"})
    try:
        resp = groups_table.scan()
        return _resp(200, {"groups": resp.get("Items", [])})
    except Exception as e:
        return _resp(500, {"error": str(e)})
def create_group(body_str):
    """POST /groups — create a new group with optional initial skills.

    Body: {"name": "team-sre", "skills": ["a", "b"], "description": "..."}
    """
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured (1.3.x deployment?)"})
    try:
        body = json.loads(body_str or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON"})
    name = (body.get("name") or "").strip()
    if not name:
        return _resp(400, {"error": "name is required"})
    # Reuse tenant DNS-label rules — group names show up in audit logs and
    # potentially in DNS-related artifacts later, so keep them safe.
    if not _NAME_RE.match(name):
        return _resp(
            400, {"error": "name must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"}
        )
    skills = body.get("skills") or []
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        return _resp(400, {"error": "skills must be a list of strings"})
    description = (body.get("description") or "").strip()
    from datetime import datetime, timezone

    item = {
        "name": name,
        "skills": sorted(set(s for s in skills if s)),
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        groups_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(#n)",
            ExpressionAttributeNames={"#n": "name"},
        )
    except groups_table.meta.client.exceptions.ConditionalCheckFailedException:
        return _resp(409, {"error": f"group '{name}' already exists"})
    except Exception as e:
        return _resp(500, {"error": str(e)})
    return _resp(201, item)
def add_skill_to_group(name, body_str):
    """POST /groups/{name}/skills — append a skill to a group's list (idempotent)."""
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured"})
    try:
        body = json.loads(body_str or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON"})
    skill = (body.get("skill") or "").strip()
    if not skill:
        return _resp(400, {"error": "skill is required"})
    try:
        existing = groups_table.get_item(Key={"name": name}).get("Item")
        if not existing:
            return _resp(404, {"error": f"group '{name}' not found"})
        cur = set(existing.get("skills") or [])
        cur.add(skill)
        groups_table.update_item(
            Key={"name": name},
            UpdateExpression="SET skills = :s",
            ExpressionAttributeValues={":s": sorted(cur)},
        )
        return _resp(200, {"name": name, "skills": sorted(cur)})
    except Exception as e:
        return _resp(500, {"error": str(e)})
def remove_skill_from_group(name, skill):
    """DELETE /groups/{name}/skills/{skill} — remove a skill from a group's list."""
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured"})
    try:
        existing = groups_table.get_item(Key={"name": name}).get("Item")
        if not existing:
            return _resp(404, {"error": f"group '{name}' not found"})
        cur = set(existing.get("skills") or [])
        cur.discard(skill)
        groups_table.update_item(
            Key={"name": name},
            UpdateExpression="SET skills = :s",
            ExpressionAttributeValues={":s": sorted(cur)},
        )
        return _resp(200, {"name": name, "skills": sorted(cur)})
    except Exception as e:
        return _resp(500, {"error": str(e)})
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_SKILL_MAX_BYTES = 256 * 1024  # 256 KiB — generous, an SKILL.md should be tiny
def read_skill(name):
    """GET /skills/{name} — return the SKILL.md content for the editor.

    Returns 404 if the skill does not exist (no SKILL.md under the
    s3://${ASSETS_BUCKET}/skills/{name}/ prefix). Body is text/plain
    in the JSON `content` field so the console can drop it straight
    into a textarea.
    """
    if not _SKILL_NAME_RE.match(name or ""):
        return _resp(400, {"error": "invalid skill name"})
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    key = f"skills/{name}/SKILL.md"
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        content = obj["Body"].read().decode("utf-8", errors="replace")
        return _resp(
            200,
            {
                "name": name,
                "content": content,
                "size": obj.get("ContentLength", len(content)),
                "last_modified": obj.get("LastModified").isoformat()
                if obj.get("LastModified")
                else None,
            },
        )
    except s3.exceptions.NoSuchKey:
        return _resp(404, {"error": f"skill '{name}' not found"})
    except Exception as e:
        # Some SDKs throw a generic ClientError with code "NoSuchKey"
        msg = str(e)
        if "NoSuchKey" in msg or "404" in msg:
            return _resp(404, {"error": f"skill '{name}' not found"})
        return _resp(500, {"error": msg})
def update_skill(name, body_str):
    """PUT /skills/{name} — create or replace the skill's SKILL.md.

    Body: {"content": "<markdown>"}. The content must:
      - be valid UTF-8
      - be ≤ _SKILL_MAX_BYTES
      - contain at least one top-level "# Title" line

    The S3 cron sync on each host picks the new file up within 5 min,
    after which new VMs will receive it at launch.
    """
    if not _SKILL_NAME_RE.match(name or ""):
        return _resp(
            400, {"error": "invalid skill name (lowercase letters, digits, hyphens)"}
        )
    try:
        body = json.loads(body_str or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON body"})
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return _resp(400, {"error": "missing or empty 'content' field"})
    if len(content.encode("utf-8")) > _SKILL_MAX_BYTES:
        return _resp(400, {"error": f"content exceeds {_SKILL_MAX_BYTES} bytes"})
    # Require a top-level Markdown heading so empty stubs don't end up published.
    has_h1 = any(
        ln.lstrip().startswith("# ") and ln.lstrip()[2:].strip()
        for ln in content.splitlines()
    )
    if not has_h1:
        return _resp(
            400,
            {"error": "SKILL.md must contain at least one top-level '# Title' line"},
        )
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    key = f"skills/{name}/SKILL.md"
    try:
        # Detect whether this is a create or replace for a more useful response.
        try:
            s3.head_object(Bucket=bucket, Key=key)
            existed = True
        except Exception:
            existed = False
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        return _resp(
            200 if existed else 201,
            {
                "name": name,
                "size": len(content.encode("utf-8")),
                "created": not existed,
            },
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})
def delete_skill(name):
    """DELETE /skills/{name} — remove the entire skills/{name}/ prefix.

    Removes SKILL.md plus any auxiliary files the operator may have
    uploaded under the skill's prefix (images, sub-docs, etc.).
    Idempotent: 404 if the skill never existed, 200 once it's gone.
    """
    if not _SKILL_NAME_RE.match(name or ""):
        return _resp(400, {"error": "invalid skill name"})
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    prefix = f"skills/{name}/"
    try:
        # List & batch-delete (S3 has no recursive delete)
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append({"Key": obj["Key"]})
        if not keys:
            return _resp(404, {"error": f"skill '{name}' not found"})
        # delete_objects max 1000 per call — way more than we'd ever have
        # in a single skill prefix, but loop defensively anyway.
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i : i + 1000]})
        return _resp(200, {"name": name, "deleted": len(keys)})
    except Exception as e:
        return _resp(500, {"error": str(e)})
