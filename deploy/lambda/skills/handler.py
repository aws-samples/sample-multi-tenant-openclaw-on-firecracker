"""Skills Lambda — manage shared skills in S3."""

import os
import json
import boto3

s3 = boto3.client("s3")
BUCKET = os.environ.get("ASSETS_BUCKET", "")
PREFIX = "skills/"


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "GET" and path == "/skills":
        return list_skills()

    return _resp(404, {"error": "not found"})


def list_skills():
    """List all skills from S3 skills/ prefix."""
    try:
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX, Delimiter="/")
        skills = []
        for cp in resp.get("CommonPrefixes", []):
            name = cp["Prefix"].replace(PREFIX, "").rstrip("/")
            if not name:
                continue
            # Read SKILL.md frontmatter for description
            desc = _read_skill_description(name)
            skills.append({"id": name, "name": name, "description": desc})
        return _resp(200, {"skills": skills})
    except Exception as e:
        return _resp(500, {"error": str(e)})


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
