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
#   verify   <asg> <region> [instance-id]   verify one healthy ASG host running the new LT version
#   rollback <asg> <region>                 re-point the ASG at the prior LT version without overwriting drift
#
# Design decisions from the 2026-07-22 cross-model review:
#  #1 MIP-safe: if the ASG uses a MixedInstancesPolicy, promote reads the FULL policy and swaps ONLY
#     the version inside it (overrides/allocation/Spot ratio preserved) — never `--launch-template`,
#     which would flatten a MIP. Plain-LT ASGs use `--launch-template`.
#  #2 identity: operate on the immutable LaunchTemplateId read FROM the ASG, not a passed name.
#  #3 no $Latest/$Default: pull refuses them — you must pin a concrete numeric version first, so a
#     freshly-created version can't be picked up by natural scaling before promote.
#  #4/#5 drift + state: pull snapshots the full ASG config + its sha; promote records the resulting
#     launch config, and rollback refuses later drift. State is a 0600 JSON file in a 0700 dir.
#  #9 boot verification: push creates a version, reads it back, and leaves the ASG alone; promote
#     is a separate gated step. Verify a host created by the controlled instance-refresh.
#  #10 refresh uses a HIGH MinHealthyPercentage (default 100 — small = bigger blast radius, backwards).
#  #11 host registration key is instance_id.  #13 every mutation is read back and compared.
#
# #389 made the LT carry a tiny bootstrap instead of the whole script, so two forms are live:
#   gzip-inline   pre-#389 — the script is a gzip+base64 blob in user-data; push repacks the blob.
#   s3-bootstrap  #389+    — user-data downloads an immutable, digest-named S3 object and verifies
#                            its sha256 before exec; push publishes a NEW object and repoints the
#                            bootstrap at it. The LT itself is never the source of the script.
# pull classifies and records the form; every later verb reads it from state instead of assuming.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LTU="$HERE/lt-userdata.py"
[ -f "$LTU" ] || { echo "FATAL: lt-userdata.py not found next to apply-lt.sh" >&2; exit 1; }
CONFIRM="APPLY"
STATE_DIR="${OC_APPLY_LT_STATE_DIR:-$HOME/.oc-apply-lt}"
ENVIRONMENT="${OC_PATCH_ENVIRONMENT:-$HERE/../environment.json}"

_gate() { echo; echo ">>> $1"; printf "    type '%s' to proceed (else abort): " "$CONFIRM"; read -r a; [ "$a" = "$CONFIRM" ] || { echo aborted; exit 3; }; }
_need() { command -v "$1" >/dev/null || { echo "FATAL: need '$1' on PATH" >&2; exit 1; }; }
_need aws; _need jq; _need python3
_sha256_stdin() {
  if command -v sha256sum >/dev/null; then
    sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null; then
    shasum -a 256 | awk '{print $1}'
  else
    echo "FATAL: need 'sha256sum' or 'shasum' on PATH" >&2
    return 1
  fi
}
_statefile() { echo "$STATE_DIR/$1.json"; }   # $1 = asg name

# The plaintext the operator edits, keyed by asg so two concurrent patches don't collide.
_plain() { echo "$STATE_DIR/$1.init-host.sh"; }
# The LT's own user-data (the bootstrap), kept verbatim: for an s3-bootstrap it is what
# rekey rewrites, and it is NOT the script the operator edits.
_boot() { echo "$STATE_DIR/$1.bootstrap"; }

_read_asg() { # $1=asg $2=region -> full ASG JSON on stdout (single describe, reused)
  aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$1" --region "$2" \
    --query 'AutoScalingGroups[0]' --output json
}

# The drift guard compares a hash of the ASG config, so anything AWS returns in a
# non-deterministic order is a false positive. SuspendedProcesses came back reordered
# between two describes in prod (#396 §6.5) and made promote unrunnable, which pushed
# the operator to bypass this tool entirely — a false alarm here is not a safe default,
# it removes the guard. Sort the set-valued fields; every ordered field stays as-is so a
# real reordering (e.g. MIP overrides, which encode priority) still trips the guard.
_asg_norm() { # stdin = ASG JSON -> canonical JSON on stdout
  jq -S '
    if has("SuspendedProcesses") then
      .SuspendedProcesses |= sort_by(.ProcessName, (.SuspensionReason // ""))
    else . end
    | if has("AvailabilityZones") then .AvailabilityZones |= sort else . end
    | if has("TerminationPolicies") then .TerminationPolicies |= sort else . end
    | if has("EnabledMetrics") then .EnabledMetrics |= sort_by(.Metric, (.Granularity // "")) else . end
    | if has("Tags") then .Tags |= sort_by(.Key, (.Value // "")) else . end'
}

# Read the rendered user-data of one exact LT version, decoded from base64.
_lt_userdata() { # $1=lt-id $2=version $3=region -> bootstrap text on stdout
  aws ec2 describe-launch-template-versions --launch-template-id "$1" --versions "$2" \
    --region "$3" --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
    --output text | base64 -d
}

# Field of `lt-userdata.py inspect`, read without a jq dependency on the python side.
_boot_field() { # $1=bootstrap-file $2=field -> value on stdout ("" when absent)
  python3 "$LTU" inspect "$1" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$2"
}

_state_form() { # $1=statefile -> bootstrap form, defaulting to the pre-#389 shape
  jq -r '.bootstrap.form // "gzip-inline"' "$1"
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

_launch_config() { # stdin = ASG JSON -> canonical type + launch config
  jq -cS '
    if .MixedInstancesPolicy then
      {type:"mip", value:.MixedInstancesPolicy}
    elif .LaunchTemplate then
      {type:"plain", value:.LaunchTemplate}
    else
      {type:"none", value:null}
    end'
}

# Resume a promote that died between the ASG mutation and the state back-fill.
# If the live ASG already launches the target version the change landed but was
# never recorded: adopt it so rollback stays bound. Otherwise the change never
# took effect; clear the stale intent so a retry can proceed cleanly.
# Returns 0 = already promoted (caller stops), 1 = proceed with a fresh promote.
_promote_reconcile() { # $1=ST $2=ASG $3=REGION $4=LTID $5=NEWVER $6=ISMIP
  local st="$1" asg="$2" region="$3" ltid="$4" newver="$5" ismip="$6"
  local asgnow nowid nowver nowtype config
  asgnow="$(_read_asg "$asg" "$region")"
  IFS=$'\t' read -r nowid nowver nowtype < <(printf '%s' "$asgnow" | _lt_ref)
  if [ "$nowid" = "$ltid" ] && [ "$nowver" = "$newver" ] && [ "$nowtype" = "$ismip" ]; then
    config="$(printf '%s' "$asgnow" | _launch_config)"
    jq --argjson config "$config" \
      'del(.promote_pending) | .promoted_launch_config=$config' "$st" > "$st.tmp"
    mv "$st.tmp" "$st"; chmod 600 "$st"
    echo "reconciled: the interrupted promote's ASG change is live; bound rollback to it."
    return 0
  fi
  jq 'del(.promote_pending)' "$st" > "$st.tmp"; mv "$st.tmp" "$st"; chmod 600 "$st"
  echo "note: the interrupted promote never changed the ASG; cleared stale intent, retrying."
  return 1
}

# Resume a refresh that died between start-instance-refresh and the state
# back-fill. If NO refresh is in flight, the start never took effect: clear the
# stale intent so a retry can proceed. If one IS in flight we cannot prove it is
# ours (the crash lost the id before we recorded it, and a concurrent CDK or
# operator refresh looks identical), so fail closed and require a human decision
# instead of adopting — and later cancelling — someone else's refresh.
# Returns 1 = proceed with a fresh start; exits non-zero when it cannot decide.
_refresh_reconcile() { # $1=ST $2=ASG $3=REGION
  local st="$1" asg="$2" region="$3" active
  active="$(aws autoscaling describe-instance-refreshes \
    --auto-scaling-group-name "$asg" --region "$region" \
    --query "InstanceRefreshes[?Status=='Pending' || Status=='InProgress']|[0].InstanceRefreshId" \
    --output text)"
  case "$active" in ''|None|null) active="" ;; esac
  if [ -n "$active" ]; then
    echo "FATAL: interrupted refresh left intent recorded and refresh $active is in flight," >&2
    echo "       but its ownership cannot be proven. Refusing to adopt it (a concurrent CDK/operator" >&2
    echo "       refresh looks identical and rollback would later cancel it). Confirm whether $active" >&2
    echo "       is this patch's; if so set .refresh_id and clear .refresh_pending in $st by hand," >&2
    echo "       otherwise wait for it to finish, then re-run." >&2
    exit 2
  fi
  jq 'del(.refresh_pending)' "$st" > "$st.tmp"; mv "$st.tmp" "$st"; chmod 600 "$st"
  echo "note: no in-flight refresh from the interrupted run; cleared stale intent, retrying."
  return 1
}

# Resume a rollback that died around the ASG restore. If the ASG already
# references the prior version, our restore landed: the caller finishes by
# replacing the bad-version hosts (the drift guard would otherwise reject the
# now-restored config and deadlock). If the live config still matches this
# patch's promote result the restore never landed: clear intent and roll back
# normally. Anything else is unexplained drift.
# Returns 0 = already restored (finish replacement), 1 = proceed normally.
_rollback_reconcile() { # $1=ST $2=ASG $3=REGION $4=LTID $5=PREV $6=ISMIP $7=PROMOTED_CONFIG
  local st="$1" asg="$2" region="$3" ltid="$4" prev="$5" ismip="$6" promoted="$7"
  local asgnow nowid nowver nowtype livecfg
  asgnow="$(_read_asg "$asg" "$region")"
  IFS=$'\t' read -r nowid nowver nowtype < <(printf '%s' "$asgnow" | _lt_ref)
  if [ "$nowid" = "$ltid" ] && [ "$nowver" = "$prev" ] && [ "$nowtype" = "$ismip" ]; then
    echo "reconciled: the interrupted rollback already restored the ASG to v$prev; replacing bad hosts."
    return 0
  fi
  livecfg="$(printf '%s' "$asgnow" | _launch_config)"
  if [ "$livecfg" = "$promoted" ]; then
    jq 'del(.rollback_pending)' "$st" > "$st.tmp"; mv "$st.tmp" "$st"; chmod 600 "$st"
    echo "note: the interrupted rollback never changed the ASG; retrying the restore."
    return 1
  fi
  echo "FATAL: ASG launch config during an interrupted rollback matches neither this patch's" >&2
  echo "       promote result nor the restored prior version; refusing to guess. Inspect $asg." >&2
  exit 2
}

# Finish a rollback: with the ASG already pointing at PREV, replace any hosts
# still running the bad NEWVER via a controlled instance-refresh, then clear the
# rollback intent. Shared by a fresh rollback and a resumed one so both converge.
_rollback_replace_bad_hosts() { # $1=ASG $2=REGION $3=ST $4=LTID $5=PREV $6=NEWVER
  local asg="$1" region="$2" st="$3" ltid="$4" prev="$5" newver="$6"
  local asg_after _id nowver _mip bad_count rid
  asg_after="$(_read_asg "$asg" "$region")"
  IFS=$'\t' read -r _id nowver _mip < <(printf '%s' "$asg_after" | _lt_ref)
  [ "$nowver" = "$prev" ] || {
    echo "FATAL: post-rollback ASG references v$nowver, expected v$prev" >&2
    exit 2
  }
  bad_count="$(printf '%s' "$asg_after" | jq --arg ltid "$ltid" --arg ver "$newver" '
    [.Instances[]? | select(
      .LaunchTemplate.LaunchTemplateId == $ltid
      and (.LaunchTemplate.Version | tostring) == $ver
    )] | length')"
  if [ "$bad_count" -gt 0 ]; then
    rid="$(aws autoscaling start-instance-refresh \
      --auto-scaling-group-name "$asg" --region "$region" \
      --preferences "MinHealthyPercentage=100,InstanceWarmup=300,SkipMatching=true" \
      --query InstanceRefreshId --output text)"
    case "$rid" in ''|None|null)
      echo "FATAL: ASG restored but rollback instance-refresh returned no id" >&2
      exit 2 ;;
    esac
    jq --arg id "$rid" 'del(.rollback_pending) | .rollback_refresh_id=$id' "$st" > "$st.tmp"
    mv "$st.tmp" "$st"; chmod 600 "$st"
    echo "OK: ASG restored to v$prev; rollback refresh $rid is replacing $bad_count v$newver host(s)."
  else
    jq 'del(.rollback_pending)' "$st" > "$st.tmp"; mv "$st.tmp" "$st"; chmod 600 "$st"
    echo "OK: ASG restored to v$prev; no running v$newver hosts require replacement."
  fi
  echo "The immutable bad LT version is retained for audit; do not delete it."
}

_usage() {
  echo "usage: apply-lt.sh pull|push|promote|refresh|rollback <asg> <region>" >&2
  echo "       apply-lt.sh verify <asg> <region> [instance-id]" >&2
}

[ $# -ge 1 ] || { _usage; exit 2; }
cmd="$1"; shift

case "$cmd" in
  pull)
    ASG="${1:?asg}"; REGION="${2:?region}"
    mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
    ST="$(_statefile "$ASG")"
    # pull is the flow's START and overwrites the state file, which holds the ONLY
    # record of prev_version plus the promote/refresh/rollback crash-resume markers.
    # Overwriting an in-flight rollout would orphan a pushed-but-unpromoted LT
    # version and destroy every resume/rollback path. Refuse unless the prior
    # rollout reached a terminal state (promoted-and-landed, or never pushed).
    if [ -f "$ST" ]; then
      # A same-named ASG in another region would let the refresh-id query below run
      # against the wrong region and strand an active refresh. The state records the
      # asg/region it was pulled for; refuse if this pull targets a different one.
      _st_asg="$(jq -r '.asg // empty' "$ST")"
      _st_region="$(jq -r '.region // empty' "$ST")"
      if { [ -n "$_st_asg" ] && [ "$_st_asg" != "$ASG" ]; } || \
         { [ -n "$_st_region" ] && [ "$_st_region" != "$REGION" ]; }; then
        echo "FATAL: $ST records asg='$_st_asg' region='$_st_region' but pull targets" >&2
        echo "       asg='$ASG' region='$REGION'. A same-named ASG in another region could" >&2
        echo "       strand an active refresh. Move $ST aside to start fresh, or re-run pull" >&2
        echo "       with the recorded region." >&2
        exit 2
      fi
      INFLIGHT="$(jq -r '
        if (.promote_pending // .refresh_pending // .rollback_pending) then "unfinished crash-resume"
        elif (.new_version and (.promoted_launch_config | not)) then "pushed but not promoted"
        else "" end' "$ST")"
      if [ -n "$INFLIGHT" ]; then
        echo "FATAL: $ST has an in-flight rollout ($INFLIGHT); refusing to overwrite it." >&2
        echo "       Finish it (promote then refresh) or undo it (rollback), then re-run pull." >&2
        echo "       To abandon it deliberately, move $ST aside first." >&2
        exit 2
      fi
      # A finished refresh clears refresh_pending but leaves refresh_id (a rollback
      # leaves rollback_refresh_id) as the ONLY anchor to attribute or cancel that
      # rollout. The flag guard above cannot see a refresh that is still rolling
      # after its start returned. Query each recorded id's LIVE status and refuse
      # while any is non-terminal, else overwriting state strands a running refresh.
      for _rid in \
        "$(jq -r '.refresh_id // empty' "$ST")" \
        "$(jq -r '.rollback_refresh_id // empty' "$ST")"; do
        [ -n "$_rid" ] || continue
        # Distinguish "the query itself failed" from "the query succeeded and the id
        # is gone". A nonzero exit (AWS/network/auth error) leaves the refresh's live
        # status unknown, so we MUST fail closed rather than let an empty status fall
        # through and overwrite the state that anchors its recovery. Only a SUCCESSFUL
        # query is trusted: an aged-out (>6 weeks) or not-found id returns exit 0 with
        # None/empty, which is genuinely terminal and correctly does not block.
        if ! _rstatus="$(aws autoscaling describe-instance-refreshes \
          --auto-scaling-group-name "$ASG" --instance-refresh-ids "$_rid" \
          --region "$REGION" --query 'InstanceRefreshes[0].Status' \
          --output text 2>/dev/null)"; then
          echo "FATAL: could not query recorded instance-refresh $_rid (AWS/network/auth" >&2
          echo "       error); its live status is unknown. Refusing to overwrite the state" >&2
          echo "       that anchors its recovery. Restore access, then re-run pull." >&2
          exit 2
        fi
        case "$_rstatus" in
          Pending|InProgress|Cancelling|RollbackInProgress|Baking)
            echo "FATAL: recorded instance-refresh $_rid is still $_rstatus; refusing to" >&2
            echo "       overwrite the state that anchors its recovery/cancel. Wait for a" >&2
            echo "       terminal status (or run rollback), then re-run pull." >&2
            exit 2 ;;
        esac
      done
    fi
    ASGJSON="$(_read_asg "$ASG" "$REGION")"
    IFS=$'\t' read -r LTID VER ISMIP < <(printf '%s' "$ASGJSON" | _lt_ref)
    [ -n "$LTID" ] || { echo "FATAL: ASG '$ASG' has no launch template (LaunchConfiguration ASGs unsupported)" >&2; exit 1; }
    case "$VER" in "\$Latest"|"\$Default"|''|null)
      echo "FATAL: ASG pins version '$VER' — refusing (#3). Pin a CONCRETE numeric version on the ASG first," >&2
      echo "       so a freshly-created version can't be launched by natural scaling before promote." >&2
      exit 2 ;;
    esac
    echo "ASG '$ASG' uses LT id=$LTID version=$VER (type=$ISMIP) — operating on the immutable id, not a name."
    # Keep the rendered user-data of THAT exact version, then classify it: the script
    # lives inside it (gzip-inline) or in an S3 object it points at (s3-bootstrap).
    _lt_userdata "$LTID" "$VER" "$REGION" > "$(_boot "$ASG")"
    BOOTINFO="$(python3 "$LTU" inspect "$(_boot "$ASG")")"
    FORM="$(printf '%s' "$BOOTINFO" | jq -r .form)"
    echo "LT user-data form: $FORM"
    if [ "$FORM" = "s3-bootstrap" ]; then
      # #389: the LT only binds bucket+key+sha256. The script itself must come from S3,
      # and we verify the object hashes to exactly what the LT will demand at boot —
      # otherwise every host it launches already fails its own sha256 gate and ABANDONs.
      S3BUCKET="$(printf '%s' "$BOOTINFO" | jq -r .bucket)"
      S3KEY="$(printf '%s' "$BOOTINFO" | jq -r .key)"
      WANTSHA="$(printf '%s' "$BOOTINFO" | jq -r .sha256)"
      aws s3 cp "s3://$S3BUCKET/$S3KEY" "$(_plain "$ASG")" --region "$REGION" --no-progress >&2
      GOTSHA="$(_sha256_stdin < "$(_plain "$ASG")")"
      [ "$GOTSHA" = "$WANTSHA" ] || {
        echo "FATAL: s3://$S3BUCKET/$S3KEY hashes to $GOTSHA but the LT demands $WANTSHA." >&2
        echo "       The live boot path is already broken (every new host would ABANDON at its" >&2
        echo "       sha256 gate); fix the object or the LT before patching anything." >&2
        exit 2
      }
      echo "bound object verified: s3://$S3BUCKET/$S3KEY sha256=$WANTSHA"
    else
      python3 "$LTU" decode "$(_boot "$ASG")" > "$(_plain "$ASG")"
    fi
    ASG_SHA="$(printf '%s' "$ASGJSON" | _asg_norm | _sha256_stdin)"
    jq -n --arg asg "$ASG" --arg region "$REGION" --arg ltid "$LTID" --arg ver "$VER" \
          --arg ismip "$ISMIP" --arg sha "$ASG_SHA" --argjson asgjson "$ASGJSON" \
          --argjson boot "$BOOTINFO" \
      '{asg:$asg,region:$region,lt_id:$ltid,prev_version:$ver,is_mip:$ismip,asg_sha:$sha,
        asg_snapshot:$asgjson,bootstrap:$boot}' \
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
    FORM="$(_state_form "$ST")"
    NEWSHA=""; NEWKEY=""
    if [ "$FORM" = "s3-bootstrap" ]; then
      # #389: publish the edited script as a NEW digest-named object, then repoint the
      # bootstrap at it. The key carries the content hash, so a new script is a new key and
      # an existing key can never be overwritten with different bytes.
      S3BUCKET="$(jq -r .bootstrap.bucket "$ST")"
      OLDKEY="$(jq -r .bootstrap.key "$ST")"
      OLDSHA="$(jq -r .bootstrap.sha256 "$ST")"
      NEWSHA="$(_sha256_stdin < "$EDITED")"
      [ "$NEWSHA" != "$OLDSHA" ] || {
        echo "FATAL: edited script is byte-identical to the live one (sha $NEWSHA); nothing to push." >&2
        exit 2
      }
      # Derive the key by swapping the digest, so the deployment's own prefix and filename
      # convention is preserved instead of reinvented here.
      NEWKEY="${OLDKEY//$OLDSHA/$NEWSHA}"
      [ "$NEWKEY" != "$OLDKEY" ] && [ "${NEWKEY#*$NEWSHA}" != "$NEWKEY" ] || {
        echo "FATAL: could not derive a digest-addressed key from '$OLDKEY'" >&2
        exit 2
      }
      python3 "$LTU" rekey "$(_boot "$ASG")" "$NEWKEY" "$NEWSHA" > "$STATE_DIR/$ASG.bootstrap.new"
      _gate "upload the edited script to s3://$S3BUCKET/$NEWKEY (new immutable object; no host reads it until promote)"
      # The digest is in the key, so an existing object with this key already has these
      # exact bytes. Refuse anyway rather than overwrite: if it exists with DIFFERENT
      # bytes something upstream is broken and silently replacing it hides that.
      if aws s3api head-object --bucket "$S3BUCKET" --key "$NEWKEY" --region "$REGION" \
           >/dev/null 2>&1; then
        echo "note: s3://$S3BUCKET/$NEWKEY already exists; verifying its bytes instead of overwriting."
      else
        aws s3 cp "$EDITED" "s3://$S3BUCKET/$NEWKEY" --region "$REGION" --no-progress >&2
      fi
      # Read the object back from S3 and hash what S3 actually serves (#13): the boot-time
      # gate hashes bytes, so only bytes read back from S3 prove the gate will pass.
      aws s3 cp "s3://$S3BUCKET/$NEWKEY" "$STATE_DIR/$ASG.s3readback" --region "$REGION" --no-progress >&2
      S3SHA="$(_sha256_stdin < "$STATE_DIR/$ASG.s3readback")"
      [ "$S3SHA" = "$NEWSHA" ] || {
        echo "FATAL: s3://$S3BUCKET/$NEWKEY reads back as $S3SHA, expected $NEWSHA — do NOT promote." >&2
        exit 2
      }
      echo "OK: object published + read-back verified: s3://$S3BUCKET/$NEWKEY sha256=$NEWSHA"
      UD_B64="$(base64 < "$STATE_DIR/$ASG.bootstrap.new" | tr -d '\n')"
    else
      # repack keeps decoded content byte-faithful; --strip is only for a first CDK bake.
      UD_B64="$(python3 "$LTU" repack "$EDITED" | base64 | tr -d '\n')"
      # sanity: decode(repack(edited)) == edited, before we ever create a version
      printf '%s' "$UD_B64" | base64 -d | python3 "$LTU" decode - > "$STATE_DIR/$ASG.roundtrip" 2>/dev/null
      diff -q "$EDITED" "$STATE_DIR/$ASG.roundtrip" >/dev/null || { echo "FATAL: round-trip mismatch — not creating a version" >&2; exit 2; }
    fi
    _gate "create a NEW version of LT $LTID from source version $PREV (does NOT touch the ASG yet)"
    NEWVER="$(aws ec2 create-launch-template-version --launch-template-id "$LTID" --source-version "$PREV" \
      --launch-template-data "{\"UserData\":\"$UD_B64\"}" --region "$REGION" \
      --query 'LaunchTemplateVersion.VersionNumber' --output text)"
    # read back the created version's UserData and prove it carries what we intended (#13)
    _lt_userdata "$LTID" "$NEWVER" "$REGION" > "$STATE_DIR/$ASG.bootstrap.readback"
    if [ "$FORM" = "s3-bootstrap" ]; then
      # Three-way equality is the whole safety property: the digest the LT will demand at
      # boot == the digest in the object key == the hash of the bytes S3 serves. Any two
      # matching while the third differs means every host launched from this version dies
      # at its own sha256 gate, so all three are asserted before promote is even offered.
      RB_SHA="$(_boot_field "$STATE_DIR/$ASG.bootstrap.readback" sha256)"
      RB_KEY="$(_boot_field "$STATE_DIR/$ASG.bootstrap.readback" key)"
      [ "$RB_SHA" = "$NEWSHA" ] && [ "$RB_KEY" = "$NEWKEY" ] || {
        echo "FATAL: LT v$NEWVER binds key=$RB_KEY sha=$RB_SHA, expected key=$NEWKEY sha=$NEWSHA" >&2
        echo "       — do NOT promote." >&2
        exit 2
      }
      echo "three-way sha match: LT v$NEWVER demands $RB_SHA = key digest = S3 bytes $S3SHA"
      jq --arg nv "$NEWVER" --arg k "$NEWKEY" --arg s "$NEWSHA" \
        '.new_version=$nv | .new_bootstrap={key:$k, sha256:$s}' "$ST" > "$ST.tmp"
      mv "$ST.tmp" "$ST"; chmod 600 "$ST"
    else
      python3 "$LTU" decode "$STATE_DIR/$ASG.bootstrap.readback" > "$STATE_DIR/$ASG.readback"
      diff -q "$EDITED" "$STATE_DIR/$ASG.readback" >/dev/null \
        || { echo "FATAL: created version $NEWVER does NOT read back as edited plaintext — do NOT promote" >&2; exit 2; }
      jq --arg nv "$NEWVER" '.new_version=$nv' "$ST" > "$ST.tmp" && mv "$ST.tmp" "$ST"; chmod 600 "$ST"
    fi
    echo "OK: LT $LTID new version $NEWVER created + read-back verified. ASG UNCHANGED."
    echo "The new UserData is already proven at push (no unresolved placeholders + round-trip + read-back)."
    echo "NEXT: apply-lt.sh promote $ASG $REGION   (point the ASG at v$NEWVER; the running fleet is controlled,"
    echo "      so a small controlled instance-refresh's first host — via normal ASG lifecycle — is the boot proof)."
    ;;

  promote)
    ASG="${1:?asg}"; REGION="${2:?region}"; ST="$(_statefile "$ASG")"
    LTID="$(jq -r .lt_id "$ST")"; NEWVER="$(jq -r .new_version "$ST")"; ISMIP="$(jq -r .is_mip "$ST")"
    PREVSHA="$(jq -r .asg_sha "$ST")"
    [ "$NEWVER" != "null" ] || { echo "FATAL: run push first" >&2; exit 1; }
    # A recorded-but-not-completed promote means a prior run died mid-mutation.
    # Reconcile against the live ASG before doing anything else: if it already
    # promoted, bind rollback and stop; otherwise clear intent and retry below.
    if [ "$(jq -r '.promote_pending // empty' "$ST")" = "1" ]; then
      _promote_reconcile "$ST" "$ASG" "$REGION" "$LTID" "$NEWVER" "$ISMIP" && exit 0
    fi
    _gate "point ASG '$ASG' at LT $LTID v$NEWVER (new instances only; running fleet untouched)"
    # drift guard (#4): re-read AFTER the confirm gate (not before) so a change during the prompt can't
    # slip through, and build the update from the re-read live JSON, not the stale pull snapshot.
    ASGNOW="$(_read_asg "$ASG" "$REGION")"
    NOWSHA="$(printf '%s' "$ASGNOW" | _asg_norm | _sha256_stdin)"
    [ "$NOWSHA" = "$PREVSHA" ] || {
      echo "FATAL: ASG config drifted since pull (someone/CDK changed it). Re-run pull." >&2
      echo "       Differing fields (pull snapshot vs live, both order-normalized):" >&2
      diff <(jq -S .asg_snapshot "$ST" | _asg_norm) <(printf '%s' "$ASGNOW" | _asg_norm) >&2 || true
      exit 2
    }
    # Journal the intent BEFORE the mutation so a crash in the mutation window is
    # recoverable: on resume _promote_reconcile adopts the live change if it
    # landed, or clears this flag if it did not. Null id, back-filled after AWS.
    jq '.promote_pending=1' "$ST" > "$ST.tmp"; mv "$ST.tmp" "$ST"; chmod 600 "$ST"
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
    # Read back (#13), then bind rollback to the exact launch config produced by
    # this promote. Dynamic ASG fields and instances are intentionally excluded.
    ASG_AFTER="$(_read_asg "$ASG" "$REGION")"
    IFS=$'\t' read -r NOWID NOWVER NOWTYPE < <(printf '%s' "$ASG_AFTER" | _lt_ref)
    [ "$NOWID" = "$LTID" ] && [ "$NOWVER" = "$NEWVER" ] && [ "$NOWTYPE" = "$ISMIP" ] || {
      echo "FATAL: post-update ASG launch config is $NOWID v$NOWVER ($NOWTYPE), expected $LTID v$NEWVER ($ISMIP)" >&2
      exit 2
    }
    PROMOTED_CONFIG="$(printf '%s' "$ASG_AFTER" | _launch_config)"
    jq --argjson config "$PROMOTED_CONFIG" \
      'del(.promote_pending) | .promoted_launch_config=$config' "$ST" > "$ST.tmp"
    mv "$ST.tmp" "$ST"
    chmod 600 "$ST"
    echo "OK: ASG launches NEW hosts on LT $LTID v$NEWVER (verified). Running hosts unchanged."
    echo "NEXT: apply-lt.sh refresh $ASG $REGION"
    echo "      Then verify the first healthy replacement: apply-lt.sh verify $ASG $REGION"
    echo "      Rollback: apply-lt.sh rollback $ASG $REGION"
    ;;

  refresh)
    ASG="${1:?asg}"; REGION="${2:?region}"; ST="$(_statefile "$ASG")"
    [ -f "$ST" ] || { echo "FATAL: run pull and push first (no $ST)" >&2; exit 1; }
    # A recorded-but-not-completed refresh means a prior run died right after
    # start-instance-refresh. Reconcile fails closed if an unattributable refresh
    # is in flight; otherwise it clears the stale intent and we start fresh below.
    if [ "$(jq -r '.refresh_pending // empty' "$ST")" = "1" ]; then
      _refresh_reconcile "$ST" "$ASG" "$REGION" || true
    fi
    # A refresh replaces hosts with whatever the ASG launches NOW. It is only safe
    # after promote pointed the ASG at this patch's version AND the live config is
    # still exactly that promote result; otherwise we would roll the fleet onto an
    # old version or an externally-drifted config. Require the promote-time config
    # and re-read the live ASG to confirm it still matches (mirrors rollback).
    PROMOTED_CONFIG="$(jq -cS '.promoted_launch_config // empty' "$ST")"
    [ -n "$PROMOTED_CONFIG" ] || {
      echo "FATAL: no promoted_launch_config in state; run promote before refresh" >&2
      exit 2
    }
    LIVE_CONFIG="$(_read_asg "$ASG" "$REGION" | _launch_config)"
    [ "$LIVE_CONFIG" = "$PROMOTED_CONFIG" ] || {
      echo "FATAL: ASG launch config drifted since promote; refusing to refresh onto it. Re-run promote or rollback." >&2
      exit 2
    }
    MINH="${OC_REFRESH_MIN_HEALTHY:-100}"   # HIGH by default (#10): 100 = replace with zero lost healthy capacity
    # #6: MINH must be a plain 0-100 integer — validate BEFORE any arithmetic (a non-numeric value in
    # $(( )) would execute as an expression). Below 90 is a break-glass choice, force an extra confirm.
    case "$MINH" in ''|*[!0-9]*) echo "FATAL: OC_REFRESH_MIN_HEALTHY must be an integer 0-100, got '$MINH'" >&2; exit 2 ;; esac
    [ "$MINH" -le 100 ] || { echo "FATAL: MinHealthyPercentage $MINH > 100" >&2; exit 2; }
    [ "$MINH" -ge 90 ] || _gate "MinHealthy=$MINH is BELOW the safe 90 (bigger blast radius) — break-glass, confirm you mean it"
    _gate "start a CONTROLLED instance-refresh of '$ASG' (MinHealthyPercentage=$MINH — higher = safer)"
    # The gate above blocks on a human; the ASG can be repointed (another operator,
    # a CDK run) while we wait. The pre-gate check at line 391 is now stale, so
    # re-read the live config immediately before the mutation — otherwise we would
    # roll the fleet onto whatever config drifted in during the prompt.
    LIVE_CONFIG="$(_read_asg "$ASG" "$REGION" | _launch_config)"
    [ "$LIVE_CONFIG" = "$PROMOTED_CONFIG" ] || {
      echo "FATAL: ASG launch config drifted during the confirmation prompt; refusing to refresh onto it. Re-run promote or rollback." >&2
      exit 2
    }
    # Deliberately do NOT pin --desired-configuration here. Pinning would force the
    # promoted config onto the group, silently reverting a concurrent operator/CDK
    # repoint that landed in the sub-window after the recheck (foreign config loss).
    # The recheck above aborts on any drift it can see; an un-pinned refresh replaces
    # instances onto the group's CURRENT config, so a residual external repoint is
    # preserved, not reverted. The read->start sub-window is irreducible (AWS has no
    # conditional ASG-update primitive) but abort-or-defer never clobbers foreign
    # state — the fail-closed direction. This skill's own concurrent runs converge
    # via the journal + reconcile below (AWS also rejects a second in-flight refresh).
    # Journal intent BEFORE the mutation: a crash before the id is recorded is
    # recoverable because _refresh_reconcile adopts the in-flight refresh next run.
    jq '.refresh_pending=1' "$ST" > "$ST.tmp"; mv "$ST.tmp" "$ST"; chmod 600 "$ST"
    REFRESH_ID="$(aws autoscaling start-instance-refresh \
      --auto-scaling-group-name "$ASG" --region "$REGION" \
      --preferences "MinHealthyPercentage=$MINH,InstanceWarmup=300,SkipMatching=true" \
      --query InstanceRefreshId --output text)"
    case "$REFRESH_ID" in ''|None|null)
      echo "FATAL: start-instance-refresh returned no id" >&2
      exit 2
      ;;
    esac
    jq --arg id "$REFRESH_ID" --arg minh "$MINH" \
      'del(.refresh_pending) | .refresh_id=$id | .refresh_min_healthy=($minh | tonumber)' \
      "$ST" > "$ST.tmp"
    mv "$ST.tmp" "$ST"
    chmod 600 "$ST"
    echo "OK: instance-refresh $REFRESH_ID started (MinHealthy=$MINH)."
    echo "Watch: aws autoscaling describe-instance-refreshes --auto-scaling-group-name $ASG --region $REGION"
    echo "NEXT: once a new host is healthy, run: apply-lt.sh verify $ASG $REGION"
    ;;

  verify)
    ASG="${1:?asg}"; REGION="${2:?region}"; REQUESTED_IID="${3:-}"; ST="$(_statefile "$ASG")"
    [ -f "$ST" ] || { echo "FATAL: run pull and push first (no $ST)" >&2; exit 1; }
    LTID="$(jq -r .lt_id "$ST")"; NEWVER="$(jq -r .new_version "$ST")"
    [ -n "$LTID" ] && [ "$LTID" != "null" ] || {
      echo "FATAL: state has no launch template id; re-run pull" >&2
      exit 1
    }
    [ -n "$NEWVER" ] && [ "$NEWVER" != "null" ] || {
      echo "FATAL: run push first (state has no new version)" >&2
      exit 1
    }

    # A standalone canary is unsafe because init-host registers it as schedulable.
    # Verify only a healthy host created through the ASG lifecycle on the pushed version.
    ASGJSON="$(_read_asg "$ASG" "$REGION")"
    CANDIDATES="$(printf '%s' "$ASGJSON" | jq -r --arg ltid "$LTID" --arg ver "$NEWVER" '
      .Instances[]?
      | select(
          .LifecycleState == "InService"
          and .HealthStatus == "Healthy"
          and .LaunchTemplate.LaunchTemplateId == $ltid
          and (.LaunchTemplate.Version | tostring) == $ver
        )
      | .InstanceId
    ')"
    [ -n "$CANDIDATES" ] || {
      echo "FATAL: no healthy InService host on LT $LTID v$NEWVER yet." >&2
      echo "       Promote + refresh first, then retry verify." >&2
      exit 2
    }
    if [ -n "$REQUESTED_IID" ]; then
      printf '%s\n' "$CANDIDATES" | grep -Fxq "$REQUESTED_IID" || {
        echo "FATAL: requested instance $REQUESTED_IID is not healthy on LT $LTID v$NEWVER" >&2
        exit 2
      }
      IID="$REQUESTED_IID"
    else
      IID="$(printf '%s\n' "$CANDIDATES" | sed -n '1p')"
    fi
    echo "signal 1/3 PASS: $IID is Healthy/InService in ASG '$ASG' on LT $LTID v$NEWVER"

    [ -f "$ENVIRONMENT" ] || {
      echo "FATAL: environment.json missing at $ENVIRONMENT; run discover-env.sh first" >&2
      exit 2
    }
    jq -e --arg asg "$ASG" '
      .asg.confirmed == true and .asg.name == $asg
      and (.hosts.table | type == "string" and length > 0)
    ' "$ENVIRONMENT" >/dev/null || {
      echo "FATAL: environment.json does not confirm ASG '$ASG' and its hosts table" >&2
      exit 2
    }
    HOSTS_TABLE="$(jq -r .hosts.table "$ENVIRONMENT")"
    HOST_KEY="$(jq -cn --arg iid "$IID" '{instance_id:{S:$iid}}')"
    LEDGER_IID="$(aws dynamodb get-item --table-name "$HOSTS_TABLE" \
      --key "$HOST_KEY" --consistent-read --region "$REGION" \
      --query 'Item.instance_id.S' --output text)"
    [ "$LEDGER_IID" = "$IID" ] || {
      echo "FATAL: signal 2/3 failed: $IID is not registered in $HOSTS_TABLE" >&2
      exit 2
    }
    echo "signal 2/3 PASS: $IID is registered in scheduler ledger $HOSTS_TABLE"

    PING="$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$IID" \
      --region "$REGION" --query 'InstanceInformationList[0].PingStatus' --output text)"
    [ "$PING" = "Online" ] || {
      echo "FATAL: signal 3/3 failed: SSM PingStatus is '$PING', expected Online" >&2
      exit 2
    }

    # WHERE the rendered script lands is decided by the LT, not by this script: pre-#389
    # inlined it to /tmp, #389 stages it to /var/lib/cloud. Asserting a hardcoded /tmp path
    # against a #389 host fails a host that booted correctly — a verifier that cannot pass
    # on a healthy fleet is worse than no verifier, because the operator learns to skip it.
    FORM="$(_state_form "$ST")"
    if [ "$FORM" = "s3-bootstrap" ]; then
      SCRIPT_PATH="$(jq -r '.bootstrap.target // "/var/lib/cloud/init-host.sh"' "$ST")"
      # Additionally prove the host ran the exact object this push published, rather than a
      # stale copy left by an earlier boot: hash the on-disk script and compare.
      WANT_SHA="$(jq -r '.new_bootstrap.sha256 // empty' "$ST")"
    else
      SCRIPT_PATH="/tmp/init-host.sh"
      WANT_SHA=""
    fi
    PARAMS="$(jq -cn --arg p "$SCRIPT_PATH" --arg want "$WANT_SHA" '{
      commands: ([
        "set -euo pipefail",
        "cloud-init status --wait",
        "test \"$(systemctl is-active host-agent.service)\" = active",
        ("test -f " + $p),
        ("! grep -Eq \"\\{\\{[A-Z][A-Z0-9_]*\\}\\}\" " + $p)
      ] + (if $want == "" then [] else
        ["test \"$(sha256sum " + $p + " | cut -d\" \" -f1)\" = \"" + $want + "\""]
      end))
    }')"
    COMMAND_ID="$(aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
      --comment "verify claw patch launch template" --parameters "$PARAMS" --timeout-seconds 180 \
      --region "$REGION" --query 'Command.CommandId' --output text)"
    case "$COMMAND_ID" in ''|None|null)
      echo "FATAL: signal 3/3 failed: SSM send-command returned no command id" >&2
      exit 2
      ;;
    esac
    if ! aws ssm wait command-executed --command-id "$COMMAND_ID" \
      --instance-id "$IID" --region "$REGION"; then
      echo "FATAL: signal 3/3 failed: remote boot checks did not succeed" >&2
      aws ssm get-command-invocation --command-id "$COMMAND_ID" \
        --instance-id "$IID" --region "$REGION" --output json >&2 || true
      exit 2
    fi
    RESULT="$(aws ssm get-command-invocation --command-id "$COMMAND_ID" \
      --instance-id "$IID" --region "$REGION" --output json)"
    STATUS="$(printf '%s' "$RESULT" | jq -r .Status)"
    [ "$STATUS" = "Success" ] || {
      echo "FATAL: signal 3/3 failed: SSM command status is '$STATUS'" >&2
      printf '%s\n' "$RESULT" | jq . >&2
      exit 2
    }
    echo "signal 3/3 PASS: cloud-init completed; host-agent active; no unresolved placeholders in $SCRIPT_PATH${WANT_SHA:+; on-disk sha256=$WANT_SHA}"
    echo "OK: LT $LTID v$NEWVER boot verification passed on ASG host $IID"
    ;;

  rollback)
    ASG="${1:?asg}"; REGION="${2:?region}"; ST="$(_statefile "$ASG")"
    [ -f "$ST" ] || { echo "FATAL: no state $ST — can't know prior config" >&2; exit 1; }
    LTID="$(jq -r .lt_id "$ST")"; PREV="$(jq -r .prev_version "$ST")"
    NEWVER="$(jq -r '.new_version // empty' "$ST")"; ISMIP="$(jq -r .is_mip "$ST")"
    PROMOTED_CONFIG="$(jq -cS '.promoted_launch_config // empty' "$ST")"
    [ -n "$PROMOTED_CONFIG" ] || {
      echo "FATAL: state has no promote-time launch config; refusing an unbound rollback" >&2
      exit 2
    }
    # A recorded-but-not-completed rollback means a prior run died around the ASG
    # restore. If the restore already landed, finish by replacing the bad hosts
    # (skipping the drift guard that would otherwise reject the restored config).
    if [ "$(jq -r '.rollback_pending // empty' "$ST")" = "1" ]; then
      if _rollback_reconcile "$ST" "$ASG" "$REGION" "$LTID" "$PREV" "$ISMIP" "$PROMOTED_CONFIG"; then
        _rollback_replace_bad_hosts "$ASG" "$REGION" "$ST" "$LTID" "$PREV" "$NEWVER"
        exit 0
      fi
    fi
    _gate "cancel this patch's active refresh, restore ASG '$ASG' to LT $LTID v$PREV, and replace bad-version hosts"

    # Re-read after confirmation. Rollback is valid only while the launch config
    # still exactly matches this patch's promote result.
    ASG_NOW="$(_read_asg "$ASG" "$REGION")"
    LIVE_CONFIG="$(printf '%s' "$ASG_NOW" | _launch_config)"
    [ "$LIVE_CONFIG" = "$PROMOTED_CONFIG" ] || {
      echo "FATAL: ASG launch config drifted since promote; refusing to overwrite it" >&2
      exit 2
    }
    ACTIVE_ID="$(aws autoscaling describe-instance-refreshes \
      --auto-scaling-group-name "$ASG" --region "$REGION" \
      --query "InstanceRefreshes[?Status=='Pending' || Status=='InProgress' || Status=='Cancelling']|[0].InstanceRefreshId" \
      --output text)"
    case "$ACTIVE_ID" in ''|None|null) ACTIVE_ID="" ;; esac
    if [ -n "$ACTIVE_ID" ]; then
      OWN_REFRESH_ID="$(jq -r '.refresh_id // empty' "$ST")"
      [ "$ACTIVE_ID" = "$OWN_REFRESH_ID" ] || {
        echo "FATAL: active refresh $ACTIVE_ID was not started by this patch; refusing to cancel it" >&2
        exit 2
      }
      aws autoscaling cancel-instance-refresh \
        --auto-scaling-group-name "$ASG" --region "$REGION" >/dev/null
      STOPPED=false
      for _attempt in $(seq 1 30); do
        REFRESH_STATUS="$(aws autoscaling describe-instance-refreshes \
          --auto-scaling-group-name "$ASG" --instance-refresh-ids "$ACTIVE_ID" \
          --region "$REGION" --query 'InstanceRefreshes[0].Status' --output text)"
        case "$REFRESH_STATUS" in
          Pending|InProgress|Cancelling) sleep 2 ;;
          *) STOPPED=true; break ;;
        esac
      done
      [ "$STOPPED" = true ] || {
        echo "FATAL: refresh $ACTIVE_ID did not stop; ASG was not changed" >&2
        exit 2
      }
    fi

    # The refresh cancellation can take up to a minute. Re-check immediately
    # before update so a concurrent CDK/operator change cannot cross that wait.
    ASG_NOW="$(_read_asg "$ASG" "$REGION")"
    LIVE_CONFIG="$(printf '%s' "$ASG_NOW" | _launch_config)"
    [ "$LIVE_CONFIG" = "$PROMOTED_CONFIG" ] || {
      echo "FATAL: ASG launch config drifted while preparing rollback; refusing to overwrite it" >&2
      exit 2
    }
    # Journal the restore intent BEFORE the mutation. A crash in the update
    # window would otherwise leave the ASG restored to PREV while the drift guard
    # above still expects PROMOTED_CONFIG, deadlocking every retry. On resume
    # _rollback_reconcile sees the restored config and finishes replacement.
    jq '.rollback_pending=1' "$ST" > "$ST.tmp"; mv "$ST.tmp" "$ST"; chmod 600 "$ST"
    if [ "$ISMIP" = "mip" ]; then
      # Build from the drift-checked live policy, changing only the LT version.
      MIP="$(printf '%s' "$ASG_NOW" | jq -c --arg pv "$PREV" \
        '.MixedInstancesPolicy | .LaunchTemplate.LaunchTemplateSpecification.Version=$pv')"
      aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG" --mixed-instances-policy "$MIP" --region "$REGION"
    else
      LIVE_LTID="$(printf '%s' "$ASG_NOW" | jq -r '.LaunchTemplate.LaunchTemplateId // empty')"
      [ "$LIVE_LTID" = "$LTID" ] || {
        echo "FATAL: live launch template id is '$LIVE_LTID', expected '$LTID'" >&2
        exit 2
      }
      aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG" \
        --launch-template "LaunchTemplateId=$LTID,Version=$PREV" --region "$REGION"
    fi
    _rollback_replace_bad_hosts "$ASG" "$REGION" "$ST" "$LTID" "$PREV" "$NEWVER"
    ;;

  *) _usage; exit 2 ;;
esac
