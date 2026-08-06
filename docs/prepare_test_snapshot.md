# Prepare a deliberately unhealthy canary snapshot

This runbook creates a negative-test image version for the current live/canary
slot model. It replaces the pre-`#394` procedure that overwrote a host's live
scripts and expected an automatic rollback.

> **Important**
>
> Use an isolated test account, assets bucket, and host. Do not run this against
> a production assets bucket. Snapshot the relevant EBS volumes and verify the
> snapshots are `available` before any destructive cleanup.

The authoritative API behavior is
[`api/pull-image-api.md`](api/pull-image-api.md).

## Expected behavior

A negative canary test must prove:

1. The candidate is stored as a separate version snapshot.
2. `pull-image?slot=canary` does not change the host's live slot or host status.
3. A tenant pinned to the candidate fails its health criterion.
4. The failed candidate is not promoted.
5. Existing live tenants continue running.
6. A later canary pull can replace the failed candidate; unreferenced files are
   removed only through `reclaim-images`.

There is no automatic "install broken files to live and roll back" step in the
current model.

## Prerequisites

- A dedicated test deployment and test host.
- An assets bucket with Amazon S3 Versioning enabled.
- A known-good live image and a separately built unhealthy candidate.
- Control plane credentials with operator access for snapshot/pull/create and
  admin access for promote/reclaim.
- The current `snapshot_time` and image-slot state recorded before the test.

Use placeholders:

```bash
export API="https://<api-id>.execute-api.<region>.amazonaws.com/v1"
export API_KEY="<x-api-key>"
export TOKEN="<operator-or-admin-id-token>"
export HOST_ID="i-0123456789abcdef0"
export REGION="<region>"
export ASSETS_BUCKET="<assets-bucket>"
```

The examples omit response parsing. Use structured JSON parsing in automation.

## 1. Build an unhealthy candidate

Create the candidate through the normal image build pipeline. Change only a
test-owned health signal, for example a test service that intentionally fails
to become ready. Do not poison `deployment/scripts/launch-vm.sh` in the shared
assets bucket; that script is host-wide and is not isolated by the image slot.

Publish the candidate without moving the global live manifest. Verify:

- the candidate objects have distinct Amazon S3 VersionIds;
- the current live manifest is unchanged;
- a fresh production host cannot select the candidate by default.

## 2. Create a version snapshot

The label must be non-empty and match `^[A-Za-z0-9._-]{1,128}$`.

```bash
curl -fsS -X POST "$API/create-image-snapshot" \
  -H "x-api-key: $API_KEY" \
  -H "Authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"label":"canary-negative-health"}'
```

Record the returned `snapshot_time`. The control plane does not derive an empty
label; `{"label":""}` returns `400 VALIDATION`.

The repository script is an equivalent operator path:

```text
scripts/snapshot-version.sh <ASSETS_BUCKET> <REGION> canary-negative-health
```

Do not place credentials in the command line.

## 3. Pull to the canary slot

```bash
curl -fsS -X POST \
  "$API/hosts/$HOST_ID/pull-image?snapshot_time=<snapshot-time>&slot=canary" \
  -H "x-api-key: $API_KEY" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: <uuid>"
```

Poll the returned `job_id`:

```bash
curl -fsS \
  "$API/hosts/$HOST_ID/pull-image-progress?job_id=<job-id>" \
  -H "x-api-key: $API_KEY" \
  -H "Authorization: Bearer $TOKEN"
```

Require `state=SUCCEEDED` before creating a canary tenant. Also query
`GET /hosts/{id}/image-slots` and verify:

- `canary` equals the candidate snapshot;
- `live` is unchanged;
- the host remains available for existing live tenants.

## 4. Create a pinned canary tenant

Use the exact `snapshot_time` returned by the successful pull:

```json
{
  "name": "canary-negative-check",
  "client_token": "canary-negative-check-1",
  "preferred_host_id": "i-0123456789abcdef0",
  "image_channel": "canary",
  "expected_image_snapshot_time": "<snapshot-time>"
}
```

Poll `GET /tenants/{id}`. The test passes only when the selected negative health
condition is observed while existing live tenants remain healthy. A launch or
health failure must not trigger `promote-canary`.

## 5. Recovery

Pull a known-good candidate into `slot=canary`, create a new pinned test tenant,
and require `status=running` plus the selected application-health criterion.
Promote only after that evidence is bound to the expected snapshot:

```json
{
  "expected_canary_snapshot_time": "<verified-good-snapshot-time>"
}
```

Send this body to `POST /hosts/{id}/promote-canary` with an idempotency key.
If the canary changed after verification, expect `409 CANARY_CHANGED`.

Rollback is not a separate API. Pull a retained older snapshot into `slot=live`.

## 6. Cleanup

Delete the test tenant through the normal tenant API. Before deleting a version
snapshot, verify that no host live/canary/previous-live slot, pinned tenant, or
nonterminal image job references it. The control plane returns
`409 IMAGE_VERSION_IN_USE` when a reference exists.

Use `POST /hosts/{id}/reclaim-images` only after the protected reference set is
complete. Do not manually remove version directories from the host.

Record the base commit, snapshot times, host slot state before/after, job IDs,
tenant ID, health evidence, commands, exit codes, timestamps, and cleanup state.
Do not record account IDs, host coordinates, tokens, or private endpoints in
committed evidence.
