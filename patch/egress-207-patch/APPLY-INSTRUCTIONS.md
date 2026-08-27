# egress-207-patch — apply by reading, no stack update

Two file replacements, both applied with the AWS CLI. No step here triggers a CloudFormation
stack update, and none may be added: this environment was deployed once from the CDK app and then
changed by hand many times, so a later stack update would overwrite those changes.

| | |
|---|---|
| `base_sha` | `a57a292a1edc8a91b24a2a906baeac4c255429bf` — the previous kit's own `patch_sha`, so there is no gap and nothing is packaged twice |
| `patch_sha` | `d5f0d0e42008449080d0c7db417d9b0a6f073d9c` (`bb-baseline: d681b0a033a765c1589d1e752faa95ca7dd401b5`) |
| CloudFormation closure | `NOT_APPLICABLE` — no CDK source changed in this range |
| Expected value for every assertion | the `patch_sha256` this kit's `manifest.json` records for each source path |

Because there is no closure, the anchor for every check is the sha256 of the file **this kit ships**.
For a pure file replacement that is a stronger anchor than a synth artifact: it is the thing the
operator can see, and `lib/selftest_egress_207.py` refuses to run if a shipped file and its recorded
hash disagree.

## What is being fixed

**`POST /hosts/egress` with `wait=true` answered `200` and `ok=true` on a partial reclaim.** A caller
could not tell a complete convergence from a partial one, and the verdict did not come from the same
place the `rollback` path used, so the two could disagree about the same fleet. A partial collection
now answers `207` and carries `expected_host_count`, `missing_hosts` and `collection_incomplete`, with
a `WARNING` in `message` naming how many targeted hosts returned no invocation inside the timeout.

The all-hosts form (`all: true`) deliberately reports **counts without a difference set**. Its
DynamoDB enumeration is a snapshot: it can include terminated or SSM-unmanaged machines and can miss a
newly registered host the tag fan-out does reach. Subtracting it would manufacture a permanent `207`.

**The edge gate's placeholder check was flaky.** The same `nginx.conf` was graded 3/4 and 4/4 across
four bb jobs on 2026-08-26, so it could block an unrelated change and could pass when it should have
failed. Two causes, both removed: an `awk … | grep -qF` pipeline raced under `set -o pipefail` (grep
exits on the first match, awk's next write gets `EPIPE` and dies non-zero, `pipefail` fails the
pipeline, and the `&&` skips the count), and the block boundary was guessed from a four-space closing
brace, which a nested Lua table closes early. It is now a single `awk` process with brace-pair
counting.

## Step 0 — discover, and confirm you are where you think you are

```bash
# Fill the four coordinates from your own deployment before running anything below.
export AWS_REGION="$OC_REGION"                     # the region this deployment lives in
aws sts get-caller-identity --output json          # confirm the account
export OPENCLAW_API_FN=openclaw-api
export OPENCLAW_API_ALIAS=live                     # the alias the API Gateway invokes
export ASSETS_BUCKET="$OC_ASSETS_BUCKET"           # the deployment's assets bucket
export EDGE_ASG="$OC_EDGE_ASG"                     # the edge auto scaling group
export OC_RUN_ID="egress207-$(date -u +%Y%m%d-%H%M%S)"
export OC_WORK_DIR=/tmp/oc-egress-207-$OC_RUN_ID   # deliberately OUTSIDE the kit
export OC_RECEIPT_FILE=/tmp/oc-egress-207-receipt-$OC_RUN_ID.txt
```

`OC_WORK_DIR` must be outside the kit directory. A previous kit wrote its run state inside itself and
then failed its own validator.

`OC_RUN_ID` binds the rollback anchor to this run. `rollback` with a different value refuses rather
than restoring a stale file from an earlier attempt.

Run the self-test first. It makes no AWS calls and it fails if this kit is inconsistent with itself:

```bash
python3 lib/selftest_egress_207.py
```

## Step 1 — the Lambda file (layer C-lambda)

**Overlay, never a prebuilt zip.** This function carries arm64 native wheels. The operation downloads
the live package, asserts it hashes to the function's declared `CodeSha256`, and replaces **only**
`services/egress_admin_service.py`. It then asserts the entry set is unchanged and that exactly one
entry differs. Do not delete `services/` and copy this kit's file in: the kit ships 1 file out of the
24 that directory holds in the deployed package, and the other 23 are imported.

Take the backup first. It must be a **versioned** key: the unwind restores `$LATEST` from a pinned
version id, and a mutable key cannot be pinned. The operation downloads the backup and asserts it
hashes to the code running now — a stale object at that key would otherwise overwrite the one
recoverable copy of the running code.

```bash
# Any bucket in this account with versioning ENABLED. The unwind restores $LATEST from a pinned
# version id, so an unversioned bucket is refused before anything is written.
export BACKUP_S3_BUCKET="$OC_BACKUP_BUCKET"
export BACKUP_S3_KEY="patch-backups/$OC_RUN_ID/openclaw-api-live.zip"

bash lib/apply-egress-207.sh lambda-api-code apply  "$AWS_REGION"
bash lib/apply-egress-207.sh lambda-api-code verify "$AWS_REGION"
```

If `BACKUP_S3_KEY` does not exist yet, apply uploads the live package there itself before mutating
anything.

Order inside apply, and why: code is written, read back, the function is invoked, and the version is
published **last**. A version snapshots code *and* configuration at publish time, so publishing
together with the code (`update-function-code --publish`) would produce a version taken before any
configuration write and the alias would then point at exactly that.

`verify` downloads the deployed package and compares the sha256 of that one file. A healthy invoke is
not evidence that this file reached the function.

Both paths move: the API Gateway invokes the alias while the dispatch event-source mapping binds
`$LATEST`. Setting `OPENCLAW_API_ALIAS` makes apply move the alias too; leaving it unset patches only
the dispatch path.

Rollback:

```bash
bash lib/apply-egress-207.sh lambda-api-code rollback "$AWS_REGION"
```

It restores `$LATEST` from the pinned backup version and moves the alias back. The version apply
published is immutable and stays behind — nothing points at it, so it is inert, but the version list
is one longer than before apply.

## Step 2 — the edge bundle file (layer deploy-other, delivered via S3)

The destination prefix is **discovered**, never supplied: it is content-addressed
(`deployment/bootstrap/edge/<sha256-of-the-rendered-init-script>`), so only the fleet knows which one
it serves. The operation reads the user data of the launch template version the ASG actually pins,
resolves `$Default` to a version number, refuses `$Latest` (the version served changes the instant a
new one is created), and refuses if the user data names zero or more than one candidate prefix.

```bash
bash lib/apply-egress-207.sh edge-bundle apply  "$AWS_REGION"
bash lib/apply-egress-207.sh edge-bundle verify "$AWS_REGION"
```

Overwriting the in-service prefix is deliberate. A new prefix would have to be computed by
synthesizing the CDK app, and would then need a launch-template roll to take effect; this file is read only when the integration suite runs, never on
a request path, so there is no running process to reconcile and no reason to roll the fleet.

The previous object's version id is recorded before the overwrite, and rollback restores it and
compares the restored digest against that version's own:

```bash
bash lib/apply-egress-207.sh edge-bundle rollback "$AWS_REGION"
```

If the bucket does not have versioning enabled, apply refuses **before** writing anything: the unwind
could not put the previous bytes back.

## Step 3 — verifications

Run the four in `manifest.json`. One of them, `v-657-partial-reclaim-207`, is `B-lifecycle`: it
invokes the real function through its real route with one instance id that cannot answer SSM, and
requires `207` with the three new fields and `ok: false`. That is the only check here that would fail
if the function were down; the other three are `A-readonly` and would pass during an outage, which is
why they are not sufficient on their own.

`v-658-gate-is-repeatable` runs the shipped `count_hint_placeholders()` twenty times on one input and
requires twenty identical answers, then requires `0` on a conf with no placeholder and `0` when the
placeholder sits outside the block. A single run cannot distinguish the fixed version from the flaky
one.

## Rollback summary

| Operation | Rollback | Leaves behind |
|---|---|---|
| `lambda-api-code` | `$LATEST` restored from the pinned backup version; alias moved back | the published version (immutable, unreferenced) |
| `edge-bundle` | the object restored to its recorded version, digest compared | nothing |

Any failure mid-apply unwinds automatically, in reverse order, and raises if an undo fails — an
incomplete unwind is reported as such rather than as a clean rollback.
