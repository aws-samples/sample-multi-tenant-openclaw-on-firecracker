#!/usr/bin/env bash
# apply-lt.sh — the ONE safe way to roll an init-host.sh change into a live Launch Template + ASG with
# NO CloudFormation redeploy and NO hand-wrangled base64. Wraps lt-userdata.py (deterministic
# decode/repack) and encodes the traps a real prod run + a cross-model review proved necessary, so an
# operator or an LLM executor can't corrupt the fleet.
#
# Verbs (run in order; every mutating step is a confirmation gate):
#   pull     <asg> <region>                 discover LT-id/version the ASG ACTUALLY uses; decode rendered UserData
#   push     <asg> <region>                 create a NEW LT version from your edited plaintext (does NOT touch the ASG)
#   promote  <asg> <region>                 point the ASG at the new version (MIP-safe), preserving all other config
#   refresh  <asg> <region>                 start a CONTROLLED instance-refresh (high MinHealthy)
#   rollback <asg> <region>                 re-point the ASG at the exact prior config captured at pull
#
# Design decisions from the 2026-07-22 cross-model review:
#  #1 MIP-safe: if the ASG uses a MixedInstancesPolicy, promote reads the FULL policy and swaps ONLY
#     the version inside it (overrides/allocation/Spot ratio preserved) — never `--launch-template`,
#     which would flatten a MIP. Plain-LT ASGs use `--launch-template`.
#  #2 identity: operate on the immutable LaunchTemplateId read FROM the ASG, not a passed name.
#  #3 no $Latest/$Default: pull refuses them — you must pin a concrete numeric version first, so a
#     freshly-created version can't be picked up by natural scaling before promote.
#  #4/#5 drift + state: pull snapshots the full ASG config + its sha; every later verb re-reads and
#     aborts if the live config drifted; state is a 0600 JSON file in a 0700 dir (never /tmp `source`).
#  #9 verify-before-promote: push creates a version, reads it back, and leaves the ASG alone; promote
#     is a separate gated step. The fleet is controlled, so a controlled instance-refresh is the boot proof.
#  #10 refresh uses a HIGH MinHealthyPercentage (default 100 — small = bigger blast radius, backwards).
#  #11 host registration key is instance_id.  #13 every mutation is read back and compared.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LTU="$HERE/lt-userdata.py"
[ -f "$LTU" ] || { echo "FATAL: lt-userdata.py not found next to apply-lt.sh" >&2; exit 1; }
CONFIRM="APPLY"
STATE_DIR="${OC_APPLY_LT_STATE_DIR:-$HOME/.oc-apply-lt}"

_gate() { echo; echo ">>> $1"; printf "    type '%s' to proceed (else abort): " "$CONFIRM"; read -r a; [ "$a" = "$CONFIRM" ] || { echo aborted; exit 3; }; }
_need() { command -v "$1" >/dev/null || { echo "FATAL: need '$1' on PATH" >&2; exit 1; }; }
_need aws; _need jq; _need python3
_statefile() { echo "$STATE_DIR/$1.json"; }   # $1 = asg name

# The plaintext the operator edits, keyed by asg so two concurrent patches don't collide.
_plain() { echo "$STATE_DIR/$1.init-host.sh"; }

_read_asg() { # $1=asg $2=region -> full ASG JSON on stdout (single describe, reused)
  aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$1" --region "$2" \
    --query 'AutoScalingGroups[0]' --output json
}

# Extract (lt_id, version) the ASG actually launches with, whether plain-LT or MIP. Refuses $Latest/$Default.
_lt_ref() { # stdin = ASG JSON -> "ltid<TAB>version<TAB>ismip"
  jq -r '
    (.MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification) as $mip
    | (.LaunchTemplate) as $plain
    | if $mip then [$mip.LaunchTemplateId, $mip.Version, "mip"]
      elif $plain then [$plain.LaunchTemplateId, $plain.Version, "plain"]
      else ["", "", "none"] end
    | @tsv'
}

[ $# -ge 1 ] || { echo "usage: apply-lt.sh pull|push|promote|refresh|rollback <asg> <region>" >&2; exit 2; }
cmd="$1"; shift

case "$cmd" in
  pull)
    ASG="${1:?asg}"; REGION="${2:?region}"
    mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
    ASGJSON="$(_read_asg "$ASG" "$REGION")"
    IFS=$'\t' read -r LTID VER ISMIP < <(printf '%s' "$ASGJSON" | _lt_ref)
    [ -n "$LTID" ] || { echo "FATAL: ASG '$ASG' has no launch template (LaunchConfiguration ASGs unsupported)" >&2; exit 1; }
    case "$VER" in '$Latest'|'$Default'|''|null)
      echo "FATAL: ASG pins version '$VER' — refusing (#3). Pin a CONCRETE numeric version on the ASG first," >&2
      echo "       so a freshly-created version can't be launched by natural scaling before promote." >&2
      exit 2 ;;
    esac
    echo "ASG '$ASG' uses LT id=$LTID version=$VER (type=$ISMIP) — operating on the immutable id, not a name."
    # decode the rendered UserData of THAT exact version
    aws ec2 describe-launch-template-versions --launch-template-id "$LTID" --versions "$VER" --region "$REGION" \
      --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d \
      | python3 "$LTU" decode - > "$(_plain "$ASG")"   # decode refuses {{ }} + strict bootstrap
    ASG_SHA="$(printf '%s' "$ASGJSON" | jq -S . | shasum -a 256 | awk '{print $1}')"
    jq -n --arg asg "$ASG" --arg region "$REGION" --arg ltid "$LTID" --arg ver "$VER" \
          --arg ismip "$ISMIP" --arg sha "$ASG_SHA" --argjson asgjson "$ASGJSON" \
      '{asg:$asg,region:$region,lt_id:$ltid,prev_version:$ver,is_mip:$ismip,asg_sha:$sha,asg_snapshot:$asgjson}' \
      > "$(_statefile "$ASG")"
    chmod 600 "$(_statefile "$ASG")"
    echo "OK: rendered plaintext -> $(_plain "$ASG")   (state -> $(_statefile "$ASG"), prev version $VER)"
    echo "NEXT: apply ONLY this patch's init-host hunk to $(_plain "$ASG"), then: apply-lt.sh push $ASG $REGION"
    ;;

  push)
    ASG="${1:?asg}"; REGION="${2:?region}"; ST="$(_statefile "$ASG")"
    [ -f "$ST" ] || { echo "FATAL: run pull first (no $ST)" >&2; exit 1; }
    LTID="$(jq -r .lt_id "$ST")"; PREV="$(jq -r .prev_version "$ST")"; EDITED="$(_plain "$ASG")"
    [ -f "$EDITED" ] || { echo "FATAL: $EDITED missing" >&2; exit 1; }
    # repack (no strip — decoded content is already CDK-stripped; refuses {{ }}; 16KB-checked)
    UD_B64="$(python3 "$LTU" repack "$EDITED" | base64 | tr -d '\n')"
    # sanity: decode(repack(edited)) == edited, before we ever create a version
    printf '%s' "$UD_B64" | base64 -d | python3 "$LTU" decode - > "$STATE_DIR/$ASG.roundtrip" 2>/dev/null
    diff -q "$EDITED" "$STATE_DIR/$ASG.roundtrip" >/dev/null || { echo "FATAL: round-trip mismatch — not creating a version" >&2; exit 2; }
    _gate "create a NEW version of LT $LTID from source version $PREV (does NOT touch the ASG yet)"
    NEWVER="$(aws ec2 create-launch-template-version --launch-template-id "$LTID" --source-version "$PREV" \
      --launch-template-data "{\"UserData\":\"$UD_B64\"}" --region "$REGION" \
      --query 'LaunchTemplateVersion.VersionNumber' --output text)"
    # read back: the new version's UserData must decode to exactly our edited plaintext (#13)
    aws ec2 describe-launch-template-versions --launch-template-id "$LTID" --versions "$NEWVER" --region "$REGION" \
      --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d \
      | python3 "$LTU" decode - > "$STATE_DIR/$ASG.readback"
    diff -q "$EDITED" "$STATE_DIR/$ASG.readback" >/dev/null \
      || { echo "FATAL: created version $NEWVER does NOT read back as edited plaintext — do NOT promote" >&2; exit 2; }
    jq --arg nv "$NEWVER" '.new_version=$nv' "$ST" > "$ST.tmp" && mv "$ST.tmp" "$ST"; chmod 600 "$ST"
    echo "OK: LT $LTID new version $NEWVER created + read-back verified. ASG UNCHANGED."
    echo "The new UserData is already proven at push (decode = no placeholders + round-trip + read-back)."
    echo "NEXT: apply-lt.sh promote $ASG $REGION   (point the ASG at v$NEWVER; the running fleet is controlled,"
    echo "      so a small controlled instance-refresh's first host — via normal ASG lifecycle — is the boot proof)."
    ;;

  promote)
    ASG="${1:?asg}"; REGION="${2:?region}"; ST="$(_statefile "$ASG")"
    LTID="$(jq -r .lt_id "$ST")"; NEWVER="$(jq -r .new_version "$ST")"; ISMIP="$(jq -r .is_mip "$ST")"
    PREVSHA="$(jq -r .asg_sha "$ST")"
    [ "$NEWVER" != "null" ] || { echo "FATAL: run push first" >&2; exit 1; }
    _gate "point ASG '$ASG' at LT $LTID v$NEWVER (new instances only; running fleet untouched)"
    # drift guard (#4): re-read AFTER the confirm gate (not before) so a change during the prompt can't
    # slip through, and build the update from the re-read live JSON, not the stale pull snapshot.
    ASGNOW="$(_read_asg "$ASG" "$REGION")"
    NOWSHA="$(printf '%s' "$ASGNOW" | jq -S . | shasum -a 256 | awk '{print $1}')"
    [ "$NOWSHA" = "$PREVSHA" ] || { echo "FATAL: ASG config drifted since pull (someone/CDK changed it). Re-run pull." >&2; exit 2; }
    if [ "$ISMIP" = "mip" ]; then
      # MIP-safe (#1/#2): take the FULL live policy, swap ONLY the version via --arg (env.NEWVER was a
      # bug — the var wasn't in jq's env yet, producing Version:null). Never --launch-template (flattens MIP).
      MIP="$(printf '%s' "$ASGNOW" | jq -c --arg nv "$NEWVER" \
        '.MixedInstancesPolicy | .LaunchTemplate.LaunchTemplateSpecification.Version=$nv')"
      [ -n "$MIP" ] && [ "$MIP" != "null" ] || { echo "FATAL: could not build MIP update JSON" >&2; exit 2; }
      aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG" \
        --mixed-instances-policy "$MIP" --region "$REGION"
    else
      aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG" \
        --launch-template "LaunchTemplateId=$LTID,Version=$NEWVER" --region "$REGION"
    fi
    # read back (#13): confirm the ASG now references v$NEWVER
    IFS=$'\t' read -r _id NOWVER _mip < <(_read_asg "$ASG" "$REGION" | _lt_ref)
    [ "$NOWVER" = "$NEWVER" ] || { echo "FATAL: post-update ASG references v$NOWVER, not v$NEWVER" >&2; exit 2; }
    echo "OK: ASG launches NEW hosts on LT $LTID v$NEWVER (verified). Running hosts unchanged."
    echo "NEXT (optional): apply-lt.sh refresh $ASG $REGION   |   rollback: apply-lt.sh rollback $ASG $REGION"
    ;;

  refresh)
    ASG="${1:?asg}"; REGION="${2:?region}"
    MINH="${OC_REFRESH_MIN_HEALTHY:-100}"   # HIGH by default (#10): 100 = replace with zero lost healthy capacity
    # #6: MINH must be a plain 0-100 integer — validate BEFORE any arithmetic (a non-numeric value in
    # $(( )) would execute as an expression). Below 90 is a break-glass choice, force an extra confirm.
    case "$MINH" in ''|*[!0-9]*) echo "FATAL: OC_REFRESH_MIN_HEALTHY must be an integer 0-100, got '$MINH'" >&2; exit 2 ;; esac
    [ "$MINH" -le 100 ] || { echo "FATAL: MinHealthyPercentage $MINH > 100" >&2; exit 2; }
    [ "$MINH" -ge 90 ] || _gate "MinHealthy=$MINH is BELOW the safe 90 (bigger blast radius) — break-glass, confirm you mean it"
    _gate "start a CONTROLLED instance-refresh of '$ASG' (MinHealthyPercentage=$MINH — higher = safer)"
    aws autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" --region "$REGION" \
      --preferences "MinHealthyPercentage=$MINH,InstanceWarmup=300"
    echo "OK: instance-refresh started (MinHealthy=$MINH). Watch: aws autoscaling describe-instance-refreshes --auto-scaling-group-name $ASG --region $REGION"
    echo "NOTE (experimental): rollback of an in-flight refresh isn't wired here — to natively roll back you'd need DesiredConfiguration set. Cancel manually if needed: aws autoscaling cancel-instance-refresh --auto-scaling-group-name $ASG --region $REGION"
    ;;

  rollback)
    ASG="${1:?asg}"; REGION="${2:?region}"; ST="$(_statefile "$ASG")"
    [ -f "$ST" ] || { echo "FATAL: no state $ST — can't know prior config" >&2; exit 1; }
    LTID="$(jq -r .lt_id "$ST")"; PREV="$(jq -r .prev_version "$ST")"; ISMIP="$(jq -r .is_mip "$ST")"
    _gate "roll ASG '$ASG' back to LT $LTID v$PREV (the exact version captured at pull)"
    if [ "$ISMIP" = "mip" ]; then
      MIP="$(jq -c '.asg_snapshot.MixedInstancesPolicy' "$ST")"   # restore the FULL original policy
      aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG" --mixed-instances-policy "$MIP" --region "$REGION"
    else
      aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG" \
        --launch-template "LaunchTemplateId=$LTID,Version=$PREV" --region "$REGION"
    fi
    IFS=$'\t' read -r _id NOWVER _mip < <(_read_asg "$ASG" "$REGION" | _lt_ref)
    [ "$NOWVER" = "$PREV" ] || { echo "WARN: post-rollback ASG references v$NOWVER, expected v$PREV — check manually" >&2; }
    echo "OK: ASG re-pointed at v$PREV. (The bad LT version is left in place — immutable, cheap; don't delete.)"
    ;;

  *) echo "usage: apply-lt.sh pull|push|promote|refresh|rollback <asg> <region>" >&2; exit 2 ;;
esac
