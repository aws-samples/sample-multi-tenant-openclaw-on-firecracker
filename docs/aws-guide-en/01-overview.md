# Solution overview

The OpenClaw on Firecracker multi-tenant AI agent platform helps you deliver, on AWS, a dedicated-kernel lightweight virtual machine to each of thousands of tenants, running an OpenClaw AI agent equipped with identity, skills, and security guardrails inside each one. This solution uses Firecracker microVMs to raise the boundary at which "privilege escalation stops at a single tenant" from the container level to the virtual machine level, and layers on five levels of defense in depth spanning from the content layer to the credential layer. It targets scenarios that must host AI agents at large scale while imposing strong requirements on both tenant isolation and performance.

## Executive summary: what this solution solves, and what it costs

**The core problem it solves**: with shared-kernel container multi-tenancy, a single escape exposes every tenant on the host; this solution gives each tenant a Firecracker microVM with its own Linux kernel (the same lightweight virtualization technology behind AWS Lambda and Fargate), physically confining any privilege escalation or escape within that tenant's own virtual machine. Isolation does not rely on model self-discipline; it is implemented in deployment code and devices: cross-tenant east-west traffic is dropped at host iptables (100% packet loss in the hardened target state), identity uses OpenClaw's native Ed25519 asymmetric device authentication, and credentials use KMS envelope encryption with zero long-term AWS credentials inside the guest.

**The key trade-offs**: lowering the security boundary to the VM level costs a fixed overhead of about 2 GB of memory per tenant (a single `r8g.metal-24xl` carries about 380 tenants at steady state, a capacity estimate), plus the operational discipline that "configuration changes require rebuilding the image; running VMs are never hot-modified." For scenarios that pursue maximum density and can accept shared-kernel risk, this solution is not the lowest-cost option; for multi-tenant AI agent hosting with compliance requirements on isolation strength, it narrows the risk surface to a single tenant.

**Its current maturity**: the control plane (registration, lifecycle, backup, health checks, AZ failover) and the image cold-injection pipeline have been verified end to end by measurement; the data plane has migrated from the earlier hub-WS model to "two-tier routing directly to the microVM gateway," a chain that has been verified on real machines in a demonstration environment but ships as an opt-in capability (`redis.enabled` / `edge.enabled` default to off). All externally quoted performance and capacity figures have first-hand measured sources; unmeasured items are marked "to be verified" in the corresponding sections.

> ⚠️ **Note (three hard prerequisites to check before deploying)**
>
> - **The host instance type must expose KVM**: the production baseline is the AWS Graviton bare metal instance `r8g.metal-24xl` (arm64), which starts Firecracker on the host's native KVM — **no nested virtualization**. Non-bare-metal Graviton instance types have no `/dev/kvm`; host bootstrap fails outright and the instance is replaced repeatedly by Auto Scaling. See "Deploy the solution — Troubleshooting."
> - **The deployment machine needs Docker**: AWS CDK uses containers to bundle arm64 native extensions for AWS Lambda. The deployment machine (macOS, Windows, or Linux) must have a working Docker daemon, otherwise `cdk deploy` stalls at asset bundling.
> - **`config.yml` is not checked in**: the repository ships only `config.yml.example`. After a fresh clone you must first run `cp config.yml.example config.yml`, otherwise `setup.sh` fails immediately.

---

## Summary for two audiences

### For CTOs and business decision makers

This solution lets you host thousands of mutually isolated AI agents on AWS in SaaS form, one dedicated-kernel virtual machine per tenant, narrowing the risk surface of data leakage and lateral movement from "the entire host" to "a single tenant," and meeting the compliance expectations that industries such as finance and exchanges place on tenant isolation and data residue. The control plane is fully serverless (Lambda + DynamoDB + API Gateway), billed by usage with no dedicated operational servers; hosts scale elastically with Auto Scaling, and with memory oversubscription and Spot, a single bare metal instance carries about 380 tenants at steady state at a cost of about 8.36 USD per tenant per month (estimated). The platform core is decoupled from the business: switching business scenarios only requires replacing one "sample" directory, with the isolation and security layers unchanged, so branded agent pools can be delivered quickly to different customers.

### For infrastructure engineers

- **Isolation foundation**: Firecracker microVMs on the host's native KVM (`r8g.metal-24xl`, Graviton4, no nested virtualization). Each tenant gets a dedicated Linux kernel + an OverlayFS rootfs (read-only base + per-VM sparse writable layer) + KMS-encrypted EBS.
- **Network isolation**: each VM owns a dedicated `/30` point-to-point TAP link, all within the `SUBNET_PREFIX/16` supernet; host iptables applies an east-west `FORWARD DROP` to the entire `/16` (100% cross-tenant packet loss, measured); each VM additionally inserts three DROP rules toward IMDS, the tenant supernet, and the host management ports.
- **Security boundary (L1–L5)**: L1 content layer, Bedrock Guardrails in both directions; L2 tool layer, `before_tool_call` ACL default-deny; L3 identity layer, OpenClaw native Ed25519 asymmetric device authentication + control-plane RBAC + `owner_id` gating; L4 network layer, IMDS blocking + DNS Firewall + NAT egress allowlist; L5 credential layer, zero long-term credentials in the guest + read-only golden image disk + in-guest auditd/Wazuh runtime monitoring.
- **Data plane**: Browser ─wss `/gw/ws`─▶ platform backend (verifies the platform JWT) ─ws─▶ ALB ─▶ OpenResty edge ASG (ElastiCache Redis routing table lookup + strips the `/ws/{tid}` prefix) ─▶ microVM gateway:18789 (Ed25519 device handshake). Opt-in (`edge`/`redis` gates).
- **Scalability figures**: microVM boot 1.74s (p50, measured); a single host carries 187 fully healthy nodes (disk bottleneck, measured) / 380 tenants at steady state (estimated); per-VM RSS ~609 MB (measured); large-scale tenant creation goes through SQS buffering to get around the SSM per-instance concurrency ceiling.

---

This solution runs Firecracker microVMs on native KVM on Amazon EC2 bare metal instances, and manages tenant registration, lifecycle, backup, and deregistration through a serverless control plane (AWS Lambda, Amazon DynamoDB, Amazon API Gateway). It provides the following capabilities:

- Provisions a dedicated-kernel Firecracker microVM for each tenant, isolating memory, CPU, and kernel between tenants at the virtual machine level.
- Cold-injects identity, skills, and configuration into a read-only golden image before the virtual machine starts, so that each virtual machine is a finished product at boot; after boot, the control plane injects no business data into it.
- Routes real-time chat requests from browsers to the OpenClaw agent inside the corresponding tenant's virtual machine through two-tier routing (platform backend → OpenResty edge → microVM gateway); the microVM exposes its port only inside the host and opens no public inbound port.
- Uses OpenClaw's native asymmetric device authentication (Ed25519) as the data-plane identity root, and gates the control plane with `x-api-key` plus optional Amazon Cognito RBAC; any verification failure downgrades to least privilege.
- Layers on five levels of defense in depth: the content layer, the tool execution layer, the identity layer, the network layer, and the credential and read-only monitoring layer. Each level is implemented in deployment code or in a device, and does not rely on model self-discipline.

This implementation guide describes the reference architecture and components, deployment planning considerations, and configuration steps for deploying the OpenClaw on Firecracker multi-tenant AI agent platform solution to the Amazon Web Services (AWS) Cloud. It is intended for platform engineers, security reviewers, operations engineers, and application developers who have architectural experience with the AWS Cloud.

> **Note**
>
> The platform core of this solution is decoupled from any specific business. The control plane, data plane, and host lifecycle scripts contain no business content; they are solely responsible for provisioning and managing isolated AI agent microVMs at large scale. The agent that runs in each virtual machine is determined by a replaceable "sample": the build script selects the sample through the `SAMPLE` environment variable, defaulting to `finance-agent`. The `finance-agent` sample published with the repository is a minimal skeleton sample (an identity persona, the standard security guardrails, and two demonstration skills: `weather` and `skill-vetter`). Deployers populate the specific business capabilities within the sample directory according to their own scenarios, without changing the platform core or the security layer.

Use the following navigation table to quickly find answers to relevant questions.

| If you want to…                                                                  | Read…                                                                    |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| Understand the architecture and the five-level security model                    | Architecture overview, Architecture details, Data plane two-tier routing |
| Understand the cost estimate for running this solution                           | Plan your deployment — Cost                                              |
| Understand the security considerations for this solution                         | Plan your deployment — Security                                          |
| Understand the supported AWS Regions and service quotas                          | Plan your deployment — Supported AWS Regions, Quotas                     |
| Deploy the infrastructure for this solution                                      | Deploy the solution                                                      |
| Programmatically call the control plane API or integrate the real-time chat path | Developer guide                                                          |
| Troubleshoot common issues during deployment or operation                        | Troubleshooting                                                          |

## Features

This solution provides the following features.

**Virtual machine-level tenant isolation**

This solution provisions a dedicated-kernel Firecracker microVM for each tenant, isolating tenant memory and CPU at the hardware-assisted virtualization layer, so that privilege escalation or escape within a single tenant stops at that tenant's own virtual machine and does not affect other tenants or the host.

**Cold-injection delivery that is a finished product at boot**

This solution cold-injects identity, skills, guardrails, and configuration into the read-only golden image disk before the virtual machine starts, so the virtual machine comes up with a complete identity and guardrails. After boot, the control plane injects no business data into the virtual machine; modifying identity or skills requires rebuilding the image rather than hot-modifying a running virtual machine.

**Five-level defense in depth**

This solution layers on five levels of protection: the content layer (Amazon Bedrock Guardrails), the tool execution layer (in-process agent ACL guardrails), the identity layer (OpenClaw native Ed25519 device authentication and control-plane role-based access control), the network layer (host iptables and DNS Firewall), and the credential and read-only monitoring layer. Each level is implemented in deployment code or in a device.

**Two-tier routing data plane**

This solution routes real-time chat from browsers directly to the OpenClaw gateway inside each tenant's virtual machine through two-tier routing: platform backend WebSocket gateway + OpenResty edge + host DNAT. The virtual machine exposes the gateway port only on the host-internal TAP interface and opens no inbound port on the public network.

**Serverless control plane**

The registration, lifecycle, health check, backup, and scaling logic of this solution is carried by AWS Lambda, Amazon DynamoDB, and Amazon API Gateway, billed by usage and requiring no dedicated operational servers.

**High availability by default**

This solution deploys two hosts across multiple Availability Zones by default (`multi_az.enabled` and `az_failover.enabled` both default to `true`). When every host in an Availability Zone stays unhealthy, the affected tenants are automatically restored from their most recent backup into a healthy Availability Zone; before restoring, the platform verifies that a backup exists (tenants without a backup are refused migration — data safety takes precedence over availability).

**Replaceable business sample**

The platform core of this solution is layered separately from the business sample. Switching business scenarios requires only replacing the sample directory; the control plane, isolation mechanism, security layer, and operational tooling remain unchanged.

## Benefits

This solution provides the following benefits.

**Stronger tenant isolation boundary**

Compared with shared-kernel container multi-tenancy, dedicated-kernel microVMs move the isolation boundary down to the virtual machine level, reducing the attack surface for lateral movement and escape.

**Defense that does not rely on model self-discipline**

Each level of protection in this solution is implemented in deployment code or in a device: the tool execution layer vetoes dangerous actions before the tool actually executes, and the network layer drops cross-tenant traffic at host iptables. Even if prompt injection bypasses the model, sensitive operations are still intercepted.

**Auditable and rollback-capable configuration changes**

This solution follows the discipline of "modify deployment code, then rebuild." Changes to identity, skills, and configuration are inherited when the image is rebuilt, can be rolled out gradually, and can be rolled back; no hot-injection channel from host to virtual machine is opened after boot.

**Pay-as-you-go elastic capacity**

The hosts of this solution are elastically scaled by Amazon EC2 Auto Scaling, and the control plane uses serverless services, so capacity scales with load.

**Performance and security conclusions verified by measurement**

The key performance, capacity, and security conclusions of this solution all have first-hand measured sources (see Architecture details and Plan your deployment). Items not confirmed by measurement are honestly marked "to be verified" in the corresponding sections.

## Use cases

**Large-scale AI agent hosting**

Host an isolated AI agent instance for each of thousands of end users or tenants, and perform unified lifecycle and resource management over them.

**Multi-tenant scenarios with compliance requirements on isolation strength**

In industries such as finance that have strong requirements on tenant isolation and data residue, use virtual machine-level isolation to meet the compliance expectation that "privilege escalation within a single tenant does not spill over."

**Self-service provisioning of AI assistants for end users**

Allow logged-in end users to provision AI agent nodes for themselves, gated by the platform according to per-user quotas and authorization policies.

**Delivery of an agent platform with security guardrails**

Deliver, to customers who require built-in jailbreak interception, malicious skill interception, and credential exfiltration interception, an agent platform that ships with guardrails out of the box, carrying specific business capabilities through a replaceable sample.

## Concepts and definitions

This section describes important concepts and terminology specific to this solution. For the complete terminology, see the glossary of this guide.

**Tenant**

The basic unit of isolation and billing in this solution. Each tenant corresponds to one Firecracker microVM and is identified by a unique `tenant_id` (the DynamoDB primary key `id`).

**microVM**

A lightweight virtual machine started by Firecracker on top of KVM, featuring a dedicated kernel, millisecond-scale startup, and strong isolation. This solution provisions one microVM for each tenant.

**Cold injection**

The process of writing identity, skills, credentials, and configuration to disk before the virtual machine starts, as opposed to "hot injection / hot modification" after boot. All identity and configuration changes in this solution go through cold injection; the virtual machine is not hot-modified after boot.

**Read-only golden image**

A read-only image disk that has identity files and authoritative skills baked in. It guarantees that the agent cannot tamper with its identity inside the virtual machine, through three layered mechanisms: the Firecracker write barrier, guest read-only mount, and read-only bind.

**Control plane**

The management plane composed of AWS Lambda, Amazon DynamoDB, and Amazon API Gateway, responsible for tenant registration, lifecycle, backup, and deregistration; it injects no business data into the virtual machine after boot.

**Data plane**

The two-tier routing chain that carries real-time chat: platform backend WebSocket gateway → OpenResty edge (Redis routing table lookup) → host DNAT → the OpenClaw gateway inside the virtual machine.

**Five levels of defense in depth**

The security model of this solution, which from outside inward consists of the content layer, the tool execution layer, the identity layer, the network layer, and the credential and read-only monitoring layer. Each level takes effect independently and is mutually orthogonal.

**Sample**

A replaceable directory that carries specific business capabilities. The platform core is layered separately from the sample; replacing the sample replaces the entire set of agent capabilities without changing the control plane.

For a general reference to AWS terminology, see the AWS glossary.
