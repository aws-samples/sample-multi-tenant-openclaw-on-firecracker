# Multi-Tenant OpenClaw on Firecracker

**[English](README.md)** | **[中文](docs/README-CN.md)**

Multi-tenant isolated deployment of OpenClaw AI agents on AWS using Firecracker microVMs, also known as **OpenClaw Pool**. Each tenant runs in its own microVM with independent kernel, filesystem, and network. Managed via API, with auto-scaling hosts and idle reclamation.

> This project uses AWS EC2 [nested virtualization](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html) to run KVM + Firecracker inside EC2 instances. Currently supports Intel instance families (c8i/m8i/r8i, etc.).

> ⚠️ This sample is for demonstration purposes only and is not intended for production use. Deploy at your own risk.

## Features

- **Tenant Management** — Create/delete/query tenants via API. Each tenant is an OpenClaw instance running in an isolated Firecracker microVM with its own rootfs, data volume, and network
- **Security Isolation** — Firecracker microVM-based isolation: independent kernel, network, and filesystem per tenant
- **Auto Scheduling** — Automatically selects a host with available resources; scales out when capacity is insufficient
- **Auto Scale-in** — Idle hosts are reclaimed after timeout (two-round confirmation to prevent false kills)
- **Health Checks** — Real-time VM health monitoring with automatic status updates
- **Web Console** — Online management console with Cognito authentication, real-time host/tenant status
- **Rootfs Pre-build** — Rootfs + data template distributed via S3, downloaded on host init
- **Dashboard Access** — One-click HTTPS access to each tenant's OpenClaw Dashboard, no custom domain required
- **Auto Backup & Restore** — EventBridge scheduled backup of all tenant data volumes to S3, with manual trigger, cross-tenant backup query, and one-click restore into a new tenant (orphan-safe — source tenant need not exist)
- **AgentCore Integration** — Optional toggle; when enabled, all VMs auto-connect to AgentCore Gateway (MCP tool hub), Memory, Code Interpreter, and Browser
- **Shared Skills** — All tenants share a unified skill set (S3-managed, auto-synced to all VMs), with independent memory
- **Config Templates** — Custom OpenClaw configuration templates for different LLM providers/models, selectable when creating tenants
- **Default Toolchain** — Each VM comes with Python3/uv/git/gh/Node.js/htop/tmux/tree pre-installed

## v1.0 Feature Matrix

> **Latest release**: [v1.2.5](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/releases/tag/v1.2.5) (386 / 386 unit + e2e tests passing, 0 failed, 0 skipped — control plane + microVM data plane + observability all end-to-end verified on real AWS, including HTTP 200 from CloudFront → ALB → Nginx → Firecracker → OpenClaw Gateway, and 6/6 Prometheus gauges flowing into Amazon Managed Prometheus). See [CHANGELOG.md](CHANGELOG.md) for details.

24 features merged in the Q2 2026 milestone, each with TDD coverage and a per-issue rollback tag.

| Category | Feature | Issue | PR |
|---|---|---|---|
| **Observability** | Per-VM CPU/memory/disk metrics in DynamoDB | [#3] | [#37] |
| | Amazon Managed Prometheus + Grafana | [#4] | [#38] |
| **Security** | EBS encryption at rest | [#6] | [#26] |
| | Optional AWS WAF integration | [#7] | [#31] |
| | RBAC via Cognito Groups (admin/operator/viewer) | [#14] | [#39] |
| | Audit log for all mutating API operations | [#17] | [#32] |
| **Tenant lifecycle** | Per-tenant resource quotas | [#9] | [#34] |
| | Tagging, grouping, and search | [#10] | [#27] |
| | Scheduled auto-stop/start (office-hours mode) | [#11] | [#30] |
| | Snapshot/clone via local cp on the same host | [#12] | [#36] |
| | SNS lifecycle notifications | [#13] | [#33] |
| | TTL with auto-stop or auto-delete on expiry | [#15] | [#28] |
| | Live VM resize — hot-add vCPU without restart | [#16] | [#35] |
| | Offline auto-resize for tenant data disks | [#22] | [#47] |
| | Batch tenant operations endpoint | [#23] | [#29] |
| **Platform** | Pluggable VM runtime protocol (Firecracker / CHV / QEMU stub) | [#5] | [#41] |
| | Multi-AZ HA opt-in | [#8] | [#42] |
| | Graviton (ARM64) host support | [#19] | [#44] |
| | Live VM migration via Firecracker snapshot/restore | [#20] | [#45] |
| **DevX** | Unified `oc` CLI | [#21] | [#40] |
| | Local development mode with LocalStack + stub host-agent | [#24] | [#46] |
| **Deployment** | Terraform module at parity with CDK core | [#18] | [#43] |

[#3]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/3
[#4]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/4
[#5]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/5
[#6]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/6
[#7]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/7
[#8]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/8
[#9]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/9
[#10]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/10
[#11]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/11
[#12]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/12
[#13]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/13
[#14]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/14
[#15]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/15
[#16]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/16
[#17]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/17
[#18]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/18
[#19]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/19
[#20]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/20
[#21]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/21
[#22]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/22
[#23]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/23
[#24]: https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/24

## Quick Start

**Prerequisites:**

- AWS account + CLI configured
- CDK CLI + Python 3.12+
- uv (Python package manager)
- For local rootfs build (`build-rootfs.sh`, optional): `sudo` access on a
  Linux host with `debootstrap` / `pigz` / `e2fsprogs`, ≥2GB free RAM, ≥10GB
  free in `/tmp`. **Not required if you use `build-rootfs-on-ec2.sh` —
  see step 3 below.**

```bash
# 1. Configure
cp config.yml.example config.yml          # Edit infrastructure config
cp templates/openclaw.json.example templates/openclaw.json  # Set your API key, model provider, etc.

# 2. Deploy infrastructure
./setup.sh ap-northeast-1 lab
# Environment variables saved to .env.deploy

# 3. Build rootfs — REQUIRED before creating any tenant.
#
#    Option A — cloud build (works from macOS / Windows / anywhere):
#      ./scripts/build-rootfs-on-ec2.sh v1.0
#    Spins up a one-shot t3.medium Ubuntu host, runs the build via SSM,
#    uploads to S3, terminates. ~10 min, no local Linux required.
#
#    Option B — local Linux host (faster if you already have one):
#      source .env.deploy
#      ./build-rootfs.sh v1.0

# 4. Create a tenant (OpenClaw instance)
source .env.deploy
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" \
  -d '{"name":"my-agent","vcpu":2,"mem_mb":4096}' | jq .

# 5. Open Console — manage tenants, templates, and settings
# Console URL is printed after deploy
```

## Management Console

Web-based console hosted on CloudFront (`/console/`), with Cognito authentication.

Features:
- **Tenants** — Host resource overview, create/delete tenants, one-click Dashboard access
- **Application** — Shared skills list, config template management (create/edit/delete)
- **Backups** — Cross-tenant backup explorer with per-tenant grouping, orphan filter, and one-click restore into a new tenant
- **Settings** — API connection, AgentCore status, system info

### Screenshots

![Tenants tab](docs/web_console.png)

![Backups tab](docs/web_console_backup.png)

## Dashboard Access

Each tenant's OpenClaw Dashboard is accessible via CloudFront + ALB + Nginx reverse proxy:

```
https://{cloudfront-domain}/vm/{tenant-id}/    → Tenant Dashboard (WebSocket)
```

HTTPS is provided by CloudFront out of the box — no custom domain or ACM certificate required. The Console's "Open Dashboard" button includes the gateway token for one-click access.

Traffic flow: `Browser → CloudFront:443 → ALB:80 → Host Nginx:80 → VM Gateway:18789`

Nginx config is automatically managed by launch-vm.sh / stop-vm.sh.

## Custom Domain (Optional)

Bind a custom domain + HTTPS to CloudFront. Configuration lives in `config.yml` under `cloudfront:`; you can edit the file directly or pass flags to `setup.sh`:

```bash
# Prerequisites:
# 1. Request an ACM certificate in us-east-1 (required by CloudFront) and complete DNS validation
# 2. CNAME your domain to the CloudFront domain (see DashboardUrl output)

# One-liner: sets config.yml + deploys in a single run
./setup.sh ap-northeast-1 lab \
  --domain claw.example.com \
  --cert   arn:aws:acm:us-east-1:xxx:certificate/xxx

# Or edit config.yml manually then run setup.sh with no flags.
# To unbind the custom domain: --domain "" and re-run setup.sh.
```

The custom domain and certificate flow through CDK (not out-of-band), so subsequent `setup.sh` runs preserve the binding.

## Auto Backup & Restore

EventBridge schedules daily backups of all running tenant data volumes to S3. Manual trigger also supported.

**Backup flow**: pause VM → pigz compress data.ext4 → resume VM → upload to S3. VM auto-resumes even on failure (trap cleanup).

```bash
source .env.deploy

# Manual backup (async, returns 202)
curl -s -X POST "${API_URL}tenants/{id}/backup" -H "x-api-key: ${API_KEY}" | jq .

# List backups for one tenant
curl -s "${API_URL}tenants/{id}/backups" -H "x-api-key: ${API_KEY}" | jq .

# List all backups across all tenants (marks orphan vs active)
curl -s "${API_URL}backups" -H "x-api-key: ${API_KEY}" | jq .

# Config (config.yml):
# backup_cron: "cron(0 19 * * ? *)"  # UTC 19:00 = Beijing 03:00
# backup_retention_days: 7            # S3 lifecycle auto-cleanup
```

Backups stored at `s3://{bucket}/backups/{tenant-id}/{timestamp}.gz`.

### Restore from Backup

Restore creates a **new** tenant using a backup's data volume. The source tenant does not need to exist — orphan backups from deleted tenants are fully restorable.

```bash
# Restore from the latest backup of a (possibly deleted) tenant
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" -d '{
  "name": "restored-agent",
  "vcpu": 2, "mem_mb": 4096,
  "restore_from": {"tenant_id": "my-agent-ab12"}
}' | jq .

# Restore from a specific backup timestamp
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" -d '{
  "name": "restored-agent",
  "restore_from": {"tenant_id": "my-agent-ab12", "timestamp": "20260428-125402"}
}' | jq .
```

- `restore_from` is decoupled from `vcpu`/`mem_mb`/`config_template` — those follow the new tenant's spec
- Data volume size equals the backup's actual size (no resize)
- The new tenant gets a fresh ID; the source's identity is not inherited

## Shared Skills

All tenants share a unified skill set (SKILL.md files), with independent memory per tenant.

```bash
# Upload skills to S3 (auto-synced to all VMs)
aws s3 sync ./my-skills/ s3://${ASSETS_BUCKET}/skills/ --profile $PROFILE

# Sync chain:
# S3 → Host /data/shared-skills/ (cron 5min) → All running VMs
# New VMs get skills injected into data volume at launch
```

## Auto Scaling

**Scale-out** — No available host when creating a tenant → tenant enters `pending` → ASG launches new instance → pending tenants auto-assigned after init

**Scale-in** — Scaler Lambda checks every 5 minutes:
1. Host with `vm_count=0` exceeding `idle_timeout_minutes` → marked `idle`
2. Next round confirms still idle and ASG instances > min → terminate
3. If a tenant is assigned during this window → auto-recover to `active`

## Observability (Optional)

When `metrics.enabled: true` in `config.yml`, the stack provisions an Amazon Managed Prometheus (AMP) workspace and an Amazon Managed Grafana (AMG) workspace. Each host runs an ADOT collector as a sibling systemd service that scrapes `host-agent`'s `/metrics` endpoint every 30 s and SigV4-signs a remote-write to AMP — no static credentials.

Per-VM gauges exposed (with `tenant=<tenant_id>` and `instance=<host_instance_id>` labels):

```
openclaw_vm_memory_used_mb        openclaw_vm_disk_used_mb
openclaw_vm_memory_balloon_mib    openclaw_vm_disk_total_mb
openclaw_vm_health (0|1)          openclaw_vm_disk_used_pct
```

Sample PromQL:
```promql
# Memory used by all running VMs of a tenant
sum by (tenant) (openclaw_vm_memory_used_mb)

# Hosts with at least one unhealthy VM in the last minute
min_over_time(openclaw_vm_health[1m]) == 0
```

Grafana access uses **AWS IAM Identity Center**:
1. After deploy, the `GrafanaWorkspaceUrl` CFN output gives you the workspace URL.
2. In IAM Identity Center, assign yourself (or a group) to that AMG workspace.
3. First login: in Grafana → *Connections → Data sources → Add → Prometheus*, pick the AMP workspace from the dropdown (the AMG service role already has `aps:QueryMetrics` for it).

To disable observability later, set `metrics.enabled: false` and re-run `setup.sh` — AMP and AMG are removed and you stop being billed for samples / active users.

## Configuration

### Config Files

| File | Purpose |
|------|---------|
| `config.yml` | Infrastructure config — copy from `config.yml.example` and customize |
| `templates/openclaw.json` | OpenClaw app config (model, API key, provider) — copy from `.example` |
| `.env.deploy` | Deploy environment (region, API URL/Key, bucket) — auto-generated by setup.sh |

### config.yml

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| host | instance_type | m8i.2xlarge | Must support NestedVirtualization (c8i/m8i/r8i) |
| host | data_volume_gb | 200 | Data volume for rootfs templates + VM disks |
| host | cpu_overcommit_ratio | 2.0 | CPU overcommit (1.0=none, 2.0=allocate 2x vCPU) |
| host | mem_overcommit_ratio | 1.0 | Memory overcommit (requires balloon enabled) |
| host | keep_data_volume | true | Keep EBS data volume after instance termination |
| vm | default_vcpu | 2 | Default vCPU per tenant |
| vm | default_mem_mb | 4096 | Default memory (MB) per tenant |
| vm | rootfs_overlay_mb | 8192 | Per-VM writable rootfs layer cap (sparse, doesn't pre-allocate) |
| vm | data_disk_mb | 8192 | Per-VM data volume `/home/agent` cap (sparse) |
| balloon | enabled | false | Firecracker balloon device for memory overcommit |
| balloon | max_inflate_ratio | 0.4 | Max reclaimable ratio of VM declared memory |
| balloon | min_guest_available_mb | 512 | Min available memory kept in guest |
| asg | min_capacity | 1 | Minimum host instances |
| asg | max_capacity | 5 | Maximum host instances |
| asg | use_spot | false | Spot instances (save ~60-70%, may be reclaimed) |
| scaler | idle_timeout_minutes | 10 | Idle host reclaim timeout |
| health_check | interval_minutes | 5 | Lambda watchdog interval |
| agentcore | enabled | false | AgentCore Gateway/Memory/CodeInterpreter/Browser |
| metrics | enabled | false | Provision Amazon Managed Prometheus + Grafana and have each host's ADOT collector remote-write per-VM gauges (`openclaw_vm_memory_used_mb`, `disk_used_pct`, `vm_health`, …). See [Observability](#observability-optional) below |
| metrics | workspace_alias | openclaw | AMP workspace alias (only used when `metrics.enabled: true`) |
| metrics | grafana_name | openclaw-metrics | AMG workspace name (only used when `metrics.enabled: true`) |
| console_auth | enabled | false | Cognito authentication for Console |
| console_auth | self_sign_up | false | Allow user self-registration |

See `config.yml.example` for all options. Redeploy after changes: `./setup.sh <region> <profile>`

### Tear Down

```bash
./scripts/destroy.sh           # Destroy stack, keep S3 bucket and DynamoDB tables
./scripts/destroy.sh --purge   # Full cleanup including S3 data and DynamoDB tables
```

## Architecture

### System Architecture

![System Architecture](docs/oc-system-arch.png)

### Deployment Architecture

![Deployment Architecture](docs/oc-deploy-arch.png)

<details>
<summary>ASCII version (for AI/text access)</summary>

```
Admin / User
    │
    ├── API Gateway (HTTPS, x-api-key) ──→ Lambda ──→ DynamoDB
    │                                                  ├── tenants
    │                                                  └── hosts
    │
    └── ALB (HTTPS) ──→ Host Nginx:80 ──→ VM Gateway:18789
                        ├── /vm/{tenant-a}/ → 172.16.1.2
                        └── /vm/{tenant-b}/ → 172.16.2.2

Lambda ── SSM Run Command ──→ EC2 Host
                               ├── microVM 01 (172.16.1.2)
                               ├── microVM 02 (172.16.2.2)
                               └── ...

S3: rootfs distribution + data backup + shared skills
ASG: auto-scaling hosts
EventBridge: health checks + idle reclamation + scheduled backup
```

</details>

## Project Structure

```
sample-multi-tenant-openclaw-on-firecracker/
├── deploy/                    # CDK project
│   ├── app.py                 # CDK app entry
│   ├── stack.py               # Infrastructure definition
│   ├── lambda/
│   │   ├── api/handler.py     # Tenant CRUD + host management
│   │   ├── templates/handler.py  # Config template CRUD
│   │   ├── skills/handler.py  # Shared skills list
│   │   ├── health_check/handler.py  # Scheduled health checks
│   │   ├── agentcore_tools/handler.py  # AgentCore Gateway Lambda tools
│   │   └── scaler/handler.py  # Idle host reclamation
│   └── userdata/
│       ├── init-host.sh       # Host initialization
│       ├── host-agent.py      # VM health polling + DDB writes + balloon
│       ├── launch-vm.sh       # microVM launch
│       └── stop-vm.sh         # microVM stop
├── console/                   # Web management console
│   ├── index.html             # Alpine.js SPA (4 tabs)
│   └── style.css
├── tests/                     # Test suite (unit + e2e)
├── templates/                 # OpenClaw config templates
│   └── openclaw.json.example  # Example config
├── pyproject.toml             # Python project config + dependencies
├── cdk.json                   # CDK app config + feature flags
├── config.yml                 # Infrastructure config (single source of truth)
├── setup.sh                   # One-click deploy + export .env.deploy
├── build-rootfs.sh            # Build rootfs locally on Linux (debootstrap)
├── scripts/
│   ├── build-rootfs-on-ec2.sh # Cloud build (no local Linux required) — recommended for macOS / Windows / Cloud9
│   ├── destroy.sh             # Tear down stack
│   ├── oc-connect.sh          # SSH-style helper to reach a tenant VM
│   └── oc-dashboard.sh        # Open a tenant's Dashboard URL
└── docs/
```

## API Reference

All requests require `x-api-key` header.

### Tenants

| Method | Path | Description |
|--------|------|-------------|
| GET | /tenants | List all tenants. Filter with `?tag=key:value` (repeatable, AND across pairs) |
| POST | /tenants | Create tenant. Body: `{"name":"xx","vcpu":2,"mem_mb":4096,"data_disk_mb":8192,"config_template":"...","tags":{"k":"v"},"ttl_hours":24,"on_expiry":"stop","schedule":{"start":"09:00","stop":"18:00","timezone":"UTC","days":["Mon","Tue"]},"restore_from":{"tenant_id":"..."},"clone_from":"<src-tenant-id>"}` — only `name` is required |
| GET | /tenants/{id} | Get tenant details |
| DELETE | /tenants/{id} | Delete tenant (`?keep_data=true` to preserve data volume) |
| POST | /tenants/{id}/restart | Restart VM (reuse disks, fast) |
| POST | /tenants/{id}/stop | Stop VM (disks preserved) |
| POST | /tenants/{id}/start | Start a stopped VM |
| POST | /tenants/{id}/pause | Freeze vCPU (Firecracker native, instant) |
| POST | /tenants/{id}/resume | Resume a paused VM |
| POST | /tenants/{id}/reset | Reinstall rootfs (data volume preserved) |
| POST | /tenants/{id}/backup | Manual data backup (async, returns 202) |
| POST | /tenants/{id}/resize | Hot-add vCPU. Body: `{"vcpu":4}`. Memory live-resize is not supported (Firecracker limitation) |
| POST | /tenants/{id}/resize-disk | Offline grow data disk. Body: `{"new_size_mb":16384}`. Pauses the VM ~seconds |
| POST | /tenants/{id}/migrate | Live VM migration via Firecracker snapshot/restore. Body: `{"target_host_id":"i-..."}` |
| GET | /tenants/{id}/backups | List backups for one tenant |
| POST | /batch/tenants | Batch operation. Body: `{"action":"stop|start|delete|backup","ids":["t1","t2"]}` or `{"action":"...","filter":{"tag":"k:v"}}`. Returns `{succeeded:[...], failed:[...]}` |

### Backups, Hosts, AgentCore, Audit, Skills, Templates

| Method | Path | Description |
|--------|------|-------------|
| GET | /backups | List all backups across tenants (marks orphan vs active) |
| GET | /hosts | List all hosts |
| POST | /hosts | Register host (rarely used — UserData writes DDB directly) |
| POST | /hosts/refresh-rootfs | Push latest rootfs to all hosts |
| GET | /hosts/rootfs-version | Query current rootfs version (manifest.json) |
| DELETE | /hosts/{id} | Deregister host |
| GET | /agentcore/status | AgentCore enable status + Gateway URL (when enabled) |
| GET | /audit-log | Query audit log. `?since=<ISO8601>&limit=<n>` (max 500). 90-day TTL via DDB |
| GET | /skills | List shared skills (S3-managed) |
| GET | /templates | List config templates |
| GET\|PUT\|DELETE | /templates/{name} | Read / save / remove a config template (`default` is read-only) |

## Network Model

Each VM uses an independent /24 subnet, communicating with the host via TAP device:

```
VM1: tap-vm1  host=172.16.1.1/24  guest=172.16.1.2/24
VM2: tap-vm2  host=172.16.2.1/24  guest=172.16.2.2/24
VMn: tap-vmN  host=172.16.N.1/24  guest=172.16.N.2/24
```

- Outbound: iptables MASQUERADE → internet
- Inbound: ALB → Nginx reverse proxy → VM:18789
- Inter-VM: fully isolated, no routing between subnets

## Rootfs Management

The build script produces two images: rootfs (OS + software) and data template (/home/agent pre-configured content).

Versions managed via S3 `manifest.json`. Hosts and tenants track their `rootfs_version`.

```bash
# Build and upload (updates manifest.json + refreshes hosts)
./build-rootfs.sh v1.8

# Manually refresh host images
source .env.deploy
curl -s -X POST "${API_URL}hosts/refresh-rootfs" -H "x-api-key: ${API_KEY}" | jq .

# Query current version
curl -s "${API_URL}hosts/rootfs-version" -H "x-api-key: ${API_KEY}" | jq .

# New VMs use the latest version; existing VMs need reset to update
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
