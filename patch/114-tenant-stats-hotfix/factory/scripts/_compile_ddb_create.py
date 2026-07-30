#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compile a NEW DynamoDB table into Bash apply/verify — create-only, no rollback.

Why this is a separate lane from `_compile_ddb.py` and not a new `setting` there: the settings
lane is built on "flip it, and flip it back". Creating a table has no back. Rolling one back means
deleting it, and after any traffic that deletes data. Putting create-only semantics behind the
same entrypoint as reversible settings would let an operator run `rollback.sh` and get a deleted
table when they expected a restored flag.

So the contract here is narrower and stated plainly:

  apply    creates the table if it is absent, waits for ACTIVE, and SKIPs when it already exists
           with a matching key schema. Rerunnable any number of times.
  verify   reads the live table and asserts the key schema and billing mode the patch declared.
  rollback REFUSES, with the reason. Removing a table the platform now depends on is a decision a
           human makes with the data in front of them, not something a patch driver does.

The reason this lane exists at all: on a customer system that has drifted from the template,
`cdk deploy` is not available, so a new table has to be created by the patch or the feature that
reads it cannot ship. That was a direct instruction, and it changes only the rollback story — the
idempotency requirement is unchanged and is what makes create-only safe to rerun.

A table created here is INVISIBLE to CloudFormation. The next `cdk deploy` that includes the same
logical table will try to create it again and fail with "already exists" unless it is imported
first. The kit must declare that follow-up, the same way the Lambda lane declares an ESM binding
conflict, so the operator learns it now rather than during a future stack update.
"""

import hashlib
import json
import os
import shlex
import sys

BILLING_MODES = {"PAY_PER_REQUEST", "PROVISIONED"}
KEY_TYPES = {"S", "N", "B"}

# Normalizes a key definition to one comparable JSON string. Lives outside the f-string templates
# because its dict/set braces would be read as format placeholders there.
# Why key NAMES are not enough: a same-named table whose `id` is type N passes a KeySchema-only
# comparison when the patch declares S, and the application fails on the first string write. Only
# the key attributes are compared — a GSI adds definitions this patch never declared.
_IDENTITY_PY = (
    "import json,sys\n"
    "d=json.load(sys.stdin)\n"
    "keys=d.get('k') or []\n"
    "attrs=dict((a['AttributeName'],a['AttributeType']) for a in (d.get('a') or []))\n"
    "named=set(k['AttributeName'] for k in keys)\n"
    "print(json.dumps({'keys':sorted(keys,key=lambda x:x['KeyType']),"
    "'types':dict((n,attrs.get(n)) for n in sorted(named))},sort_keys=True))"
)


def _q(value):
    return shlex.quote(str(value))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def ddb_create_recipe_id(table):
    return "ddbnew-" + hashlib.sha256(f"create:{table}".encode()).hexdigest()[:10]


def _key_schema_json(spec):
    """Build the two CLI arguments that define the key, from the declared attributes.

    Emitted as JSON rather than the shorthand syntax because the shorthand's comma/equals parsing
    breaks on nothing here but is unreadable, and a malformed key schema creates a table with the
    wrong primary key — which cannot be fixed in place.
    """
    keys = [{"AttributeName": spec["partition_key"]["name"], "KeyType": "HASH"}]
    attrs = [
        {
            "AttributeName": spec["partition_key"]["name"],
            "AttributeType": spec["partition_key"]["type"],
        }
    ]
    if spec.get("sort_key"):
        keys.append({"AttributeName": spec["sort_key"]["name"], "KeyType": "RANGE"})
        attrs.append(
            {
                "AttributeName": spec["sort_key"]["name"],
                "AttributeType": spec["sort_key"]["type"],
            }
        )
    return json.dumps(keys), json.dumps(attrs)


def _common(manifest, spec):
    keys, attrs = _key_schema_json(spec)
    identity_py = _q(_IDENTITY_PY)
    return f"""\
ARTIFACT_ID={_q(manifest["id"])}
CONTENT_VERSION={_q(manifest["patch_sha"])}
TABLE={_q(spec["table"])}
TARGET_ACCOUNT={_q(spec["target_account"])}
TARGET_REGION={_q(spec["target_region"])}
BILLING={_q(spec.get("billing_mode") or "PAY_PER_REQUEST")}
KEY_SCHEMA={_q(keys)}
ATTR_DEFS={_q(attrs)}
WANT_PITR={_q("true" if spec.get("pitr") else "false")}
RESOURCE_ID={_q(ddb_create_recipe_id(spec["table"]))}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REVIEW_RECEIPT="$SCRIPT_DIR/../../../REVIEW.json"
[[ -f "$REVIEW_RECEIPT" ]] || {{
  echo "FATAL: final REVIEW.json is missing; no reviewed state namespace is available" >&2
  exit 44
}}
KIT_FINGERPRINT="$(jq -er '
  .kit_fingerprint
  | select(type == "string" and test("^[0-9a-f]{{64}}$"))
' "$REVIEW_RECEIPT" 2>/dev/null)" || {{
  echo "FATAL: REVIEW.json has no valid final kit_fingerprint" >&2
  exit 44
}}

REGION="${{OC_PATCH_REGION:?OC_PATCH_REGION required}}"
# The account is ASKED OF STS, and a declared value is CHECKED against it rather than trusted.
# Taking OC_PATCH_ACCOUNT at face value let a run mutate one account while filing its state under
# another and reporting success: the state directory said one thing, the write went elsewhere.
# A configured endpoint would send these calls somewhere else entirely while every check passed.
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
# One place that turns an AWS error into an exit code a driver can branch on. This definition must
# precede STS, which is the first AWS call in every generated script.
#   41 transient  — throttling, capacity, service error: rerun
#   46 unreadable — could not read state: rerun, but look at credentials first
#   49 permanent  — permissions, parameters, validation: a human has to act
classify_aws_error() {{
  local text="$1"
  if printf '%s' "$text" | grep -qE 'Throttling|ProvisionedThroughputExceeded|LimitExceeded|ServiceUnavailable|InternalServerError|InternalFailure|RequestTimeout|RequestLimitExceeded'; then
    printf '41'
  elif printf '%s' "$text" | grep -qE 'AccessDenied|UnauthorizedOperation|AuthFailure|InvalidClientTokenId|ValidationException|InvalidParameter|MissingParameter'; then
    printf '49'
  else
    printf '46'
  fi
}}

# The target is FIXED IN THE KIT, and the live credentials are checked against it. STS alone only
# proves "these credentials belong to someone" — it cannot prove the someone is the intended
# account, so any valid credentials would have created and verified the table successfully.
# An STS failure is classified too: a throttle is retryable, a missing or denied credential is not,
# and collapsing both into 3 told the driver nothing about which.
STS_OUT="$(aws sts get-caller-identity --region "$REGION" --query Account --output text 2>&1)" \\
  || {{
    printf '%s\n' "$STS_OUT" >&2
    echo "FATAL: cannot read the live account from STS — refusing to act blind" >&2
    exit "$(classify_aws_error "$STS_OUT")"
  }}
LIVE_ACCOUNT="$STS_OUT"
[[ "$LIVE_ACCOUNT" =~ ^[0-9]{{12}}$ ]] || {{
  echo "FATAL: STS answered '$LIVE_ACCOUNT', which is not an account id" >&2; exit 3; }}
if [[ "$LIVE_ACCOUNT" != "$TARGET_ACCOUNT" ]]; then
  echo "FATAL: this kit targets account $TARGET_ACCOUNT but the credentials are for" >&2
  echo "       $LIVE_ACCOUNT. Refusing to touch the wrong account." >&2
  exit 3
fi
if [[ "$REGION" != "$TARGET_REGION" ]]; then
  echo "FATAL: this kit targets $TARGET_REGION but OC_PATCH_REGION is $REGION." >&2
  exit 3
fi
ACCOUNT_ID="$LIVE_ACCOUNT"
STATE_ROOT="${{OC_PATCH_STATE_ROOT:-${{HOME:-/tmp}}/.oc-patch-ddbnew}}"
STATE_DIR="${{STATE_ROOT}}/${{ACCOUNT_ID}}/${{REGION}}/${{ARTIFACT_ID}}/${{CONTENT_VERSION}}/${{KIT_FINGERPRINT}}/${{RESOURCE_ID}}"
mkdir -p "$STATE_DIR"

# Ownership evidence lives in AWS, not in a local file: a state directory can be empty, copied or
# hand-written, so "the anchor file exists" proves nothing about who created the table. These tags
# are written by create-table itself and read back from the API.
OWNER_TAG_KEY="oc-patch-artifact"
OWNER_TAG_VALUE="${{ARTIFACT_ID}}@${{CONTENT_VERSION}}"

# The ARN is checked against the pinned target, not just read. STS constrains the identity of the
# caller, not the destination of a later call, so the resource the API answered about must itself
# prove it is the intended one.
# Reads the ARN out of the snapshot and checks it against the pinned target. `|| return 0` here
# turned a read failure into a successful empty ARN, which the caller then read as "no tag" and
# went on to retag a table that might belong to another patch.
ARN_CHECKED=""
require_table_arn() {{
  local arn want
  arn="$(require_snapshot_field TableArn)"
  [[ -n "$arn" ]] || {{
    echo "FATAL: the snapshot carries no TableArn — refusing to act on an unidentified table" >&2
    exit "$READ_FAILED_RC"
  }}
  want="arn:aws:dynamodb:${{TARGET_REGION}}:${{TARGET_ACCOUNT}}:table/${{TABLE}}"
  [[ "$arn" == "$want" ]] || {{
    echo "FATAL: the API answered about $arn, but this kit targets $want" >&2
    exit 3
  }}
  ARN_CHECKED="$arn"
}}

# Ownership evidence, read into globals so a failure can exit the script rather than be mistaken
# for "no tag" — which is what let a table belonging to a DIFFERENT patch be adopted when
# list-tags happened to fail. Requires require_table_arn to have run.
OWNER_TAG_FOUND=""
ORIGIN_TAG_FOUND=""
require_owner_tags() {{
  local out
  if ! out="$(aws dynamodb list-tags-of-resource --region "$REGION" \\
      --resource-arn "$ARN_CHECKED" --output json 2>&1)"; then
    printf '%s\n' "$out" >&2
    # "The table is gone" is not the same failure as "I could not read its tags", and lumping them
    # into one code sends a driver to retry something that will never succeed.
    if printf '%s' "$out" | grep -q 'ResourceNotFoundException'; then
      echo "FATAL: $TABLE disappeared while reading its tags." >&2
      exit 40
    fi
    echo "FATAL: cannot read the tags of $TABLE, so ownership is unknown. Refusing to treat an" >&2
    echo "       unreadable tag set as 'no tag', which would adopt another patch's table." >&2
    exit "$(classify_aws_error "$out")"
  fi
  # A parse failure is a read failure with its own code, not a bare 1 from `set -e`.
  local parsed
  parsed="$(printf '%s' "$out" | python3 -c "import json,sys
t={{x['Key']: x['Value'] for x in json.load(sys.stdin).get('Tags') or []}}
print('%s\\t%s' % (t.get('$OWNER_TAG_KEY') or 'NO_TAG', t.get('oc-patch-origin') or 'NO_ORIGIN'))")" \\
    || {{
      echo "FATAL: cannot parse the tag list of $TABLE" >&2
      exit "$READ_FAILED_RC"
    }}
  OWNER_TAG_FOUND="${{parsed%%$'\\t'*}}"
  ORIGIN_TAG_FOUND="${{parsed##*$'\\t'}}"
  return 0
}}

# Every safety-critical read is THREE-STATE: the value, a definite ABSENT, or READ_FAILED.
# Collapsing them with `|| true` turned an AccessDenied or a throttle into an empty string, which
# the caller read as "the table does not exist" — and then created one. A read that failed is not
# evidence of anything.
READ_FAILED_RC=46

# Returns 0 with the JSON, 2 for "not there right now", or the CLASS of the failure (41/46/49) so a
# driver learns whether to retry. A single 1 for every failure made AccessDenied and a throttle
# indistinguishable at the one place that decides whether to create a table.
# The absence test requires the exception name AND a DescribeTable-shaped message, so an unrelated
# error that happens to mention the string cannot be read as "the table is gone".
READ_ERR=""
describe_table_json() {{
  local out
  if out="$(aws dynamodb describe-table --region "$REGION" --table-name "$EXPECTED_ARN" \\
      --output json 2>&1)"; then
    READ_ERR=""
    printf '%s' "$out"
    return 0
  fi
  READ_ERR="$out"
  if printf '%s' "$out" | grep -q 'ResourceNotFoundException' \\
     && printf '%s' "$out" | grep -qiE 'requested resource not found|describe.table'; then
    return 2
  fi
  printf '%s\\n' "$out" >&2
  return "$(classify_aws_error "$out")"
}}

# A three-state read CANNOT report through a command substitution: `exit` inside `$(...)` ends only
# the sub-shell, so the caller reads an empty string and carries on — which is the very confusion
# this was meant to remove. So the value lands in a global and the RETURN CODE carries the state.
#
#   0 = OK (TABLE_SNAPSHOT holds the DescribeTable JSON)
#   2 = definitely absent
#   1 = could not tell
TABLE_SNAPSHOT=""
read_table() {{
  local rc
  TABLE_SNAPSHOT=""
  TABLE_SNAPSHOT="$(describe_table_json)" && rc=0 || rc=$?
  return "$rc"
}}

# Fails the SCRIPT (not a sub-shell) on an unreadable table, because treating unreadable as absent
# would create a second table.
require_table_read() {{
  read_table && return 0
  local rc=$?
  [[ "$rc" -eq 2 ]] && return 2
  echo "FATAL: cannot read table $TABLE, and the answer was not 'it does not exist'." >&2
  echo "       Refusing to treat an unreadable table as absent: that would create a second one." >&2
  # The class describe_table_json determined, propagated: 41 retry, 49 needs a human, 46 unknown.
  exit "$rc"
}}

# Reads a field out of the snapshot require_table_read already fetched, so one logical check is one
# API call and cannot mix two generations of a deleted-and-recreated table.
# A parse failure is a read failure, not an empty field: an empty status would be compared against
# ACTIVE and reported as drift, sending an operator after a table that is fine.
# A parse failure is a read failure, not an empty field — and it CANNOT be reported by exiting:
# this is called inside `$(...)` at several sites, where `exit` ends only the sub-shell and the
# caller compares an empty string. That is the same trap fixed once already, reintroduced here.
SNAPSHOT_VALUE=""
snapshot_field() {{
  SNAPSHOT_VALUE=""
  SNAPSHOT_VALUE="$(printf '%s' "$TABLE_SNAPSHOT" | python3 -c "import json,sys
d=json.load(sys.stdin)['Table']
print(d.get('$1') or '')")" || return 1
  printf '%s' "$SNAPSHOT_VALUE"
}}

# Same value, but fails the SCRIPT on a parse error. Used everywhere a bare value is needed.
require_snapshot_field() {{
  snapshot_field "$1" >/dev/null || {{
    echo "FATAL: cannot parse '$1' out of the DescribeTable snapshot for $TABLE" >&2
    exit "$READ_FAILED_RC"
  }}
  printf '%s' "$SNAPSHOT_VALUE"
}}

# Waits for ACTIVE. Returns 0 when it arrives, 1 on timeout, and exits on a read failure — the
# status is never squeezed through a command substitution.
# Waits for ACTIVE. Returns 0 when it arrives, 1 on timeout, and exits on a read failure — the
# status never travels through a command substitution.
#
# DescribeTable can answer ResourceNotFoundException for a short while after a successful
# CreateTable (its metadata is eventually consistent). Treating that as "gone" made a successful
# creation exit 42, so while waiting it counts as NOT YET VISIBLE and polling continues.
wait_active() {{
  local deadline rc
  deadline=$(( $(date +%s) + ${{OC_PATCH_DDB_TIMEOUT:-300}} ))
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    read_table && rc=0 || rc=$?
    case "$rc" in
      0) snapshot_field TableStatus >/dev/null || {{
           echo "FATAL: cannot parse TableStatus while waiting for $TABLE" >&2
           exit "$READ_FAILED_RC"
         }}
         [[ "$SNAPSHOT_VALUE" == "ACTIVE" ]] && return 0 ;;
      2) ;;  # not visible yet — keep waiting rather than calling a created table absent
      *) echo "FATAL: cannot read $TABLE while waiting for ACTIVE" >&2
         # The class read_table determined, not a blanket 46: a throttle here is retryable and an
         # AccessDenied is not, and a driver branches on exactly that difference.
         exit "$rc" ;;
    esac
    sleep 5
  done
  return 1
}}

# "Already exists with the right key" is a comparison, not a guess. A table that exists with a
# DIFFERENT key identity is the one case this lane must refuse: a primary key cannot be changed in
# place, so the feature reading the table breaks in a way no rerun fixes.
IDENTITY_PY={identity_py}

# Reads the key out of the snapshot require_table_read fetched, so one logical check is one API
# call and two generations of a deleted-and-recreated table cannot be mixed.
# A parse failure exits with the read-failure code rather than leaking a bare 1 from `set -e`: the
# exit-code protocol is what an unattended driver branches on.
live_key_identity() {{
  local out
  out="$(printf '%s' "$TABLE_SNAPSHOT" | python3 -c "import json,sys
d=json.load(sys.stdin)['Table']
print(json.dumps({{'k': d.get('KeySchema') or [], 'a': d.get('AttributeDefinitions') or []}}))" \\
    | python3 -c "$IDENTITY_PY")" || {{
      echo "FATAL: cannot parse the key identity of $TABLE" >&2
      exit "$READ_FAILED_RC"
    }}
  printf '%s' "$out"
}}

want_key_identity() {{
  printf '{{"k":%s,"a":%s}}' "$KEY_SCHEMA" "$ATTR_DEFS" | python3 -c "$IDENTITY_PY"
}}

# PITR has its own describe call; the create/update call returning 200 is not evidence it is on.
# Every call that supports it addresses the table by ARN, not by name. A bare name is resolved
# fresh by each `aws` process, so a credential_process handing back a different account mid-run
# would answer about a DIFFERENT table with the same name, and each individual check would still
# pass. The ARN pins account, region and table in one string.
EXPECTED_ARN="arn:aws:dynamodb:${{TARGET_REGION}}:${{TARGET_ACCOUNT}}:table/${{TABLE}}"

# ResourceInUseException is deliberately NOT here. On CreateTable it means "the table already
# exists", which is retryable — a rerun adopts or resumes it. On other calls it means the table is
# busy. One classifier cannot say both, so the create path handles it explicitly and this function
# stays honest about the errors it can classify without knowing the caller.
# Prints the status, or returns non-zero on a read failure — never an empty string a caller could
# read as "off".
# Returns the status, or the failure CLASS (41/46/49). Returning a bare 1 made every caller exit
# 46, so a throttle and an AccessDenied sent an unattended driver down the same branch.
pitr_status() {{
  local out
  out="$(aws dynamodb describe-continuous-backups --region "$REGION" \\
    --table-name "$EXPECTED_ARN" \\
    --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' \\
    --output text 2>&1)" || {{
      printf '%s\n' "$out" >&2
      echo "FATAL: cannot read the PITR setting of $TABLE" >&2
      return "$(classify_aws_error "$out")"
    }}
  printf '%s' "$out"
}}

ttl_status() {{
  local out
  out="$(aws dynamodb describe-time-to-live --region "$REGION" --table-name "$EXPECTED_ARN" \\
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>&1)" || {{
      printf '%s\n' "$out" >&2
      echo "FATAL: cannot read the TTL setting of $TABLE" >&2
      return "$(classify_aws_error "$out")"
    }}
  printf '%s' "$out"
}}

# On-demand throughput caps, checked from the snapshot. Missing or -1 is uncapped; a positive value
# throttles the application even though BillingModeSummary still says PAY_PER_REQUEST.
assert_no_throughput_cap() {{
  local caps
  caps="$(printf '%s' "$TABLE_SNAPSHOT" | python3 -c "import json,sys
d=json.load(sys.stdin)['Table']
t=d.get('OnDemandThroughput') or {{}}
vals=[t.get('MaxReadRequestUnits'), t.get('MaxWriteRequestUnits')]
print(','.join(str(v) for v in vals if v is not None and int(v) > 0))")" || {{
    echo "FATAL: cannot parse the on-demand throughput of $TABLE" >&2
    exit "$READ_FAILED_RC"
  }}
  [[ -z "$caps" ]] || {{
    echo "FATAL: $TABLE has on-demand throughput capped at $caps. The billing mode reads as" >&2
    echo "       $BILLING but the application would be throttled. This patch declares no cap." >&2
    exit 40
  }}
}}

"""


def _apply(common):
    return f"""#!/usr/bin/env bash
set -euo pipefail
{common}

# Ensure PITR matches what the patch declared, proven by reading the API back. Shared by the adopt
# path and the create path: a table found ACTIVE without PITR gets the same treatment as a new one.
ensure_pitr() {{
  [[ "$WANT_PITR" == "true" ]] || return 0
  local now
  now="$(pitr_status)" || exit $?
  [[ "$now" == "ENABLED" ]] && {{ echo "PITR already ENABLED on $TABLE"; return 0; }}
  # By ARN, like every other call: a name is resolved fresh by each `aws` process, and PITR is the
  # one write here that would otherwise land on a same-named table in another account.
  pitr_err="$(aws dynamodb update-continuous-backups --region "$REGION" \\
    --table-name "$EXPECTED_ARN" \\
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true 2>&1 >/dev/null)" || {{
      printf '%s\n' "$pitr_err" >&2
      echo "FATAL: could not enable PITR on $TABLE" >&2
      exit "$(classify_aws_error "$pitr_err")"
    }}
  local pd
  pd=$(( $(date +%s) + ${{OC_PATCH_DDB_TIMEOUT:-300}} ))
  while [[ "$(date +%s)" -lt "$pd" ]]; do
    now="$(pitr_status)" || exit $?
    [[ "$now" == "ENABLED" ]] && break
    sleep 5
  done
  [[ "$now" == "ENABLED" ]] || {{
    echo "FATAL: PITR reports '$now' on $TABLE, expected ENABLED" >&2
    exit 43
  }}
  echo "PITR ENABLED on $TABLE (read back from the API)"
}}

# The read-only half of adoption: everything that can disqualify a table, checked before the run
# writes anything at all.
assert_shape_readonly() {{
  local mode ttl gsi count
  mode="$(printf '%s' "$TABLE_SNAPSHOT" | python3 -c "import json,sys
d=json.load(sys.stdin)['Table']
print((d.get('BillingModeSummary') or {{}}).get('BillingMode') or '')")" || {{
    echo "FATAL: cannot parse the billing mode of $TABLE" >&2; exit "$READ_FAILED_RC"; }}
  [[ "$mode" == "$BILLING" ]] || {{
    echo "FATAL: $TABLE has billing mode '$mode', this patch declares $BILLING." >&2
    echo "       Refusing to adopt a table whose cost model is not the declared one." >&2
    exit 40
  }}
  # PAY_PER_REQUEST is not the whole cost story: an on-demand table can carry positive
  # MaxRead/MaxWriteRequestUnits, which throttles the application while the billing mode still
  # reads as declared. Absent or -1 means uncapped.
  assert_no_throughput_cap
  ttl="$(ttl_status)" || exit $?
  [[ "$ttl" == "DISABLED" ]] || {{
    echo "FATAL: $TABLE has TTL '$ttl'. This patch never enables TTL, so something else is" >&2
    echo "       expiring items out of this table. Refusing to adopt it." >&2
    exit 40
  }}
  gsi="$(printf '%s' "$TABLE_SNAPSHOT" | python3 -c "import json,sys
d=json.load(sys.stdin)['Table']
n=len(d.get('GlobalSecondaryIndexes') or [])+len(d.get('LocalSecondaryIndexes') or [])
print(n)")" || {{
    echo "FATAL: cannot parse the index list of $TABLE" >&2; exit "$READ_FAILED_RC"; }}
  [[ "$gsi" == "0" ]] || {{
    echo "FATAL: $TABLE carries $gsi secondary index(es) this patch did not declare." >&2
    echo "       Their projections and capacity are not free. Refusing to adopt it." >&2
    exit 40
  }}
}}

# Everything the ACTIVE path has to establish, in one place, so the CREATING path can reach the
# same conclusions instead of assuming the table is ours.
adopt_or_resume() {{
  require_table_arn
  local live want
  live="$(live_key_identity)"
  want="$(want_key_identity)"
  if [[ "$live" != "$want" ]]; then
    echo "FATAL: table $TABLE exists with a different key identity" >&2
    echo "       live: $live" >&2
    echo "       patch expects: $want" >&2
    echo "       A primary key cannot be changed in place. Resolve by hand." >&2
    exit 40
  fi
  # Every READ-ONLY shape check runs BEFORE anything is written. Tagging and enabling PITR first
  # meant a table with the wrong billing mode, an enabled TTL or an extra index could be mutated in
  # this run and only rejected by verify afterwards — the production resource had already changed.
  assert_shape_readonly
  require_owner_tags
  if [[ "$OWNER_TAG_FOUND" == "$OWNER_TAG_VALUE" ]]; then
    # Our tag. Trust the CLOUD record of how it got here, not the local file.
    case "$ORIGIN_TAG_FOUND" in
      created|resumed) ORIGIN=resumed ;;
      adopted)         ORIGIN=adopted ;;
      *)
        # Our owner tag but an origin this version does not know. Treating it as `resumed` meant
        # PITR was enabled and success reported, with only verify rejecting it afterwards — after
        # the write had already happened.
        echo "FATAL: $TABLE carries our owner tag but oc-patch-origin='$ORIGIN_TAG_FOUND'," >&2
        echo "       which this version does not recognise. Refusing to write to a table whose" >&2
        echo "       history it cannot read." >&2
        exit 40 ;;
    esac
  elif [[ "$OWNER_TAG_FOUND" == "NO_TAG" ]]; then
    # A same-named, same-key table with no owner tag was made by something else. Adopting it
    # unattended means taking responsibility for data this patch knows nothing about.
    if [[ "${{OC_PATCH_DDB_ADOPT:-}}" != "yes" ]]; then
      echo "FATAL: $TABLE already exists with the right key but no $OWNER_TAG_KEY tag, so this" >&2
      echo "       patch did not create it. Set OC_PATCH_DDB_ADOPT=yes to adopt it deliberately." >&2
      exit 45
    fi
    tag_err="$(aws dynamodb tag-resource --region "$REGION" --resource-arn "$ARN_CHECKED" \\
      --tags "Key=$OWNER_TAG_KEY,Value=$OWNER_TAG_VALUE" "Key=oc-patch-origin,Value=adopted" \\
      2>&1 >/dev/null)" || true
    # Captured into a variable rather than a predictable /tmp path, in full rather than the first
    # line, because the classifier needs the whole message.
    # No immediate failure gate here: ListTagsOfResource is eventually consistent, so a read right
    # after the write can legitimately miss the tag. Straight into the poll.
    # Poll until the tag is READABLE: writing it is not the same as being able to prove it, and
    # verify will look for exactly this.
    local td
    td=$(( $(date +%s) + ${{OC_PATCH_TAG_TIMEOUT:-90}} ))
    while [[ "$(date +%s)" -lt "$td" ]]; do
      require_owner_tags
      # BOTH tags: the owner says whose it is, the origin says how it got here, and verify checks
      # both — so both must be readable before the adoption is claimed.
      [[ "$OWNER_TAG_FOUND" == "$OWNER_TAG_VALUE" && "$ORIGIN_TAG_FOUND" == "adopted" ]] && break
      sleep 3
    done
    [[ "$OWNER_TAG_FOUND" == "$OWNER_TAG_VALUE" && "$ORIGIN_TAG_FOUND" == "adopted" ]] || {{
      [[ -n "$tag_err" ]] && printf '%s\n' "$tag_err" >&2
      echo "FATAL: tagged $TABLE but the tags do not read back (owner='$OWNER_TAG_FOUND'," >&2
      echo "       origin='$ORIGIN_TAG_FOUND'), so the adoption cannot be verified." >&2
      exit "$(classify_aws_error "${{tag_err:-unreadable}}")"
    }}
    ORIGIN=adopted
  else
    echo "FATAL: $TABLE carries $OWNER_TAG_KEY=$OWNER_TAG_FOUND, a DIFFERENT patch." >&2
    echo "       Refusing to take over another patch's table." >&2
    exit 40
  fi
  ensure_pitr
  printf '%s' "$ORIGIN" > "$STATE_DIR/origin"
  printf '%s' "$OWNER_TAG_VALUE" > "$STATE_DIR/applied"
  echo "SKIP $TABLE already exists with the expected key identity (origin=$ORIGIN)"
}}

ORIGIN=created
require_table_read && READ_RC=0 || READ_RC=$?

# Treating any non-empty status as "already done" was wrong in three ways, each reporting a broken
# table as success: CREATING means an earlier run may have been interrupted, DELETING means the
# table is going away underneath us, an unknown status means we do not know.
if [[ "$READ_RC" -eq 0 ]]; then
  status="$(require_snapshot_field TableStatus)"
  case "$status" in
    ACTIVE)
      adopt_or_resume
      exit 0 ;;
    UPDATING)
      # An UPDATING table is being changed by something else right now. Tagging it or enabling PITR
      # mid-update writes into someone else's operation, so wait for it to settle first.
      echo "$TABLE is UPDATING; waiting for it to settle before touching it"
      wait_active || {{
        echo "FATAL: $TABLE is still UPDATING; rerun once it settles (this is idempotent)" >&2
        exit 41
      }}
      adopt_or_resume
      exit 0 ;;
    CREATING)
      # "CREATING" does NOT mean "we started it". Assuming so made the recipe enable PITR on, and
      # write an anchor for, a table someone else was mid-creating.
      echo "$TABLE is CREATING; waiting for it to settle before deciding whose it is"
      wait_active || {{
        echo "FATAL: $TABLE is still not ACTIVE; rerun once it settles (this is idempotent)" >&2
        exit 42
      }}
      adopt_or_resume
      exit 0 ;;
    DELETING|ARCHIVING|ARCHIVED)
      echo "FATAL: $TABLE is $status — refusing to act on a table being removed." >&2
      echo "       Recreating it while a delete is in flight yields a table with none of the" >&2
      echo "       previous data and no record that it happened." >&2
      exit 40 ;;
    *)
      echo "FATAL: $TABLE reports an unrecognized status '$status' — refusing to guess." >&2
      exit 40 ;;
  esac
fi

# Definitely absent (require_table_read returned 2): create it. The tags go on in the SAME call, so
# there is no window where the table exists without the evidence of who made it.
# CreateTable is the ONE call that cannot take an ARN: the resource does not exist yet, so there is
# nothing to address. The account was pinned and checked against STS at the top of this script, and
# the ARN in the RESPONSE is checked below, which closes the same gap from the other side.
echo "CREATING $TABLE billing=$BILLING (by name: there is no ARN for a table that does not exist)"
if ! create_out="$(aws dynamodb create-table --region "$REGION" --table-name "$TABLE" \\
    --key-schema "$KEY_SCHEMA" --attribute-definitions "$ATTR_DEFS" \\
    --billing-mode "$BILLING" \\
    --tags "Key=$OWNER_TAG_KEY,Value=$OWNER_TAG_VALUE" \\
      "Key=oc-patch-origin,Value=created" \\
      "Key=oc-patch-account,Value=$TARGET_ACCOUNT" \\
      --query 'TableDescription.TableArn' --output text 2>&1)"; then
  # A race with another writer is retryable and must not look like a permissions problem.
  # Checked here rather than in classify_aws_error because its meaning is call-specific: on
  # CreateTable it means the table already exists, which a rerun resolves by adopting or resuming.
  if printf '%s' "$create_out" | grep -q 'ResourceInUseException'; then
    echo "NOTE $TABLE appeared while we were creating it (another writer won the race)." >&2
    echo "     Rerun: this recipe is idempotent and will adopt or resume it." >&2
    exit 41
  fi
  printf '%s\n' "$create_out" >&2
  create_rc="$(classify_aws_error "$create_out")"
  case "$create_rc" in
    41) echo "FATAL: create-table hit a transient service condition — safe to rerun." >&2 ;;
    49) echo "FATAL: create-table was refused for a permissions, parameter or already-in-use" >&2
        echo "       reason. Retrying will not help; a human has to act." >&2 ;;
    *)  echo "FATAL: create-table failed on $TABLE for an unclassified reason." >&2 ;;
  esac
  exit "$create_rc"
fi
# The response carries the ARN of what was ACTUALLY created; check it before trusting a later
# DescribeTable, which is a separate call that could resolve differently.
# The ARN comes straight out of the CreateTable response, asked for with --query so the shape does
# not depend on the caller's configured output format. Parsing JSON with `|| true` meant a
# text/table/yaml default produced an empty string and the check was skipped entirely — on the one
# call that could have created the table in the wrong account.
created_arn="$create_out"
[[ -n "$created_arn" && "$created_arn" != "None" ]] || {{
  echo "FATAL: create-table returned no TableArn, so what it created cannot be identified." >&2
  echo "       A table may now exist; rerun to inspect and adopt or resume it." >&2
  exit "$READ_FAILED_RC"
}}
[[ "$created_arn" == "$EXPECTED_ARN" ]] || {{
  echo "FATAL: create-table made $created_arn, but this kit targets $EXPECTED_ARN" >&2
  exit 3
}}
echo "CREATED $created_arn"

wait_active || {{
  echo "FATAL: $TABLE did not reach ACTIVE within the timeout" >&2
  echo "       It may still be creating; rerun to pick it up (this recipe is idempotent)." >&2
  exit 42
}}
require_table_arn
ensure_pitr

# Re-read the tags so the anchor records something proven, not something written — and POLL,
# because ListTagsOfResource is eventually consistent even for tags supplied to CreateTable itself.
# Asserting immediately made a correct creation fail: the adopt path already learned this, and the
# create path had the same bug until a test actually RAN the script.
tag_deadline=$(( $(date +%s) + ${{OC_PATCH_TAG_TIMEOUT:-90}} ))
while [[ "$(date +%s)" -lt "$tag_deadline" ]]; do
  require_owner_tags
  [[ "$OWNER_TAG_FOUND" == "$OWNER_TAG_VALUE" && "$ORIGIN_TAG_FOUND" == "created" ]] && break
  sleep 3
done
[[ "$OWNER_TAG_FOUND" == "$OWNER_TAG_VALUE" && "$ORIGIN_TAG_FOUND" == "created" ]] || {{
  echo "FATAL: created $TABLE but its tags do not read back (owner='$OWNER_TAG_FOUND'," >&2
  echo "       origin='$ORIGIN_TAG_FOUND'); the creation cannot be proven." >&2
  exit 44
}}
printf '%s' "$OWNER_TAG_VALUE" > "$STATE_DIR/applied"
printf 'created' > "$STATE_DIR/origin"
echo "APPLIED $TABLE created and ACTIVE"
echo "NOTE this table is invisible to CloudFormation. A later cdk deploy that declares the same"
echo "     table will fail with AlreadyExists unless it is imported into the stack first."
"""


def _verify(common):
    return f"""#!/usr/bin/env bash
set -euo pipefail
{common}

# Read-only. What is asserted is live state — one DescribeTable snapshot for the shape, plus the
# calls that own PITR and the tags. A read failure is never a pass and never a drift: it gets its
# own exit code, because an unattended driver retries a read failure and escalates a drift.
[[ -f "$STATE_DIR/applied" ]] || {{
  echo "FATAL: no patch-owned anchor for $TABLE" >&2
  exit 44
}}
anchor="$(cat "$STATE_DIR/applied" 2>/dev/null || true)"
[[ "$anchor" == "$OWNER_TAG_VALUE" ]] || {{
  echo "FATAL: the anchor says '$anchor', this kit is '$OWNER_TAG_VALUE' — the state belongs" >&2
  echo "       to a different patch or version" >&2
  exit 44
}}

require_table_read && READ_RC=0 || READ_RC=$?
if [[ "$READ_RC" -eq 2 ]]; then
  echo "DRIFT: $TABLE does not exist, but this patch recorded creating it" >&2
  exit 40
fi
status="$(require_snapshot_field TableStatus)"
[[ "$status" == "ACTIVE" ]] || {{
  echo "DRIFT: $TABLE is '$status', expected ACTIVE" >&2
  exit 40
}}
require_table_arn
live="$(live_key_identity)"
want="$(want_key_identity)"
[[ "$live" == "$want" ]] || {{
  echo "DRIFT: $TABLE key identity is $live, expected $want" >&2
  exit 40
}}

# Provenance comes from the CLOUD, whatever the local file says. A hand-written origin used to skip
# this check entirely, so a local file was enough to pass on another patch's table.
require_owner_tags
[[ "$OWNER_TAG_FOUND" == "$OWNER_TAG_VALUE" ]] || {{
  echo "DRIFT: $TABLE carries $OWNER_TAG_KEY='$OWNER_TAG_FOUND', expected" >&2
  echo "       '$OWNER_TAG_VALUE'. A local anchor is not evidence of what is true in AWS." >&2
  exit 40
}}
case "$ORIGIN_TAG_FOUND" in
  created|resumed) ;;
  adopted)
    echo "NOTE oc-patch-origin=adopted: this patch took ownership of a table it did not create." \\
      >&2 ;;
  *)
    echo "DRIFT: $TABLE carries no recognizable oc-patch-origin tag ('$ORIGIN_TAG_FOUND')" >&2
    exit 40 ;;
esac

if [[ "$WANT_PITR" == "true" ]]; then
  now_pitr="$(pitr_status)" || exit $?
  [[ "$now_pitr" == "ENABLED" ]] || {{
    echo "DRIFT: $TABLE PITR is '$now_pitr', expected ENABLED" >&2
    exit 40
  }}
fi

# Not being able to READ the billing mode is not the same as it being correct. Passing without the
# evidence is how a check turns into decoration.
mode="$(printf '%s' "$TABLE_SNAPSHOT" | python3 -c "import json,sys
d=json.load(sys.stdin)['Table']
print((d.get('BillingModeSummary') or {{}}).get('BillingMode') or '')")" || {{
  echo "FATAL: cannot parse the billing mode of $TABLE" >&2
  exit "$READ_FAILED_RC"
}}
if [[ -z "$mode" ]]; then
  echo "DRIFT: cannot read the billing mode of $TABLE; expected $BILLING, and this will not" >&2
  echo "       report VERIFIED without the evidence" >&2
  exit 40
elif [[ "$mode" != "$BILLING" ]]; then
  echo "DRIFT: $TABLE billing mode is $mode, expected $BILLING" >&2
  exit 40
fi

# TTL is asserted OFF unless the patch asked for it: a table adopted with TTL enabled would keep
# deleting items, and a shape check that ignores TTL would call that verified.
ttl="$(ttl_status)" || exit $?
[[ "$ttl" == "DISABLED" ]] || {{
  echo "DRIFT: $TABLE has TTL '$ttl'. This patch never enables TTL, so an enabled TTL means" >&2
  echo "       something else is expiring items out of this table." >&2
  exit 40
}}

# No secondary indexes unless declared: an adopted table could carry a GSI this patch knows
# nothing about, and its projections and capacity are not free.
# `|| true` here was a definite false green: a parse failure produced an empty string, and empty
# is exactly how "no indexes" is spelled. So the COUNT is printed and asserted, and a parse failure
# exits as a read failure instead of reading as a clean table.
gsi="$(printf '%s' "$TABLE_SNAPSHOT" | python3 -c "import json,sys
d=json.load(sys.stdin)['Table']
names=[i['IndexName'] for i in (d.get('GlobalSecondaryIndexes') or [])]
names+=[i['IndexName'] for i in (d.get('LocalSecondaryIndexes') or [])]
print('%d %s' % (len(names), ','.join(sorted(names))))")" || {{
  echo "FATAL: cannot parse the index list of $TABLE — refusing to read a parse failure as" >&2
  echo "       'no indexes'" >&2
  exit "$READ_FAILED_RC"
}}
gsi_count="${{gsi%% *}}"
gsi_names="${{gsi#* }}"
[[ "$gsi_count" == "0" ]] || {{
  echo "DRIFT: $TABLE carries $gsi_count secondary index(es) this patch did not declare:" >&2
  echo "       $gsi_names" >&2
  exit 40
}}

assert_no_throughput_cap

echo "VERIFIED $TABLE is ACTIVE with the expected key identity, PITR, billing mode, no TTL, no"
echo "         no undeclared indexes (origin=$ORIGIN_TAG_FOUND)"
"""


def _rollback(common):
    # The refusal is printed BEFORE the shared preamble runs. Sourcing it first meant an STS
    # failure or an unwritable state directory could exit 1 or 3 before the fixed refusal code 25
    # was ever emitted, and an unattended driver would read that as an ordinary error.
    return f"""#!/usr/bin/env bash
set -uo pipefail
if [[ "${{1:-}}" != "--after-preamble" ]]; then
  echo "ROLLBACK REFUSED: creating a table is not reversible by this driver." >&2
  echo "  'Undo' means deleting it, which destroys whatever has been written since." >&2
  echo "  Nothing was changed by this call. Run with --after-preamble for the full detail," >&2
  echo "  which needs credentials." >&2
  exit 25
fi
set -e
{common}

# This lane has no rollback, and saying so is the point. Deleting a table the platform now reads
# destroys data, and a driver must never do that on its own. An operator who genuinely wants the
# table gone does it deliberately, after looking at what is in it.
echo "ROLLBACK REFUSED for $TABLE" >&2
echo "  Creating a table is not reversible by this driver: 'undo' means deleting it, which" >&2
echo "  destroys whatever has been written since. Nothing was changed by this call." >&2
echo "  If the table must go, snapshot it first and delete it deliberately:" >&2
echo "    aws dynamodb create-backup --region $REGION --table-name $TABLE \\\\" >&2
echo "      --backup-name ${{TABLE}}-predelete" >&2
echo "    aws dynamodb delete-table --region $REGION --table-name $TABLE" >&2
exit 25
"""


def compile_ddb_create_kit(kit, _repo=None):
    with open(os.path.join(kit, "manifest.json")) as handle:
        manifest = json.load(handle)
    specs = manifest.get("ddb_tables") or []
    if len(specs) != 1:
        raise SystemExit(
            "a compiled DynamoDB create kit declares exactly one entry in `ddb_tables` "
            f"(found {len(specs)}) — one table per kit, so each gets its own verify"
        )
    spec = specs[0]
    if not spec.get("table"):
        raise SystemExit("ddb_tables[0].table is required")
    # The target is pinned in the KIT, not supplied at run time. STS proves only that the caller's
    # credentials belong to someone; without a declared target, any valid credentials would create
    # and verify the table successfully, in whatever account happened to be configured.
    for coord in ("target_account", "target_region"):
        if not (spec.get(coord) or "").strip():
            raise SystemExit(
                f"ddb_tables[0].{coord} is required: the run checks the live caller against it, "
                f"so a kit built for one account cannot silently create the table in another."
            )
    if not str(spec["target_account"]).isdigit() or len(str(spec["target_account"])) != 12:
        raise SystemExit("ddb_tables[0].target_account must be a 12-digit account id")
    pk = spec.get("partition_key") or {}
    if not pk.get("name") or not pk.get("type"):
        raise SystemExit(
            "ddb_tables[0].partition_key needs both name and type — a wrong primary key cannot "
            "be corrected in place"
        )
    for label, key in (("partition_key", pk), ("sort_key", spec.get("sort_key") or {})):
        if key and key.get("type") and key["type"] not in KEY_TYPES:
            raise SystemExit(
                f"ddb_tables[0].{label}.type must be one of {sorted(KEY_TYPES)}"
            )
    mode = spec.get("billing_mode") or "PAY_PER_REQUEST"
    if mode not in BILLING_MODES:
        raise SystemExit(
            f"ddb_tables[0].billing_mode must be one of {sorted(BILLING_MODES)}"
        )
    if mode == "PROVISIONED":
        raise SystemExit(
            "ddb_tables[0].billing_mode=PROVISIONED is not supported: it needs capacity numbers "
            "that depend on the customer's traffic, and guessing them either throttles the "
            "feature or bills for capacity nobody asked for. Use PAY_PER_REQUEST, or create the "
            "table by hand."
        )
    if manifest.get("status") != "READY":
        raise SystemExit(
            f"preflight: manifest.status={manifest.get('status')} (not READY) — a flagged kit "
            f"is not auto-appliable"
        )
    # The table will not exist in the template. That is acceptable and sometimes the only option,
    # but it must be declared, because the next stack update fails on it.
    if not (spec.get("cfn_follow_up") or "").strip():
        raise SystemExit(
            "ddb_tables[0].cfn_follow_up is required: a table created by CLI is invisible to "
            "CloudFormation, so a later cdk deploy declaring the same table fails with "
            "AlreadyExists. Name the follow-up (an import, or the decision to keep it "
            "out-of-band) so it is not discovered during a future stack update."
        )

    rid = ddb_create_recipe_id(spec["table"])
    common = _common(manifest, spec)
    written = []
    for name, content in (
        ("apply.sh", _apply(common)),
        ("verify.sh", _verify(common)),
        ("rollback.sh", _rollback(common)),
    ):
        rel = f"lib/compiled/{rid}/{name}"
        dest = os.path.join(kit, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as handle:
            handle.write(content)
        os.chmod(dest, 0o755)
        manifest.setdefault("kit_files", {})[rel] = {"sha256": _sha(content.encode())}
        written.append(rel)

    with open(os.path.join(kit, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return {
        "resource_id": rid,
        "table": spec["table"],
        "billing_mode": mode,
        "reversible": False,
        "files": written,
    }


def main(argv):
    if len(argv) < 2:
        print(
            "usage: _compile_ddb_create.py <patch-kit> [<source-repo>]", file=sys.stderr
        )
        return 2
    print(json.dumps(compile_ddb_create_kit(os.path.abspath(argv[1])), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
