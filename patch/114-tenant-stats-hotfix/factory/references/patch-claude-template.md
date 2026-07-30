# Claude patch executor: {{KIT_ID}}

This file applies only to the patch kit in this directory. The delivery lane is
`{{LANE}}`.

Your job is to complete the patch against the intended environment, not to rewrite
the generated patch. Read `manifest.json`, `APPLY-INSTRUCTIONS.md`, `REVIEW.json`,
and `CLAUDE-REVIEW.txt` before doing anything.

## Hard rules

- Run only the packaged driver under `runtime/scripts/`. Never invoke or edit
  `lib/compiled/*` directly.
- Never edit any reviewed kit file. A changed byte invalidates the review receipt.
- Never run CDK, setup, or CloudFormation deployment commands. This patch updates
  only the resources declared in its manifest.
- Discovery and planning are read-only. Apply only after the account, region, target
  resource, plan, and review receipt all agree.
- Customer `api.mode` is only a hint. Never infer an API target from it. The operator
  must explicitly confirm the REST API ID, stage, client URL, and authentication
  headers file before any write.
- `ANY /{proxy+}` is never a valid target for this patch. The confirmed API must
  expose exact `GET /tenants` and `GET /hosts` resources, and both authenticated
  live probes must return 2xx.
- Backups and state are create-only. Do not replace an existing anchor.
- Do not treat a timeout as proof of failure. Inspect the recorded operation, wait for
  a terminal result, then rerun the same fixed driver so it can resume.
- Do not delete resources or use wildcards. This kit never authorizes destructive
  cleanup.

## Complete workflow

1. Resolve the intended AWS region and customer config. Ask the operator to confirm
   the real explicit-resource REST API coordinates, then export:

   ```bash
   export OC_CONTROL_PLANE_API_ID='<rest-api-id>'
   export OC_CONTROL_PLANE_STAGE='<stage>'
   export OC_CONTROL_PLANE_URL='<https-client-base-url>'
   export OC_CONTROL_PLANE_PROBE_HEADERS_FILE='<absolute-headers-json>'
   export OC_PATCH_HTTP_HEADERS_FILE="$OC_CONTROL_PLANE_PROBE_HEADERS_FILE"
   export OC_PATCH_CUSTOMER_CONFIG='<absolute-config.yml>'
   ```

   Run:

   ```bash
   bash runtime/scripts/discover-env.sh "$REGION" manifest.json ../environment.json
   ```

   Read the full confirmation block. `control_plane_api.confirmed` must be true,
   `entrypoint_kind` must be `explicit-rest-resources`, and the ID/stage must match
   the operator-confirmed URL plus authenticated `/tenants` and `/hosts` probes. A
   name, route-shape guess, config mode, or proxy resource is not enough.

2. Check the independent review receipt:

   ```bash
   python3 runtime/scripts/review-kit.py check .
   ```

   Continue only when the current fingerprint has score at least 6.5 and zero
   blockers.

3. Generate and inspect the read-only plan:

   ```bash
   bash runtime/scripts/patch-plan.sh . ../environment.json
   ```

   Resolve every conflict. Unknowns require an explicit operator decision; never
   silently convert an unknown into approval.

4. Print the complete interview once:

   ```bash
   python3 runtime/scripts/interview-once.py ask .
   ```

   Present the exact API ID, stage, URL, and proxy rejection to the operator. Record
   `Q-api-entrypoint=yes` only after explicit confirmation. Never infer this answer
   from config or discovery.

5. Run the fixed set driver. For one kit:

   ```bash
   bash runtime/scripts/patch-set.sh apply ../environment.json ../answers .
   ```

   For sibling kits, use the first kit's packaged driver and pass every kit in strict
   dependency order. The driver performs preflight, apply, independent verify, and a
   real second run. Completion requires `SET_COMPLETE` and a second receipt with
   `result=SKIP` and `writes=0`.

6. On failure, read the exact exit code and script output before acting:

   - `3`: target identity mismatch.
   - `40`: drift or a resource owned by someone else. Stop for a merge decision.
   - `41`: transient AWS condition. Re-observe, then rerun the same command.
   - `42`: timeout or unclassified failure. Inspect operation status first.
   - `43`: intended live effect is absent.
   - `44`: patch-owned anchor is absent or incomplete.
   - `45`: an explicit adoption opt-in is required.
   - `46`: live state could not be read.
   - `47`: unrelated API Gateway changes are waiting to be deployed.
   - `49`: permissions, invalid parameters, or a non-retryable conflict.
   - `25`: this lane intentionally has no automatic rollback.

   Do not change generated code to make a failure disappear. Fix the environment or
   stop with the evidence.

7. Report the exact kit fingerprint, environment hash, commands, exit codes, receipts,
   and any skipped or unverified checks. A partial set is not complete.
