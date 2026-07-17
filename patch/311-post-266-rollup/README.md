# Patch 311: Post-266 Rollup (deployment + data-plane fixes)

Companion to `patch/266-token-drift-fix/`. Patch 266 fixed gateway-token drift on
the VM recovery path. **This patch bundles every customer-facing fix made after
266** so you can apply them all from one place.

## What this patch fixes

| Symptom | Root cause | Layer |
| ------- | ---------- | ----- |
| VM creation fails with `log: command not found` (rc=127); tenant stuck creating/down | The DDB token-fallback block calls `log()`, but `log()` is defined later in the script. Under `set -e`, the call returns 127 and the script exits — right on the fallback success path. | host script |
| After a rootfs upgrade the VM still runs old code / an upgrade rebuild wipes the data disk / the version number is misreported | `restart` keeps the old overlay (half-new/half-old); a rebuild that hits a data-template size drift would rebuild the disk and lose data; the version was stamped without verifying the VM actually booted the new rootfs. | host script + Lambda |
| On a private-API deployment every route except `/ping` returns 404 | The API handler dispatches by `resource` template, but a private API's `event["resource"]` is always `/{proxy+}`, so nothing matches. | Lambda |
| Host boot hangs at "installing tools + firecracker"; hosts end up ABANDONED with zero healthy hosts | A stack-output lookup queried an output key that CDK had prefixed (so it never matched), burning a 5-minute silent retry loop on every boot. | host script |
| On a clean CDK deploy the host fails to launch VMs with AccessDenied; tenants stuck creating | The token fallback reads `openclaw-tenant-secrets` with the host instance role, but the role was never granted read access to that table. | CDK (IAM) + host script |
| `cdk deploy` rolls back with `private-dns-enabled ... conflicts` on the Secrets Manager VPC endpoint | The stack unconditionally creates a Secrets Manager interface endpoint with private DNS enabled; AWS allows only one such endpoint per service per VPC, so it conflicts when one already exists. | CDK |

## Three application layers

Patch 266 touched only a host script, so it was a plain file replacement. **This
rollup spans three layers** — apply the ones that match how you deploy:

1. **Host scripts (hot-swappable)** — `host-scripts/`: `launch-vm.sh.patched` and
   `init-host.sh.patched`. Copy onto the host and upload to S3 for future hosts.
2. **Lambda code (redeploy the function)** — `lambda/APPLY-LAMBDA.md`: the
   private-API routing fix and the rebuild-semantics fix live in the control-plane
   Lambda; apply via `cdk deploy` or `update-function-code`.
3. **CDK / permissions (deploy, or manual inline policy)** — `cdk/APPLY-CDK.md`
   and `iam/`: the host-role read grant (a fail-closed prerequisite of the token
   fallback) and the Secrets Manager VPC-endpoint toggle.

## Dependency order (must follow — wrong order fails mid-way)

```
1. IAM grant  — do this FIRST; it is a fail-closed prerequisite.
   A CDK deployment grants it automatically; otherwise run iam/apply-iam.sh.
      |
2. Host scripts — replace the files, upload to S3.
      |
3. Lambda code — cdk deploy, or update-function-code.
      |
4. CDK deploy — for the VPC-endpoint toggle. BEFORE deploying, check whether the
   VPC already has a Secrets Manager endpoint; if so set
   logging.aos.create_secretsmanager_vpce: false (see cdk/APPLY-CDK.md).
```

**Simplest path:** after step 1 (or after confirming the grant already exists),
a single `cdk deploy` covers 2–4 (it repackages the Lambda code, updates IAM and
the VPC endpoint, and triggers hosts to pull the new scripts). The per-layer steps
are for when you only want to hot-patch part of it.

## Why the IAM grant must come first

`launch-vm.sh` reads the gateway token from `openclaw-tenant-secrets` on the
recovery path, and **aborts the launch if that read is denied** (fail-closed). If
you replace the host script or rebuild tenants before the host role can read that
table, the VM will not start at all. Grant read access first — either via
`cdk deploy` or `iam/apply-iam.sh`.

## Files

| Path | Purpose |
| ---- | ------- |
| `README.md` | This file: fixes, layers, dependency order |
| `APPLY-INSTRUCTIONS.md` | Step-by-step application guide (IAM first, in dependency order) |
| `host-scripts/launch-vm.sh.patched` | Complete patched launch-vm.sh — direct replacement |
| `host-scripts/init-host.sh.patched` | Complete patched init-host.sh — upload to S3 for future hosts |
| `iam/host-role-tenant-secrets.json` | Inline policy: host role read on tenant-secrets (fill in region/account) |
| `iam/apply-iam.sh` | Idempotently applies the inline policy (for a non-CDK hotfix) |
| `lambda/APPLY-LAMBDA.md` | How to redeploy the API Lambda (private-API routing + rebuild semantics) |
| `cdk/APPLY-CDK.md` | The IAM grant + Secrets Manager VPC-endpoint toggle, incl. conflict handling |

## Verification summary

- Host no longer fails with rc=127, and no longer hangs at the tools/firecracker step.
- The tenant-secrets get-item probe no longer returns AccessDenied.
- `cdk deploy` reaches CREATE_COMPLETE without a VPC-endpoint conflict.
- Private-API routes other than `/ping` work; a rebuild keeps the data disk and
  reports the version truthfully.
- Per-step verification is in each layer's APPLY doc.
