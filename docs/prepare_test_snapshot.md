# Prepare a "broken image" test snapshot (#217 version-canary)

## Purpose

Build a snapshot record in DynamoDB `openclaw-version-snapshots` that, when **pulled**
onto a host, installs a **deliberately broken `launch-vm.sh`** — so the canary VM
launch fails and we can watch the version-canary / rollback path do its job. This is a
test fixture, and the procedure leaves the S3-latest `launch-vm.sh` **good** at the end
so no fresh host boot is harmed.

## How the snapshot → pull mechanic works (read this first)

- `scripts/snapshot-version.sh` scans **everything under `deployment/`** in the assets
  bucket and records, per file, the **current-latest** `{path, s3_version_id, etag}`
  into ONE DDB item keyed by `snapshot_time` (ISO8601 UTC). See
  `scripts/snapshot-version.sh:62-72`. → **Whatever is S3-latest at the moment you run
  the script is what gets frozen into the snapshot.**
- `pull-image` (`?snapshot_time=<ISO>`) reads that item and, per file, runs
  `aws s3api get-object --key <path> --version-id <s3_version_id>` and checks the
  returned ETag against the recorded one — `host_service._verify_lines`
  (`deploy/lambda/api/services/host_service.py:463`). Because it pulls **by VersionId**,
  the snapshot keeps pointing at the exact bytes it captured, even after S3-latest moves
  on.
- It installs each file to its **live** location — for
  `deployment/scripts/launch-vm.sh` that is **`/home/ubuntu/launch-vm.sh`**, the exact
  script the scaler / host-agent / `start-all-vms.sh` invoke to boot a tenant VM
  (`host_service._script_live_dest`, line 443).

**Why we snapshot broken, then restore good:** S3 versioning is **Enabled** on this
bucket, so the broken bytes survive as a non-latest version after we overwrite latest
with the good bytes again. The broken snapshot still pulls the broken version by ID
(the test works), while every fresh host boot (`init-host.sh:439`) always gets the good
S3-latest `launch-vm.sh` (zero blast radius).

## `snapshot-version.sh` parameters

```
./scripts/snapshot-version.sh <BUCKET> <REGION> [LABEL] [--profile P]
```

| Pos | Name | Required | Meaning |
| --- | ---- | -------- | ------- |
| `$1` | `BUCKET` | yes | Assets bucket holding `deployment/`, e.g. `openclaw-assets-454394050889`. |
| `$2` | `REGION` | yes | AWS region of the bucket + DDB table, e.g. `ap-southeast-1`. |
| `$3` | `LABEL` | no | Human-readable label stored on the snapshot. If omitted, auto-filled from `deployment/rootfs/manifest.json`'s `version` (e.g. `v1.0`). Pass an explicit label for a test snapshot so it is easy to spot. |
| `$4 $5` | `--profile P` | no | AWS CLI profile. Omit to use the default profile / ambient credentials. |

It writes one item to `openclaw-version-snapshots`: `snapshot_time` (S, ISO8601 UTC
key), `files` (S, JSON of `{path,s3_version_id,etag}`), `file_count` (N), and `label`
(S, if set). It **fails loud** (exit 1) if it collects 0 files — it will not write an
empty snapshot.

### Example command

```bash
./scripts/snapshot-version.sh openclaw-assets-454394050889 ap-southeast-1 canary-broken-launchvm
```

## Procedure — step by step (what was actually run)

Environment: account `454394050889`, region `ap-southeast-1` (the #217 **test** env),
bucket `openclaw-assets-454394050889`, S3 versioning **Enabled**, canary host
`i-0abc123def4567890`.

```bash
export AWS_PAGER="" AWS_REGION=ap-southeast-1
BUCKET=openclaw-assets-454394050889
KEY=deployment/scripts/launch-vm.sh
```

### Step 0 — save the current-latest good bytes (so restore is faithful)

```bash
GOOD_VER=$(aws s3api head-object --bucket "$BUCKET" --key "$KEY" \
  --region ap-southeast-1 --query VersionId --output text)
aws s3api get-object --bucket "$BUCKET" --key "$KEY" --version-id "$GOOD_VER" \
  --region ap-southeast-1 /tmp/launchvm.good.sh >/dev/null
```

**Why:** we will overwrite S3-latest twice. Grabbing the exact good bytes now lets us
put them back byte-for-byte, so the final S3-latest ETag/size equals the original.
(Verified: 59121 bytes, ETag `492347ab9c8ac18b8bf3e9d5032e5e79`.)

### Step 1 — copy the broken fixture to S3 (it becomes S3-latest)

```bash
aws s3 cp /tmp/launchvm.broken.sh "s3://$BUCKET/$KEY" \
  --region ap-southeast-1 --content-type text/x-shellscript
```

**Why:** `snapshot-version.sh` only ever captures the **current-latest** version. To get
the broken bytes into a snapshot, the broken file must be latest at snapshot time. The
fixture prints `FATAL … exit 42` and contains a hard shell syntax error, so any attempt
to run it aborts. → new broken version `dj3GGFBfPRB_Qx2Ya_eFbAH6GGf7Qn_3`, ETag
`160f8ea524ef6d2265876995603aeb23`.

### Step 2 — snapshot the broken state

```bash
./scripts/snapshot-version.sh openclaw-assets-454394050889 ap-southeast-1 canary-broken-launchvm
```

**Why:** freezes the whole `deployment/` tree (46 files) — including the broken
`launch-vm.sh` version — into one DDB row. → `snapshot_time=2026-07-14T18:11:59Z`,
label `canary-broken-launchvm`. This is the row a pull-image test targets to break the
canary.

### Step 3 — restore the good bytes as S3-latest (close the exposure)

```bash
aws s3 cp /tmp/launchvm.good.sh "s3://$BUCKET/$KEY" \
  --region ap-southeast-1 --content-type text/x-shellscript
```

**Why:** immediately return S3-latest to the good script so no fresh host boot / scaler
launch ever pulls the broken one. → new good version
`9dry9wsB_MzO3uPx7W8s7h7FktIbgluB`, ETag `492347ab…` (matches the Step-0 original).

### Step 4 — snapshot the good state (a clean rollback target)

```bash
./scripts/snapshot-version.sh openclaw-assets-454394050889 ap-southeast-1 canary-good-launchvm
```

**Why:** gives a known-good snapshot to pull for recovery, and documents the good
version by ID. → `snapshot_time=2026-07-14T18:13:00Z`, label `canary-good-launchvm`.

### Net effect (two S3 object versions of one key + two snapshot rows)

| snapshot_time | label | launch-vm.sh version → | S3-latest? |
| --- | --- | --- | --- |
| `2026-07-14T18:11:59Z` | `canary-broken-launchvm` | `dj3GGF…` (broken, exit 42) | no |
| `2026-07-14T18:13:00Z` | `canary-good-launchvm` | `9dry9wsB…` (good, 59121 B) | **yes** |

The repo `launch-vm.sh` and the S3-latest end-state are unchanged. We created two S3
versions of `deployment/scripts/launch-vm.sh` (broken, then good-restored) and two DDB
snapshot rows.

## Verify the broken snapshot is wired to break

```bash
# 1) launch-vm.sh entry in the broken snapshot should carry the broken VersionId:
aws dynamodb get-item --table-name openclaw-version-snapshots --region ap-southeast-1 \
  --key '{"snapshot_time":{"S":"2026-07-14T18:11:59Z"}}' \
  --query 'Item.files.S' --output text | tr ',' '\n' | grep -A2 launch-vm.sh
#   -> "s3_version_id":"dj3GGFBfPRB_Qx2Ya_eFbAH6GGf7Qn_3"

# 2) Fetching that exact version returns the poisoned script:
aws s3api get-object --bucket openclaw-assets-454394050889 \
  --key deployment/scripts/launch-vm.sh \
  --version-id dj3GGFBfPRB_Qx2Ya_eFbAH6GGf7Qn_3 \
  --region ap-southeast-1 /tmp/pull-check.sh >/dev/null
head -3 /tmp/pull-check.sh    # -> FATAL / #217 CANARY TEST POISON

# 3) S3-latest is good (fresh boots are safe):
aws s3 cp s3://openclaw-assets-454394050889/deployment/scripts/launch-vm.sh - \
  --region ap-southeast-1 2>/dev/null | head -4   # -> real launch-vm.sh v1.4
```

Then pull the broken snapshot onto the canary host (console "Pull" or API
`?snapshot_time=2026-07-14T18:11:59Z`).

**Expected outcome — rolls back to the previous version, does NOT get stuck** (traced
through `host_service.py`):

1. **Verify passes.** The broken file is a valid S3 object; its recorded etag
   `160f8ea…` matches on `get-object`, so `_verify_lines` (line 463) is satisfied — the
   breakage is in the script's *content*, not its integrity.
2. **Backup taken before install.** `_snapshot_pull_script` runs `_backup_live_lines`
   (line 662) first, copying the current good `/home/ubuntu/launch-vm.sh` into
   `$BK = /data/firecracker-assets/backup-pre-pull/` (line 549).
3. **Broken script installed to live** (line 664). Host stays `upgrading` (status not
   reset yet — line 676 "status stays upgrading; canary next").
4. **Canary launch fails.** `_run_canary` (line 917) creates a canary tenant whose VM
   boot runs the broken `launch-vm.sh` → `exit 42`, VM never comes up →
   `_poll_canary_healthy` never sees `running` and returns `False` (line 914).
5. **Rollback.** `_rollback_live` (line 972 → 993) runs `_restore_backup_script`, which
   `cp -a`'s the backed-up good `launch-vm.sh` back to `/home/ubuntu/launch-vm.sh`
   (line 581) and resets host `status → prev` with `REMOVE upgrading_at` via the
   `snapshot_time is None` branch (line 602; called at line 584) — so the host is
   **not** left stuck in `upgrading` and `snapshot_time` is **not** advanced (the live
   version is the old one). `pull-image` returns
   `502 canary unhealthy; live rolled back to previous version` (line 975).

The backup-before-install in step 2 is what guarantees a rollback source exists; that
is why a crash-on-launch (like this broken `launch-vm.sh`) is caught and reverted rather
than bricking the host. (Blind spot, for context: the canary only proves "a VM boots +
port 18789 answers any HTTP code" — host-agent.py:535 — so a newer version that boots
but is functionally broken could still promote. A crash like ours is firmly in the
caught-and-rolled-back category.)

## Cleanup (after the test)

```bash
# S3-latest is already good (Step 3). Optionally delete the broken test snapshot row:
aws dynamodb delete-item --table-name openclaw-version-snapshots --region ap-southeast-1 \
  --key '{"snapshot_time":{"S":"2026-07-14T18:11:59Z"}}'
# The broken S3 version can stay (versioning keeps it harmlessly) or be aged out by a
# noncurrent-version lifecycle rule. Do NOT delete the good S3-latest.
```

## What is saved in DynamoDB (real example)

One row per snapshot in table `openclaw-version-snapshots`, keyed by `snapshot_time`.
Below is the actual broken snapshot written above (`file_count` = 46; the `files`
attribute is a JSON **string** holding one object per `deployment/` file).

```
snapshot_time : 2026-07-14T18:11:59Z        (HASH key, S)
label         : canary-broken-launchvm      (S)
file_count    : 46                           (N)
files         : (S — JSON string) list of 46 {path, s3_version_id, etag}:
    {"path":"deployment/edge/.busted",           "s3_version_id":"null", "etag":"\"a657d2db…\""}
    {"path":"deployment/edge/install-edge.sh",   "s3_version_id":"null", "etag":"\"6b5d929b…\""}
    ...
    {"path":"deployment/scripts/launch-vm.sh",
     "s3_version_id":"dj3GGFBfPRB_Qx2Ya_eFbAH6GGf7Qn_3",           <-- the BROKEN version
     "etag":"\"160f8ea524ef6d2265876995603aeb23\""}
    ...
```

Field meanings:

- **`snapshot_time`** — ISO8601 UTC, the primary key; also what you pass to
  `pull-image ?snapshot_time=`.
- **`label`** — free-text tag from `$3` (or auto rootfs version).
- **`file_count`** — number of entries in `files` (integrity sanity-check).
- **`files`** — a JSON string (not a DDB list); each entry is:
  - `path` — S3 key under `deployment/`.
  - `s3_version_id` — the exact S3 VersionId to pull (`"null"` for objects that predate
    versioning — still pullable). **This is the field that makes the snapshot point at
    broken vs good bytes.**
  - `etag` — S3 ETag captured at snapshot time; pull-image re-checks it after download
    (`_verify_lines`) so a mismatch (wrong/corrupt bytes) fails before install.

The good snapshot (`2026-07-14T18:13:00Z`) is identical in shape; only the launch-vm.sh
entry differs — `s3_version_id` = `9dry9wsB_MzO3uPx7W8s7h7FktIbgluB`, etag
`492347ab…` (the restored good bytes).
