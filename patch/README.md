# `patch/` — apply-by-reading hotfix kits (no CDK / CloudFormation redeploy)

> **Executor prompt:** _"Apply the patch kit at `patch/<id>/`: read `manifest.json`, then follow
> `APPLY-INSTRUCTIONS.md` top to bottom — every step is a confirmation gate; never run cdk/CFN."_

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

**Root `manifest-*.json` / `push-marker-*.md`** are publish-sync audit logs, not kits — not applied.

**Iron rule:** never `cdk deploy` / `setup.sh` / any CloudFormation redeploy — every stack change
has a manual CLI path in the kit.
