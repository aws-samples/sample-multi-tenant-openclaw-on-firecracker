"""Templates Lambda — manage OpenClaw config templates in S3."""

import os
import json
import boto3

s3 = boto3.client("s3")
BUCKET = os.environ.get("ASSETS_BUCKET", "")
PREFIX = "templates/openclaw/"


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_params = event.get("pathParameters") or {}
    name = path_params.get("name", "")

    if method == "GET" and not name:
        return list_templates()
    elif method == "GET" and name:
        return get_template(name)
    elif method == "PUT" and name:
        return put_template(name, event.get("body", ""))
    elif method == "DELETE" and name:
        return delete_template(name)

    return _resp(404, {"error": "not found"})


def list_templates():
    """List all template names with metadata."""
    try:
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX, Delimiter="/")
        templates = []
        for cp in resp.get("CommonPrefixes", []):
            name = cp["Prefix"].replace(PREFIX, "").rstrip("/")
            if not name:
                continue
            # Check if openclaw.json exists
            try:
                meta = s3.head_object(Bucket=BUCKET, Key=f"{PREFIX}{name}/openclaw.json")
                size = meta.get("ContentLength", 0)
                modified = meta.get("LastModified", "").isoformat() if meta.get("LastModified") else ""
            except Exception:
                size = 0
                modified = ""
            templates.append({"name": name, "size": size, "modified": modified})
        return _resp(200, {"templates": templates})
    except Exception as e:
        return _resp(500, {"error": str(e)})


def get_template(name):
    """Get template content."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}{name}/openclaw.json")
        content = obj["Body"].read().decode("utf-8")
        return _resp(200, {"name": name, "content": json.loads(content)})
    except s3.exceptions.NoSuchKey:
        return _resp(404, {"error": f"template '{name}' not found"})
    except Exception as e:
        return _resp(500, {"error": str(e)})


def put_template(name, body):
    """Create or update template."""
    if name == "default":
        return _resp(403, {"error": "cannot modify default template"})
    try:
        content = json.loads(body) if isinstance(body, str) else body
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{PREFIX}{name}/openclaw.json",
            Body=json.dumps(content, indent=2),
            ContentType="application/json",
        )
        return _resp(200, {"name": name, "status": "saved"})
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON"})
    except Exception as e:
        return _resp(500, {"error": str(e)})


def delete_template(name):
    """Delete a template."""
    if name == "default":
        return _resp(403, {"error": "cannot delete default template"})
    try:
        s3.delete_object(Bucket=BUCKET, Key=f"{PREFIX}{name}/openclaw.json")
        return _resp(200, {"name": name, "status": "deleted"})
    except Exception as e:
        return _resp(500, {"error": str(e)})


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }
