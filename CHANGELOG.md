# Changelog

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
