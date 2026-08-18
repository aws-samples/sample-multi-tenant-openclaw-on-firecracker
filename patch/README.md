# `patch/` — apply-by-reading hotfix kits (no CDK / CloudFormation redeploy)

## Getting started

1. **Clone the gateway branch** (this branch has the kits):

   ```bash
   git clone --branch gateway --single-branch \
     https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker.git
   cd sample-multi-tenant-openclaw-on-firecracker
   git rev-parse HEAD          # record this SHA; every piece of evidence binds to it
   ```

2. **Start Claude Code** in that directory, with credentials for the TARGET environment:

   ```bash
   claude                      # Manual mode (the default): every mutating command asks you first
   ```

   **Do not use `--permission-mode auto` (or `bypassPermissions`) against a production
   environment.** In `auto` mode a classifier approves tool calls instead of you. The executor
   prompt below tells Claude to stop at every side-effecting command, but permission rules are
   enforced by Claude Code, not by the model — an instruction in a prompt does not change what
   Claude Code allows. Manual mode is the only thing that actually holds an irreversible AWS
   write until a human looks at it. Being idempotent does not remove the need for that gate:
   idempotency protects you from running the *same correct* command twice, not from running one
   command against the wrong account, region, or REST API id — and delete operations are
   idempotent too. Admins can hard-disable the mode with `permissions.disableAutoMode` in
   managed settings.

   Two mode choices that are safe and useful:

   - **Read-only discovery first:** `claude --permission-mode plan` while you run Step 0 probes
     and read `manifest.json` / `APPLY-INSTRUCTIONS.md`, then switch to Manual mode to apply.
   - **A disposable/test environment** (not the customer's live one) is the place for
     `--permission-mode auto`.

   If the Step 0 read-only probes are too chatty, do **not** reach for `auto` — `aws` is not in
   Claude Code's built-in read-only command set, so it prompts by default. Instead add narrow
   allow rules with `/permissions` (or `.claude/settings.local.json`) for read verbs only, and
   leave everything that mutates on Manual:

   ```json
   { "permissions": { "allow": [
       "Bash(aws sts get-caller-identity:*)",
       "Bash(aws cloudformation describe-stacks:*)",
       "Bash(aws apigateway get-*:*)",
       "Bash(aws lambda get-*:*)",
       "Bash(aws lambda list-*:*)",
       "Bash(aws autoscaling describe-*:*)",
       "Bash(aws ec2 describe-*:*)",
       "Bash(aws s3api head-object:*)",
       "Bash(aws s3api list-object-versions:*)",
       "Bash(aws ssm describe-instance-information:*)",
       "Bash(aws dynamodb describe-table:*)"
   ] } }
   ```

   Deny rules beat allow rules, so a blanket `Bash(aws *)` deny would also block these — scope
   any deny you add to the exact mutating verbs you want blocked.

3. **Switch to max reasoning: `/effort xhigh`** — applying a patch to production is high-stakes;
   run at the highest reasoning effort so nothing is skimmed.
4. **Paste the executor prompt below** (fill in `<id>`, e.g. `353-secret-ttl-plus-post315-rollup`).

> **Executor prompt (copy verbatim, fill `<id>`):**
> _"Clone `https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker`, switch to
> the `gateway` branch, and record the HEAD SHA. Then start applying the OpenClaw patch kit
> `patch/<id>/` (browse it at
> `https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/tree/gateway/patch/<id>`).
> First read `manifest.json` and
> `APPLY-INSTRUCTIONS.md` fully, then execute it top to bottom. This is a PRODUCTION environment,
> so: before touching any file or resource, BACK IT UP (record the live host script / S3
> object-version / Lambda code + config / DDB item, so every step is reversible). Run Step 0
> probes first and fill in the real values; verify each artifact's SHA-256 == manifest
> `patch_sha256` before use; treat EVERY side-effecting command as a confirmation gate — show it,
> back up, wait for my OK, apply, then verify. Hot-fix running hosts before future-machine
> sources. NEVER run `cdk deploy` / `setup.sh` / any CloudFormation redeploy — use the manual CLI
> the kit gives. If `status != READY`, stop and surface the manual-review ops first. Run every
> falsifiable verification in the manifest before any teardown, and never delete with a wildcard —
> only the exact ids you created. Prove every fix with FRESH evidence from the real entry point:
> run the actual `curl` / CLI call and show the business output (status code plus the response
> field the fix is about). A green log line, `systemctl is-active`, a running process, a
> successful build, or a zero metric is NOT proof. If a probe cannot run, report it as FAIL or
> INCONCLUSIVE — never turn missing evidence into a pass, and tell me which verifications you
> skipped and why."_

Each `patch/<id>/` fixes a **live** deployment in place (it was CDK-provisioned once then
hand-modified — a redeploy would wipe that). Two files are all you read:

- **`manifest.json`** — source of truth: `base_sha`/`patch_sha` (range), `status`
  (`READY`/`MANUAL_REVIEW`/`BLOCKED`), each file's `patch_sha256`, `fixes[]`
  (+ `params_changed`), `verifications[]` (a falsifiable check per fix).
- **`APPLY-INSTRUCTIONS.md`** — the one apply doc, fixed order: probe → hash-verify →
  hot-fix running hosts → future-machine source (S3 + Launch Template) → stack changes as
  manual CLI → verify every fix → precise teardown.

**How:** pick the kit matching what's already applied → verify each artifact's SHA-256 ==
`patch_sha256` → follow the doc, approving each gated command → if `status != READY`, clear
the listed manual ops first. A kit's `lib/` (when present) holds deterministic tooling
(e.g. `apply-lt.sh` for a MIP-safe Launch-Template roll — never hand-wrangle base64).

**Chain (apply in order, each rolls up the prior):** `266` → `311` → `315` (ran on real prod —
the reference shape) → `353` (latest).

## Target-bound generated patches

`114-tenant-stats-hotfix/` generates three target-bound kits for the tenant
statistics backend, API Lambda overlay, and explicit REST API route. It requires
operator-confirmed API coordinates, authenticated `/tenants` and `/hosts`
probes, independent AI review, ordered apply, live HTTP verification, and a
second no-write run. It always rejects `ANY /{proxy+}` as a target. Read its
`CLAUDE.md` before use.

**Root `manifest-*.json` / `push-marker-*.md`** are publish-sync audit logs, not kits — not applied.

**Iron rule:** never `cdk deploy` / `setup.sh` / any CloudFormation redeploy — every stack change
has a manual CLI path in the kit.
