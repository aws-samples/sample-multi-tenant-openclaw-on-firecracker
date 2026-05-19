# Changelog

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

First major milestone — 24 features merged, 25 issues closed.

### Added — Observability
- **#3** Per-VM CPU/memory/disk metrics in DynamoDB ([#37])
- **#4** Amazon Managed Prometheus + Grafana ([#38])

### Added — Security
- **#6** EBS encryption at rest ([#26])
- **#7** Optional AWS WAF ([#31])
- **#14** RBAC via Cognito Groups ([#39])
- **#17** Audit log for mutations ([#32])

### Added — Tenant lifecycle
- **#9** Per-tenant quotas ([#34])
- **#10** Tagging + search ([#27])
- **#11** Scheduled stop/start ([#30])
- **#12** Snapshot/clone ([#36])
- **#13** SNS notifications ([#33])
- **#15** TTL with auto-stop/delete ([#28])
- **#16** Live VM resize ([#35])
- **#22** Offline disk resize ([#47])
- **#23** Batch operations ([#29])

### Added — Platform
- **#5** Pluggable runtime protocol ([#41])
- **#8** Multi-AZ HA ([#42])
- **#19** Graviton (ARM64) ([#44])
- **#20** Live VM migration ([#45])

### Added — DevX
- **#21** Unified `oc` CLI ([#40])
- **#24** Local dev with LocalStack ([#46])

### Added — Deployment
- **#18** Terraform module ([#43])

### Testing
- ~250 new unit tests, every PR shipped TDD red→green
- Per-issue tags `v0.3.0-issue3` … `v0.13.0-issue22`

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
