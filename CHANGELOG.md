# Changelog

> **Versioning note (2026-05-19)**: tags `v1.0.0-milestone-q2-2026` /
> `v1.0.1-fix-issue48` / `v1.0.2-e2e-fixes` / `v1.0-final` were originally
> created as descriptive aliases during the Q2 2026 release cycle, but they
> break SemVer monotonicity (the previous release was already `1.1.x`).
> The SemVer-authoritative version ladder for this cycle is
> `1.2.0 → 1.2.1 → 1.2.2 → 1.2.3 → 1.2.4` and the entries below are headed
> with the SemVer name; the descriptive tag is kept as an alias on each entry.
> `pyproject.toml` is **1.2.6** as of the most recent entry.

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

Closes the **observability** gap. The 1.2.4 release verified the data
plane all the way to "HTTP 200 from CloudFront → ALB → Nginx → Firecracker"
but never actually checked that AMP / Grafana were receiving samples.
A live probe today found two stacked bugs that meant zero metrics had
ever flowed into AMP since the feature shipped (issue #4 / PR #38).

### Fixed (real production bugs found via live AMP probe)
- **`deploy/userdata/adot-config.yaml` / `host-agent.py` — split-port
  mismatch.** ADOT was configured to scrape `127.0.0.1:9090`, but
  `host-agent.py main()` only ever bound the single HTTPServer on
  `OC_AGENT_PORT` (8899). The agent's own comment block stated the
  design as "/metrics on the same HTTPServer as /health to avoid a
  second listener" — so the architecture was correct, the wiring was
  not. Result: every ADOT scrape since the metrics feature shipped
  failed with "Failed to scrape Prometheus endpoint". Fix: ADOT now
  scrapes `127.0.0.1:8899`; the dead `OC_AGENT_PROM_PORT` variable
  was removed.
- **`deploy/userdata/host-agent.py` — `_status` did not include the
  computed metrics.** `_write_ddb()` computed per-VM metrics
  (`memory_used_mb`, `disk_used_mb`, etc.) and wrote them to DynamoDB,
  but never assigned them back into the in-memory `info` dict that
  `_status` is rebuilt from each poll cycle. The `/metrics` endpoint
  reads `_status`, so it could only ever emit `openclaw_vm_health`
  (which is derived directly from `vm_health`). Every other gauge
  was empty in AMP, even when DDB had the values. Fix: mirror the
  computed `metrics` dict back into `info["metrics"]` before the DDB
  write so the Prometheus exporter sees it.
- **`deploy/stack.py` + `tests/test_prometheus.py` + `init-host.sh`
  — stale `:9090` references.** Comments and assertions still pointed
  at the never-bound 9090 port. Updated to `:8899` and added two
  regression tests that fail loudly if either bug recurs:
  `test_collector_config_has_amp_endpoint` now asserts
  `127.0.0.1:8899` is the scrape target (and explicitly bans 9090);
  `test_write_ddb_mirrors_metrics_back_into_status_for_prom_exporter`
  asserts the back-mirror line stays in `_write_ddb`.
- **`uv.lock` — bumped `idna` 3.11 → 3.15** to close the medium-severity
  Dependabot CVE alert (CVE-2024-3651-related; idna < 3.15 lets
  specially-crafted inputs to `idna.encode()` bypass the original
  patch). Pure transitive bump (boto3 → urllib3 → idna), no API change.
  Verified by full unit-test re-run.

### Verified end-to-end on real AWS (live AMP probe, this exact tag)
- ✅ ADOT collector active + `Everything is ready. Begin running and
  processing data.` (no more `Failed to scrape Prometheus endpoint`).
- ✅ `host-agent /metrics` emits all 6 gauges with per-tenant labels
  (`openclaw_vm_memory_used_mb`, `openclaw_vm_memory_balloon_mib`,
  `openclaw_vm_disk_used_mb`, `openclaw_vm_disk_total_mb`,
  `openclaw_vm_disk_used_pct`, `openclaw_vm_health`).
- ✅ AMP PromQL `query` API returns real values for all 6 gauges
  (probed via SigV4-signed HTTPS GET). Example: `openclaw_vm_health
  {tenant="obs-test-366f", instance="i-0bb45368534d350e2"} = 1`.
- ✅ Grafana workspace `g-5e6517493c` ACTIVE on Grafana 10.4 with
  AWS_SSO authentication; AMP shows up under PROMETHEUS data sources.

### Test status
- **386 passed / 0 failed / 0 skipped** locally (1.2.5 adds 2 new
  regression tests on top of the 1.2.4 baseline of 384).

### Operator notes
- After upgrading from 1.2.4 → 1.2.5, redeploy the stack and roll the
  ASG (or push the new `host-agent.py` + `adot-config.yaml` via SSM)
  to pick up the fixes on existing hosts. New hosts launched by
  `setup.sh` get the fix automatically.
- `metrics.enabled: false` (default in `config.yml.example`) skips
  AMP and Grafana entirely — there is no cost surprise for users
  who don't opt in.


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
- ✅ `POST /tenants` → `creating` → `running` in ~11 s (latest live probe);
  `app_health=up` in ~16 s; full `creating → deleted` cycle in ~23 s.
- ✅ Firecracker VM responds on `172.16.x.2:18789`; gateway token auto-injected.
- ✅ `GET https://${DASHBOARD_URL}/vm/{tenant_id}/?token=…` → **HTTP 200**
  (177 ms TTFB through CloudFront → ALB → Nginx → Firecracker).
- ✅ Per-VM metrics field correctly populated in DDB
  (`memory_used_mb`, `disk_used_mb`, `disk_used_pct`, `cpu_pct`, `memory_balloon_mib`).
- ✅ `DELETE /tenants/{id}` cleanly tears down (ALB rule + iptables DNAT
  removed, Firecracker SIGKILL'd, no zombies).

### Test status (post-1.2.4, with valid AWS creds + rootfs in S3)
- **385 passed / 0 failed / 0 skipped** locally — fully clean run on a
  freshly-cleaned host. (Earlier runs in this cycle reported 384+1 skip
  due to back-to-back roundtrip cleanup races; the new pre-flight sweep
  + stop-vm.sh SIGKILL fix close that gap.)

## [1.2.3] — 2026-05-19

Aliases: `v1.0-final`. End-of-Q2-2026 milestone tag — closes the work cycle
that started with `v1.0.0-milestone-q2-2026` (now 1.2.0). Identical code to
1.2.2; this entry exists purely to make the version ladder SemVer-monotone.

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

Aliases: `v1.0.2-e2e-fixes`. Patch release — real-world AWS deploy bugs
surfaced during E2E verification.

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

Aliases: `v1.0.1-fix-issue48`. Patch release — full regression repair after
the 1.2.0 cross-PR squash-merge.

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
