# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""routes/skills_groups — skill/group CRUD 端点(handler-split #132)。

路由层:参数解析+调数据源+响应包装。函数体逐字搬。依赖 core.clients(groups_table/s3)+
core.utils(_resp/_NAME_RE)。专属常量 _SKILL_NAME_RE/_SKILL_MAX_BYTES 随迁。
"""
import base64
import hashlib
import json
import re
import os
import shlex
import core.ddb_scan as ddb_scan  # #432 —— Scan 必须翻页
from core.clients import groups_table, hosts_table, s3, ssm
from core.utils import _resp, _NAME_RE

def list_groups():
    """GET /groups — list all groups."""
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured (1.3.x deployment?)"})
    try:
        # 全表字节数算,表一长这里就静默只返第一页。
        return _resp(200, {"groups": ddb_scan.scan_all(groups_table)})
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


def _dispatch_shared_skill_sync(bucket):
    """Install the version-aware syncer on existing hosts and run it once."""
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    try:
        hosts = ddb_scan.scan_all(
            hosts_table,
            FilterExpression="#s IN (:a, :i)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":a": "active", ":i": "idle"},
        )
    except Exception as exc:
        print(f"[skills] could not enumerate hosts for shared-skill sync: {exc}")
        return {"status": "dispatch_failed", "hosts": 0}
    instance_ids = [
        host.get("instance_id")
        for host in hosts
        if host.get("instance_id") and not str(host["instance_id"]).startswith("__")
    ]
    if not instance_ids:
        return {"status": "no_active_hosts", "hosts": 0}
    q = shlex.quote
    sync_command = (
        "/usr/bin/python3 /opt/openclaw/sync-shared-skills.py "
        f"--bucket {q(bucket)} --region {q(region)}"
    )
    script = "\n".join(
        [
            "set -eu",
            "tmp=$(mktemp /opt/openclaw/.sync-shared-skills.py.XXXXXX)",
            (
                f"aws s3 cp s3://{q(bucket)}/deployment/scripts/"
                f"sync-shared-skills.py \"$tmp\" --region {q(region)} --no-progress"
            ),
            'install -o root -g root -m 0755 "$tmp" /opt/openclaw/sync-shared-skills.py',
            'rm -f "$tmp"',
            "mkdir -p /data/shared-skills /var/lib/openclaw",
            (
                "printf '%s\\n' "
                + q(
                    f"*/5 * * * * root {sync_command} "
                    ">>/var/log/openclaw-skills-sync.log 2>&1"
                )
                + " > /etc/cron.d/openclaw-skills-sync"
            ),
            "chmod 0644 /etc/cron.d/openclaw-skills-sync",
            sync_command,
            "chown -R ubuntu:ubuntu /data/shared-skills",
        ]
    )
    command_ids = []
    accepted_hosts = 0
    for offset in range(0, len(instance_ids), 50):
        batch = instance_ids[offset : offset + 50]
        try:
            response = ssm.send_command(
                InstanceIds=batch,
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [script], "executionTimeout": ["300"]},
            )
            command_id = (response.get("Command") or {}).get("CommandId")
            if not command_id:
                raise RuntimeError("SSM response had no command id")
            command_ids.append(command_id)
            accepted_hosts += len(batch)
        except Exception as exc:
            print(f"[skills] shared-skill sync dispatch failed: {exc}")
    status = "accepted" if accepted_hosts == len(instance_ids) else "partial"
    if not command_ids:
        status = "dispatch_failed"
    result = {
        "status": status,
        "hosts": len(instance_ids),
        "accepted_hosts": accepted_hosts,
        "command_ids": command_ids,
    }
    if len(command_ids) == 1:
        result["command_id"] = command_ids[0]
    return result


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
        content_bytes = content.encode("utf-8")
        sha256 = hashlib.sha256(content_bytes).hexdigest()
        put_result = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content_bytes,
            ContentType="text/markdown; charset=utf-8",
            Metadata={"sha256": sha256},
            ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode("ascii"),
        )
        if not isinstance(put_result, dict):
            put_result = {}
        sync = _dispatch_shared_skill_sync(bucket)
        return _resp(
            200 if existed else 201,
            {
                "name": name,
                "size": len(content_bytes),
                "created": not existed,
                "version_id": put_result.get("VersionId"),
                "etag": str(put_result.get("ETag") or "").strip('"') or None,
                "sha256": sha256,
                "sync": sync,
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
        return _resp(
            200,
            {
                "name": name,
                "deleted": len(keys),
                "sync": _dispatch_shared_skill_sync(bucket),
            },
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})
