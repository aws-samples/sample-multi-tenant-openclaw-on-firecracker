# Changelog

## [1.0-final] - 2026-05-19

Aggregate milestone tag. Closes the Q2 2026 work cycle: v1.0.0 + v1.0.1 + v1.0.2.

### Snapshot at this tag

| Metric | Value |
|---|---|
| New PRs merged | **27** (24 features + 3 patches) |
| Net new unit tests | **~250** (84 → 368) |
| Test status | **368 / 368 passed**, 0 failed |
| Issues closed | **25** (24 features + #48 regression) |
| Git tags | **18** (11 per-issue + 4 release + 3 historical) |
| GitHub Releases | **4** (v1.0.0, v1.0.1, v1.0.2, v1.0-final) |
| Real-AWS deploy | ✅ CFN `CREATE_COMPLETE` in 450s, control plane verified end-to-end |
| Open issues / PRs | **0 / 0** |

### Known limitations / future work

These are explicit non-goals at v1.0; raise an issue if any block your use case.

- **Multi-agent runtime abstraction** — OpenClaw is hard-coded into `build-rootfs.sh` (`npm install -g openclaw`, `openclaw onboard`, `templates/openclaw.json`). Supporting LangGraph / CrewAI / Bedrock Agent / custom Python agents needs a new agent-runtime ABC (the same pattern PR #41 used for Firecracker / Cloud Hypervisor / QEMU).
- **VM data plane on real AWS** — `./build-rootfs.sh` requires a Linux build host with `debootstrap`; rootfs upload to S3 is a separate workflow. The v1.0 E2E verified the **control plane** (API → DDB → ASG → host registration → tenant lifecycle), not Firecracker boot.
- **Cross-arch rootfs caching** — `--arch arm64` switch rebuilds from scratch. S3 caching by arch + version would speed up Graviton deploys.
- **Cross-region / cross-account federation** — single region per CDK stack today.
- **Pre-copy live migration** — current snapshot/restore (#20) pauses for the duration of memory transfer. Pre-copy iterations would drop downtime to sub-second.

## [1.0.2-e2e-fixes] - 2026-05-19

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

## [1.0.1-fix-issue48] - 2026-05-19

Patch release — full regression repair after the v1.0.0 cross-PR squash-merge.

### Fixed
- **#48** — Restored helpers + call-sites lost during v1.0.0 batch merge (`-X theirs` auto-resolution discarded code from earlier PRs):
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
- **368 passed / 1 skipped / 0 failed** (was 123 failed at v1.0.0)

### Process
- Documented best practices in release notes:
  - Limit ≤10 concurrent same-file PRs per batch
  - Avoid `git merge -X theirs` for cross-PR conflicts
  - Per-PR tags (`v0.N.0-issueX`) for surgical rollback

## [1.0.0-milestone-q2-2026] - 2026-05-18

First major milestone — **24 features merged, 25 issues closed**, ~250 new unit tests.

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
