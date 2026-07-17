# Patch 311: Post-266 Rollup (deployment + data-plane fixes)

Companion to `patch/266-token-drift-fix/`. Patch 266 fixed gateway-token drift on the VM
recovery path. **This patch bundles every customer-facing fix made after 266** — covering
`base_sha f7a4d08` → `patch_sha 18e469e` (see `manifest.json`) — so you can apply them all
from one place, **without `cdk deploy`.**

## `cdk deploy` is forbidden on this deployment

This system was CDK-deployed once and then **manually modified on the live system**. Running
`cdk deploy` (or `setup.sh`, which wraps it) now would OVERWRITE those manual changes and
break the running deployment. So this patch has **no cdk-deploy step** — not as a path, not
as a follow-up, not as "later". Every fix, including the ones that originally lived in CDK
stacks (an IAM grant and a VPC endpoint), has a manual AWS CLI equivalent here. Anything with
no safe CLI path is listed as "manual intervention required" for a human — never a deploy.

## What this patch fixes

| Symptom                                                                                                        | Root cause                                                                                                                                                         | Layer                    |
| -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------ |
| VM creation fails with `log: command not found` (rc=127); tenant stuck creating/down                           | DDB token-fallback block calls `log()` before it is defined; under `set -e` the call returns 127 and the script exits on the fallback success path.                | host script (S3)         |
| After a rootfs upgrade the VM still runs old code / a rebuild wipes the data disk / the version is misreported | `restart` keeps the old overlay; a rebuild on a data-template size drift rebuilt the disk; the version was stamped without verifying the VM booted the new rootfs. | Lambda                   |
| On a private-API deployment every route except `/ping` returns 404                                             | A private API's `event["resource"]` is always `/{proxy+}`, so the resource-template dispatch matched nothing.                                                      | Lambda                   |
| Host boot hangs at "installing tools + firecracker"; hosts end up ABANDONED                                    | A stack-output lookup queried a CDK-prefixed key that never matched, burning a 5-minute silent retry per boot.                                                     | host script (LT-baked)   |
| Host fails to launch VMs with AccessDenied; tenants stuck creating                                             | The token fallback reads `openclaw-tenant-secrets` with the host role, which was never granted read on that table.                                                 | IAM (was CDK)            |
| The AOS rolesmapping Lambda times out reaching Secrets Manager on an imported VPC                              | No NAT egress and no Secrets Manager VPC endpoint.                                                                                                                 | network (was CDK)        |
| A manual S3 upload fails on a Linux bastion with `shasum: command not found`                                   | `setup.sh` used `shasum` (macOS) not `sha256sum` (Linux/AL2023).                                                                                                   | meta (folded into apply) |

## Layers and how each is applied (all CDK-free)

- **Host scripts (S3-pulled, hot-swappable)** — `host-scripts/launch-vm.sh.patched`: scp
  onto the live host, then upload to S3 for future hosts.
- **Launch-Template-baked** — `launch-template/init-host.sh.patched` +
  `launch-template/APPLY-LT.md`: full file + manual new-LT-version + `update-auto-scaling-group`
  (this ASG pins a version; a new default is ignored).
- **Lambda code** — `lambda/APPLY-LAMBDA.md`: `update-function-code` (private-API routing +
  rebuild semantics).
- **IAM** — `iam/apply-iam.sh`: inline policy granting the host role read on
  `openclaw-tenant-secrets` (fail-closed prerequisite — applied first).
- **Network / VPCE** — `network/APPLY-NETWORK.md`: **describe-only, human-gated**. Probe
  whether the endpoint is even needed; propose (never auto-run) the `create-vpc-endpoint`.

## Dependency order (must follow)

```
1. IAM grant (fail-closed prerequisite) — FIRST.  [iam/]
2. Hot-fix running hosts (S3-pulled launch-vm.sh).  [host-scripts/]
3. Lambda code (update-function-code).  [lambda/]
4. Future-machine source: S3 upload + LT-baked init-host.sh.  [host-scripts/, launch-template/]
5. Network/VPCE — probe first, human-approved create only.  [network/]
```

## Files

| Path                                                   | Purpose                                                           |
| ------------------------------------------------------ | ----------------------------------------------------------------- |
| `manifest.json`                                        | base/patch SHAs + every changed path → layer + anti-revert hashes |
| `README.md`                                            | This file                                                         |
| `APPLY-INSTRUCTIONS.md`                                | Step-by-step, CDK-free, confirmation-gated apply guide            |
| `host-scripts/launch-vm.sh.patched`                    | Full replacement for the S3-pulled launch-vm.sh                   |
| `launch-template/init-host.sh.patched` + `APPLY-LT.md` | LT-baked file + manual LT/ASG update                              |
| `lambda/APPLY-LAMBDA.md`                               | Lambda redeploy via update-function-code                          |
| `iam/apply-iam.sh` + `host-role-tenant-secrets.json`   | Inline-policy hotfix (fail-closed prereq)                         |
| `network/APPLY-NETWORK.md`                             | Probe-driven, human-gated Secrets Manager VPCE                    |

## Verification summary

- Host no longer fails rc=127, no longer hangs at the tools step.
- The tenant-secrets get-item probe (from the host role) no longer returns AccessDenied.
- Private-API routes other than `/ping` work; a rebuild keeps the data disk and reports the
  version truthfully only after the VM boots the new rootfs.
- If the VPCE was needed, the rolesmapping Lambda reaches Secrets Manager with no timeout.
- Per-step verification (normal + recovery path) is in `APPLY-INSTRUCTIONS.md`.
