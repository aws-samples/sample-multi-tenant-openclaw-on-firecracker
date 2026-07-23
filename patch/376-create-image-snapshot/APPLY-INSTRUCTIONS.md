# APPLY — patch 376-create-image-snapshot

`POST /create-image-snapshot` control-plane endpoint (the API equivalent of
`scripts/snapshot-version.sh`): scan `deployment/` in the assets bucket for every current
object's `{path, s3_version_id, etag}`, write one snapshot row (`snapshot_time` PK) into the
version-snapshots table, so an operator can take a snapshot from the console and then pull it
onto hosts. This patch reaches the running system with **no stack redeploy** — four moving parts:

| Part                                                    | Layer         | How it lands                                                                 |
| ------------------------------------------------------- | ------------- | ---------------------------------------------------------------------------- |
| `handler.py` route + `host_service.py` logic            | C-lambda      | Lambda code overlay onto the live `openclaw-api` function                    |
| DynamoDB `PutItem` grant on the version-snapshots table | D-cdk (iam)   | one inline role policy via CLI                                               |
| `POST /create-image-snapshot` API route                 | D-cdk (apigw) | clone auth+integration from live `GET /images` via `lib/apply-api-routes.sh` |
| console "Take snapshot" button                          | deploy-other  | replace two web files at the console origin                                  |

`base_sha 85faaaf`, `patch_sha 12ad3e5`. This patch is **READY** (every operation is AUTO_CLI).
Do **not** run a stack deploy — that would overwrite whatever the customer changed by hand.

Set once:

```bash
REGION=<the deployment region, e.g. ap-southeast-1>
PDIR=<abs path to this patch dir>
```

---

## Step 0 · DISCOVER (read-only) — resolve the live coordinates

```bash
bash "$PDIR/lib/discover-env.sh" "$REGION" | tee "$PDIR/environment.json"
```

It must resolve, and you must confirm two hard gates before touching anything:

- the control-plane REST API id + stage (the API that owns `GET /images`), and
- the `openclaw-api` Lambda function name + its live alias (the alias API Gateway invokes).

Also capture, from the stack outputs / DynamoDB, the version-snapshots table name + ARN and the
`ASSETS_BUCKET` the `openclaw-api` function already has in its env (this endpoint reuses that env
var; the patch does **not** add or change any Lambda env var — `params_changed` is empty).

```bash
API=<rest-api-id from discover>
STAGE=<stage from discover>
FN=<openclaw-api function name>
ALIAS=<live alias, e.g. live>
SNAP_TABLE_ARN=<version-snapshots table ARN>
ROLE=<the openclaw-api execution role name>
```

---

## Step 1 · BACKUP (so every op below is reversible)

```bash
mkdir -p "$PDIR/.backup"
# live Lambda zip (for RESTORE rollback of the code overlay)
aws lambda get-function --function-name "$FN" --region "$REGION" \
  --query 'Code.Location' --output text | xargs curl -s -o "$PDIR/.backup/openclaw-api.live.zip"
aws lambda get-function-configuration --function-name "$FN" --region "$REGION" \
  > "$PDIR/.backup/openclaw-api.config.json"
# current alias target + $LATEST version, and the current API stage deployment id
aws lambda get-alias --function-name "$FN" --name "$ALIAS" --region "$REGION" \
  > "$PDIR/.backup/alias.$ALIAS.json"
aws apigateway get-stage --rest-api-id "$API" --stage-name "$STAGE" --region "$REGION" \
  > "$PDIR/.backup/stage.$STAGE.json"
# current console web objects — capture before replacing (see Step 5)
```

---

## Step 1.5 · HASH GATE (the source you overlay must be exactly patch_sha)

The overlay must ship the patch's own vetted source, not drift. For each shipped file, the sha256
of `lib/`/`lambda/` artifact must equal the `patch_sha256` in `manifest.json` (validate-patch.sh
already checks this against `git show 12ad3e5:<path>` — re-run it here if in doubt):

```bash
bash <repo>/.claude/skills/claw-patch-skill/scripts/validate-patch.sh "$PDIR" <src-repo>
```

---

## Step 2 · IAM — grant DynamoDB PutItem (AUTO_CLI, rollback RETAIN)

`create_image_snapshot` needs `dynamodb:PutItem` on the version-snapshots table. The stack change
is `version_snapshots_table.grant(api_fn, "dynamodb:PutItem")` — minimal, no Delete/Update. Apply
it as one inline policy on the live execution role:

```bash
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name claw-patch-376-snapshot-putitem \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"dynamodb:PutItem\",\"Resource\":\"$SNAP_TABLE_ARN\"}]}" \
  --region "$REGION"
```

Rollback is RETAIN: a lone extra `PutItem` grant is harmless, and removing it would break the
endpoint if the code is still live. Only remove it if the whole patch is being backed out:
`aws iam delete-role-policy --role-name "$ROLE" --policy-name claw-patch-376-snapshot-putitem`.

Apply IAM **before** the code overlay so the first live call already has permission.

---

## Step 3 · Lambda code overlay (AUTO_CLI, rollback RESTORE)

**Surgical overlay — replace ONLY the two files #376 changed.** This patch touches exactly
`handler.py` and `services/host_service.py`; the kit ships only those two. The live zip carries
the customer's current first-party code (tenant_service, dispatch_service, other services that
may have drifted or been hot-fixed independently). We start from the live zip and overwrite only
the two patched files in place — never `rm -rf` a whole tree and re-lay a snapshot, which would
silently revert every unrelated live file to this patch's base commit.

Before overwriting, assert each target's live bytes equal the patch's recorded `base_sha256`
(`manifest.json`), so we only apply onto the exact code this patch was built against. A mismatch
means the live host drifted — STOP and re-cut the patch against the drifted base, do not blindly
overwrite:

```bash
WORK="$(mktemp -d)"; cd "$WORK"
unzip -q "$PDIR/.backup/openclaw-api.live.zip" -d live

# hash gate: live target bytes must equal manifest base_sha256 (else the host drifted -> STOP)
python3 - "$PDIR/manifest.json" "$WORK/live" <<'PY'
import hashlib, json, sys, pathlib
manifest, live_root = json.load(open(sys.argv[1])), pathlib.Path(sys.argv[2])
# map: live path inside the zip  ->  recorded base_sha256
targets = {"handler.py": None, "services/host_service.py": None}
for repo_path, meta in manifest["paths"].items():
    art = meta.get("artifact") or ""
    for rel in list(targets):
        if art.endswith("lambda/api/" + rel):
            targets[rel] = meta["base_sha256"]
bad = []
for rel, want in targets.items():
    f = live_root / rel
    if want is None or not f.exists():
        bad.append(f"{rel}: missing target or base hash"); continue
    got = hashlib.sha256(f.read_bytes()).hexdigest()
    if got != want:
        bad.append(f"{rel}: live={got} != base={want}")
if bad:
    sys.stderr.write("DRIFT — refusing overlay:\n  " + "\n  ".join(bad) + "\n"); sys.exit(1)
print("hash gate OK: both live targets match manifest base_sha256")
PY

# overwrite ONLY the two patched files in place; every other live file is untouched
cp "$PDIR/lambda/api/handler.py"               live/handler.py
cp "$PDIR/lambda/api/services/host_service.py" live/services/host_service.py
cd live && zip -qr "$WORK/openclaw-api.patched.zip" . && cd "$WORK"
```

Capture the live function's `RevisionId` at backup time (Step 1) and pass it to
`update-function-code` as `--revision-id`, so a concurrent deploy between our backup and our push
makes the call fail loudly instead of clobbering someone else's change (optimistic concurrency):

```bash
REV="$(jq -r .RevisionId "$PDIR/.backup/openclaw-api.config.json")"
aws lambda update-function-code --function-name "$FN" \
  --zip-file "fileb://$WORK/openclaw-api.patched.zip" --region "$REGION" --publish \
  --revision-id "$REV" \
  --query 'Version' --output text | tee "$WORK/new_version.txt"
# on RevisionId conflict the CLI errors (PreconditionFailed) -> re-run Step 1 backup + this step
```

`--publish` updates `$LATEST` **and** cuts a new numbered version. Point the alias API Gateway
invokes at that new version, and keep `$LATEST` current too, so both the alias-bound API route and
any `$LATEST`-bound SQS event source see the new code:

```bash
NEWV="$(cat "$WORK/new_version.txt")"
aws lambda update-alias --function-name "$FN" --name "$ALIAS" \
  --function-version "$NEWV" --region "$REGION"
```

Rollback RESTORE: redeploy the backed-up zip and flip the alias back:

```bash
aws lambda update-function-code --function-name "$FN" \
  --zip-file "fileb://$PDIR/.backup/openclaw-api.live.zip" --region "$REGION" --publish
aws lambda update-alias --function-name "$FN" --name "$ALIAS" \
  --function-version "$(jq -r .FunctionVersion "$PDIR/.backup/alias.$ALIAS.json")" --region "$REGION"
```

---

## Step 4 · API Gateway route (AUTO_CLI, rollback RESTORE)

Add `POST /create-image-snapshot` by cloning method auth + AWS_PROXY integration from the live
`GET /images` (same key_required, operator+, same Lambda alias URI). The tool is gated (you type
`APPLY`), records a rollback state file, and re-verifies after deploying:

```bash
bash "$PDIR/lib/apply-api-routes.sh" plan   "$API" "$STAGE" "$REGION"   # dry inspect
bash "$PDIR/lib/apply-api-routes.sh" apply  "$API" "$STAGE" "$REGION"   # gated create + deploy + verify
```

Rollback: `bash "$PDIR/lib/apply-api-routes.sh" rollback "$API" "$STAGE" "$REGION"` restores the
prior stage deployment and deletes only the resource it created.

---

## Step 5 · console web (deploy-other, rollback RESTORE)

Replace the two console web files at the console origin (the same S3 object / host path
`init-host.sh` installs the console to — resolve it from the deployment, do not guess), then
invalidate the console cache. Back up the current objects first for RESTORE.

```bash
# example when the console is served from an S3 origin:
aws s3 cp "$PDIR/lib/console-web/index.html"      s3://<console-web-bucket>/index.html      --region "$REGION"
aws s3 cp "$PDIR/lib/console-web/js/app.hosts.js" s3://<console-web-bucket>/js/app.hosts.js --region "$REGION"
# then invalidate the CloudFront distribution that fronts the console
```

---

## Step 6 · VERIFICATION (run all — falsifiable; each has a hard signal)

Resolve the invoke URL, an operator-scoped api-key, and a viewer-scoped key (a key WITHOUT the
operator role — needed for the auth-boundary check). Also resolve the version-snapshots table name
so writes can be asserted against the datastore, not only the HTTP body:

```bash
API_URL="https://$API.execute-api.$REGION.amazonaws.com/$STAGE"
OPERATOR_KEY=<an operator+ api-key value>
VIEWER_KEY=<a viewer-only api-key value (no operator role)>
SNAP_TABLE=<version-snapshots table name from discover>
```

- **v-376-created** — take a snapshot (happy path):

  ```bash
  curl -s -o /tmp/snap.json -w '%{http_code}\n' -X POST "$API_URL/create-image-snapshot" \
    -H "x-api-key: $OPERATOR_KEY" -H 'content-type: application/json' -d '{}'
  cat /tmp/snap.json
  ```

  PASS: `200` and body `file_count > 0` and `snapshot_time` matches
  `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`.
  FAIL: `403` (route auth wrong / key not operator+), or `500 EMPTY_SNAPSHOT` (Step 2 grant
  missing or `ASSETS_BUCKET`/`deployment/` prefix wrong), or `503 NOT_CONFIGURED`.

- **v-376-listed** — the new snapshot shows up in the list the console reads, AND is really in the
  table (datastore assertion, not just the list route):

  ```bash
  SNAP_TS="$(python3 -c 'import json;print(json.load(open("/tmp/snap.json"))["snapshot_time"])')"
  curl -s "$API_URL/list_image_versions" -H "x-api-key: $OPERATOR_KEY" \
    | python3 -c 'import sys,json; print([r["snapshot_time"] for r in json.load(sys.stdin)][:3])'
  aws dynamodb get-item --table-name "$SNAP_TABLE" --region "$REGION" \
    --key "{\"snapshot_time\":{\"S\":\"$SNAP_TS\"}}" --query 'Item.snapshot_time.S' --output text
  ```

  PASS: `SNAP_TS` is present in the list (newest-first) AND `get-item` returns exactly `SNAP_TS`.

- **v-376-viewer-denied** — a viewer-scoped key cannot take a snapshot (auth boundary):

  ```bash
  curl -s -o /tmp/vd.json -w '%{http_code}\n' -X POST "$API_URL/create-image-snapshot" \
    -H "x-api-key: $VIEWER_KEY" -H 'content-type: application/json' -d '{}'
  ```

  PASS: `403` (route is operator+; a viewer is refused).
  FAIL: `200`/`500` (a non-operator reached the write path — auth clone from GET /images is wrong).

- **v-376-label-reject** — the label whitelist rejects shell metacharacters before any write, and
  NO row is created:

  ```bash
  BEFORE="$(aws dynamodb scan --table-name "$SNAP_TABLE" --region "$REGION" --select COUNT --query Count --output text)"
  BAD_LABEL='bad; touch /tmp/x'   # shell metacharacters; must be rejected pre-write
  curl -s -o /tmp/bad.json -w '%{http_code}\n' -X POST "$API_URL/create-image-snapshot" \
    -H "x-api-key: $OPERATOR_KEY" -H 'content-type: application/json' \
    --data-binary "{\"label\":\"$BAD_LABEL\"}"
  cat /tmp/bad.json
  AFTER="$(aws dynamodb scan --table-name "$SNAP_TABLE" --region "$REGION" --select COUNT --query Count --output text)"
  echo "row count before=$BEFORE after=$AFTER"
  ```

  PASS: `400` with body `code == VALIDATION` AND `AFTER == BEFORE` (no row written).
  FAIL: `200`, or the row count grew (an unsanitized label was accepted and stored).

- **v-376-empty-scan** (optional, fault-injection) — when `deployment/` is empty the endpoint fails
  loud instead of writing a hollow snapshot. Run only on a scratch env where the assets bucket's
  `deployment/` prefix can be emptied (do NOT run against a live prefix): with an empty prefix,
  `POST {}` must return `500` with body `code == EMPTY_SNAPSHOT` and add no row.

- **v-376-collision** (optional, fault-injection) — the conditional put is idempotent under a
  same-second collision. Fire two `POST {}` within the same wall-clock second (the PK is second-
  resolution `snapshot_time`); exactly one must return `200` and the other `409` (code `CONFLICT`),
  and the table must gain exactly one row for that `snapshot_time`, never a silent overwrite.

---

## Step 7 · teardown (only if backing the patch out)

Reverse order: `apply-api-routes.sh rollback` → restore console web objects + invalidate →
restore Lambda zip + alias → (optionally) `delete-role-policy`. Confirm `GET /list_image_versions`
still works and the console loads afterward.
