# Patch 311 — Launch-Template layer: init-host.sh (#309 stack-output prefix fix)

`init-host.sh` is NOT pulled from S3 — it is baked into the host Launch Template
`openclaw-host-lt` as a `base64(gzip(...))` bootstrap (see `ha_edge.py`). So neither a
live host nor a future host picks up a repo change on its own, and **`cdk deploy` (the
normal way to re-bake it) is forbidden here** — it would overwrite the customer's manual
changes. This layer is handled by hand.

The full patched file is shipped here: `init-host.sh.patched`
(patch_hash `5e8f50c1…`, base_hash `2b91afa5…`).

## Anti-revert gate (run first)

The live host's `/tmp/init-host.sh` (or the LT's decoded UserData) must hash to base_hash
before you treat this as a clean apply. If it hashes to patch_hash → already applied, skip.
If neither → it diverged (a newer fix); STOP and show the diff to the terminal user before
overwriting. Never downgrade.

## What actually needs doing

Most init-host.sh fixes only matter at BOOT. A running host already booted, so:

- **Running hosts** — usually need nothing here (the #309 fix affects how a host resolves a
  stack output at boot; live hosts are past that). If a specific live host must pick it up
  without a reboot, SSM the targeted change on directly. Do NOT re-bake a running host just
  to carry this.
- **Future hosts** — new LT version + point the ASG at it. `modify-launch-template
--default-version` is NOT enough: this ASG PINS a specific version
  (`ha_edge.py`: `LaunchTemplate.Version = _pinned_ver`, or the MixedInstancesPolicy LT
  spec), so it ignores `$Default`.

## Permissions needed

`ec2:CreateLaunchTemplateVersion`, `ec2:DescribeLaunchTemplateVersions`,
`autoscaling:DescribeAutoScalingGroups`, `autoscaling:UpdateAutoScalingGroup`
(+ `autoscaling:StartInstanceRefresh` only if you refresh existing hosts).

## Steps (future hosts)

```bash
# 1. Render the patched bootstrap the same way ha_edge.py does (base64(gzip(init-host.sh))),
#    verify it stays under the 16KB EC2 user-data limit.
BLOB=$(gzip -9 -c init-host.sh.patched | base64 | tr -d '\n')
BOOT="#!/bin/bash
echo $BLOB | base64 -d | gunzip > /tmp/init-host.sh
exec bash /tmp/init-host.sh"
python3 -c "import sys;b=open('/dev/stdin').read().encode();print('OK' if len(b)<16384 else 'TOO BIG: %d'%len(b))" <<<"$BOOT"

# 2. New LT version carrying it
aws ec2 create-launch-template-version --launch-template-name openclaw-host-lt \
  --source-version '$Latest' \
  --launch-template-data "{\"UserData\":\"$(printf %s "$BOOT" | base64 | tr -d '\n')\"}" \
  --region <region>
# note the new VersionNumber

# 3. Point the ASG at the new version (pinned — $Default is ignored)
#    plain LaunchTemplate:
aws autoscaling update-auto-scaling-group --auto-scaling-group-name openclaw-hosts-asg \
  --launch-template LaunchTemplateName=openclaw-host-lt,Version=<new> --region <region>
#    MixedInstancesPolicy (instance pool >= 2): describe it, edit the LT-spec Version, put it back.
```

New scale-outs now boot the patched script. **Existing instances do NOT update.** Only if
this fix truly must reach existing hosts, do a CONTROLLED instance refresh (disruptive —
gate on operator approval, keep the prior version for rollback):

```bash
aws autoscaling start-instance-refresh --auto-scaling-group-name openclaw-hosts-asg \
  --preferences MinHealthyPercentage=90,InstanceWarmup=300 --region <region>
```

## Backward-compat shortcut

If the patched init-host.sh is backward-compatible with the old baked one (future hosts
boot fine either way), you MAY defer the LT update — meaning "do the manual steps above
whenever convenient", NOT "wait for a cdk deploy" (there is none). Deferring is safe only
because new hosts keep working on the old compatible script.

## Rollback

`update-auto-scaling-group` back to the prior version number; if you refreshed, the old
LT version is still there to refresh back to.
