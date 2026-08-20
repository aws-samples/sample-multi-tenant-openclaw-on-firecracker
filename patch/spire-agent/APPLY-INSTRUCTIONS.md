# Applying Patch `spire-agent` — SPIRE join-broker rollup (no stack update)

`base_sha` `8ca280d4f8a616a6f7e1ef3a48fc8981304942dc` → `patch_sha` `d3ee0c6593596639df9652609015c4c251d4630d`

Everything the operator reads is in this file plus `manifest.json`. There is no other doc.

## Absolute rules

1. **Any CloudFormation stack update is FORBIDDEN** — the infrastructure-as-code CLI's apply
   verb, `setup.sh`, or anything else that triggers a stack update. Not as the primary path,
   not as cleanup, not as a follow-up on the next release. This environment was provisioned
   once from code and then changed by hand; a stack update would overwrite those changes.
   Every change below is a manual AWS CLI equivalent.
2. **Running machines first, then future machines.** Restore or extend service on live hosts
   before touching the Launch Template.
3. **Every side-effecting command is a confirmation gate.** Print it, read it, approve it, then
   run it. Never chain two writes past one approval.
4. **No operation runs before its backup succeeded.** The contract per operation is
   precheck → backup → approval → apply → verify → rollback-ready.
5. **Precise teardown only.** Test tenants and test hosts are removed by their exact recorded
   ids. A prefix glob on a real host is data loss: real tenants live beside yours.

`manifest.json` `status` is **MANUAL_REVIEW**. That is not a comment on quality — it is
mechanical: 10 operations are `MANUAL_CLI_REVIEW` (the Launch-Template edit, plus the 9 guest
files whose only delivery path is a rootfs re-bake). Read those two groups before starting.

---

## Step 0.0 — Prove the shipped artifacts are authentic (do this FIRST)

Run from inside this patch directory. Each artifact must hash to its `patch_sha256`.

```bash
python3 - <<'PY'
import hashlib, json, pathlib
m = json.load(open("manifest.json"))
bad = []
for src, e in m["paths"].items():
    art = e.get("artifact")
    if not art:
        continue
    got = hashlib.sha256(pathlib.Path(art).read_bytes()).hexdigest()
    if got != e["patch_sha256"]:
        bad.append((src, art, e["patch_sha256"], got))
for row in bad:
    print("MISMATCH", *row)
raise SystemExit("STOP: artifact hash mismatch" if bad else 0)
PY
```

Cross-check against the public source (recommended if you have a gateway checkout):

```bash
GW="$GATEWAY_CHECKOUT"    # path to your gateway checkout
git -C "$GW" show d3ee0c6593596639df9652609015c4c251d4630d:deploy/userdata/spire-kit/spire-join-broker.py \
  | shasum -a 256         # must equal the manifest patch_sha256 for that path
```

A mismatch means the patch was re-packaged or truncated in transit. Do not continue.

## Step 0 — Read-only discovery: bind this patch to THIS environment

```bash
bash lib/discover-env.sh "$REGION"   # writes environment.json + a CONFIRM block; makes no writes
```

`discover-env.sh` reports every REST API's routes, method auth, API-key requirement and
resource-policy presence **without declaring a winner**. You must match the URL your deployed
client configuration actually uses and prove one real call from that call site with that
client's auth. For the host ASG it requires a unique host-not-edge identity and reads the
Launch-Template version the ASG **actually pins** — never the floating default, never the
first regex match.

Fill these in from the CONFIRM block before proceeding:

```bash
export REGION=ap-southeast-1                          # your region
export ASSETS_BUCKET=openclaw-assets-000000000000     # your assets bucket
export LT_ID=lt-0000000000000000                      # host launch template id
export OLD_DEFAULT_VERSION=1                          # the version the ASG actually pins
export ASG=openclaw-hosts-asg                         # host ASG name
export API_ID=abcdefghij                              # the confirmed control-plane API id
```

Then decide per fix whether it applies at all:

```bash
# fix-516-spire-broker: is the SSM master switch set? If it is not "true", the first-boot step
# installs nothing and the broker fixes stay inert (the files still land; that is intended).
aws ssm get-parameter --region "$REGION" --name /openclaw/spire-kit/enabled \
  --query Parameter.Value --output text 2>/dev/null || echo "(not set)"

# fix-516-spire-broker prerequisite: the values the setup script requires.
for k in trust-domain spire-server-address registrar-url; do
  printf '%s = ' "$k"
  aws ssm get-parameter --region "$REGION" --name "/openclaw/spire-kit/$k" \
    --query Parameter.Value --output text 2>/dev/null || echo "(not set)"
done
# spire-server-address MUST NOT be a loopback address. The setup script refuses 127.0.0.1,
# localhost and ::1 — but a WRONG non-loopback address (an old IP, another environment's
# hostname) cannot be caught here: the host side stays green and the guest never attests.
# Confirm this value against your SPIRE Server before continuing.

# fix-546 / fix-521: the control-plane function and its current code anchor
aws lambda get-function-configuration --region "$REGION" --function-name openclaw-api \
  --query '[CodeSha256,length(Environment.Variables)]' --output text
```

## Step 1 — Impact assessment (write this down before changing anything)

State, with the values you just pulled: who is affected, the current symptom, the root cause
per fix, and the expected post-fix state. Attach the discovery output. No assessment, no edit.

Two facts specific to this rollup belong in the assessment:

- **`deploy/edge/fluent-bit/install-fluent-bit.sh` has the largest blast radius here.** Host
  first-boot pulls it and aborts init if the fetch fails; the lifecycle hook then times out and
  the ASG judges ABANDON. Uploading a broken object breaks *future* hosts, not running ones.
  Record the current object version id before you overwrite it.
- **The first-boot spire step is fail-open by design.** A failed install leaves
  `/var/lib/openclaw/spire-kit.install-failed` plus a log token, and the host still registers
  and serves tenants. Do not "fix" that by making it fail-closed.

## Step 1.5 — Backup the minimal set (only what this patch replaces) — BEFORE any write

```bash
mkdir -p ./backup && cd ./backup

# (a) S3 objects this patch overwrites — record the CURRENT version id for rollback
aws s3api list-object-versions --bucket "$ASSETS_BUCKET" \
  --prefix deployment/observability/fluent-bit/install-fluent-bit.sh \
  --query 'Versions[?IsLatest==`true`].[Key,VersionId]' --output text --region "$REGION" \
  | tee FB_OLD_VERSION

# The four spire-kit objects are additions, not overwrites — record their absence instead
for f in spire-kit-setup.sh install.sh spire-join-broker.py spire-join-broker.service; do
  aws s3api head-object --bucket "$ASSETS_BUCKET" \
    --key "deployment/scripts/spire-kit/$f" --region "$REGION" 2>&1 | tail -1
done

# (b) Lambda anchor for the overlay: publish a version, then download the live package
aws lambda publish-version --region "$REGION" --function-name openclaw-api \
  --description "pre-spire-agent-patch anchor" --query Version --output text | tee BACKUP_VERSION
aws lambda get-function --region "$REGION" --function-name openclaw-api \
  --query Code.Location --output text | xargs curl -fsS -o backup.zip
aws lambda get-function-configuration --region "$REGION" --function-name openclaw-api \
  --query CodeSha256 --output text | tee BACKUP_CODESHA
aws lambda list-aliases --region "$REGION" --function-name openclaw-api \
  --query 'Aliases[?Name==`live`].FunctionVersion' --output text | tee BACKUP_ALIAS_VERSION

# (c) Launch Template: record the version the ASG pins and its DECODED UserData
aws ec2 describe-launch-template-versions --region "$REGION" --launch-template-id "$LT_ID" \
  --versions "$OLD_DEFAULT_VERSION" \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
  | base64 -d > lt-pinned.userdata
grep -c '{{' lt-pinned.userdata      # expect 0 — the in-service form is already rendered
cd ..
```

That last line matters. The in-service UserData is **already rendered**. The shipped
`launch-template/init-host.sh.patched` is a TEMPLATE carrying placeholder tokens that the
build substitutes at synth time. Packing the template into a new version boots hosts with
literal tokens and they fail. Step 3b grafts only this patch's hunk onto the rendered form.

## Step 2 — Hot-fix the RUNNING machines

This rollup changes nothing a running host needs immediately: the broker installs at first
boot, the Fluent Bit script is consumed at first boot, and the Lambda overlay is handled in
Step 4. **There is no mandatory host hot-fix in this patch.**

If you want the broker on an already-running host without waiting for a replacement, that path
exists and is idempotent (it does not disturb running microVMs). Run it only AFTER Step 3a has
uploaded the objects:

```bash
# Per host, via SSM. Uses the host role — the same role the VM launcher already uses.
sudo install -d /opt/openclaw/spire-kit
for f in spire-kit-setup.sh install.sh spire-join-broker.py spire-join-broker.service; do
  sudo aws s3 cp "s3://$ASSETS_BUCKET/deployment/scripts/spire-kit/$f" \
    "/opt/openclaw/spire-kit/$f" --region "$REGION"
done
sudo OC_REGION="$REGION" bash /opt/openclaw/spire-kit/spire-kit-setup.sh \
  && sudo rm -f /var/lib/openclaw/spire-kit.install-failed
```

Clearing the marker is not optional: the marker means "the most recent attempt failed", and
only the first-boot success path clears it automatically. A hand-repaired host that keeps the
marker alarms forever.

## Step 3 — Fix the FUTURE-machine source (S3, then Launch Template)

### 3a. S3 objects — `AUTO_CLI`, rollback `RESTORE` by version id

Upload to a temporary key, verify the bytes, then promote. Never overwrite in place.

```bash
put() {   # $1=artifact  $2=key
  aws s3 cp "$1" "s3://$ASSETS_BUCKET/$2.new" --region "$REGION"
  aws s3 cp "s3://$ASSETS_BUCKET/$2.new" - --region "$REGION" | shasum -a 256
  aws s3 cp "s3://$ASSETS_BUCKET/$2.new" "s3://$ASSETS_BUCKET/$2" --region "$REGION"
  aws s3 rm "s3://$ASSETS_BUCKET/$2.new" --region "$REGION"
}

# fix-516-spire-broker — WITHOUT these four objects the first-boot step downloads nothing and
# every new host lands with the fail-open marker. This upload is mandatory, not optional.
for f in spire-kit-setup.sh install.sh spire-join-broker.py spire-join-broker.service; do
  put "host-scripts/spire-kit/$f.patched" "deployment/scripts/spire-kit/$f"
done

# fix-245-fb-enum — an overwrite; you recorded the old version id in Step 1.5
put host-scripts/deploy-machine/install-fluent-bit.sh.patched \
    deployment/observability/fluent-bit/install-fluent-bit.sh
```

Compare each printed sha256 to the manifest `patch_sha256` for that path — that is
verification `v-516-s3-objects`. Rollback for the overwrite is an `aws s3api copy-object` whose
copy-source carries the recorded `versionId`.

### 3b. Launch Template — this patch's hunk only. `MANUAL_CLI_REVIEW`, rollback `LT_REVERT`

A NEW Launch-Template version does **not** update the running ASG: the group pins a specific
version and only new instances use a new one. The controlled path is below.

```bash
bash lib/apply-lt.sh pull            # decodes the pinned version's RENDERED UserData
python3 lib/lt-userdata.py graft \
  --rendered ./lt-current.userdata \
  --artifact launch-template/init-host.sh.patched \
  --hunk step4c --out ./lt-next.userdata
grep -c '{{' ./lt-next.userdata      # MUST be 0 before you push
grep -c 'step4c' ./lt-next.userdata  # MUST be 1
bash lib/apply-lt.sh push            # creates a new version from lt-next.userdata
bash lib/apply-lt.sh promote         # points the ASG at it; does NOT touch MinSize
```

Then validate by launching **one** host and watching three signals — never trust a re-bake
blind: no placeholder tokens in the decoded UserData of the new version, the instance registers
into the hosts table, and the ASG lifecycle action is CONTINUE rather than a heartbeat timeout.
That is `v-516-lt-rendered` plus `v-516-new-host-boot`.

Rollback: `bash lib/apply-lt.sh rollback --to "$OLD_DEFAULT_VERSION"`.

### 3c. Guest kit — rootfs re-bake. `MANUAL_CLI_REVIEW`, 9 files

**This half has no automated delivery channel in this repository.** The image build script does
not reference these files, so nothing bakes them into the golden image for you. Without this
sub-step the host mints join tokens that no guest agent consumes: end-to-end identity does not
work even though every host-side check passes.

```bash
# Stage the guest artifacts where your image build machine can reach them
for f in agent.conf.tmpl install-guest-kit.sh shim.env.example \
         spire-agent-system.service spire-agent.service spire-bootstrap.sh \
         spire-header-shim-system.service spire-header-shim.py spire-header-shim.service; do
  aws s3 cp "host-scripts/spire-kit/guest/$f.patched" \
    "s3://$ASSETS_BUCKET/deployment/scripts/spire-kit/guest/$f" --region "$REGION"
done
# On the golden-image build machine: fetch them, then bake with install-guest-kit.sh,
# rebuild the rootfs image, and roll it out through your normal image path.
```

Verification is `v-516-guest-attest`: in a microVM started from the new image the agent unit is
active and its journal records a successful node attestation; on the host,
`install.sh --check-attest` reports that VM as having just attested. Rollback is rebuilding the
image from the previous rootfs snapshot.

## Step 4 — Control-plane Lambda overlay. `AUTO_CLI`, rollback `ALIAS_FLIP`

`fix-546-taint-timestamp` touched one Python module and no dependency manifest, so **reuse the
customer's own package dependencies**. Do not prebuild a zip: that freezes your dependency
versions onto this function, which is an unrequested change and fragile across architectures.

```bash
aws lambda get-function --region "$REGION" --function-name openclaw-api \
  --query Code.Location --output text | xargs curl -fsS -o live.zip
mkdir -p pkg && (cd pkg && unzip -q ../live.zip)
cp lambda/api/core/host_taint.py pkg/core/host_taint.py    # replace ONLY this module
(cd pkg && zip -qr ../new.zip .)
aws lambda update-function-code --region "$REGION" --function-name openclaw-api \
  --zip-file fileb://new.zip
aws lambda wait function-updated --region "$REGION" --function-name openclaw-api
```

Verify with `v-546-code-changed` (CodeSha256 differs from `backup/BACKUP_CODESHA`, and the
environment-variable key count is unchanged — an overlay that moved customer configuration is a
failure), then `v-546-taint-roundtrip`. Flip the alias only after both pass:

```bash
NEW_VERSION=$(aws lambda publish-version --region "$REGION" --function-name openclaw-api \
  --query Version --output text)
aws lambda update-alias --region "$REGION" --function-name openclaw-api \
  --name live --function-version "$NEW_VERSION"
```

Rollback is **both** directions: redeploy `backup/backup.zip` to the unqualified function and
point the alias back at `backup/BACKUP_ALIAS_VERSION`. An alias flip alone leaves the broken
code on the unqualified version, which the queue consumers bind to.

**No stack source changed in this patch.** `manifest.json` records
`cloudformation.status = NOT_APPLICABLE` because the stack sources have zero changes in this
range, so there is no CloudFormation resource to translate and nothing here is review-gated for
topology.

### 4b. Operator-side files (no live resource)

`host-scripts/deploy-machine/{preflight-check.sh,python.sh,oc-consistency.py}.patched` run on
**your** machine, not on any customer resource. Copy them into your checkout at the matching
paths. Note `python.sh` now reads its scanner configuration from `pyproject.toml`; that file is
repository metadata and is **not** shipped by this patch, so a checkout lacking the
corresponding configuration section behaves differently. Verifications:
`v-489-gate-both-directions`, `v-checks-python-scope`, `v-521-field-compare`.

## Step 5 — Post-fix: is a fresh-machine validation needed?

**Yes.** This patch changed the Launch Template (3b) and a first-boot S3 script (3a), so an
already-running host proves nothing about the future-machine path. Launch exactly one new host
on the new Launch-Template version and let it boot clean with no hot-fix applied. Had only the
S3 objects changed, a hot-fixed live host would have been sufficient — that is not the case.

## Step 6 — Guided verification plan (one falsifiable check per fix)

Run every verification in `manifest.json`, gated by phase: all `A-readonly` always, each
`B-lifecycle` once, `B-optional` only when re-verifying a high-risk path. Execute each entry's
`action` and judge strictly by its `pass_when` / `fail_when`.

```bash
python3 - <<'PY'
import json
m = json.load(open("manifest.json"))
for v in sorted(m["verifications"], key=lambda x: (x["phase"], x["id"])):
    print(f"[{v['phase']}] {v['id']}  (fix {v['fix_id']}, timeout {v['timeout_s']}s)")
    print("  action  :", v["action"])
    print("  observe :", v["observable"])
    print("  PASS if :", v["pass_when"])
    print("  FAIL if :", v["fail_when"])
    print("  cleanup :", v.get("cleanup"))
PY
```

"A tenant was created and it is running" is **not** verification — it proves only that code
loaded. Two checks here deserve extra attention because a green-looking system hides their
failure:

- **`v-245-fb-forwarding`** — this patch REMOVED the guard that refused to start a collector
  with no forwarding output configured. "The unit is active" therefore no longer implies "logs
  leave the machine". The check requires a marked test line to arrive downstream. An active
  unit with nothing arriving downstream is exactly the state the removed guard used to block.
- **`v-546-taint-roundtrip`** — the point of the fix is that a non-integral stored value must
  make the field **absent**, not silently truncated. Testing only the happy path passes on the
  old code too, so the check deliberately includes the non-integral case.

Falsifiable invariants to hold across the whole run: no tenant stuck creating; no assignment
recorded failed while its tenant is running; host capacity never over-subscribed.

## Step 7 — Precise teardown (one-to-one, zero wildcards)

Real hosts carry real tenants. A stray glob is data loss.

- Delete only the exact test tenant ids returned at create — loop the recorded list, never a
  prefix pattern. For each, delete with the data-retention flag turned off (the default is a
  soft delete that leaves the disk), poll to deleted, then confirm via SSM that the VM's data
  directory for that exact id is gone and no orphan process remains.
- Remove the one validation host from Step 5 through the controlled path, not by terminating
  the instance out from under the ASG.
- Delete the temporary config copies made for `v-489-gate-both-directions` and the injected
  fixture from `v-checks-python-scope` by exact filename.
- Confirm the real-tenant count is identical before and after the whole run.
