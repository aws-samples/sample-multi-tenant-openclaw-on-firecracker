"""Rebuild and atomically publish the control-plane tenant statistics snapshot."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os

import boto3


REGISTERED_STATUSES = (
    "creating",
    "pending",
    "running",
    "stopped",
    "paused",
    "migrating",
    "deleting",
    "deleted",
    "failed",
    "requires_intervention",
    "failover_recovering",
    "failover_blocked",
    "failover_failed",
    "failover_failed_partial",
)
ABNORMAL = {
    "failed",
    "requires_intervention",
    "failover_blocked",
    "failover_failed",
    "failover_failed_partial",
}
INTERNAL_PREFIXES = ("activename#", "inflight#", "__")
SEGMENTS = int(os.environ.get("STATS_SCAN_SEGMENTS", "8"))
ddb = boto3.resource("dynamodb")
tenants = ddb.Table(os.environ["TENANTS_TABLE"])
snapshots = ddb.Table(os.environ["TENANT_STATS_TABLE"])
s3 = boto3.client("s3")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tenant(item):
    item_id = str(item.get("id", ""))
    return bool(item_id) and not item_id.startswith(INTERNAL_PREFIXES)


def _scan_segment(segment):
    items = []
    kwargs = {
        "Segment": segment,
        "TotalSegments": SEGMENTS,
        "ConsistentRead": True,
        "ProjectionExpression": "id, #s, host_id, rootfs_version",
        "ExpressionAttributeNames": {"#s": "status"},
    }
    while True:
        out = tenants.scan(**kwargs)
        items.extend(out.get("Items", []))
        key = out.get("LastEvaluatedKey")
        if not key:
            return items
        kwargs["ExclusiveStartKey"] = key


def _target_version(previous):
    try:
        obj = s3.get_object(
            Bucket=os.environ["ASSETS_BUCKET"],
            Key=f"{os.environ['ROOTFS_PREFIX'].rstrip('/')}/manifest.json",
        )
        value = json.loads(obj["Body"].read())["version"]
        if not isinstance(value, str) or not value:
            raise ValueError("manifest version is empty")
        return value, False
    except Exception:
        old = (previous or {}).get("target_version")
        return old, True


def aggregate(items, target_version):
    rows = [item for item in items if _tenant(item)]
    statuses = Counter(item.get("status") for item in rows)
    active = [item for item in rows if item.get("status") != "deleted"]
    versions = Counter()
    hosts = Counter()
    unknown_version = 0
    overlength = 0
    unassigned = 0
    target_count = 0

    for item in active:
        version = item.get("rootfs_version")
        if not isinstance(version, str) or not version:
            unknown_version += 1
        elif len(version.encode("utf-8")) > 256:
            overlength += 1
        else:
            versions[version] += 1
            if target_version is not None and version == target_version:
                target_count += 1
        host_id = item.get("host_id")
        if host_id:
            hosts[str(host_id)] += 1
        else:
            unassigned += 1

    top_versions = sorted(versions.items(), key=lambda pair: (-pair[1], pair[0]))[:50]
    others = sum(versions.values()) - sum(count for _, count in top_versions)
    pending_upgrade = (
        None
        if target_version is None
        else len(active) - unknown_version - target_count
    )
    return {
        "status_counts": [
            {"status": status, "count": statuses.get(status, 0)}
            for status in REGISTERED_STATUSES
        ],
        "unknown_status_count": sum(
            count
            for status, count in statuses.items()
            if status not in REGISTERED_STATUSES
        ),
        "business": {
            "total": len(active),
            "running": statuses.get("running", 0),
            "pending_upgrade": pending_upgrade,
            "abnormal": sum(statuses.get(status, 0) for status in ABNORMAL),
            "unknown_version": unknown_version,
        },
        "rootfs_distribution": [
            {"version": version, "count": count}
            for version, count in top_versions
        ],
        "rootfs_distribution_truncated": len(versions) > 50,
        "others_count": others,
        "overlength_version_count": overlength,
        "target_version": target_version,
        "per_host_counts": [
            {"host_id": host_id, "count": count}
            for host_id, count in sorted(hosts.items())
        ],
        "unassigned_count": unassigned,
    }


def lambda_handler(event, context):
    data_as_of = _now()
    previous = snapshots.get_item(
        Key={"id": "current"}, ConsistentRead=True
    ).get("Item")
    target_version, target_stale = _target_version(previous)
    with ThreadPoolExecutor(max_workers=SEGMENTS) as pool:
        parts = list(pool.map(_scan_segment, range(SEGMENTS)))
    result = aggregate(
        [item for part in parts for item in part],
        target_version,
    )
    computed_at = _now()
    refreshed_at = _now()
    result.update(
        {
            "id": "current",
            "target_version_stale": target_stale,
            "data_as_of": data_as_of,
            "computed_at": computed_at,
            "refreshed_at": refreshed_at,
        }
    )
    snapshots.put_item(Item=result)
    return {
        "refreshed_at": refreshed_at,
        "active_tenant_count": result["business"]["total"],
    }
