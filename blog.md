# OpenClaw Pool: Multi-tenant OpenClaw AI agents with Firecracker microVM isolation on AWS

by Neo Sun and Aleck Lin | AWS | Architecture, Compute, Serverless, SaaS

---

[OpenClaw](https://github.com/anthropics/openclaw) is an open-source AI agent framework that provides a complete runtime for autonomous coding agents — including a gateway server, interactive dashboard, tool execution, and session management. When organizations want to offer OpenClaw-powered agents to multiple teams or customers, they need a way to run many independent instances with strong isolation, without managing infrastructure for each one.

In this post, we introduce **OpenClaw Pool** ([GitHub](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker)), an open-source reference architecture that deploys multi-tenant OpenClaw agents on AWS using [Firecracker](https://firecracker-microvm.github.io/) microVMs. Each tenant gets a fully isolated OpenClaw instance — its own kernel, filesystem, network, and pre-configured development environment — while a serverless control plane handles scheduling, scaling, health monitoring, and backup through a single REST API.

## The multi-tenant OpenClaw challenge

A single OpenClaw instance includes a Node.js gateway server, a persistent workspace, configuration files, and optional MCP tool integrations. When you need to provide isolated OpenClaw environments to multiple teams, departments, or customers, you typically consider three approaches:

| Approach | Isolation level | Startup time | Density | Operational complexity |
|----------|----------------|-------------|---------|----------------------|
| One EC2 instance per tenant | Hardware-level (strongest) | Minutes | Low — idle instances waste resources | Low per tenant, high total cost |
| Containers (ECS/EKS) | Namespace-level (shared kernel) | Seconds | High | High — requires orchestration expertise |
| Firecracker microVMs | **Kernel-level (independent kernel)** | **<125ms** | **High — minimal overhead per VM** | **Low — automated by control plane** |

Containers share the host kernel, which means a kernel vulnerability could potentially affect all tenants. Dedicated EC2 instances provide strong isolation but at significant cost and with slow provisioning. Firecracker microVMs combine the security properties of virtual machines — each tenant gets its own Linux kernel — with the speed and resource efficiency typically associated with containers. This is the same virtualization technology that powers [AWS Lambda](https://aws.amazon.com/lambda/) and [AWS Fargate](https://aws.amazon.com/fargate/).

## Solution overview

OpenClaw Pool consists of three layers: a serverless control plane, EC2-based host instances running Firecracker microVMs, and an access layer for HTTPS dashboard connectivity.

*Figure 1. System architecture showing the control plane, data plane, and access layer.*

![System architecture](docs/oc-system-arch.png)

### Control plane (serverless)

[Amazon API Gateway](https://aws.amazon.com/api-gateway/) exposes a REST API secured with API keys. Seven [AWS Lambda](https://aws.amazon.com/lambda/) functions handle tenant lifecycle operations, host management, backup orchestration, configuration template management, shared skills listing, health monitoring, and idle host reclamation. [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) stores tenant state (status, health, gateway token, rootfs version) and host resource tracking (vCPU, memory, VM count, idle timestamp). [Amazon EventBridge](https://aws.amazon.com/eventbridge/) schedules three recurring tasks: health checks (every 5 minutes), idle host reclamation (every 3 minutes), and daily data backups.

### Data plane (EC2 hosts)

An [Auto Scaling group](https://docs.aws.amazon.com/autoscaling/ec2/userguide/auto-scaling-groups.html) manages EC2 instances that support [nested virtualization](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html) (Intel c8i/m8i/r8i families). Each host runs multiple Firecracker microVMs. A host agent service polls all local VMs every 5 seconds, performing three functions:

1. **Health monitoring** — Pings each VM and probes the OpenClaw gateway port (18789). Writes health status directly to DynamoDB, promoting tenants from `creating` to `running` when the gateway becomes responsive.
2. **Auto-recovery** — Detects VMs where `vm.json` exists but the Firecracker process is not running (for example, after an unexpected crash), and automatically re-launches them.
3. **Balloon memory management** — Reads host `/proc/meminfo` and adjusts Firecracker balloon devices to reclaim or return memory based on host pressure.

### Access layer

[Amazon CloudFront](https://aws.amazon.com/cloudfront/) provides HTTPS termination without requiring a custom domain or ACM certificate. An [Application Load Balancer](https://aws.amazon.com/elasticloadbalancing/application-load-balancer/) routes requests to the correct host using path-based rules (`/vm/{tenant-id}/`). Nginx on each host reverse-proxies to the tenant's microVM gateway with WebSocket support for the interactive dashboard. Nginx configuration is automatically managed by the VM launch and stop scripts.

## Deployment architecture

The entire infrastructure is defined as a single [AWS CDK](https://aws.amazon.com/cdk/) stack and deployed with one command.

*Figure 2. Deployment architecture showing AWS services and their relationships.*

![Deployment architecture](docs/oc-deploy-arch.png)

The deployment creates the following resources:

- Two DynamoDB tables (`tenants`, `hosts`) with pay-per-request billing
- An S3 bucket for rootfs images, data templates, backups, shared skills, configuration templates, and the web console
- Seven Lambda functions (API, health check, scaler, backup, skills, templates, AgentCore tools)
- API Gateway with usage plan and API key authentication
- Auto Scaling group with lifecycle hooks for host initialization and graceful termination cleanup
- ALB with path-based routing for tenant dashboard access
- CloudFront distribution with S3 origin for the web console and a CloudFront Function for URL rewriting
- Optional [Amazon Cognito](https://aws.amazon.com/cognito/) user pool for console authentication
- Optional [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) integration (Gateway, Memory, Code Interpreter, Browser)

## How tenant isolation works

Each microVM is isolated at multiple levels:

**Kernel isolation.** Every tenant runs its own Linux kernel inside a Firecracker microVM. Unlike container-based isolation where all tenants share the host kernel, a vulnerability in one tenant's kernel cannot affect other tenants.

**Filesystem isolation.** The rootfs uses OverlayFS with a shared read-only base layer and a per-VM writable overlay (2 GB sparse file). A custom `/sbin/overlay-init` script runs as the VM's init process, mounting the overlay before handing off to systemd. Each tenant also has an independent data volume (`data.ext4`) mounted at `/home/agent`, which persists across restarts and rootfs resets.

**Network isolation.** Each VM gets a dedicated TAP device and a `/24` subnet (for example, `172.16.1.0/24` for VM 1, `172.16.2.0/24` for VM 2). Outbound traffic uses iptables MASQUERADE through the host's default interface. There is no routing between VM subnets — tenants cannot communicate with each other at the network level.

**Access isolation.** Each tenant's OpenClaw gateway generates a unique authentication token (48-character hex) at creation time. The token is stored in DynamoDB and included in the console's "Open Dashboard" link for one-click access.

## What each OpenClaw tenant includes

Every tenant is a complete, self-contained OpenClaw environment running inside a Firecracker microVM:

- **OpenClaw Gateway** — The agent's HTTP/WebSocket server (port 18789), auto-started as a systemd user service. Provides the interactive Dashboard for chatting with the agent, managing sessions, and approving tool use.
- **OpenClaw CLI** — Pre-installed globally. Tenants can run `openclaw` commands directly.
- **Configurable LLM backend** — Each tenant's `openclaw.json` specifies the model provider, API key, and model ID. You can use configuration templates to standardize settings or customize per tenant.
- **Full development toolchain** — Python 3.12, Node.js 22, uv, git, GitHub CLI, build-essential, and common utilities (curl, jq, htop, tmux, tree).
- **Shared skills, independent memory** — All tenants share a centrally managed skill set (synced from S3), while each tenant's conversation history and workspace are fully isolated on its own data volume.
- **Optional AgentCore integration** — When enabled, each tenant automatically connects to Amazon Bedrock AgentCore for MCP tools, persistent memory, code interpreter, and browser capabilities.

## Rootfs management and pre-installed toolchain

The `build-rootfs.sh` script produces two images using debootstrap on Ubuntu Noble (24.04):

- **rootfs** (read-only, shared) — The base OS with pre-installed tools: Python 3.12, Node.js 22, uv (Python package manager), git, GitHub CLI (gh), OpenClaw CLI, curl, jq, htop, tmux, tree, vim, and build-essential.
- **data template** (copied per tenant) — The `/home/agent` directory containing OpenClaw configuration, gateway service definition, and workspace structure.

Both images are compressed with pigz and uploaded to S3 with a `manifest.json` version pointer. Hosts track their rootfs version, and the `POST /hosts/refresh-rootfs` API pushes new images to all active hosts. New tenants automatically use the latest version; existing tenants can be updated using the `reset` action, which replaces the overlay while preserving the data volume.

## Tenant lifecycle operations

The API supports a full set of lifecycle operations, each implemented via [AWS Systems Manager](https://aws.amazon.com/systems-manager/) Run Command to the host:

| Operation | Behavior | Use case |
|-----------|----------|----------|
| `create` | Allocate resources, launch microVM, configure networking and ALB routing | New tenant |
| `delete` | Stop VM, remove ALB rule, update host counters. Optional `?keep_data=true` preserves the data volume | Tenant removal |
| `stop` / `start` | Graceful shutdown / cold boot (reuses existing disks) | Maintenance windows |
| `pause` / `resume` | Freeze / unfreeze vCPUs via Firecracker API (instant, sub-millisecond) | Temporary suspension |
| `restart` | Stop then start (reuses disks, fast) | Recover from issues |
| `reset` | Delete overlay, re-launch (data volume preserved, rootfs refreshed to latest version) | System update |
| `backup` | Async: pause VM, compress data volume, resume VM, upload to S3 | Data protection |

When creating a tenant, you can specify a `config_template` parameter to apply a pre-configured OpenClaw configuration (LLM provider, model, API key) from S3-managed templates. You can also use `restore_from` to create a new tenant from an existing backup.

## Shared skills

All tenants share a unified set of skills (SKILL.md files) managed centrally in S3, while maintaining independent memory per tenant. The synchronization chain works as follows:

1. You upload skills to `s3://{bucket}/skills/` (for example, via `aws s3 sync`)
2. A cron job on each host syncs from S3 to `/data/shared-skills/` every 5 minutes
3. When a new VM launches, `launch-vm.sh` mounts the data volume and copies skills into `.openclaw/skills/`
4. Running VMs receive updated skills on their next host sync cycle

## Auto scaling and idle reclamation

**Scale-out.** When you create a tenant and no host has sufficient resources (accounting for overcommit ratios), the tenant enters a `pending` state and the control plane increments the Auto Scaling group's desired capacity. The host initialization process takes approximately 3–5 minutes (KVM setup, Firecracker installation, rootfs download from S3, DynamoDB self-registration, lifecycle hook completion). An EventBridge rule on the `EC2 Instance Launch Successful` event triggers the API Lambda to assign all pending tenants to newly available hosts.

**Scale-in.** A scaler Lambda runs every 3 minutes and implements a two-round confirmation process to prevent premature termination:

1. A host with `vm_count=0` exceeding the configured `idle_timeout_minutes` (default: 10) is marked `idle`
2. On the next check, if the host is still idle and the Auto Scaling group's desired capacity exceeds its minimum, the host is terminated via the ASG API with `ShouldDecrementDesiredCapacity=True`
3. If a tenant is assigned to the host between rounds, the scaler automatically recovers the host to `active` status

A termination lifecycle hook triggers the API Lambda to clean up DynamoDB records and ALB rules for any tenants on the terminating host.

## Two-tier health monitoring

Health monitoring uses a two-tier architecture for reliability:

**Primary: Host agent (every 5 seconds).** The agent runs as a systemd service on each host, probing all local VMs via ping and HTTP. It writes health status directly to DynamoDB, avoiding the latency and cost of per-tenant SSM commands. When a VM transitions from `creating` to healthy, the agent reads the gateway token via SSH and promotes the tenant to `running`.

**Secondary: Lambda watchdog (every 5 minutes).** The health check Lambda scans for running tenants with stale health data (no update for 2 minutes). If all tenants on a host are stale, the Lambda concludes the host agent itself is down and restarts it via SSM (`systemctl restart host-agent`). A 10-minute cooldown prevents restart storms.

## Backup and restore

EventBridge triggers daily backups of all running tenants' data volumes to [Amazon S3](https://aws.amazon.com/s3/). The backup script on the host executes the following sequence:

1. Pause the VM (Firecracker API, instant)
2. Compress `data.ext4` with pigz (parallel gzip)
3. Resume the VM
4. Upload the compressed file to `s3://{bucket}/backups/{tenant-id}/{timestamp}.gz`

A `trap` handler ensures the VM resumes even if compression or upload fails. S3 lifecycle rules (configured via a CDK CustomResource) automatically delete backups older than the retention period (default: 7 days).

*Figure 3. The Backups tab showing cross-tenant backup management with orphan detection.*

![Web console — Backups tab](docs/web_console_backup.png)

The `GET /backups` API returns all backups across all tenants, left-joined with the tenants table to mark each backup as `active` (source tenant exists) or `orphan` (source tenant deleted). Restore creates a new tenant using a backup's data volume — the source tenant does not need to exist:

```bash
# Restore from the latest backup of a (possibly deleted) tenant
curl -s -X POST "${API_URL}tenants" \
  -H "x-api-key: ${API_KEY}" \
  -d '{
    "name": "restored-agent",
    "vcpu": 2, "mem_mb": 4096,
    "restore_from": {"tenant_id": "original-agent-ab12"}
  }' | jq .

# Restore from a specific timestamp
curl -s -X POST "${API_URL}tenants" \
  -H "x-api-key: ${API_KEY}" \
  -d '{
    "name": "restored-agent",
    "restore_from": {"tenant_id": "original-agent-ab12", "timestamp": "20260428-125402"}
  }' | jq .
```

## Cost optimization: multi-layer overcommit and resource efficiency

OpenClaw Pool implements multiple layers of cost optimization, from compute scheduling to storage efficiency. The following table summarizes each layer and its impact:

| Layer | Mechanism | Typical savings | Configuration |
|-------|-----------|----------------|---------------|
| CPU overcommit | Schedule more vCPUs than physical cores. AI agents are typically bursty — idle most of the time, active during inference. | 2x density (default) | `cpu_overcommit_ratio: 2.0` |
| Memory overcommit + Balloon | Schedule more memory than physical RAM. Firecracker's balloon device dynamically reclaims idle memory from VMs and returns it when needed. | 1.5x density | `mem_overcommit_ratio: 1.5` + `balloon.enabled: true` |
| Shared rootfs (OverlayFS) | All VMs on a host share a single read-only rootfs image (~1.5 GB). Each VM only stores its delta writes in a 2 GB sparse overlay file. Actual disk usage per VM is typically 50–200 MB. | ~90% rootfs storage savings | Built-in, no configuration needed |
| Sparse data volumes | Data volumes use `cp --sparse=always` for template copying and `fallocate --dig-holes` after decompression. A nominally 8 GB data volume may consume only 1–2 GB of actual disk blocks. | ~75% data storage savings | Built-in |
| Spot instances | EC2 Spot pricing for host instances. | 60–70% compute cost reduction | `asg.use_spot: true` |
| Idle host auto-reclamation | Two-round confirmation: empty hosts are marked idle, then terminated on the next check if still empty. No paying for hosts with zero tenants. | Eliminates idle waste | `scaler.idle_timeout_minutes: 10` |
| Serverless control plane | Lambda + DynamoDB on-demand + EventBridge. Zero cost when no API calls or scheduled events are firing. | Near-zero baseline cost | Built-in |
| Firecracker minimal overhead | Each microVM consumes <5 MB of host memory for the VMM process itself, compared to ~500 MB+ for a full QEMU/KVM virtual machine. | ~100x lower per-VM overhead | Built-in |

### CPU overcommit in detail

AI agent workloads are inherently bursty: an agent spends most of its time waiting for user input or LLM API responses, with brief CPU spikes during tool execution. A `cpu_overcommit_ratio` of 2.0 means an 8-vCPU host can schedule 16 vCPUs across its tenants. The Linux CFS scheduler on the host transparently time-shares the physical cores. This is safe as long as not all tenants burst simultaneously — which is statistically unlikely for AI agent workloads.

### Memory overcommit with balloon in detail

Memory overcommit requires the Firecracker balloon device to be effective. Without it, overcommit is purely a scheduling optimization — the host kernel may OOM-kill Firecracker processes if physical memory is exhausted.

```yaml
balloon:
  enabled: true
  max_inflate_ratio: 0.4       # Reclaim up to 40% of a VM's declared memory
  min_guest_available_mb: 512  # Never reduce guest available memory below 512 MB
  deflate_on_oom: true         # Automatically return memory when guest hits OOM
  free_page_reporting: true    # Guest proactively reports free pages to host
```

The host agent reads `/proc/meminfo` every 5 seconds and adjusts balloon targets:

- Host available memory < 20% → inflate balloons on VMs with spare memory (reclaim to host)
- Host available memory > 40% → deflate balloons (return memory to VMs)
- 20%–40% hysteresis band prevents oscillation
- Each VM retains at least `min_guest_available_mb`, preventing over-reclamation

### Cost example

Consider a single `m8i.2xlarge` host (8 vCPU, 32 GB RAM, ~$0.46/hr on-demand in us-east-1):

| Scenario | Overcommit | Tenants per host | Cost per tenant/hr |
|----------|-----------|-----------------|-------------------|
| No overcommit | 1.0x CPU, 1.0x Mem | 3 (2C/8G each) | ~$0.15 |
| CPU overcommit only | 2.0x CPU, 1.0x Mem | 6 | ~$0.08 |
| Full overcommit | 2.0x CPU, 1.5x Mem | 8–10 | ~$0.05 |
| Full overcommit + Spot | 2.0x CPU, 1.5x Mem, Spot | 8–10 | ~$0.015 |

The web console visualizes overcommit on the host resource bars: a marker indicates the physical capacity boundary, and the fill extends beyond it when overcommitted resources are allocated.

## Web management console

The project includes a web-based management console, hosted on CloudFront and built with Alpine.js as a zero-dependency single-page application.

*Figure 4. The Tenants tab showing host resource utilization and tenant status.*

![Web console — Tenants tab](docs/web_console.png)

The console provides four tabs:

- **Tenants** — Host resource dashboard with CPU/memory utilization bars (including overcommit visualization with physical capacity markers), tenant list with VM and gateway health indicators, and one-click actions (start, stop, pause, resume, restart, reset, delete, open dashboard). Supports filtering by host and by status.
- **Application** — Configuration template CRUD (create, edit, delete OpenClaw JSON configs for different LLM providers), and shared skills listing with descriptions parsed from SKILL.md frontmatter.
- **Backups** — Cross-tenant backup browser with per-tenant grouping (collapsible history), orphan backup filtering, size and timestamp display, and one-click restore into a new tenant.
- **Settings** — API connection configuration (URL and key with visibility toggle), AgentCore status, system version, and GitHub project link.

Optional Cognito authentication protects the console with an OAuth2 implicit flow — when enabled, users must sign in before accessing any tab.

## Optional: AgentCore integration

When you enable AgentCore in `config.yml`, the CDK stack provisions the following [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) resources:

- **Gateway** — An MCP-compatible tool hub with Lambda-backed tools (registered via the `agentcore_tools` Lambda). The gateway URL is automatically injected into each VM's `openclaw.json` as an MCP server.
- **Memory** — Per-tenant persistent memory with semantic and user-preference strategies, namespaced by tenant ID.
- **Code Interpreter** — Secure sandboxed Python execution environment.
- **Browser** — Cloud-based web automation capability.
- **Workload Identity** — IAM identity for agent AWS access.

Each VM automatically connects to these services at launch — no per-tenant configuration required.

## Considerations and limitations

When evaluating this solution for your use case, consider the following:

- **Tenant scale limit.** The ALB listener supports up to 100 rules by default (200 with a quota increase request). Each tenant requires one path-based rule, so the maximum number of tenants per cluster is approximately 200. For larger deployments, you would need to modify the routing architecture (for example, host-header-based routing or a custom proxy layer).
- **Instance family requirement.** Nested virtualization on AWS currently requires Intel-based instances (c8i, m8i, r8i families). AMD and Graviton instances are not supported.
- **No GPU passthrough.** Firecracker does not support GPU device passthrough. This solution is designed for CPU-based AI agent workloads.
- **Single-AZ deployment.** The default deployment uses a single Availability Zone within the default VPC. For production workloads requiring high availability, you would need to extend the architecture with cross-AZ considerations.
- **Sample project status.** This is a reference architecture published under [aws-samples](https://github.com/aws-samples). It is intended for learning and experimentation, not for production use without additional hardening (for example, encryption at rest, VPC endpoints, WAF integration).
- **DynamoDB scan operations.** The tenant and host listing operations use DynamoDB full-table scans, which may become less efficient as the number of tenants grows. For deployments approaching the 200-tenant limit, consider adding Global Secondary Indexes.
- **SSM command concurrency.** All VM operations are executed via SSM Run Command. Concurrent operations on many tenants may be subject to SSM API throttling limits.
- **Spot instance risk.** The optional Spot instance mode (`use_spot: true`) reduces costs by 60–70% but introduces the risk of instance reclamation. All tenants on a reclaimed host are terminated (the termination lifecycle hook cleans up DynamoDB records).

## Best practices

When deploying and operating OpenClaw Pool, we recommend the following:

- Start with conservative overcommit ratios (CPU 1.5x, memory 1.0x) and increase based on observed workload patterns. Monitor the host agent's balloon statistics to understand actual memory utilization.
- Enable the balloon device when using memory overcommit. Without it, memory overcommit is purely a scheduling optimization — the host kernel may OOM-kill Firecracker processes if physical memory is exhausted.
- Use configuration templates to standardize LLM provider settings across tenants, reducing configuration drift. The `default` template is protected from deletion.
- Use the `reset` action (which preserves the data volume but refreshes the rootfs overlay to the latest version) rather than delete-and-recreate when you need to update a tenant's base system.
- For cost optimization, consider Spot instances for non-critical workloads (development, testing, hackathons). The `keep_data_volume: true` setting preserves EBS data volumes even when hosts are terminated, allowing data recovery.
- Set up S3 lifecycle rules appropriate for your backup retention requirements. The default 7-day retention balances storage cost with recovery flexibility.

## Cleanup

To remove all deployed resources:

```bash
./scripts/destroy.sh           # Destroys the CDK stack, retains S3 bucket and DynamoDB tables
./scripts/destroy.sh --purge   # Full cleanup including S3 data, DynamoDB tables, orphaned IAM roles, and EBS volumes
```

## Conclusion

OpenClaw Pool demonstrates how Firecracker microVMs can provide kernel-level tenant isolation for AI agent workloads while maintaining the operational simplicity of a serverless control plane. By combining Firecracker's security properties with AWS managed services for scheduling, scaling, and observability, you can deploy up to 200 isolated AI agent instances with a single CDK deployment and a REST API.

The solution is well-suited for internal AI platforms, training environments, hackathons, and proof-of-concept deployments where strong tenant isolation is required without the operational overhead of Kubernetes or the cost of dedicated EC2 instances per tenant.

The complete source code, deployment instructions, and API reference are available on GitHub:

**[https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker)**

## Resources

- [Firecracker microVM](https://firecracker-microvm.github.io/) — The open-source VMM powering this solution
- [AWS CDK](https://aws.amazon.com/cdk/) — Infrastructure as code framework used for deployment
- [Amazon EC2 nested virtualization](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html) — Required capability for running Firecracker on EC2
- [Building multi-tenant SaaS applications with AWS Lambda's tenant isolation mode](https://aws.amazon.com/blogs/compute/building-multi-tenant-saas-applications-with-aws-lambdas-new-tenant-isolation-mode/) — Related AWS approach to multi-tenant isolation
- [SaaS Lens for the AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/saas-lens.html) — Best practices for SaaS architectures on AWS
- [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) — Optional integration for agent tool gateway, memory, and code interpreter

---

### About the authors

**Neo Sun** is a Solutions Architect at Amazon Web Services, focused on AI/ML and cloud-native architectures for the financial services industry. He works with customers across the Asia Pacific region to design and implement scalable solutions on AWS.

**Aleck Lin** is a Solutions Architect at Amazon Web Services, specializing in serverless and container architectures. He helps customers build modern applications on AWS with a focus on operational excellence and cost optimization.
