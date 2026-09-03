#!/usr/bin/env bash
# verify-prod-parity.sh — READ-ONLY final check that an environment which applied lc3-patch
# matches the 2026-09-03 production baseline (H1, H3, H4, H5, H6, H7, H10, H14; H2 is
# informational). It never writes: only get-*/list-*/describe-* calls plus one HTTPS download of
# the function package that `lambda get-function` hands out.
#
# Usage:
#   bash lib/verify-prod-parity.sh --region <region> [--baseline baseline.json]
#        [--gateway-root <clone of the gateway branch>] [--report report.json]
# Exit: 0 = no FAIL, 1 = at least one FAIL, 2 = could not run (missing tool / unreadable target).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION=""; BASELINE="$HERE/../baseline.json"; REPORT=""; GATEWAY_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --baseline) BASELINE="$2"; shift 2 ;;
    --gateway-root) GATEWAY_ROOT="$2"; shift 2 ;;
    --report) REPORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$REGION" ] || { echo "--region is required" >&2; exit 2; }
[ -f "$BASELINE" ] || { echo "baseline not found: $BASELINE" >&2; exit 2; }
if [ -z "$GATEWAY_ROOT" ]; then
  GATEWAY_ROOT="$(cd "$HERE" && git rev-parse --show-toplevel 2>/dev/null || true)"
fi
for tool in aws python3 curl unzip; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export AWS_PAGER=""
A() { aws --region "$REGION" --output json "$@"; }

API_FN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["functions"]["api"])' "$BASELINE")"
CONSUMER_FN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["functions"]["consumer"])' "$BASELINE")"

# ---- collect (every call is a read) --------------------------------------------------------
A sts get-caller-identity >"$T/identity.json" || { echo "cannot read caller identity" >&2; exit 2; }
A lambda get-function-configuration --function-name "$API_FN" >"$T/cfg-api.json"
A lambda get-function-configuration --function-name "$CONSUMER_FN" >"$T/cfg-consumer.json"
A lambda get-alias --function-name "$API_FN" --name live >"$T/alias-api.json" 2>/dev/null || echo '{}' >"$T/alias-api.json"
LIVE_VER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("FunctionVersion",""))' "$T/alias-api.json")"
if [ -n "$LIVE_VER" ]; then
  A lambda get-function-configuration --function-name "$API_FN:$LIVE_VER" >"$T/cfg-api-live.json"
else
  echo '{}' >"$T/cfg-api-live.json"
fi
A lambda get-function-concurrency --function-name "$CONSUMER_FN" >"$T/conc-consumer.json" 2>/dev/null || echo '{}' >"$T/conc-consumer.json"
A lambda list-event-source-mappings --function-name "$API_FN" >"$T/esm-api.json"
A lambda list-event-source-mappings --function-name "$CONSUMER_FN" >"$T/esm-consumer.json"
A ssm get-parameters-by-path --path /openclaw --recursive --no-with-decryption >"$T/ssm.json"
A dynamodb describe-table --table-name openclaw-tenants >"$T/tenants.json"
A autoscaling describe-auto-scaling-groups --auto-scaling-group-names openclaw-hosts-asg >"$T/asg.json"

# package download: the URL is what get-function returns (presigned, read-only); file:// is
# accepted so the offline selftest can serve a local zip through the same path.
fetch_pkg() { # <function or function:version> <out.zip>
  local loc
  loc="$(A lambda get-function --function-name "$1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Code"]["Location"])')"
  curl -sSL -o "$2" "$loc"
}
fetch_pkg "$API_FN" "$T/pkg-api.zip"
fetch_pkg "$CONSUMER_FN" "$T/pkg-consumer.zip"
if [ -n "$LIVE_VER" ]; then fetch_pkg "$API_FN:$LIVE_VER" "$T/pkg-api-live.zip"; fi

# host scripts on S3 (H10): bucket comes from the API function's own env, never typed by hand.
ASSETS="$(python3 -c 'import json,sys;print((json.load(open(sys.argv[1])).get("Environment") or {}).get("Variables",{}).get("ASSETS_BUCKET",""))' "$T/cfg-api.json")"
mkdir -p "$T/s3"
if [ -n "$ASSETS" ]; then
  for name in $(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["host_scripts"]))' "$BASELINE"); do
    A s3api get-object --bucket "$ASSETS" --key "deployment/scripts/$name" "$T/s3/$name" >/dev/null 2>&1 || true
  done
fi

# ---- evaluate ------------------------------------------------------------------------------
python3 - "$T" "$BASELINE" "$GATEWAY_ROOT" "$REPORT" <<'PY'
import hashlib, json, os, sys, zipfile
T, BASELINE, GW, REPORT = sys.argv[1:5]
B = json.load(open(BASELINE))
rows = []
def add(h, verdict, what, got="", want=""):
    rows.append({"h": h, "verdict": verdict, "check": what, "got": str(got), "want": str(want)})
def J(name):
    p = os.path.join(T, name)
    try: return json.load(open(p))
    except Exception: return {}
def env(cfg): return (cfg.get("Environment") or {}).get("Variables") or {}
def sha(b): return hashlib.sha256(b).hexdigest()
def zip_sha(zip_name, member):
    p = os.path.join(T, zip_name)
    if not os.path.exists(p): return None
    try:
        with zipfile.ZipFile(p) as z: return sha(z.read(member))
    except KeyError: return "absent"
def eq(a, b, kind="str"):
    if kind == "float":
        try: return float(a) == float(b)
        except Exception: return False
    if kind == "bool": return str(a).lower() == str(b).lower()
    return str(a) == str(b)

api, consumer, live = J("cfg-api.json"), J("cfg-consumer.json"), J("cfg-api-live.json")
live_ver = J("alias-api.json").get("FunctionVersion", "")

# H1 — deployed create_deadline.py bytes
want = B["lambda_code"]["core/create_deadline.py"]["sha256"]
for tag, zn in (("api $LATEST", "pkg-api.zip"), ("consumer $LATEST", "pkg-consumer.zip")):
    got = zip_sha(zn, "core/create_deadline.py")
    add("H1", "PASS" if got == want else "FAIL", f"{tag} core/create_deadline.py sha256", got, want)
if live_ver:
    got = zip_sha("pkg-api-live.zip", "core/create_deadline.py")
    add("H1", "PASS" if got == want else "FAIL", f"api live (v{live_ver}) core/create_deadline.py sha256", got, want)
    same = api.get("CodeSha256") == live.get("CodeSha256")
    add("H1", "PASS" if same else "WARN", f"api live (v{live_ver}) package == $LATEST package",
        live.get("CodeSha256", "")[:12], api.get("CodeSha256", "")[:12])
else:
    add("H1", "WARN", "api alias live", "absent", "present")

# H2 — informational: backup step budget 60 == host TERM grace 60 -> backup handler falls back to 300
add("H2", "INFO", "backup budget 60s <= TERM grace 60s: backup/handler.py falls back to 300s (documented, expected)")

# H3 — SSM deadlines + fence lease; env deadline values are informational carriers
ssm = {p["Name"]: p.get("Value") for p in J("ssm.json").get("Parameters", [])}
for name, val in B["ssm"]["exact"].items():
    got = ssm.get(name, "<absent>")
    add("H3", "PASS" if eq(got, val) else "FAIL", f"ssm {name}", got, val)
for fn_tag, cfg in (("api", api), ("consumer", consumer)):
    e = env(cfg)
    for k, allowed in B["lambda_env"]["deadline_env_allowed"].items():
        got = e.get(k, "<absent>")
        add("H3", "PASS" if got in allowed else "FAIL", f"{fn_tag} env {k} (SSM wins at runtime)", got, "/".join(allowed))

# H4 — env knobs on api $LATEST, consumer, and the version the live alias serves
knobs = B["lambda_env"]["exact"]
def check_env(tag, cfg, fail_verdict):
    e = env(cfg)
    for k, spec in knobs.items():
        got = e.get(k, "<absent>")
        ok = eq(got, spec["value"], spec.get("kind", "str"))
        add("H4", "PASS" if ok else fail_verdict, f"{tag} env {k}", got, spec["value"])
check_env("api $LATEST", api, "FAIL")
check_env("consumer", consumer, "FAIL")
if live_ver: check_env(f"api live (v{live_ver})", live, "FAIL")

# H5 — event source mappings + reserved concurrency
esms = J("esm-api.json").get("EventSourceMappings", []) + J("esm-consumer.json").get("EventSourceMappings", [])
for suffix, spec in B["esm"].items():
    m = [x for x in esms if str(x.get("EventSourceArn", "")).endswith(suffix)]
    if not m:
        add("H5", "FAIL", f"ESM on queue …{suffix}", "absent", "present"); continue
    x = m[0]
    got = {"BatchSize": x.get("BatchSize"), "Window": x.get("MaximumBatchingWindowInSeconds") or 0,
           "MaximumConcurrency": (x.get("ScalingConfig") or {}).get("MaximumConcurrency"), "State": x.get("State")}
    for k, v in spec.items():
        add("H5", "PASS" if eq(got.get(k), v) else "FAIL", f"ESM …{suffix} {k}", got.get(k), v)
rc = J("conc-consumer.json").get("ReservedConcurrentExecutions", "<absent>")
add("H5", "PASS" if eq(rc, B["reserved_concurrency"]["consumer"]) else "FAIL", "consumer ReservedConcurrentExecutions", rc, B["reserved_concurrency"]["consumer"])

# H6 — tenants GSIs
gsis = {g["IndexName"]: g.get("IndexStatus") for g in (J("tenants.json").get("Table") or {}).get("GlobalSecondaryIndexes", [])}
for name in B["dynamodb"]["openclaw-tenants"]["gsi"]:
    st = gsis.get(name, "<absent>")
    add("H6", "PASS" if st == "ACTIVE" else "FAIL", f"openclaw-tenants GSI {name}", st, "ACTIVE")
extra = sorted(set(gsis) - set(B["dynamodb"]["openclaw-tenants"]["gsi"]))
if extra: add("H6", "WARN", "openclaw-tenants extra GSIs not in baseline", ",".join(extra), "")

# H7 — origins
for name in B["ssm"]["origins_contain_wildcard"]:
    got = ssm.get(name, "<absent>")
    parts = [p.strip() for p in str(got).split(",")]
    add("H7", "PASS" if "*" in parts else "FAIL", f"ssm {name} contains '*'", got, "*")

# H10 — host scripts on S3 == gateway tree
if not GW or not os.path.isdir(os.path.join(GW, "deploy", "userdata")):
    add("H10", "WARN", "gateway root not found; pass --gateway-root <clone>", GW, "")
else:
    for name in B["host_scripts"]:
        s3p = os.path.join(T, "s3", name); gwp = os.path.join(GW, "deploy", "userdata", name)
        if not os.path.exists(s3p):
            add("H10", "WARN", f"S3 deployment/scripts/{name}", "not downloaded (bucket unknown or object missing)", "gateway sha"); continue
        g, w = sha(open(s3p, "rb").read()), sha(open(gwp, "rb").read())
        add("H10", "PASS" if g == w else "FAIL", f"S3 deployment/scripts/{name} == gateway deploy/userdata/{name}", g[:16], w[:16])

# H14 — host ASG instance mix; capacity and suspended processes are operator facts, reported only
groups = J("asg.json").get("AutoScalingGroups") or [{}]
asg = next((g for g in groups if g.get("AutoScalingGroupName") == "openclaw-hosts-asg"), groups[0])
mip = asg.get("MixedInstancesPolicy") or {}
types = sorted(o.get("InstanceType") for o in (mip.get("LaunchTemplate") or {}).get("Overrides", []) if o.get("InstanceType"))
want_types = sorted(B["asg"]["instance_types"])
add("H14", "PASS" if types == want_types else "FAIL", "openclaw-hosts-asg mixed instance types", ",".join(types), ",".join(want_types))
add("H14", "INFO", "openclaw-hosts-asg capacity", f"min={asg.get('MinSize')} max={asg.get('MaxSize')} desired={asg.get('DesiredCapacity')} instances={len(asg.get('Instances', []))}")
add("H14", "INFO", "openclaw-hosts-asg suspended processes", ",".join(p.get("ProcessName") for p in asg.get("SuspendedProcesses", [])) or "none")

# ---- print + report
ident = J("identity.json")
print(f"# prod-parity check · account={ident.get('Account','?')} · baseline={B.get('baseline_id')}")
for r in rows:
    if r["verdict"] in ("FAIL", "WARN"): tail = f"  got={r['got']}  want={r['want']}"
    elif r["verdict"] == "INFO" and r["got"]: tail = f"  {r['got']}"
    else: tail = ""
    print(f"{r['verdict']:5} {r['h']:4} {r['check']}{tail}")
counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("PASS", "FAIL", "WARN", "INFO")}
print("RESULT:", "FAIL" if counts["FAIL"] else "PASS", counts)
if REPORT:
    json.dump({"account": ident.get("Account"), "baseline_id": B.get("baseline_id"), "rows": rows, "counts": counts}, open(REPORT, "w"), indent=1)
sys.exit(1 if counts["FAIL"] else 0)
PY
