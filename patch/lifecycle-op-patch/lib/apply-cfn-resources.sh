#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Apply / verify / roll back the CloudFormation resource changes a patch computed, ONE RESOURCE
# AT A TIME, without ever updating the stack.
#
# Why not a stack update: the customer deployed once with CDK and then changed the live system
# by hand. A template-driven update would overwrite those changes. So the closure captured at
# generation time (resources/cloudformation/<stack>.{base,patch}.json) is used as a diff to
# read, and each changed resource is dealt with individually.
#
# What this tool does NOT do: guess a CLI for an arbitrary resource type. It prints, per
# resource, the before/after properties and the exact reviewed decision required, then STOPS
# for the operator. That is deliberate — `AWS::IAM::Policy` and `AWS::CodeBuild::Project` need
# different calls, and inventing one from a template is how you break a live system.
#
# Usage: apply-cfn-resources.sh {plan|verify|rollback} <closure-dir> <region>
set -euo pipefail

MODE="${1:?usage: apply-cfn-resources.sh plan|verify|rollback <closure-dir> <region>}"
CLOSURE="${2:?closure dir required}"
REGION="${3:?region required}"

case "$MODE" in plan|verify|rollback) ;; *) echo "unknown mode $MODE" >&2; exit 2 ;; esac
# mapfile below is bash 4+; macOS ships 3.2, where this failed at exit 127 with
# "mapfile: command not found" — an error that looks like a broken script instead of the wrong
# shell. Say which it is.
if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
  echo "FATAL: bash ${BASH_VERSION} is too old; this needs bash 4+ (mapfile)." >&2
  exit 3
fi
[[ -d "$CLOSURE" ]] || { echo "no closure dir $CLOSURE" >&2; exit 2; }
for tool in jq python3 aws; do
  command -v "$tool" >/dev/null || { echo "FATAL: need $tool" >&2; exit 2; }
done

# The side files are named <stack>.base.json / <stack>.patch.json by capture-cfn-closure.py.
mapfile -t STACKS < <(
  find "$CLOSURE" -maxdepth 1 -name '*.patch.json' -printf '%f\n' 2>/dev/null |
    sed 's/\.patch\.json$//' | sort
)
[[ "${#STACKS[@]}" -gt 0 ]] || { echo "FATAL: no captured templates in $CLOSURE" >&2; exit 3; }

CHANGED=0
for stack in "${STACKS[@]}"; do
  base="$CLOSURE/${stack}.base.json"
  patch="$CLOSURE/${stack}.patch.json"
  [[ -f "$base" && -f "$patch" ]] || { echo "FATAL: incomplete closure for $stack" >&2; exit 3; }
  printf '\n=== stack %s ===\n' "$stack"
  # Per-resource A/M/D with the properties that differ, so the operator reviews facts rather
  # than a whole template.
  python3 - "$base" "$patch" "$MODE" "$REGION" <<'PYEOF'
import json, sys

base = json.load(open(sys.argv[1])).get("Resources", {})
patch = json.load(open(sys.argv[2])).get("Resources", {})
mode, region = sys.argv[3], sys.argv[4]

changed = 0
for logical in sorted(set(base) | set(patch)):
    before, after = base.get(logical), patch.get(logical)
    if before == after:
        continue
    changed += 1
    if before is None:
        kind, rtype = "ADD", after["Type"]
    elif after is None:
        kind, rtype = "DELETE", before["Type"]
    else:
        kind, rtype = "MODIFY", after["Type"]
    print(f"\n  [{kind}] {logical}  ({rtype})")
    if kind == "MODIFY":
        bp, ap = before.get("Properties", {}), after.get("Properties", {})
        for key in sorted(set(bp) | set(ap)):
            if bp.get(key) != ap.get(key):
                b = json.dumps(bp.get(key))[:120]
                a = json.dumps(ap.get(key))[:120]
                print(f"    {key}:\n      before: {b}\n      after : {a}")
    if mode == "plan":
        print(f"    DECIDE: is there a safe single-resource CLI for {rtype}? If yes, run it and")
        print(f"            record it. If not, this resource is UNPATCHABLE without a stack")
        print(f"            update and must be raised with the owner.")
    elif mode == "verify":
        print(f"    CHECK : describe {logical} in {region} and confirm it matches the 'after'")
        print(f"            values above; still matching 'before' means it never applied.")
    else:
        print(f"    REVERT: re-apply the 'before' values, unless this resource's")
        print(f"            rollback_policy is RETAIN (a read-only grant must not be removed).")
print(f"\n  {changed} changed resource(s) in this stack")
raise SystemExit(0 if changed >= 0 else 1)
PYEOF
  CHANGED=$((CHANGED + 1))
done

printf '\n%s over %d stack(s) — every resource above needs an explicit operator decision.\n' \
  "${MODE^^}_REVIEWED" "$CHANGED"
# Non-zero on purpose for plan/rollback: nothing was applied, so a driver must not read this as
# "done". verify exits 0 because reporting is its whole job.
[[ "$MODE" == "verify" ]] && exit 0
exit 25
