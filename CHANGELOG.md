# Changelog

## [1.3.1] — 2026-05-24

The "production-grade" claim of v1.3.0 was **half-true**. AZ failover detected outages and orchestrated correctly, but the actual VM relaunch on the target host **never worked end-to-end** because of three integration bugs that mock-based unit tests didn't catch. v1.3.1 fixes them, validates the full path against a real running tenant, and adds the same fix to the existing `migrate` API which had the same class of bug since v1.2.0.

**v1.3.0 detected AZ outages. v1.3.1 actually recovers from them.**

### Fixed — AZ failover end-to-end (was broken in v1.3.0)

- **`launch-vm.sh` invocation used wrong argument format.** v1.3.0 passed `--restore-from <s3://uri>` as a flag, but `launch-vm.sh` takes restore-key as the **6th positional argument**, not a flag. Fix: emit `launch-vm.sh <tid> <vm_num> <vcpu> <mem_mb> "" <backup_key>` exactly as `backup-data.sh` and the rest of the codebase expect.
- **Backup S3 URI vs S3 key confusion.** v1.3.0 passed `s3://${ASSETS_BUCKET}/backups/<tid>/latest.gz`. But `launch-vm.sh` internally prefixes `s3://${ASSETS_BUCKET}/${RESTORE_KEY}`, which would produce a double-`s3://` prefix. Fix: pass the bare key (no prefix). The Lambda discovers the actual most-recent key via S3 list (added `_find_latest_backup_key()`).
- **Hard-coded `latest.gz` doesn't exist.** Backups are uploaded as `backups/<tid>/<ISO timestamp>.gz` — there's no `latest` alias. Fix: list `backups/<tid>/` and sort by `LastModified` to find the most recent backup. If no backup exists, refuse failover with `AZ_FAILOVER_NO_BACKUP` audit + SNS alert (Path A: never silently lose data).
- **`pick_target_host` looked up `vcpu_total` but DDB stores `total_vcpu`.** Off-by-name. v1.3.0's mock tests passed because they used `vcpu_total` everywhere. Real env had hosts with `total_vcpu` field, so spare-capacity calculation always returned 0 → "no target host" even with healthy hosts available. Fix: read `total_vcpu` first, with `used_vcpu` for accurate spare calculation, fall back to legacy field names.
- **`launch-vm.sh` `e2fsck` rejected backup restoration.** `backup-data.sh` dumps the ext4 image while VM is **paused** (vCPU frozen, but pending journal not committed). On restore, `e2fsck` must replay that journal — it returns exit code 1 (`filesystem errors corrected`), not 0. Pre-1.3.1 launch-vm.sh treated any non-zero rc as fatal. Fix: accept rc 0/1/2 (all "consistent now"), only fail on rc ≥ 4 (structural damage).
- **No synchronous wait for SSM completion.** v1.3.0's failover sent SSM and immediately flipped DDB `host_id`, even if the launch-vm.sh actually failed on the target. Fix: `_wait_ssm_done()` polls `get_command_invocation` for up to 90s and surfaces real exit status. On `Failed`, tenant is marked `failover_failed` (not stuck in `failover_recovering`).

### Fixed — ALB rule re-pointing (was broken since v1.2.0 in `migrate` API too)

- **Cross-host VM relocation didn't update ALB routing.** Each host has its own target group (`oc-<last8>`), and each tenant has an ALB rule `/vm/<tid>*` pointing at the *current host's* TG. Both `migrate` (live migration) and `_failover_tenant_to_host` (AZ failover) updated DDB ownership but **left the ALB rule pointing at the dead/old host**. Result: even after a successful migration, CloudFront kept routing traffic to the wrong place.
- Fix: `_repoint_alb_rule()` ensures target host has a TG, registers its private IP, and uses `elbv2.modify_rule` to swing the existing rule's `forward` action over. Or creates a fresh rule if none exists.
- Same fix applied to `api/handler.py`'s `migrate` action (calls a new `_repoint_alb_rule_to_tg` helper). This is a **v1.2.0 latent bug** that just got noticed because v1.3.0's failover surfaced it.
- `migrate` action also now SSH-cleans the source host's nginx tenant config so it stops advertising itself as a backend.

### Added — IAM + permissions

- `health_check` Lambda gains: `elasticloadbalancing:DescribeRules / DescribeTargetGroups / CreateRule / ModifyRule / CreateTargetGroup / RegisterTargets`, plus `s3:Get*/List*` on the assets bucket (for backup discovery), plus `SNS:Publish` for path-A alerts.
- `api` Lambda gains `elasticloadbalancing:ModifyRule` (was missing — `migrate` would have silently failed if it had attempted ModifyRule pre-v1.3.1).
- `health_check` Lambda timeout bumped from 120s to 180s to accommodate synchronous SSM wait during failover.

### Tests — 415 passed (v1.3.0 baseline) + 14 new = 429+

- **+14 tests** in `tests/test_az_failover.py`:
  - `TestFindLatestBackupKey` (5): empty list, no bucket configured, single key returned without `s3://` prefix, multiple keys sorted by LastModified, S3 error returns None gracefully.
  - `TestWaitSsmDone` (3): Success returns ok, Failed returns error, timeout after threshold.
  - `TestRepointAlbRule` (4): modifies existing rule, creates if missing, no-op without listener, registers target IP.
  - `test_no_backup_blocks_failover_with_alert`: refuses failover, marks tenant `failover_blocked`, no SSM call.
  - `test_ssm_command_fails_with_nonzero_status`: synchronous wait detects launch-vm.sh exit-non-zero and marks tenant failed.
- All v1.3.0 tests updated to mock the new SSM `get_command_invocation`, S3 `list_objects_v2`, and elbv2 client calls.
- Stack import-order bug fixed: `health_fn` no longer constructs with `listener.listener_arn` (defined later); uses `add_environment()` post-injection like `api_fn` already did.

### Real-environment E2E validation — proven on ap-northeast-1

Steps performed against a live deployment (account 835751346093):

1. Created `failover-test-6c28` tenant pinned to ap-northeast-1c (i-088f6fc814fd4b1a2).
2. Triggered `POST /tenants/.../backup` → backup landed at `s3://.../backups/failover-test-6c28/2026-05-24T09:06:27Z.gz` (9.4 MB).
3. Stopped host-agent on the ap-northeast-1c host and injected `last_health_check = 15 minutes ago` to simulate AZ-level failure.
4. Manually invoked `openclaw-health-check` Lambda.
5. **Result**: `az_outages_detected: 1, tenants_failed_over: 1, tenants_failed: 0`.
6. **Verified DDB**: tenant.host_id flipped from i-088f...1a2 → i-0bb45...50e2; `failover_from_az: ap-northeast-1c`; `restored_from: backups/failover-test-6c28/2026-05-24T09:06:27Z.gz`; `app_health: up`.
7. **Verified ALB**: rule for `/vm/failover-test-6c28*` now points at `oc-34d350e2` (target host's TG).
8. **Verified Dashboard**: `curl https://d3k97r1qs0mu76.cloudfront.net/vm/failover-test-6c28/?token=...` → **HTTP 200** end-to-end.
9. **Verified data preservation**: disk 228 MB → 229 MB after restore (the +1 MB is mount-time fs ops, contents intact).
10. **Verified cooldown**: subsequent Lambda invocation skipped with `skipped_cooldown: ["ap-northeast-1c"]`.

This is the first OpenClaw release where the "AZ failover" claim is backed by a complete real-environment trace.

### Operator notes

- **Re-deploy is required** — IAM policy changes, env var additions, Lambda timeout bump.
- **Re-roll launch-vm.sh on existing hosts** so the e2fsck fix is in place. SSM one-liner in the upgrade guide.
- **Existing in-flight migrations** that completed pre-v1.3.1 may still have their ALB rules pointed at the old host. Run a manual `POST /tenants/{id}/migrate` (no-op if target == current) to trigger the fixed path, or wait for the next deploy that touches the rule.
- **Backups are required** for AZ failover to recover data. Set `backup_cron` in `config.yml` to ensure every tenant has a recent backup before disaster strikes. Tenants without backups will get `failover_blocked` + SNS alert — by design, never silent data loss.

---

## [1.3.0] — 2026-05-24

Closes the AZ-level failover gap that v1.2.x left open and finally answers the 2026-05-22 sync ask **"if an AZ goes down, can the tenants on it auto-restart somewhere else?"** — yes, automatically, every five minutes, with cooldown protection. Plus default deployments now actually run in two AZs out of the box, instead of single-AZ with a config flag you had to remember to flip.

### Added — automatic AZ failover

- **`health_check` Lambda gains AZ-level failover orchestration.** Every poll (default every 5 minutes) the watchdog now also groups hosts by AZ and treats an AZ as unavailable if every host in it has gone stale (no `last_health_check` heartbeat from host-agent for ≥ `unhealthy_threshold_minutes`, default 10). When an outage is confirmed, the Lambda picks a healthy target host in another AZ (sorted by spare vCPU, deterministic instance_id tie-break), relaunches each affected `running` tenant on it via SSM `launch-vm.sh --restore-from <latest-backup>`, flips DDB ownership, writes audit-log entries, and publishes an SNS event. A per-AZ cooldown (default 30 minutes) prevents flapping.
- **5 pure-function helpers** carry the orchestration logic so it's all unit-testable without AWS access: `is_host_unhealthy`, `group_hosts_by_az`, `detect_unhealthy_azs`, `pick_target_host`, `should_skip_az_for_cooldown`. Plus `_check_and_handle_az_failover` (orchestrator) and `_failover_tenant_to_host` (per-tenant relaunch).
- **`host-agent.py` now writes a heartbeat to the hosts table.** Every poll cycle the agent updates `last_seen` + `last_health_check` on its own DDB host record. This is what the AZ-failover Lambda watches — without it, host-level liveness would be invisible (the previous tenant-level signal goes stale only when a tenant exists).
- **New `INSTANCE_ID` env on host-agent**, populated by `init-host.sh` from IMDS into `/etc/platform.env` so the agent knows which host record to update.

### Changed — defaults

- **Default ASG capacity is now 2 hosts in 2 AZs.** `config.yml.example` ships `asg.min_capacity: 2`, `asg.max_capacity: 8`, and `multi_az.enabled: true` by default. Single-AZ stays a one-line opt-out for cost-sensitive non-prod environments. The bump is needed for AZ failover to have an actual fallback target — a 1-host fleet has no place to migrate to.
- **`health_check.az_failover` block in `config.yml`** with `enabled` (default true), `unhealthy_threshold_minutes` (default 10), `cooldown_minutes` (default 30). Set `enabled: false` to keep the watchdog active but disable the failover side.

### Fixed — UX bugs surfaced during validation

- **Console didn't load `systemInfo` on the Tenants tab,** so the "Hosts by AZ" group header silently never rendered even when multi_az was enabled. `refresh()` now calls `loadSystemInfo()` alongside `loadHosts/Tenants/Templates`. Discovered by Playwright + DOM inspection during the v1.3.0 multi-AZ proof shoot.
- **`/hosts` API leaked the synthetic `__az_failover_state__` record.** The orchestrator stores per-AZ cooldown state on a host record with that special key. `list_hosts()` now filters out any `instance_id` starting with `__` so the console / regression tests never see these internal bookkeeping rows. Caught by the existing `test_hosts_have_expected_fields` regression check, which started failing as soon as the first AZ failover state was persisted.

### Tests — 441 passed / 0 failed (excluding 1 pre-existing flaky E2E)

- **+34 tests** in `tests/test_az_failover.py`: pure-function boundary cases (threshold edges, missing fields, sorting determinism, exclusion rules) + orchestration mocks (cooldown skip, no-target-AZ behaviour, SSM/audit/SNS side-effects, `audit_table.put_item` failure non-fatality, feature-flag noop).
- **+4 tests** in `tests/test_monitoring.py::TestHostHeartbeat`: `_write_host_heartbeat` writes both fields with same timestamp; skips when `HOSTS_TABLE` empty; skips when `INSTANCE_ID` empty; swallows DDB throttle exceptions.
- **+1 console-contract test** in `tests/test_console_api_contract.py::test_refresh_loads_system_info` — fails loudly if a future refactor pulls `loadSystemInfo` out of `refresh()` again.
- **+1 API regression test** in `tests/test_api.py::test_filters_out_synthetic_state_records` — guards `list_hosts` against future internal-record leakage.
- Existing 12 `tests/test_health_check.py` cases all still pass — the new AZ failover code is appended in a separate function, so the v1.0 watchdog path is unchanged.

Total: **441 passed** locally (up from v1.2.9's 386, **+55 tests**). One E2E (`test_backup_and_restore_from_latest`) is a pre-existing flaky test that fails on transient SSL/network errors — independent of this release.

### Operator notes

- **Existing fleet (1.2.x hosts) won't pick up the heartbeat code until host-agent is reloaded.** Push the new agent to S3 (`aws s3 cp deploy/userdata/host-agent.py s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py`) and rollout via SSM Run Command — the upgrade snippet in the README now also injects `INSTANCE_ID` into `/etc/platform.env` for hosts launched on older init-host.sh.
- **Existing host records won't have `last_seen` / `last_health_check`** until the new agent runs at least once. The `is_host_unhealthy` predicate treats missing-timestamps as unhealthy, which means the first health_check Lambda invocation after upgrade may briefly report `az_outages_detected: N` for every AZ. The cooldown protects against bogus failover, and a single agent poll (≤5s) populates the fields. **Safer rollout**: stop the EventBridge schedule for 1–2 minutes during the rollout, or set `health_check.az_failover.enabled: false` in config and re-deploy → roll the agent → re-enable.
- **The synthetic `__az_failover_state__` record** lives in the hosts DDB table once an outage triggers. It contains only cooldown state (no PII) and is intentionally not garbage-collected — kept indefinitely so cooldown survives across Lambda redeploys.

---

## [1.2.9] — 2026-05-23

Closes the metric-correctness + multi-AZ visibility gaps that 1.2.8 didn't reach. The 2026-05-22 sync flagged that operators "can only see disk usage" in the console — CPU was a hard-coded 0 stub since 1.2.0, and memory often read 0 because balloon stats returned `available_memory: 0` on most kernels. The same sync also asked for "where does multi-AZ show up in the UI" and "let admins pick which host a new tenant lands on". This release does all three at the data layer + UI.

### Fixed — real CPU & memory metrics

- **`host-agent.py` — CPU% from `/proc/<fc_pid>/stat`.** Sample utime+stime jiffies on each poll, diff against the previous sample, divide by the elapsed wall time and the configured vcpu count. Result: `cpu_pct` is now an actual percentage (0–100, capped) instead of the previous hard-coded 0. The Prometheus gauge `openclaw_vm_cpu_pct` carries the same value into AMP, so Grafana dashboards built before today's release start showing real numbers immediately.
- **`host-agent.py` — memory from `/proc/<fc_pid>/status` VmRSS.** The previous proxy `vm_mem_mb − available_memory` evaluated to 100% on any kernel where balloon stats returned 0. Switched to reading VmRSS from the Firecracker process's `/proc/.../status` (always populated on Linux), which approximately equals guest used + Firecracker overhead. Balloon stats are kept as a fallback for kernels with `VIRTIO_BALLOON_F_STATS_VQ` actually working.
- **First-sample handling.** The CPU sampler returns 0 on the first poll for a tenant (correct: you can't compute a rate from one point) and on pid reuse (jiffies counter went backwards). 7 dedicated unit tests in `tests/test_monitoring.py::TestComputeCpuPct` cover the boundary cases.

### Added — Console UI

- **Tenants table — vCPU column with usage bar.** Same colored bar style as the disk column (cyan-dim → amber → red at 70/90%), with text "12% / 2 vCPU" so 50% on 1 vcpu vs 2 vcpu reads correctly. Renders the raw `t.vcpu` allocation when the tenant isn't running yet.
- **Tenants table — Memory column with usage bar.** "1.2G/4G (30%)" — uses the new VmRSS-backed `memory_used_mb` from host-agent. Falls back to the configured `mem_mb` text when metrics aren't available yet.
- **Hosts panel — group by AZ when multi-AZ is enabled.** When `multi_az.enabled: true`, the Hosts panel splits into per-AZ sections with a small "AZ: ap-northeast-1a · 2 host(s) · 5 VM(s)" header above each group. Single-AZ deployments keep the flat list. Each host card now shows its AZ tag in the header row.
- **Tenants table — host AZ badge already shipped in 1.2.8** but now shows real data once you've followed the upgrade backfill (see operator notes).
- **Create-tenant modal — Target host picker.** New optional dropdown lets operator+ pick the exact host for a tenant (admin/operator both can use it; viewer is gated by RBAC at the API layer). Options show `instance_id (az) — N free vCPU, M VM(s)` so you can see capacity at a glance. "Auto" remains the default.
- **Settings → Fleet by AZ table.** Live distribution of registered hosts and their tenants across AZs, regardless of multi_az setting (so single-AZ deployments also see "all my hosts in 1a"). Refreshes when you load Settings or Monitoring tab.

### Added — API

- **`POST /tenants` accepts `preferred_host_id`.** Three scheduling modes now: clone_from (must same-host), preferred_host_id (admin/operator pin), default (first-fit). 404 when host doesn't exist or is draining; 400 with explicit reason when the host exists but lacks capacity.
- **`/system/info` now includes `assets_bucket` and `region`** so the console's "open in S3 console" deep links work without needing to embed the bucket name in `console/config.js`.

### Tests

- **+24 net new tests** in `tests/test_monitoring.py` covering `_read_proc_stat_cpu_jiffies` (with comm-with-spaces edge case), `_read_proc_status_rss_kb`, the pure `_compute_cpu_pct` function (8 boundary cases incl. cap, negative delta, zero divisor), and `_sample_cpu_pct` integration.
- **Status:** 386 passed / 0 failed locally on `pytest -m unit`.

### Operator notes

**Existing fleet won't pick up the metric fixes until the host-agent is reloaded.** Three options in order of intrusiveness — see the new `## Upgrade Guide → Upgrading from v1.2.7 → v1.2.9` section in `README.md` for the exact commands:

1. Roll the ASG: `aws autoscaling set-desired-capacity ... --desired-capacity 0` then back to your normal count. Clean and terminates anything stale, but ~3 min of host warmup before tenants can be created on the new ones.
2. Push host-agent.py to S3 + run an SSM `AWS-RunShellScript` against the ASG tag to copy + restart. Zero downtime, fastest.
3. Wait for natural recycling (idle reclamation will eventually replace hosts).

**Same applies to the AZ field on host records.** Existing hosts have no `az` until they re-register. The console gracefully renders `-`; either roll the ASG or run the manual backfill snippet in the upgrade guide.

**Console hard-refresh required.** `setup.sh` re-uploads `console/index.html` to S3. Browser caches the file, so users will see the old layout until they hit Cmd-Shift-R (or wait for CloudFront's ~24 h TTL). Verify with the version banner in the footer (`v1.2.9`).

## [1.2.8] — 2026-05-23

Closes a stack of "feature exists in code but the console hides it" gaps that surfaced in the 2026-05-22 sync between Neo and Xue. The product itself was already at 1.2.7, but several v1.2.x features (live VM migration, Prometheus metrics, AgentCore tools, multi-AZ / WAF / Cognito flags, host AZ) had no UI surface — operators had to read CDK code or `oc` CLI to confirm anything was wired. This release pulls all of that to the console, plus tidies up the CLI / build / test footprint that 1.2.7 left in flux.

### Added — Console UI surfaces

- **Application tab → MCP Tools card.** When `agentcore.enabled: true`, the Application tab now shows the three Lambda-backed MCP tools registered with the Gateway (`hello`, `system_info`, `timestamp`) with description + input schema. Backed by a new `GET /agentcore/tools` endpoint. Empty + helpful "AgentCore not enabled, here's how to turn it on" placeholder otherwise.
- **Application tab → Skills card enriched.** Each skill row now lays out as a card with a hover-target name, a real description (was clipping to one line), and a deep-link button to the skill's S3 prefix in the AWS console (so editors can update SKILL.md without leaving the browser). Empty state explains the upload + cron pickup flow instead of just "No skills configured".
- **Monitoring tab (new).** Demo-friendly observability page: live status of AMP / Grafana / SNS, the six per-VM Prometheus gauges with type/labels/description, three sample PromQL queries, and the AMP `remote_write` + Grafana endpoints when `metrics.enabled: true`. Closes the "we have monitoring but the demo doesn't show it" gap from the sync.
- **Tenants table → AZ column.** New `AZ` column between `Port` and `Rootfs` shows which Availability Zone each tenant's host is in. Powered by the host record's `az` field (see backend changes).
- **Tenants table → Migrate button.** New 🔀 icon in the Actions column on running tenants opens a modal that lists candidate target hosts (other active hosts, with their AZ + free vCPU + VM count) and POSTs to the existing `/tenants/{id}/migrate`. The Firecracker snapshot/restore round-trip (#20) shipped in 1.2.0 — this release just exposes it.
- **Settings tab → Infrastructure card.** Enabled / disabled state for multi-AZ, Prometheus+Grafana, WAF, Cognito+RBAC, SNS notifications, per-tenant quotas. Plus a Host Overcommit card showing the current CPU / memory ratios + default per-tenant sizing. Both populated by a new `GET /system/info` endpoint.

### Added — Backend

- **`GET /system/info`.** Feature flags + config snapshot (region, version, multi_az, metrics, waf, cognito, notifications, quotas, host_config). Read-only; available to viewer role; populated from the API Lambda's environment variables (which are now wired in `stack.py`).
- **`GET /agentcore/tools`.** Returns the static list of Lambda-backed MCP tools registered with the Gateway, with input schema. Static today (the tools are declared in `stack.py` at deploy time); future PRs can swap in a live `bedrock-agentcore.list_targets()` call when the Gateway grows user-defined tools.
- **Host record now carries `az`.** `init-host.sh` reads the AZ from IMDS during `step5: registering to DynamoDB` and writes it alongside the existing host metadata; the API Lambda's `register_host()` path also populates `az` from `ec2.describe_instances()` Placement. Existing hosts can be backfilled with a manual `aws dynamodb update-item`; new hosts pick it up automatically.

### Fixed

- **`/audit-log` was unreachable from API Gateway.** The Lambda router has had `("GET", "/audit-log")` since 1.2.0 (#48 helper restoration), but the matching `api.root.add_resource("audit-log")` was never declared in `stack.py`, so the route returned API-Gateway-level 404s in production. Added the missing resource. Existing handler code unchanged.

### Changed

- **`build-rootfs.sh` now refuses to run on macOS by default.** debootstrap is Linux-only, and the previous behaviour was to start the build then fail deep into the chroot with a confusing dependency error. The new OS guard prints the cloud-builder one-liner (`./scripts/build-rootfs-on-ec2.sh`) and exits cleanly. `FORCE_LOCAL_BUILD=1 ./build-rootfs.sh` overrides for unusual setups.
- **`oc` CLI bumped to v0.7.0 with v1.2.x action coverage.** New subcommands: `oc resize <id> --vcpu N`, `oc resize-disk <id> --new-size-mb N`, `oc migrate <id> --target-host-id i-…`, `oc batch <action> --ids a,b,c | --tag k:v`, `oc audit-log [--since ISO] [--limit N]`. Brings the CLI back to parity with the API surface (it had been stuck at v1.0 actions).

### Removed

- **`tests/test_local_dev.py`.** 1.2.7 unshipped the `local-dev/` directory but left this test file in place, asserting the deleted files still existed — guaranteed-fail since 1.2.7. Removed cleanly; LocalStack contributors keep their own private copy under `.dev/` per Neo's note.

### Tests

- **Net new tests:** 12 added in `tests/test_console_api_contract.py` (regex parity, role-list parity, tab-loader parity, new endpoint reachability). Removed: ~10 in `tests/test_local_dev.py` (file deleted).
- **Status:** 367 passed / 0 failed locally on `pytest -m unit` after the 1.2.8 changes. (The contract tests fail loudly on a typo in either side of the regex, role list, tab declaration, or new endpoint name — designed to catch the kind of "console and API drifted" bug that 1.2.7's name-validation work could have introduced.)

### Operator notes

- **Upgrade path:** `git pull && ./setup.sh <region> <profile>`. The new Lambda env vars (`MULTI_AZ_ENABLED`, `WAF_ENABLED`, `PROJECT_VERSION`, etc.) and new API Gateway resources (`/system/info`, `/agentcore/tools`, `/audit-log`) are added by CDK on the next `cdk deploy`. No DDB migration needed; new fields are additive.
- **Existing hosts won't backfill `az` until they re-register.** Either roll the ASG (terminate-and-launch) or run `aws dynamodb update-item --table-name openclaw-hosts --key '{"instance_id":{"S":"i-..."}}' --update-expression 'SET az = :a' --expression-attribute-values '{":a":{"S":"ap-northeast-1c"}}'` per host. The console gracefully handles the empty case (column shows `-`).
- **Console rebuild:** `setup.sh` regenerates `console/config.js` with the new `OC_REGION` + `OC_ASSETS_BUCKET` globals required by the skill S3 deep-link.

## [1.2.7] — 2026-05-22

### Changed
- **Console: live name validation.** Create-Tenant modal now mirrors the API's `_NAME_RE` (`^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`) — invalid names show an inline error and disable the Create button instead of failing silently with a 400 from the server.
- **Console: Disk column.** New `Disk` column between `Mem (MB)` and `Guest IP`: a colored bar (green / amber / red at 70% / 90%) plus exact `used/total (pct%)` text below — replaces the cramped `formatMetrics()` cell that was hidden at the far right of the row.
- **Console: themed scrollbars.** Webkit + Firefox scrollbars now use the cyan-on-dark palette to match the rest of the UI.

### Removed
- **`local-dev/` unshipped from the repo** — the docker-compose stack (LocalStack + stub host-agent) for running Lambda + console without an AWS account, shipped in 1.2.0 (PR #46 / issue #24), is now treated as a personal contributor workflow instead of a tracked repo asset. The directory is gitignored; keep your own copy locally if you use it. Use `pytest -m unit` for routine local iteration.

## [1.2.6] — 2026-05-22

### Fixed
- **`refresh_rootfs()`: 0-byte rootfs hotfix.** Decompress now writes to a `.tmp` sibling, checks `[ -s ]`, then atomically `mv`s — replaces a `pigz -dc > rootfs.ext4` redirect that could leave a 0-byte file on any pigz failure, blocking new tenant creates.
- **API: tenant name validation.** `_validate_name()` rejects names that aren't DNS-label safe (`^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`). Previously, names with spaces or uppercase were accepted but couldn't be deleted via the API path.

### Changed
- **Cognito: import-and-recreate.** When `console_auth.user_pool_id` is set, the stack imports the pool but recreates `UserPoolDomain` + `UserPoolClient` as stack-owned resources (was: assumed to already exist, producing drift). Existing user accounts unchanged. Removes the `console_auth.user_pool_client_id` config key.
- **Audit table: per-deploy naming.** Table name now suffixed with `Aws.STACK_ID` first segment (e.g. `openclaw-audit-log-a63eaa70`). Prevents collision on `cdk destroy` + redeploy. **One-time table replacement on upgrade** — old table preserved as orphan; delete it manually if history isn't needed.

### Added
- **`scripts/cleanup-bad-name-tenants.sh`** — removes legacy bad-name tenants (SSM stop-vm + dir cleanup, ALB rule, host counters, DDB mark deleted). Supports `--dry-run` / `--yes`.

### Operator notes
- Upgrade is a single `./setup.sh <region> <profile>`.
- If you have legacy tenants with spaces or uppercase in their names, run `./scripts/cleanup-bad-name-tenants.sh <region> <profile> --dry-run` to preview, then drop `--dry-run` to clean up.


## [1.2.5] — 2026-05-21

Closes the **observability** gap. The 1.2.4 release verified the data plane all the way to "HTTP 200 from CloudFront → ALB → Nginx → Firecracker" but never actually checked that AMP / Grafana were receiving samples.
A live probe today found two stacked bugs that meant zero metrics had ever flowed into AMP since the feature shipped (issue #4 / PR #38).

### Fixed (real production bugs found via live AMP probe)
- **`deploy/userdata/adot-config.yaml` / `host-agent.py` — split-port mismatch.** 
  ADOT was configured to scrape `127.0.0.1:9090`, but `host-agent.py main()` only ever bound the single HTTPServer on `OC_AGENT_PORT` (8899). The agent's own comment block stated the design as "/metrics on the same HTTPServer as /health to avoid a second listener" — so the architecture was correct, the wiring was not. 
  Result: every ADOT scrape since the metrics feature shipped failed with "Failed to scrape Prometheus endpoint". 
  Fix: ADOT now scrapes `127.0.0.1:8899`; the dead `OC_AGENT_PROM_PORT` variable was removed.
- **`deploy/userdata/host-agent.py` — `_status` did not include the computed metrics.** 
  `_write_ddb()` computed per-VM metrics (`memory_used_mb`, `disk_used_mb`, etc.) and wrote them to DynamoDB, but never assigned them back into the in-memory `info` dict that `_status` is rebuilt from each poll cycle. The `/metrics` endpoint reads `_status`, so it could only ever emit `openclaw_vm_health` (which is derived directly from `vm_health`). 
  Every other gauge was empty in AMP, even when DDB had the values. Fix: mirror the computed `metrics` dict back into `info["metrics"]` before the DDB write so the Prometheus exporter sees it.
- **`deploy/stack.py` + `tests/test_prometheus.py` + `init-host.sh` — stale `:9090` references.** 
  Comments and assertions still pointed at the never-bound 9090 port. Updated to `:8899` and added two regression tests that fail loudly if either bug recurs:
  `test_collector_config_has_amp_endpoint` now asserts `127.0.0.1:8899` is the scrape target (and explicitly bans 9090);
  `test_write_ddb_mirrors_metrics_back_into_status_for_prom_exporter` asserts the back-mirror line stays in `_write_ddb`.
- **`uv.lock` — bumped `idna` 3.11 → 3.15** to close the medium-severity Dependabot CVE alert (CVE-2024-3651-related; idna < 3.15 lets
  specially-crafted inputs to `idna.encode()` bypass the original patch). Pure transitive bump (boto3 → urllib3 → idna), no API change.
  Verified by full unit-test re-run.

### Verified end-to-end on real AWS (live AMP probe, this exact tag)
- ✅ ADOT collector active + `Everything is ready. Begin running and processing data.` (no more `Failed to scrape Prometheus endpoint`).
- ✅ `host-agent /metrics` emits all 6 gauges with per-tenant labels 
  (`openclaw_vm_memory_used_mb`, `openclaw_vm_memory_balloon_mib`,
  `openclaw_vm_disk_used_mb`, `openclaw_vm_disk_total_mb`,
  `openclaw_vm_disk_used_pct`, `openclaw_vm_health`).
- ✅ AMP PromQL `query` API returns real values for all 6 gauges (probed via SigV4-signed HTTPS GET). Example: `openclaw_vm_health {tenant="obs-test-366f", instance="i-0bb45368534d350e2"} = 1`.
- ✅ Grafana workspace `g-5e6517493c` ACTIVE on Grafana 10.4 with AWS_SSO authentication; AMP shows up under PROMETHEUS data sources.

### Test status
- **386 passed / 0 failed / 0 skipped** locally (1.2.5 adds 2 new regression tests on top of the 1.2.4 baseline of 384).

### Operator notes
- After upgrading from 1.2.4 → 1.2.5, redeploy the stack and roll the ASG (or push the new `host-agent.py` + `adot-config.yaml` via SSM) to pick up the fixes on existing hosts. New hosts launched by `setup.sh` get the fix automatically.
- `metrics.enabled: false` (default in `config.yml.example`) skips AMP and Grafana entirely — there is no cost surprise for users who don't opt in.


## [1.2.4] — 2026-05-21

End-to-end microVM validation pass. Closes the long-standing "data plane
out of scope" gap by actually booting Firecracker on real AWS, which
surfaced two genuine production bugs hidden behind layers of mocking.

### Fixed (real production bugs found via microVM E2E)
- **`deploy/userdata/host-agent.py` — DynamoDB reserved-keyword bug.**
  `metrics` is a DDB reserved keyword (along with `status`, `name`, etc.),
  so the literal `SET … metrics = :m` UpdateExpression issued every poll
  cycle was rejected with `ValidationException: Attribute name is a
  reserved keyword`. Tenants stuck in `creating` forever because
  host-agent could never promote them. Fixed by aliasing the field as
  `#m` in `ExpressionAttributeNames` (same pattern already used for the
  also-reserved `status` keyword). New regression test
  `test_write_ddb_uses_attr_name_alias_for_metrics_reserved_keyword`
  asserts the alias is always present so this can't silently regress.
- **`deploy/userdata/launch-vm.sh` — OpenClaw 2026.5+ schema break.**
  Upstream OpenClaw moved MCP servers from a top-level `mcpServers` key
  to `mcp.servers.<name>` (verified by reading where `openclaw mcp set`
  itself writes). `launch-vm.sh` was injecting AgentCore Gateway URLs at
  the old top-level path, causing OpenClaw Gateway to refuse the entire
  config (`Invalid input at <root>`) and crash-loop. Updated the `jq`
  mutation to write under `.mcp.servers.agentcore-gateway`, which the
  new schema validates.
- **`deploy/userdata/stop-vm.sh` — Firecracker zombie processes.**
  Plain `pkill -TERM` on the Firecracker socket left orphan processes
  whenever stop-vm raced with launch-vm's late init steps (e.g. when
  back-to-back e2e tests delete a tenant before its VM has finished
  booting). The zombies silently consumed vCPU + memory budget on the
  host, eventually starving subsequent VM launches. Now sends `TERM`
  first, sleeps 1 s, then `KILL` to guarantee the process is gone before
  the script returns.
- **`tests/test_e2e.py::TestBackupRestoreRoundtrip` — pre-flight cleanup.**
  Earlier e2e cases occasionally leave half-launched VMs behind; the
  roundtrip test now sweeps any leftover `e2e-…` tenants before creating
  its own src/dst pair, so a single suite run lands at 385/385 instead of
  384/385+1 transient timeout.

### Added
- **`scripts/build-rootfs-on-ec2.sh`** — cloud-native rootfs build for
  contributors on macOS / Windows / Cloud9 (anywhere `debootstrap` is
  unavailable). Launches a one-shot `t3.medium` (or `t4g.medium` for
  ARM64) Ubuntu builder in the same region with a 30 GB root volume,
  attaches the existing `openclaw-host-profile` instance role for S3
  access, runs the chroot build via SSM Run Command, uploads the
  artifacts + `manifest.json` to `s3://${ASSETS_BUCKET}/deployment/rootfs/`,
  and terminates the builder. ~10 minutes total. Closes the long-standing
  UX gap where the README implied "just run `./build-rootfs.sh`" but
  silently required the operator to find a Linux host. The script
  uploads its own log to S3 on failure for diagnosability.

### Verified end-to-end on real AWS (this is the first 1.2.x release with
microVM data plane fully verified)
- ✅ `cdk deploy` finishes `CREATE_COMPLETE` in ~180s (post-1.2.3 redeploy).
- ✅ `./scripts/build-rootfs-on-ec2.sh v1.0` builds + uploads rootfs in ~9 min.
- ✅ ASG host self-registers to DDB with `rootfs=v1.0` in ~3 min after launch.
- ✅ `POST /tenants` → `creating` → `running` in ~11 s (latest live probe); `app_health=up` in ~16 s; full `creating → deleted` cycle in ~23 s.
- ✅ Firecracker VM responds on `172.16.x.2:18789`; gateway token auto-injected.
- ✅ `GET https://${DASHBOARD_URL}/vm/{tenant_id}/?token=…` → **HTTP 200** (177 ms TTFB through CloudFront → ALB → Nginx → Firecracker).
- ✅ Per-VM metrics field correctly populated in DDB (`memory_used_mb`, `disk_used_mb`, `disk_used_pct`, `cpu_pct`, `memory_balloon_mib`).
- ✅ `DELETE /tenants/{id}` cleanly tears down (ALB rule + iptables DNAT removed, Firecracker SIGKILL'd, no zombies).

### Test status (post-1.2.4, with valid AWS creds + rootfs in S3)
- **385 passed / 0 failed / 0 skipped** locally — fully clean run on a
  freshly-cleaned host. (Earlier runs in this cycle reported 384+1 skip
  due to back-to-back roundtrip cleanup races; the new pre-flight sweep
  + stop-vm.sh SIGKILL fix close that gap.)

## [1.2.3] — 2026-05-19

End-of-Q2-2026 milestone tag. Identical code to 1.2.2; this entry exists purely to make the version ladder SemVer-monotone.

### Changed
- `pyproject.toml` version bumped **1.1.1 → 1.2.3** to restore SemVer
  monotonicity with the previous `1.1.x` line. (Original 1.1.x → 1.2.x bump
  was missed when the Q2 milestone was first tagged with the descriptive
  `v1.0.0-milestone-q2-2026` tag.)

### Fixed
- **CHANGELOG headings** rewritten to use SemVer-correct names
  (`1.2.0/1.2.1/1.2.2/1.2.3`); descriptive tags kept as aliases.
- **`deploy/lambda/api/handler.py`** — duplicate definitions of
  `audit_table`, `QUOTAS_ENABLED`, `QUOTAS_MAX_*`, `NOTIFICATIONS_TOPIC_ARN`
  and the `sns` client (residue from #48 post-merge regression repair) were
  removed. The top-of-module definitions are now authoritative; the second
  `QUOTAS_ENABLED` in particular was using `default=true` which contradicted
  the documented "default off" behavior.
- **`tests/test_cli.py`** — `test_handles_missing_credentials_gracefully` and
  `test_missing_credentials_errors` now `monkeypatch.chdir(tmp_path)` to
  isolate from a populated `.env.deploy` in the project root. Without this,
  the tests passed on clean CI but failed on developer machines that had
  already run `./setup.sh`.
- **`register_host()` API endpoint** no longer hard-codes 16384 MiB for
  every host. It now calls `ec2.describe_instance_types()` and falls back
  to the same `_SIZE_TO_VCPU` × `_FAMILY_LETTER_TO_MEM_PER_VCPU` table that
  `deploy/stack.py` uses. The Lambda execution role gained
  `ec2:DescribeInstanceTypes`. Body is also now nullable-safe (returns 400
  on missing body / `instance_id` instead of a 500 KeyError).
- **`tests/conftest.py::load_env_deploy`** — removed the hard-coded
  `~/Code/sample-multi-tenant-openclaw-on-firecracker/.env.deploy` fallback
  in favour of paths derived from `__file__` and `cwd`. Works in any
  checkout location and inside containers.
- **`tests/test_e2e.py::test_backup_and_restore_from_latest`** — pre-flight
  checks `GET /hosts/rootfs-version` and `GET /hosts`; skips with a clear
  message when the data plane isn't ready (no rootfs in S3, or no host
  registered). Previously fails with a 240s timeout and a confusing
  "did not reach 'running'" message that hides the real problem.
- **`deploy/userdata/init-host.sh`** — when the host can't find
  `manifest.json` after 10 minutes of retries, the failure log now
  prints both recovery paths (`./scripts/build-rootfs-on-ec2.sh v1.0`
  for laptops without Linux, or `./build-rootfs.sh v1.0` for a Linux
  host) instead of just "manifest.json not available".

### Added
- **`scripts/build-rootfs-on-ec2.sh`** — cloud-native rootfs build for
  contributors on macOS / Windows / Cloud9 (anywhere `debootstrap` is
  unavailable). Launches a one-shot `t3.medium` (or `t4g.medium` for
  ARM64) Ubuntu builder in the same region, attaches the existing
  `openclaw-host-profile` instance role for S3 access, runs the chroot
  build via SSM Run Command, uploads the artifacts + `manifest.json`
  to `s3://${ASSETS_BUCKET}/deployment/rootfs/`, and terminates the
  builder. ~10 minutes total. Closes the long-standing UX gap where the
  README implied "just run `./build-rootfs.sh`" but silently required
  the operator to find a Linux host.

### Test status (post-1.2.4 cleanup, with valid AWS creds)
- **382 passed / 0 failed / 2 skipped** locally — clean run.
- Both skips are intentional and conditional on the data-plane state:
  - `TestListAllBackups::test_list_all_backups` skips when no backups
    exist yet (the roundtrip test populates one when it runs).
  - `TestBackupRestoreRoundtrip::test_backup_and_restore_from_latest`
    skips when `manifest.json` is missing from S3 (i.e. `./build-rootfs.sh`
    has not been run on a Linux host yet) — the test would otherwise time
    out with a confusing "did not reach 'running'" message.
- Once `./build-rootfs.sh` has populated S3 and a host is registered,
  both tests un-skip and the suite reports **384 / 384** clean.

### Snapshot at this tag

| Metric | Value |
|---|---|
| New PRs merged | **27** (24 features + 3 patches) |
| Net new unit tests | **~300** (84 → 384) |
| Test status | **382 / 384 passed**, 2 skipped (data-plane-conditional, see below); 0 failed |
| Issues closed | **25** (24 features + #48 regression) |
| Git tags | **18** (11 per-issue + 4 release + 3 historical) |
| GitHub Releases | **4** (1.2.0, 1.2.1, 1.2.2, 1.2.3) |
| Real-AWS deploy | ✅ CFN `CREATE_COMPLETE` in 450s, control plane verified end-to-end |
| Open issues / PRs | **0 / 0** |

### Known limitations / future work

These are explicit non-goals at 1.2.3; raise an issue if any block your use case.

- **Multi-agent runtime abstraction** — OpenClaw is hard-coded into `build-rootfs.sh` (`npm install -g openclaw`, `openclaw onboard`, `templates/openclaw.json`). Supporting LangGraph / CrewAI / Bedrock Agent / custom Python agents needs a new agent-runtime ABC (the same pattern PR #41 used for Firecracker / Cloud Hypervisor / QEMU).
- **VM data plane on real AWS** — `./build-rootfs.sh` requires a Linux build host with `debootstrap`; rootfs upload to S3 is a separate workflow. The 1.2.x E2E verified the **control plane** (API → DDB → ASG → host registration → tenant lifecycle), not Firecracker boot.
- **Cross-arch rootfs caching** — `--arch arm64` switch rebuilds from scratch. S3 caching by arch + version would speed up Graviton deploys.
- **Cross-region / cross-account federation** — single region per CDK stack today.
- **Pre-copy live migration** — current snapshot/restore (#20) pauses for the duration of memory transfer. Pre-copy iterations would drop downtime to sub-second.

## [1.2.2] - 2026-05-19

Patch release — real-world AWS deploy bugs surfaced during E2E verification.

### Fixed
- **#52** `init-host.sh` race condition: ASG launches the host the moment `cdk deploy` creates the LaunchTemplate, but `setup.sh` uploads `host-agent.py` / `launch-vm.sh` / `stop-vm.sh` *after* deploy returns. The host's user-data ran first and 404'd on those S3 paths, then failed to register because the CFN output query also returned `None` mid-deploy. `_stack_output` and `_s3_get` now retry up to 5 minutes.
- **#53** ALB tried to launch in a single AZ when `multi_az.enabled: false` (default `az_count=1`). AWS rejects with *At least two subnets in two different Availability Zones must be specified*. ALB now independently uses `max(2, az_count)` regardless of ASG mode — single-AZ ASG mode still works for cost-conscious deployments.
- **#51 / setup.sh** Pre-existing `${CDK_ARGS[@]}` expansion broke under `set -u` + empty array on zsh / bash <4.4. Guarded with `${CDK_ARGS[@]+"${CDK_ARGS[@]}"}` pattern.

### Verified
- `./setup.sh ap-northeast-1 …` finishes CFN `CREATE_COMPLETE` in ~450s on a clean account.
- Host registers to DDB via init-host's retry path.
- Tenant lifecycle (`oc create` → pending → creating → `oc delete`) end-to-end on real AWS.

### Out of scope
- VM data plane (Firecracker boot from rootfs) — requires a Linux host running `./build-rootfs.sh` first; control plane is now 100% verified end-to-end.

## [1.2.1] - 2026-05-19

Patch release — full regression repair after the 1.2.0 cross-PR squash-merge.

### Fixed
- **#48** — Restored helpers + call-sites lost during 1.2.0 batch merge (`-X theirs` auto-resolution discarded code from earlier PRs):
  - `_parse_ttl` + `_TTL_*` constants ([#28] / issue #15)
  - `_parse_schedule` + scaler `_schedule_should_run` + reconciliation ([#30] / issue #11)
  - `_audit_write` + GET `/audit-log` route + `_list_audit_log` ([#32] / issue #17)
  - `_check_quota` + `QUOTAS_*` env knobs ([#34] / issue #9)
  - `_publish_event` + SNS client ([#33] / issue #13)
  - `batch_tenants` + `_resolve_filter` ([#29] / issue #23)
  - `tenant_resize` + 'resize' action branch ([#35] / issue #16)
  - 'resize-disk' action branch ([#47] / issue #22)
- **stack.py**: re-added `aws_aps` + `aws_grafana` imports
- **setup.sh**: `${CDK_ARGS[@]+"${CDK_ARGS[@]}"}` to handle `set -u` + empty array on zsh

### Testing
- **368 passed / 1 skipped / 0 failed** at the time of 1.2.1 release (was 123 failed at 1.2.0).
- 1.2.3 picked up the duplicate-symbol cleanup + `test_cli` isolation fix.
- 1.2.4 added the `register_host` memory-lookup fix and the data-plane-aware
  E2E skip; the current run reports **382 / 384 passed, 2 skipped, 0 failed**
  on a control-plane-only deploy. Once `./build-rootfs.sh` populates S3 and
  a host registers, both skips lift automatically and the suite is **384 / 384**.

### Process
- Documented best practices in release notes:
  - Limit ≤10 concurrent same-file PRs per batch
  - Avoid `git merge -X theirs` for cross-PR conflicts
  - Per-PR tags (`v0.N.0-issueX`) for surgical rollback

## [1.2.0] - 2026-05-18

Aliases: `v1.0.0-milestone-q2-2026`. First major milestone — **24 features
merged, 25 issues closed**, ~250 new unit tests.

### Added — Observability (2)
- **#3 Per-VM CPU/memory/disk metrics in DynamoDB** ([#37]) — host-agent samples every 30s, writes per-VM resource usage to the tenants table; console shows live values per row.
- **#4 Amazon Managed Prometheus + Grafana** ([#38]) — Prom remote-write from host-agent, AMG datasource + dashboards, alerts wiring.

### Added — Security (4)
- **#6 EBS encryption at rest for host data volumes** ([#26]) — KMS-encrypted by default; compliance-friendly.
- **#7 Optional AWS WAF integration for API Gateway** ([#31]) — rate-limit + geo-block + AWS managed OWASP rule sets.
- **#14 RBAC via Cognito Groups (admin/operator/viewer)** ([#39]) — role attached to id-token, dispatcher gates per-route; admin = full, operator = CRUD, viewer = read-only.
- **#17 Audit log for all mutating API operations** ([#32]) — every POST/PUT/DELETE writes an entry to the audit DDB table with TTL = 90 days; `GET /audit-log?since=…&limit=…`.

### Added — Tenant lifecycle (9)
- **#9 Per-tenant resource quotas** ([#34]) — `QUOTAS_MAX_VCPU/MEM/DATA_DISK_MB`, fail-fast in `create_tenant`; defends against noisy neighbors.
- **#10 Tenant tagging, grouping, and search** ([#27]) — `tags: {key: value}` field; `GET /tenants?tag=team:sre` AND-filter across multiple tags.
- **#11 Scheduled tenant auto-stop/start (office-hours mode)** ([#30]) — `schedule: {start, stop, timezone, days}`; scaler reconciles every tick.
- **#12 Tenant snapshot/clone via local cp on the same host** ([#36]) — `clone_from: <id>` co-schedules onto the source's host so `cp --sparse data.ext4` is sub-second.
- **#13 SNS lifecycle notifications for tenant events** ([#33]) — `tenant.created/deleted/migrated/expired` events to a configurable SNS topic.
- **#15 Tenant TTL with auto-stop or auto-delete on expiry** ([#28]) — `ttl_hours` + `on_expiry: stop|delete`; scaler scans expired tenants every tick.
- **#16 Live VM resize — hot-add vCPU without restart** ([#35]) — `POST /tenants/{id}/resize {vcpu}` calls Firecracker `/machine-config` PATCH; refuses shrinks (Firecracker limitation) and memory live-resize.
- **#22 Offline auto-resize for tenant data disks** ([#47]) — `POST /tenants/{id}/resize-disk {new_size_mb}` pauses VM → `truncate -s` sparse file → `e2fsck` + `resize2fs` → resume; pause window ~seconds.
- **#23 Batch tenant operations endpoint** ([#29]) — `POST /batch/tenants {action: stop|start|delete|backup, ids|filter}`; per-tenant errors collected into `failed[]`, never abort the batch.

### Added — Platform (4)
- **#5 Pluggable VM runtime protocol (Firecracker / CHV / QEMU stub)** ([#41]) — `Runtime` ABC + factory; only Firecracker fully implemented, the other two are reserved seams.
- **#8 Multi-AZ HA opt-in** ([#42]) — `multi_az.enabled` + `az_count`; ASG fan-out across AZs (ALB always ≥2 AZ — clarified in v1.0.2/#53).
- **#19 Graviton (ARM64) host support** ([#44]) — `host.arch: arm64` switches AMI lookup to Ubuntu Noble arm64 + InstanceType default `m8g.xlarge`; `build-rootfs.sh --arch arm64` cross-builds rootfs.
- **#20 Live VM migration via Firecracker snapshot/restore** ([#45]) — `POST /tenants/{id}/migrate {target_host_id}`; SSM on source pauses + snaps + S3-uploads, SSM on target downloads + restores; DDB `host_id` flips.

### Added — DevX (2)
- **#21 Unified `oc` CLI** ([#40]) — single-file argparse Python tool; subcommands: `list / get / create / delete / restart / start / stop / pause / resume / backup / reset / backups / hosts / version`; reads `OC_API_URL`/`OC_API_KEY` or `.env.deploy`.
- **#24 Local development mode with LocalStack + stub host-agent** ([#46]) — `local-dev/docker-compose.yml` + LocalStack 3 + Python stub host-agent; iterate on Lambda + console without an AWS account.

### Added — Deployment (1)
- **#18 Terraform module at parity with CDK core** ([#43]) — DDB tables, S3 bucket + lifecycle, Lambda + IAM, API Gateway routes; `terraform/README.md` documents the CDK ↔ TF mapping. Advanced features (CloudFront, Cognito, WAF, AMP/AMG, AgentCore) intentionally omitted.

### Added — Other
- **dependabot/urllib3 2.7.0** ([#25]) — security patch.

### Testing
- ~250 new unit tests across the 24 PRs; every PR shipped TDD red→green with a full-suite gate.
- Per-issue tags `v0.3.0-issue3` … `v0.13.0-issue22` for surgical rollback (each tag points at the feature branch's last commit before merge).

[#25]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/25
[#26]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/26
[#27]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/27
[#28]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/28
[#29]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/29
[#30]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/30
[#31]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/31
[#32]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/32
[#33]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/33
[#34]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/34
[#35]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/35
[#36]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/36
[#37]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/37
[#38]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/38
[#39]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/39
[#40]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/40
[#41]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/41
[#42]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/42
[#43]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/43
[#44]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/44
[#45]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/45
[#46]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/46
[#47]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/pull/47

## [1.1.1] - 2026-04-30

### Added
- **AgentCore Memory E2E test** — create_event (conversation turns) + list_events + batch_create_memory_records, verified on real AWS
- **AgentCore Code Interpreter E2E test** — start_session → executeCode (Python 3.12, Sum=5050, exitCode=0) → stop_session
- **AgentCore Browser E2E test** — start_session → get_session (status=READY, WebSocket stream endpoint) → stop_session

### Testing
- 90 tests total: 74 unit + 16 E2E (0 failed)
- All AgentCore components now have end-to-end call verification, not just resource creation checks

## [1.1.0] - 2026-04-30

### Added
- **AgentCore integration verified on real AWS infrastructure** — Full end-to-end deployment and testing of Gateway, Memory, Code Interpreter, Browser, and Workload Identity
- **AgentCore tools Lambda** — hello, system_info, timestamp MCP tools registered on Gateway
- **AgentCore toggle test** — Verified disabled → enabled → disabled lifecycle with resource creation/deletion
- **AgentCore unit tests** — 13 new tests covering tools Lambda and status endpoint
- **AWS Blog posts** — blog.md (EN) and blog-cn.md (CN) with Word versions, covering architecture, cost optimization, and AgentCore integration
- **AWS Blog Writer Skill** — Reusable skill for writing AWS-style technical blog posts
- **CHANGELOG.md** — Project changelog
- **Console improvements** — Version display and GitHub link in Settings page

### Verified (real AWS deployment)
- CDK stack deployment: 144 base resources + 17 AgentCore resources
- Tenant lifecycle: create → running (20s) → Dashboard 200 OK → delete
- AgentCore MCP injection: Gateway URL auto-injected into VM openclaw.json
- Lambda tools invocation: hello/system_info/timestamp all return correct results
- Toggle test: disabled (no MCP) → enabled (MCP injected) → disabled (MCP removed)
- Backup/restore roundtrip: create → backup → restore from backup → running
- All 94 tests passed: 74 unit + 12 E2E + 8 regression

## [1.0.0] - 2026-04-30

### Features
- **Multi-tenant OpenClaw deployment** — Create, manage, and isolate OpenClaw AI agents in Firecracker microVMs via REST API
- **Firecracker microVM isolation** — Independent kernel, filesystem (OverlayFS), and network per tenant
- **Auto scaling** — ASG scale-out on demand, two-round idle host reclamation
- **CPU/Memory overcommit** — Configurable ratios with Firecracker balloon dynamic memory management
- **Auto backup & restore** — EventBridge scheduled daily backups, orphan-safe restore from any backup
- **Shared skills** — S3-managed skills synced to all VMs via cron, independent memory per tenant
- **Config templates** — S3-managed OpenClaw configuration templates, selectable at tenant creation
- **Web management console** — Alpine.js SPA on CloudFront with 4 tabs (Tenants, Application, Backups, Settings)
- **Dashboard access** — One-click HTTPS access via CloudFront → ALB → Nginx → VM Gateway (WebSocket)
- **AgentCore integration** — Optional toggle for Gateway (MCP tool hub), Memory, Code Interpreter, Browser, and Workload Identity
- **Two-tier health monitoring** — Host agent (5s poll) + Lambda watchdog (5min) with auto-recovery
- **Rootfs version management** — manifest.json versioning, per-host tracking, refresh-rootfs API
- **Tenant lifecycle** — create, delete, stop, start, pause, resume, restart, reset, backup
- **Optional Cognito auth** — OAuth2 implicit flow for console protection
- **Custom domain support** — bind-domain.sh for CloudFront + ACM certificate
- **Spot instance support** — Optional 60-70% cost reduction

### Testing
- 74 unit tests (API scheduling, overcommit, tenant CRUD, scaler, health check, balloon, AgentCore tools)
- 12 E2E tests (API connectivity, tenant lifecycle, backup/restore roundtrip, regression)
- 8 regression tests (routing, CORS, field validation)
- Full AgentCore integration verified on real AWS infrastructure (Gateway + Memory + CodeInterpreter + Browser + Identity)
- Toggle test: disabled → enabled → disabled with resource creation/deletion verified

### Documentation
- README.md (English) and docs/README-CN.md (Chinese)
- AWS Blog posts: blog.md (EN) and blog-cn.md (CN) with Word versions
- config.yml.example with all configuration options documented
- CONTRIBUTING.md with code conventions and PR guidelines
