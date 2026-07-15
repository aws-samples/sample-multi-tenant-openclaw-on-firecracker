# Solution overview

The OpenClaw on Firecracker multi-tenant AI agent platform helps you deliver, on AWS, a dedicated-kernel lightweight virtual machine to each of thousands of tenants, running an OpenClaw AI agent equipped with identity, skills, and security guardrails inside each one. This solution uses Firecracker microVMs to raise the boundary at which "privilege escalation stops at a single tenant" from the container level to the virtual machine level, and layers on five levels of defense in depth spanning from the content layer to the credential layer. It targets scenarios that must host AI agents at large scale while imposing strong requirements on both tenant isolation and performance.

This solution runs Firecracker microVMs on native KVM on Amazon EC2 bare metal instances, and manages tenant registration, lifecycle, backup, and deregistration through a serverless control plane (AWS Lambda, Amazon DynamoDB, Amazon API Gateway). It provides the following capabilities:

- Provisions a dedicated-kernel Firecracker microVM for each tenant, isolating memory, CPU, and kernel between tenants at the virtual machine level.
- Cold-injects identity, skills, and configuration into a read-only golden image before the virtual machine starts, so that each virtual machine is a finished product at boot; after boot, the control plane injects no business data into it.
- Routes real-time chat requests from browsers or frontends to the OpenClaw-native gateway inside the corresponding tenant's virtual machine through a two-tier edge route: Amazon CloudFront and an Application Load Balancer (least-outstanding-requests) front an OpenResty edge Auto Scaling group that looks up the tenant's route in a Redis route table, then host iptables PREROUTING DNAT (ports 10000-10400) forwards to the microVM's `:18789` gateway.
- Uses Amazon Cognito as the single root of trust for control-plane identity, uniformly verifying all access to the control plane and downgrading to least privilege whenever verification fails; the data plane authenticates instead with a per-tenant `gateway_token`.
- Layers on five levels of defense in depth: the content layer, the tool execution layer, the identity layer, the network layer, and the credential and read-only monitoring layer. Each level is implemented in deployment code or in a device, and does not rely on model self-discipline.

This implementation guide describes the reference architecture and components, deployment planning considerations, and configuration steps for deploying the OpenClaw on Firecracker multi-tenant AI agent platform solution to the Amazon Web Services (AWS) Cloud. It is intended for platform engineers, security reviewers, operations engineers, and application developers who have architectural experience with the AWS Cloud.

> **Note**
>
> The platform core of this solution is decoupled from any specific business. The control plane, data plane, and host lifecycle scripts contain no business content; they are solely responsible for provisioning and managing isolated AI agent microVMs at large scale. The agent that runs in each virtual machine is determined by a replaceable "sample": the build script selects the sample through the `SAMPLE` environment variable, defaulting to `finance-agent`. The `finance-agent` sample published with the repository is a minimal skeleton sample (an identity persona, the standard security guardrails, and one `weather` demonstration skill). Deployers populate the specific business capabilities within the sample directory according to their own scenarios, without changing the platform core or the security layer.

Use the following navigation table to quickly find answers to relevant questions.

| If you want to…                                                                  | Read…                                                |
| -------------------------------------------------------------------------------- | ---------------------------------------------------- |
| Understand the architecture and the five-level security model                    | Architecture overview, Architecture details          |
| Understand the cost estimate for running this solution                           | Plan your deployment — Cost                          |
| Understand the security considerations for this solution                         | Plan your deployment — Security                      |
| Understand the supported AWS Regions and service quotas                          | Plan your deployment — Supported AWS Regions, Quotas |
| Deploy the infrastructure for this solution                                      | Deploy the solution                                  |
| Programmatically call the control plane API or integrate the real-time chat path | Developer guide                                      |
| Troubleshoot common issues during deployment or operation                        | Troubleshooting                                      |

## Features

This solution provides the following features.

**Virtual machine-level tenant isolation**

This solution provisions a dedicated-kernel Firecracker microVM for each tenant, isolating tenant memory and CPU at the hardware-assisted virtualization layer, so that privilege escalation or escape within a single tenant stops at that tenant's own virtual machine and does not affect other tenants or the host.

**Cold-injection delivery that is a finished product at boot**

This solution cold-injects identity, skills, guardrails, and configuration into the read-only golden image disk before the virtual machine starts, so the virtual machine comes up with a complete identity and guardrails. After boot, the control plane injects no business data into the virtual machine; modifying identity or skills requires rebuilding the image rather than hot-modifying a running virtual machine.

**Five-level defense in depth**

This solution layers on five levels of protection: the content layer (Amazon Bedrock Guardrails), the tool execution layer (in-process agent guardrails), the identity layer (Amazon Cognito verification and role-based access control), the network layer (host iptables and DNS Firewall), and the credential and read-only monitoring layer. Each level is implemented in deployment code or in a device.

**Unified root of trust for identity**

This solution uses Amazon Cognito as the single root of trust for control-plane identity; the control plane accepts only tokens issued and verified by Cognito. External identity providers federate into the same Cognito user pool through OpenID Connect (OIDC). The data plane does not use Cognito; it authenticates each tenant with a per-tenant `gateway_token`.

**Serverless control plane**

The registration, lifecycle, health check, backup, and scaling logic of this solution is carried by AWS Lambda, Amazon DynamoDB, and Amazon API Gateway, billed by usage and requiring no dedicated operational servers.

**Real-time chat routing (two-tier edge route)**

This solution carries real-time chat between browsers and agents through a two-tier edge route. Amazon CloudFront and an Application Load Balancer (least-outstanding-requests) front an OpenResty edge Auto Scaling group, which looks up the tenant's route in a Redis route table (L1 lrucache 5s, L2 shared_dict 60s, L3 Amazon ElastiCache). Host iptables PREROUTING DNAT (ports 10000-10400) then forwards to the OpenClaw-native gateway on the virtual machine's `:18789`, which authenticates the request with the tenant's `gateway_token` (`Authorization: Bearer`).

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

In industries such as finance and exchanges that have strong requirements on tenant isolation and data residue, use virtual machine-level isolation to meet the compliance expectation that "privilege escalation within a single tenant does not spill over."

**Self-service provisioning of AI assistants for end users**

Allow logged-in end users to provision AI agent nodes for themselves, gated by the platform according to per-user quotas and authorization policies.

**Delivery of an agent platform with security guardrails**

Deliver, to customers who require built-in jailbreak interception, malicious skill interception, and credential exfiltration interception, an agent platform that ships with guardrails out of the box, carrying specific business capabilities through a replaceable sample.

## Concepts and definitions

This section describes important concepts and terminology specific to this solution. For the complete terminology, see the glossary of this guide.

**Tenant**

The basic unit of isolation and billing in this solution. Each tenant corresponds to one Firecracker microVM and is identified by a unique `tenant_id`.

**microVM**

A lightweight virtual machine started by Firecracker on top of KVM, featuring a dedicated kernel, millisecond-scale startup, and strong isolation. This solution provisions one microVM for each tenant.

**Cold injection**

The process of writing identity, skills, credentials, and configuration to disk before the virtual machine starts, as opposed to "hot injection / hot modification" after boot. All identity and configuration changes in this solution go through cold injection; the virtual machine is not hot-modified after boot.

**Read-only golden image**

A read-only image disk that has identity files and authoritative skills baked in. It guarantees that the agent cannot tamper with its identity inside the virtual machine, through three layered mechanisms: the Firecracker write barrier, guest read-only mount, and read-only bind.

**Control plane**

The management plane composed of AWS Lambda, Amazon DynamoDB, and Amazon API Gateway, responsible for tenant registration, lifecycle, backup, and deregistration; it injects no business data into the virtual machine after boot.

**Data plane**

The real-time chat path composed of a two-tier edge route (Amazon CloudFront → Application Load Balancer → OpenResty edge Auto Scaling group with a Redis route table → host iptables PREROUTING DNAT), which routes browser requests to the OpenClaw-native gateway inside the corresponding tenant's virtual machine, authenticated by a per-tenant `gateway_token`.

**Five levels of defense in depth**

The security model of this solution, which from outside inward consists of the content layer, the tool execution layer, the identity layer, the network layer, and the credential and read-only monitoring layer. Each level takes effect independently and is mutually orthogonal.

**Sample**

A replaceable directory that carries specific business capabilities. The platform core is layered separately from the sample; replacing the sample replaces the entire set of agent capabilities without changing the control plane.

For a general reference to AWS terminology, see the AWS glossary.
