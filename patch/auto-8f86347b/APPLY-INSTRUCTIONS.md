# auto-8f86347b — apply by reading, no stack update

Every step below is an AWS CLI call. No step triggers a CloudFormation stack update and none
may be added: this environment was deployed once from the CDK app and then changed by hand, so
a later stack update would overwrite those changes.

| | |
|---|---|
| `base_sha` | `d681b0a033a765c1589d1e752faa95ca7dd401b5` — the previous kit's own `patch_sha`, so there is no gap and nothing is packaged twice |
| `patch_sha` | `8f86347bc8f06b207c74107745717d2757b49588` |
| CloudFormation closure | `NOT_APPLICABLE` — no CDK source changed in this range |
| Anchor for every assertion | the `patch_sha256` this kit's `manifest.json` records |

There is no closure, so the expected value for every check is the sha256 of the file this kit
ships. For a pure file replacement that is the stronger anchor: it is what the operator can
see, and the self-test refuses to run if a shipped file and its recorded hash disagree.

## What changed

- **f-657** — From the bb commit subject(s) that touched `deploy/lambda/api/services/egress_admin_service.py` in this range: feat(#657): [BUG] POST /hosts/egress 的 wait=true 回收不全仍返 200/ok=true,与 rollback 的判定不同源(应为 207). The authoritative description is the issue itself; this kit's job is to deliver the resulting file bytes and to prove they arrived.

## Step 0 — coordinates and the self-test

```bash
export AWS_REGION="$OC_REGION"
aws sts get-caller-identity --output json     # confirm the account
export OC_RUN_ID="auto-8f86347b-$(date -u +%Y%m%d-%H%M%S)"
export OC_WORK_DIR="/tmp/oc-work-$OC_RUN_ID"  # deliberately OUTSIDE the kit
export OC_RECEIPT_FILE="/tmp/oc-receipt-$OC_RUN_ID.txt"
python3 lib/selftest.py
```

`OC_WORK_DIR` must be outside the kit: a previous kit wrote its run state inside itself and
then failed its own validator. `OC_RUN_ID` binds the rollback anchor to this run — a rollback
with a different value refuses rather than restoring a stale attempt.

## Operations

### `lambda-api-code` — `services/egress_admin_service.py`

Source: `deploy/lambda/api/services/egress_admin_service.py`. Shipped as `lambda/api/services/egress_admin_service.py`.

```bash
OC_RUN_ID="$OC_RUN_ID" OPENCLAW_API_FN="$OPENCLAW_API_FN" OPENCLAW_API_ALIAS="$OPENCLAW_API_ALIAS" BACKUP_S3_BUCKET="$BACKUP_S3_BUCKET" BACKUP_S3_KEY="$BACKUP_S3_KEY" bash lib/apply.sh lambda-api-code apply "$AWS_REGION"
OC_RUN_ID="$OC_RUN_ID" OPENCLAW_API_FN="$OPENCLAW_API_FN" OPENCLAW_API_ALIAS="$OPENCLAW_API_ALIAS" bash lib/apply.sh lambda-api-code verify "$AWS_REGION"
```

Rollback:

```bash
OC_RUN_ID="$OC_RUN_ID" OPENCLAW_API_FN="$OPENCLAW_API_FN" OPENCLAW_API_ALIAS="$OPENCLAW_API_ALIAS" bash lib/apply.sh lambda-api-code rollback "$AWS_REGION"
```

The replacement is an overlay: the live package is downloaded, asserted against the
function's declared `CodeSha256`, and exactly one entry is replaced. The entry set is
asserted unchanged afterwards — deleting a directory and copying this kit's file in
would drop every sibling module the function imports.

`BACKUP_S3_BUCKET` must have versioning enabled: the unwind restores `$LATEST` from a
pinned version id, and a mutable key cannot be pinned. The operation downloads the
backup and asserts it holds the code running now.

The version is published **last**, because a version snapshots code and configuration
at publish time. Both paths move: the API Gateway invokes the alias while the dispatch
event-source mapping binds `$LATEST`.

## Verifications

Run all 2 in `manifest.json`. One is `B-lifecycle` and calls the control plane
through its real route: a set made only of read-only checks passes during a total outage.

## Rollback summary

| Operation | Rollback | Leaves behind |
|---|---|---|
| `lambda-api-code` | restores the recorded state and reads it back | the published version (immutable, unreferenced) |

Any failure mid-apply unwinds automatically, in reverse order, and raises if an undo fails —
an incomplete unwind is reported as such rather than as a clean rollback.
