"""Fail-closed configuration checks for the tenant-query GSI rollout."""

GSI_GATES = (
    ("add_gsi_tenant_user", "gsi_tenant_user"),
    ("add_gsi_tenant_host", "gsi_host"),
    ("add_gsi_tenant_status", "gsi_status"),
    ("add_gsi_tenant_rootfs", "gsi_rootfs_version"),
)


def desired_query_indexes(cfg):
    scaler = cfg.get("scaler", {}) or {}
    return [
        index_name for gate, index_name in GSI_GATES if bool(scaler.get(gate, False))
    ]


def validate_tenant_query_config(cfg):
    """Reject skipped rollout stages and queries enabled ahead of prerequisites."""
    scaler = cfg.get("scaler", {}) or {}
    enabled = [bool(scaler.get(gate, False)) for gate, _ in GSI_GATES]
    seen_disabled = False
    for position, is_enabled in enumerate(enabled):
        if not is_enabled:
            seen_disabled = True
        elif seen_disabled:
            gate = GSI_GATES[position][0]
            raise ValueError(
                f"{gate}=true skips an earlier tenant-query GSI rollout stage"
            )

    query_cfg = cfg.get("tenant_query", {}) or {}
    backfill_complete = bool(query_cfg.get("rootfs_backfill_complete", False))
    if enabled[-1] and not backfill_complete:
        raise ValueError(
            "add_gsi_tenant_rootfs=true requires "
            "tenant_query.rootfs_backfill_complete=true"
        )
    if query_cfg.get("enabled", False) and not all(enabled):
        raise ValueError(
            "tenant_query.enabled=true requires all four cumulative GSI gates"
        )


def validate_live_rollout(cfg, indexes, table_exists=True):
    """Validate the desired config against DescribeTable GSI state."""
    validate_tenant_query_config(cfg)
    desired = set(desired_query_indexes(cfg))
    if not table_exists:
        # CloudFormation creates a brand-new table together with all of its GSIs in a
        # single operation, so DynamoDB's "one GSI per update" limit — the reason the
        return desired
    query_indexes = {index_name for _, index_name in GSI_GATES}
    actual = {
        item["IndexName"]: item["IndexStatus"]
        for item in indexes
        if item.get("IndexName") in query_indexes
    }

    removed = set(actual) - desired
    if removed:
        raise ValueError(
            "config would remove existing tenant-query GSIs: "
            + ", ".join(sorted(removed))
        )

    missing = desired - set(actual)
    if len(missing) > 1:
        raise ValueError(
            "a deployment may create at most one tenant-query GSI; missing: "
            + ", ".join(sorted(missing))
        )

    inactive = sorted(
        name for name in desired & set(actual) if actual[name] != "ACTIVE"
    )
    if inactive:
        raise ValueError("tenant-query GSIs are not ACTIVE: " + ", ".join(inactive))

    if (cfg.get("tenant_query", {}) or {}).get("enabled", False):
        unavailable = query_indexes - {
            name for name, status in actual.items() if status == "ACTIVE"
        }
        if unavailable:
            raise ValueError(
                "tenant_query.enabled=true but GSIs are unavailable: "
                + ", ".join(sorted(unavailable))
            )

    return missing
