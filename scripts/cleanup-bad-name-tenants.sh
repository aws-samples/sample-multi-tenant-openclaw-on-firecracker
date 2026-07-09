#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# 清理历史上名字不合法 (含空格、大写等) 的 tenant.
# 这种 tenant 的 id 含 URL 不合法字符, 通过 API DELETE /tenants/{id}
# 路由不到 Lambda. 此脚本直接走 SSM + DynamoDB + ELBv2 完成清理.
#
# v1.2.6+ 起 API 层已加 _validate_name 校验, 不再产生这类脏数据.
#
# Usage:
#   ./scripts/cleanup-bad-name-tenants.sh <region> <profile> [--dry-run] [--yes]
set -euo pipefail

REGION="${1:?Usage: $0 <region> <profile> [--dry-run] [--yes]}"
PROFILE="${2:?Usage: $0 <region> <profile> [--dry-run] [--yes]}"
shift 2

DRY_RUN=false
ASSUME_YES=false
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=true ;;
    --yes|-y)  ASSUME_YES=true ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

REGION="$REGION" PROFILE="$PROFILE" DRY_RUN="$DRY_RUN" ASSUME_YES="$ASSUME_YES" \
python3 - <<'PYEOF'
import os, re, sys, json, subprocess, time

REGION = os.environ["REGION"]
PROFILE = os.environ["PROFILE"]
DRY_RUN = os.environ["DRY_RUN"] == "true"
ASSUME_YES = os.environ["ASSUME_YES"] == "true"
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

# Tenant id format is "<name>-<4hex>". The 4-hex suffix is always valid, so
# the dirty part is always in <name>. Treat any id whose <name> portion
# fails NAME_RE as a cleanup candidate.
def name_part(tenant_id):
    return tenant_id.rsplit("-", 1)[0] if "-" in tenant_id else tenant_id

def aws(*args, **kw):
    cmd = ["aws", "--region", REGION, "--profile", PROFILE, *args]
    return subprocess.run(cmd, check=kw.get("check", True),
                          capture_output=True, text=True)

def ddb_scan_all(table):
    items, last = [], None
    while True:
        cmd = ["dynamodb", "scan", "--table-name", table, "--output", "json"]
        if last:
            cmd += ["--exclusive-start-key", json.dumps(last)]
        r = aws(*cmd)
        d = json.loads(r.stdout or "{}")
        items.extend(d.get("Items", []))
        last = d.get("LastEvaluatedKey")
        if not last:
            break
    return items

def s_(item, key, default=""):
    return item.get(key, {}).get("S", default)

def n_(item, key, default=0):
    v = item.get(key, {}).get("N")
    return int(v) if v is not None else default

print(f"→ Scanning openclaw-tenants in {REGION} (profile={PROFILE})...")
all_tenants = ddb_scan_all("openclaw-tenants")
candidates = []
for it in all_tenants:
    tid = s_(it, "id")
    if s_(it, "status") == "deleted":
        continue
    if NAME_RE.match(name_part(tid)):
        continue
    candidates.append(it)

if not candidates:
    print("✓ No tenants with invalid names found. Nothing to clean up.")
    sys.exit(0)

print(f"\nFound {len(candidates)} tenant(s) with invalid names:\n")
print(f"  {'id':<32} {'status':<10} {'host_id':<22} {'vm_num':<7} {'host_port'}")
print(f"  {'-'*32} {'-'*10} {'-'*22} {'-'*7} {'-'*9}")
for it in candidates:
    print(f"  {s_(it,'id'):<32} {s_(it,'status'):<10} "
          f"{s_(it,'host_id') or '(none)':<22} "
          f"{n_(it,'vm_num') or '-':<7} {n_(it,'host_port') or '-'}")
print()

if DRY_RUN:
    print("(dry-run) — no changes made. Re-run without --dry-run to clean up.")
    sys.exit(0)

if not ASSUME_YES:
    ans = input("Proceed with cleanup? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

# ── Resolve ALB listener ARN once (reused across all candidates) ────────────
listener_arn = ""
try:
    cf = aws("cloudformation", "describe-stacks",
             "--stack-name", "OpenClawOrchestrator",
             "--query", "Stacks[0].Outputs[?OutputKey==`AlbListenerArn`].OutputValue",
             "--output", "text")
    listener_arn = (cf.stdout or "").strip()
    if listener_arn in ("None", ""):
        # Fall back to looking up the listener by ALB name
        alb = aws("elbv2", "describe-load-balancers",
                  "--query", "LoadBalancers[?contains(LoadBalancerName,`openclaw`)].LoadBalancerArn",
                  "--output", "text")
        alb_arn = (alb.stdout or "").strip().split()[0] if alb.stdout.strip() else ""
        if alb_arn:
            ls = aws("elbv2", "describe-listeners",
                     "--load-balancer-arn", alb_arn,
                     "--query", "Listeners[0].ListenerArn", "--output", "text")
            listener_arn = (ls.stdout or "").strip()
except Exception as e:
    print(f"⚠ Could not resolve ALB listener ARN: {e}", file=sys.stderr)

def remove_alb_rule(tenant_id):
    """Delete ALB rule whose path-pattern condition references this tenant."""
    if not listener_arn or listener_arn == "None":
        return
    try:
        r = aws("elbv2", "describe-rules", "--listener-arn", listener_arn,
                "--output", "json")
        for rule in json.loads(r.stdout)["Rules"]:
            for cond in rule.get("Conditions", []):
                if cond.get("Field") != "path-pattern":
                    continue
                if any(f"/vm/{tenant_id}" in v for v in cond.get("Values", [])):
                    aws("elbv2", "delete-rule", "--rule-arn", rule["RuleArn"])
                    print(f"    ✓ removed ALB rule {rule['RuleArn'].split('/')[-1]}")
                    return
    except Exception as e:
        print(f"    ⚠ ALB rule cleanup failed: {e}")

def ssm_run(instance_id, command, label):
    """Fire-and-forget SSM command. Bad-name tenants rarely have live VMs;
    we don't block on completion to keep the script fast."""
    try:
        params = json.dumps({"commands": [command], "executionTimeout": ["120"]})
        r = aws("ssm", "send-command",
                "--instance-ids", instance_id,
                "--document-name", "AWS-RunShellScript",
                "--parameters", params,
                "--query", "Command.CommandId", "--output", "text")
        print(f"    ✓ SSM {label}: {r.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        print(f"    ⚠ SSM {label} failed: {e.stderr.strip() if e.stderr else e}")

def decrement_host_counters(host_id, vcpu, mem_mb):
    """Mirror delete_tenant()'s host counter decrement."""
    try:
        aws("dynamodb", "update-item",
            "--table-name", "openclaw-hosts",
            "--key", json.dumps({"instance_id": {"S": host_id}}),
            "--update-expression",
            "SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
            "--expression-attribute-values",
            json.dumps({":v":{"N":str(vcpu)}, ":m":{"N":str(mem_mb)}, ":one":{"N":"1"}}))
        print(f"    ✓ host counters decremented (-{vcpu} vCPU, -{mem_mb} MB)")
    except subprocess.CalledProcessError as e:
        # Likely already 0 or item missing — non-fatal
        print(f"    ⚠ host counter decrement skipped: {e.stderr.strip() if e.stderr else e}")

def mark_deleted(tenant_id):
    aws("dynamodb", "update-item",
        "--table-name", "openclaw-tenants",
        "--key", json.dumps({"id": {"S": tenant_id}}),
        "--update-expression", "SET #s = :s, updated_at = :t",
        "--expression-attribute-names", json.dumps({"#s": "status"}),
        "--expression-attribute-values",
        json.dumps({":s":{"S":"deleted"}, ":t":{"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}))

# ── Process each candidate ─────────────────────────────────────────────────
for it in candidates:
    tid = s_(it, "id")
    host_id = s_(it, "host_id")
    vm_num = n_(it, "vm_num")
    host_port = n_(it, "host_port")
    guest_ip = s_(it, "guest_ip")
    vcpu = n_(it, "vcpu")
    mem_mb = n_(it, "mem_mb")
    print(f"\n→ Cleaning {tid!r}")

    if host_id and vm_num:
        # Quote tenant id to survive the embedded space.
        ssm_run(host_id,
                f"/home/ubuntu/stop-vm.sh '{tid}' {vm_num} 2>&1 || true",
                "stop-vm")
        ssm_run(host_id,
                f"rm -rf '/data/firecracker-vms/{tid}'",
                "rm vm dir")
        if host_port and guest_ip:
            # Best-effort DNAT removal — wildcard match because the iface
            # name varies. Failure is OK if the rule never got installed.
            ssm_run(host_id,
                    f"sudo iptables -t nat -S PREROUTING 2>/dev/null | "
                    f"grep -- '--dport {host_port}' | "
                    f"sed 's/^-A /-D /' | "
                    f"while read line; do sudo iptables -t nat $line 2>/dev/null || true; done",
                    "remove DNAT")

    remove_alb_rule(tid)

    if host_id and vcpu and mem_mb:
        decrement_host_counters(host_id, vcpu, mem_mb)

    mark_deleted(tid)
    print(f"    ✓ {tid!r} marked deleted in DynamoDB")

print(f"\n✓ Cleanup complete: {len(candidates)} tenant(s) processed.")
print("  SSM commands run async; check 'aws ssm list-command-invocations' "
      "if you want to confirm host-side cleanup.")
PYEOF
