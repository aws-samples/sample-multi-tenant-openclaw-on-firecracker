# Production parity addendum — final check for every environment that applied lc3-patch

This addendum closes the gap between what `lc3-patch` installs and what the production
control plane actually runs as of 2026-09-03. It is separate from the parent manifest on
purpose: the parent kit is an immutable historical range, and it ships an older
`core/create_deadline.py` and sets `HOST_SELECTION_SCORE_FLOOR=0.25`. An environment that
stopped at the parent kit is **not** at the production baseline.

Two deliverables:

1. **Apply** (Steps A–D) — the small set of changes that production made after lc3-patch, each
   declared in `manifest.json` under `config_changes` with before/after values.
2. **Verify** (Step E) — `lib/verify-prod-parity.sh`, a read-only checker that compares the
   target environment with `baseline.json` for H1, H3, H4, H5, H6, H7, H10 and H14, and exits
   non-zero on any FAIL. Run it after this addendum, and again after **every** future kit: it is
   the cross-check that a later patch did not silently overwrite these values.

Every value in `baseline.json` was read back from the running production deployment and
cross-checked against gateway PR #242, which carries the same values in `config.yml.example`.
The IDs (H1 … H14) are the hypothesis numbers from that drift investigation, kept so the
evidence trail stays traceable.

| ID | What production runs | Where it lives | This addendum |
| --- | --- | --- | --- |
| H1 | `core/create_deadline.py` with exec budgets suspend (backup 60, stop-vm 46) / restore (launch-vm 60) / rebuild (backup 60, rebuild-vm 66), queue 74 / 120 / 54; deadline table unchanged (180) | Lambda code, `openclaw-api` + `openclaw-lifecycle-consumer` | Step A overlays the file (`076ea708…` → `980b49d2…`) |
| H2 | backup step budget 60 s equals the host TERM grace, so `backup/handler.py` falls back to its 300 s default and logs `[#565]` | behaviour, no knob | informational only |
| H3 | SSM `/openclaw/lifecycle/deadline-sec/{suspend,restore,restart,start,rebuild}` = 235; create 180; backup/delete 600; `fence-lease-sec` 240 | SSM (runtime carrier, wins over env) | Step B |
| H4 | env `HOST_SELECTION_SCORE_FLOOR=0.39`, `SPREAD_MAX_HOSTS_PER_BATCH=3`, `TENANT_QUERY_ENABLED=true`, `DISPATCH_MODE=ddb`, `CREATE_VIA_QUEUE=false`, `CPU_OVERCOMMIT_RATIO=6.0`, `MEM_OVERCOMMIT_RATIO=2` on api + consumer | Lambda env | Step C sets the floor; the rest is verified |
| H5 | dispatch ESM 25 / batch 30 / window 2; lifecycle ESM 75 / batch 1; consumer reserved concurrency 75 | ESM + function config | Step D |
| H6 | `openclaw-tenants` has five ACTIVE GSIs: `gsi_owner`, `gsi_tenant_user`, `gsi_host`, `gsi_status`, `gsi_rootfs_version` | DynamoDB | verified; creation belongs to `addenda/restore-gsi` |
| H7 | `/openclaw/control-ui-allowed-origins` and `/openclaw/cloudfront-origin` = `*` | SSM | Step B (operator decision, see warning) |
| H10 | S3 `deployment/scripts/{launch-vm.sh,stop-vm.sh,backup-data.sh,host-agent.py,route_ops.py,migrate-vm.sh}` byte-identical to the gateway tree | S3 assets | verified only |
| H14 | host ASG mixed instance types `r8g.metal-24xl`, `m8g.metal-24xl`, `r7g.metal`, `m7g.metal` | ASG | verified only; capacity and suspended processes are reported, not judged |

## Preconditions

- Parent `lc3-patch` applied (Step 2 of the parent finished: the live package has this
  addendum's `base_sha256` for `core/create_deadline.py`). If the live file already equals
  `patch_sha256`, Step A is a no-op — the verifier tells you.
- Manual permission mode in the executor; every command below that writes is run one at a time
  and read back.
- Read-only discovery first: `bash ../../lib/discover-env.sh > environment.json` in the parent
  kit directory, then `export AWS_REGION=<region>`.

## Step A — H1: overlay `core/create_deadline.py` (api + consumer)

Same mechanics as the parent kit's Step 2, driven by **this directory's** `manifest.json`:

```bash
cd patch/lc3-patch/addenda/prod-parity
for FN in openclaw-api openclaw-lifecycle-consumer; do
  aws lambda get-function --function-name "$FN" --region "$AWS_REGION" --query Code.Location --output text \
    | xargs curl -sSL -o "backup-$FN.zip"
  aws lambda get-alias --function-name "$FN" --name live --region "$AWS_REGION" --query FunctionVersion --output text \
    > "backup-$FN.alias-version" 2>/dev/null || echo none > "backup-$FN.alias-version"
  WORK=$(mktemp -d); unzip -q "backup-$FN.zip" -d "$WORK"
  python3 - "$WORK" <<'PY'
import hashlib, json, pathlib, sys
work = pathlib.Path(sys.argv[1]); m = json.load(open("manifest.json"))
v = m["paths"]["deploy/lambda/api/core/create_deadline.py"]
live = work / "core/create_deadline.py"; got = hashlib.sha256(live.read_bytes()).hexdigest()
if got == v["patch_sha256"]: print("already at production bytes — nothing to overlay"); sys.exit(0)
if got != v["base_sha256"]: sys.exit(f"live {got} is neither base nor patch — stop and reconcile")
live.write_bytes(pathlib.Path(v["artifact"]).read_bytes()); print("overlaid core/create_deadline.py")
PY
  (cd "$WORK" && zip -qr "../parity-$FN.zip" .) && mv "$WORK/../parity-$FN.zip" .
  aws lambda update-function-code --function-name "$FN" --region "$AWS_REGION" \
    --zip-file "fileb://parity-$FN.zip" --query '[LastUpdateStatus,CodeSha256]' --output text
  aws lambda wait function-updated --function-name "$FN" --region "$AWS_REGION"
done
# openclaw-api serves traffic through the `live` alias: publish and move it, or the fix is invisible.
aws lambda invoke --function-name openclaw-api --region "$AWS_REGION" \
  --payload '{"httpMethod":"GET","path":"/ping"}' --cli-binary-format raw-in-base64-out /dev/null \
  --query FunctionError --output text                      # must print None
aws lambda update-alias --function-name openclaw-api --name live --region "$AWS_REGION" \
  --function-version "$(aws lambda publish-version --function-name openclaw-api --region "$AWS_REGION" --query Version --output text)"
```

Rollback: redeploy `backup-$FN.zip` to `$LATEST` and point `live` back at
`backup-openclaw-api.alias-version`.

## Step B — H3 / H7: SSM parameters

Read first, write only what differs, keep the before-values:

```bash
for a in suspend restore restart start rebuild; do
  N="/openclaw/lifecycle/deadline-sec/$a"
  CUR=$(aws ssm get-parameter --name "$N" --region "$AWS_REGION" --query Parameter.Value --output text)
  echo "$N before=$CUR" | tee -a ssm-before.txt
  [ "$CUR" = "235" ] || aws ssm put-parameter --name "$N" --type String --value 235 --overwrite --region "$AWS_REGION"
done
aws ssm get-parameter --name /openclaw/lifecycle/fence-lease-sec --region "$AWS_REGION" --query Parameter.Value --output text   # expect 240
```

`create` (180), `backup` and `delete` (600) are not touched. These parameters take effect
within the 60 s in-process cache; no redeploy. **If the environment is later deployed with the
CDK, `config.yml` must carry `lifecycle.deadline_sec` = 235 for the five actions or the deploy
resets them to 180** (see PR #242 `config.yml.example`).

Origins (H7) — this disables tenant control-UI Origin checking for the whole fleet; it is what
production runs, and it is an operator decision. Skip it if the deployment has a fixed front-end
origin:

```bash
for N in /openclaw/control-ui-allowed-origins /openclaw/cloudfront-origin; do
  aws ssm get-parameter --name "$N" --region "$AWS_REGION" --query Parameter.Value --output text | tee -a ssm-before.txt
done
# only if the operator confirms:
# aws ssm put-parameter --name /openclaw/control-ui-allowed-origins --type String --value '*' --overwrite --region "$AWS_REGION"
# aws ssm put-parameter --name /openclaw/cloudfront-origin          --type String --value '*' --overwrite --region "$AWS_REGION"
```

## Step C — H4: `HOST_SELECTION_SCORE_FLOOR` 0.25 → 0.39 (api + consumer)

The parent kit's `lambda-env-spread-and-floor` op writes 0.25. Merge, never replace:

```bash
for FN in openclaw-api openclaw-lifecycle-consumer; do
  aws lambda get-function-configuration --function-name "$FN" --region "$AWS_REGION" --output json > "cfg-$FN.json"
  python3 - "cfg-$FN.json" "env-$FN.json" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1])); env = (cfg.get("Environment") or {}).get("Variables") or {}
assert env, "read back 0 variables — writing now would wipe the environment"
before = env.get("HOST_SELECTION_SCORE_FLOOR"); env["HOST_SELECTION_SCORE_FLOOR"] = "0.39"
json.dump({"Variables": env}, open(sys.argv[2], "w")); print("HOST_SELECTION_SCORE_FLOOR", before, "->", "0.39", "| keys:", len(env))
PY
  REV=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["RevisionId"])' "cfg-$FN.json")
  aws lambda update-function-configuration --function-name "$FN" --region "$AWS_REGION" \
    --environment "file://env-$FN.json" --revision-id "$REV" --query 'Environment.Variables.HOST_SELECTION_SCORE_FLOOR' --output text
  aws lambda wait function-updated --function-name "$FN" --region "$AWS_REGION"
done
```

Then publish + move `live` for `openclaw-api` exactly as in Step A (published versions freeze
their env). `TENANT_QUERY_ENABLED=true` and the GSIs are **not** set here — follow
`addenda/restore-gsi` first; the verifier will keep failing H4/H6 until that is done.

## Step D — H5: ESM concurrency and reserved concurrency

```bash
LC=$(aws lambda list-event-source-mappings --function-name openclaw-lifecycle-consumer --region "$AWS_REGION" \
     --query 'EventSourceMappings[?ends_with(EventSourceArn, `openclaw-lifecycle.fifo`)].[UUID,BatchSize,ScalingConfig.MaximumConcurrency]' --output text)
DP=$(aws lambda list-event-source-mappings --function-name openclaw-api --region "$AWS_REGION" \
     --query 'EventSourceMappings[?ends_with(EventSourceArn, `openclaw-dispatch`)].[UUID,BatchSize,MaximumBatchingWindowInSeconds,ScalingConfig.MaximumConcurrency]' --output text)
echo "lifecycle: $LC"; echo "dispatch: $DP"       # record the before-values
aws lambda put-function-concurrency --function-name openclaw-lifecycle-consumer --region "$AWS_REGION" --reserved-concurrent-executions 75
aws lambda update-event-source-mapping --uuid "$(echo "$LC" | cut -f1)" --region "$AWS_REGION" --scaling-config MaximumConcurrency=75
aws lambda update-event-source-mapping --uuid "$(echo "$DP" | cut -f1)" --region "$AWS_REGION" --scaling-config MaximumConcurrency=25
```

Prerequisite that this addendum cannot verify from the control plane: the host-side SSM agent
must run `Mds.CommandWorkersLimit=20` (gateway `init-host.sh` step1c). 75 consumers against 20
workers is the production trade-off — the surplus queues in front of the agent and that wait
counts against the deadline. Batch sizes (30 / 1) and the 2 s window are left as they are.

## Step E — Final check (run after this addendum, and after every later kit)

```bash
bash lib/verify-prod-parity.sh --region "$AWS_REGION" --gateway-root "$(git rev-parse --show-toplevel)" --report parity-report.json
```

Reading the result:

- `PASS` / `FAIL` per row, grouped by H-id; `RESULT: FAIL` and exit code 1 if any FAIL.
- `WARN H1 api live (vN) package == $LATEST package` — the alias still serves an older
  package; Step A's publish + alias move was skipped.
- `FAIL H4 api live (vN) env HOST_SELECTION_SCORE_FLOOR` while `api $LATEST` passes — same cause.
- `FAIL H6` / `FAIL H4 … TENANT_QUERY_ENABLED` — apply `addenda/restore-gsi`.
- `WARN H10 … not downloaded` — the API function's `ASSETS_BUCKET` env is missing or the caller
  cannot `s3:GetObject`; nothing was written.

Prove the checker before trusting it (no AWS access needed; replays a forensic capture):

```bash
bash lib/selftest.sh <forensic-capture-dir> "$(git rev-parse --show-toplevel)"
```

## Why a later kit can undo this, and what stops it

`lifecycle-op-patch` merged eight `LIFECYCLE_DEADLINE_SEC_*` values into the function env from
constants written inside the kit (180/600) and asserted them — that is how production ended up
with env 180 while SSM says 235. Any kit that carries a literal for one of the keys in
`baseline.json` can do the same to the values above. Rules for kits after this one:

1. No literal for a baseline key inside a kit; read the value from `config.yml` or from the
   target and declare the change in `manifest.json` → `config_changes`.
2. Env writes merge with `--revision-id`; SSM writes create only what is missing; ESM and
   concurrency changes are read-modify-write of the declared field only.
3. Run `lib/verify-prod-parity.sh` **before and after** the kit from a separate session. Any row
   that flips to FAIL and is not listed in that kit's `config_changes` is a regression: roll back.
