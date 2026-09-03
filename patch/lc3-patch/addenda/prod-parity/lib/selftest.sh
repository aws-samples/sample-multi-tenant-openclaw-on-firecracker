#!/usr/bin/env bash
# selftest.sh — proves verify-prod-parity.sh itself is sound, with NO AWS access.
# It replays a read-only forensic capture (the layout produced by the 2026-09-03 drift probe:
# lambda/<fn>/{config.latest.json,config.live.json,live.json,concurrency.json,esm.json,code.*.zip},
# ssm/parameters.json, ddb/tenants.describe.json, asg-lt/asg.json) through a fake `aws`, runs the
# verifier, then mutates one SSM value and proves the verifier turns red.
#
# Usage: bash lib/selftest.sh <forensic-dir> <gateway-root>
set -euo pipefail
FORENSIC="${1:?forensic dir}"; GW="${2:?gateway root}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cp -R "$FORENSIC" "$WORK/f"; F="$WORK/f"

cat >"$WORK/aws" <<'SH'
#!/usr/bin/env bash
# fake aws: serves the forensic capture. Any verb that is not a read is a selftest failure.
set -euo pipefail
F="${SELFTEST_FORENSIC:?}"; GW="${SELFTEST_GW:?}"
args=("$@"); svc=""; verb=""; fn=""; key=""; out=""
i=0
while [ $i -lt ${#args[@]} ]; do
  a="${args[$i]}"
  case "$a" in
    --region|--output|--path|--name|--table-name|--auto-scaling-group-names|--bucket) i=$((i+2)); continue ;;
    --function-name) fn="${args[$((i+1))]}"; i=$((i+2)); continue ;;
    --key) key="${args[$((i+1))]}"; i=$((i+2)); continue ;;
    --recursive|--no-with-decryption) i=$((i+1)); continue ;;
    -*) i=$((i+1)); continue ;;
  esac
  if [ -z "$svc" ]; then svc="$a"; elif [ -z "$verb" ]; then verb="$a"; else out="$a"; fi
  i=$((i+1))
done
case "$verb" in get-*|list-*|describe-*) ;; *) echo "SELFTEST: non-read verb '$svc $verb' reached aws" >&2; exit 99 ;; esac
base="${fn%%:*}"; ver="${fn#*:}"; [ "$ver" = "$fn" ] && ver=""
cfg="$F/lambda/$base/config.latest.json"; zip="$F/lambda/$base/code.latest.zip"
if [ -n "$ver" ] && [ -f "$F/lambda/$base/config.live.json" ]; then cfg="$F/lambda/$base/config.live.json"; zip="$F/lambda/$base/code.live.zip"; fi
case "$svc $verb" in
  "sts get-caller-identity") echo '{"Account":"000000000000","Arn":"arn:aws:iam::000000000000:user/selftest"}' ;;
  "lambda get-function-configuration") cat "$cfg" ;;
  "lambda get-alias") cat "$F/lambda/$base/live.json" ;;
  "lambda get-function") python3 -c 'import json,sys;print(json.dumps({"Configuration":json.load(open(sys.argv[1])),"Code":{"Location":"file://"+sys.argv[2]}}))' "$cfg" "$zip" ;;
  "lambda get-function-concurrency") c="$F/lambda/$base/concurrency.json"; [ -s "$c" ] && cat "$c" || echo '{}' ;;
  "lambda list-event-source-mappings") cat "$F/lambda/$base/esm.json" ;;
  "ssm get-parameters-by-path") python3 -c 'import json,sys;print(json.dumps({"Parameters":json.load(open(sys.argv[1]))}))' "$F/ssm/parameters.json" ;;
  "dynamodb describe-table") cat "$F/ddb/tenants.describe.json" ;;
  "autoscaling describe-auto-scaling-groups") cat "$F/asg-lt/asg.json" ;;
  "s3api get-object") cp "$GW/deploy/userdata/$(basename "$key")" "$out"; echo '{}' ;;
  *) echo "SELFTEST: unmapped read '$svc $verb'" >&2; exit 98 ;;
esac
SH
chmod +x "$WORK/aws"
export SELFTEST_FORENSIC="$F" SELFTEST_GW="$GW" PATH="$WORK:$PATH"

echo "== run 1: unmodified capture =="
set +e
bash "$HERE/verify-prod-parity.sh" --region selftest --gateway-root "$GW" --report "$WORK/r1.json" | tee "$WORK/r1.txt"
rc1=$?
set -e
python3 - "$WORK/r1.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))["rows"]
def v(h, sub): return [x["verdict"] for x in r if x["h"] == h and sub in x["check"]]
must_pass = [("H1", "api $LATEST core/create_deadline.py"), ("H1", "consumer $LATEST core/create_deadline.py"),
             ("H3", "deadline-sec/suspend"), ("H4", "consumer env HOST_SELECTION_SCORE_FLOOR"),
             ("H5", "openclaw-lifecycle.fifo MaximumConcurrency"), ("H5", "ReservedConcurrentExecutions"),
             ("H6", "GSI gsi_host"), ("H7", "control-ui-allowed-origins"), ("H10", "launch-vm.sh"), ("H14", "mixed instance types")]
bad = [(h, s, v(h, s)) for h, s in must_pass if v(h, s) != ["PASS"]]
assert not bad, f"expected PASS rows are not PASS: {bad}"
print("selftest: expected PASS rows all PASS")
PY

echo "== run 2: mutate ssm deadline-sec/suspend -> 180, expect FAIL =="
python3 - "$F/ssm/parameters.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
for x in p:
    if x["Name"].endswith("/deadline-sec/suspend"): x["Value"] = "180"
json.dump(p, open(sys.argv[1], "w"))
PY
set +e
bash "$HERE/verify-prod-parity.sh" --region selftest --gateway-root "$GW" --report "$WORK/r2.json" >"$WORK/r2.txt"
rc2=$?
set -e
grep -q 'FAIL  H3   ssm /openclaw/lifecycle/deadline-sec/suspend' "$WORK/r2.txt" || { echo "selftest: mutation not detected"; cat "$WORK/r2.txt"; exit 1; }
[ "$rc2" -eq 1 ] || { echo "selftest: exit code after mutation was $rc2, want 1"; exit 1; }
echo "selftest: mutation detected, exit code 1 as required"
echo "SELFTEST PASS (run1 exit=$rc1; FAIL rows in run1 are the capture's own drift, see r1 above)"
