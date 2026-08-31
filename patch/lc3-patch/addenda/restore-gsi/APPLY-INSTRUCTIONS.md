# Restore occupancy GSI addendum

This optional addendum removes the strongly consistent full-table
`openclaw-tenants` Scan from `phys_occupied_pairs`.

It is intentionally separate from the parent `lc3-patch` manifest. The parent kit is an
immutable historical range. This addendum applies only when the deployed
`core/scheduling.py` matches `base_sha256` in this directory's `manifest.json`.

## Preconditions

1. `gsi_host` and `gsi_status` both exist, are `ACTIVE`, and are not backfilling.
2. Existing physical slots are represented by an ACTIVE GSI row or a host `ps_*` claim.
3. Take a local ZIP backup and a published-version rollback anchor for every function changed.
4. Apply the file to both `openclaw-api` and `openclaw-lifecycle-consumer` when both functions
   run the shared API package.
5. Keep `TENANT_QUERY_ENABLED=false` during the code upload. Flip it only after byte-level
   verification and a canary.

Read-only GSI check:

```bash
aws dynamodb describe-table \
  --table-name openclaw-tenants \
  --query 'Table.GlobalSecondaryIndexes[?IndexName==`gsi_host` || IndexName==`gsi_status`].{Name:IndexName,Status:IndexStatus,Backfilling:Backfilling}'
```

Both rows must report `ACTIVE`; `Backfilling` must be absent or false.

## Apply

For each target function:

1. Download and retain its current ZIP.
2. Unzip it into a temporary directory.
3. Verify `core/scheduling.py` equals the manifest `base_sha256`.
4. Replace only that file with `lambda/api/core/scheduling.py` from this addendum.
5. Re-zip the complete original package.
6. Update function code using the current `RevisionId`.
7. Verify every ZIP member except `core/scheduling.py` is byte-identical.

Do not rebuild dependencies, replace the environment map, or change handler, layer, memory,
timeout, concurrency, alias, or event-source mapping as part of this file overlay.

## Activate and verify

Activate one execution path at a time by setting `TENANT_QUERY_ENABLED=true`.

Required assertions:

- `phys_scan_pages=0` on the indexed path.
- no `phys_occupied_pairs ... read failed` log;
- no DynamoDB `ValidationException`;
- one create canary reaches `running`;
- one suspend/restore canary returns to `running`;
- base-table `ReadThrottleEvents` remains zero.

The query-page counter is separate from Scan pages. A non-zero query-page count is expected.

## Rollback

Restore the backed-up ZIP. Restore the original environment map if the activation flag changed.
Verify the restored `CodeSha256` and function version/alias target.
