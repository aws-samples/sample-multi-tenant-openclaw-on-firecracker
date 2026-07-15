# Architecture overview

This section describes the reference architecture of this solution, the high-level data flow between components, and how the design principles of the AWS Well-Architected Framework are applied to this solution.

## Architecture diagram

Deploying this solution with the default parameters builds the following components in your AWS account. The delivery-level architecture comprises three areas: the data plane (end users flow through Amazon CloudFront → Application Load Balancer → OpenResty edge Auto Scaling group with a Redis route table → host iptables PREROUTING DNAT → the OpenClaw-native gateway inside the virtual machine), the control plane (fully managed and serverless, orchestrating the full tenant lifecycle), and the L1-through-L5 defense-in-depth boundaries. The numbered flow below and the component descriptions in Architecture details cover each of these areas.

> **Note**
>
> A rendered architecture diagram is provided in the Chinese edition of this guide; an English-labeled version is to be published. The textual data flow below is authoritative in the meantime.

> **Note**
>
> The AWS resources of this solution are created based on AWS Cloud Development Kit (AWS CDK) constructs. At deployment time, the AWS CDK synthesizes an AWS CloudFormation template and manages the lifecycle of the resources. The rendered diagram may still show the legacy WebSocket hub; that component has been removed. The authoritative data-plane flow is the two-tier edge route described in the text below: an OpenResty edge Auto Scaling group behind the Application Load Balancer resolves each tenant's route from a Redis route table and forwards to the tenant's virtual machine through host iptables DNAT.

The components of the solution deployed by the AWS CloudFormation template follow this high-level flow.

1. A platform engineer or automation program calls the control plane REST API through Amazon API Gateway to submit management operations such as tenant registration, lifecycle, and backup. The request carries a token issued by Amazon Cognito (`Authorization: Bearer`), which the control plane verifies and then authorizes by role-based access control (RBAC).

2. Amazon API Gateway invokes the AWS Lambda control plane functions, which read and write metadata in Amazon DynamoDB (such as tenants, hosts, and skill groups) and issue virtual machine lifecycle commands to the target host through AWS Systems Manager.

3. The hosts (Amazon EC2 bare metal instances) are managed by an Amazon EC2 Auto Scaling group. After a new host starts, it bootstraps itself, downloads the read-only golden image, registers into the host table, and starts a Firecracker microVM for each tenant on top of KVM.

4. Before each microVM starts, the host script cold-injects identity, skills, and tenant-specific configuration (gateway_token, billing virtual key) to disk. The microVM mounts the read-only golden image disk and is a finished product at boot. This step is entirely completed before the Firecracker `InstanceStart`, corresponding to "zero runtime operations."

5. End users access the chat interface through a browser or frontend, which reaches the platform back end and then Amazon CloudFront. CloudFront serves as the single public entry point, originating to the Application Load Balancer (least-outstanding-requests), which forwards to the OpenResty edge Auto Scaling group.

6. The OpenResty edge Auto Scaling group looks up the tenant's route in a Redis route table, backed by three cache tiers: an L1 lrucache (5s), an L2 shared_dict (60s), and L3 Amazon ElastiCache. The resolved route identifies the host and port that front the tenant's virtual machine.

7. Host iptables PREROUTING DNAT (ports 10000-10400) forwards the request to the OpenClaw-native gateway on the virtual machine's `:18789`. The gateway authenticates the request solely with the tenant's `gateway_token` (`Authorization: Bearer`); the gateway's controlUi is disabled. The sole data-plane credential is a per-tenant, AWS KMS envelope-encrypted `gateway_token` (`EncryptionContext={tenant_id}`), whose ciphertext is stored in Amazon DynamoDB with a 15-minute TTL and cold-injected by `launch-vm.sh`; the control-plane API Lambda holds no `kms:Decrypt` permission.

8. Media and image handling (for example, upload and download via Amazon Simple Storage Service presigned URLs) is out of scope of the current sample chat demo, which exchanges text messages only.

9. Key events of the control plane and data plane are recorded as metrics and logs through Amazon CloudWatch; host probes expose Prometheus metrics for the monitoring platform to collect; security-related events can be aggregated into a unified alerting channel through Amazon GuardDuty and Amazon EventBridge (disabled by default, enabled on demand).

## AWS Well-Architected design considerations

This solution uses the best practices of the AWS Well-Architected Framework to help customers design and operate reliable, secure, efficient, cost-effective, and sustainable workloads in the cloud.

This section describes how the design principles and best practices of the Well-Architected Framework benefit this solution.

### Operational Excellence

This section describes how this solution applies the principles and best practices of the Operational Excellence pillar.

- This solution uses AWS CloudFormation templates synthesized from AWS CDK constructs, defining all resources as infrastructure as code.
- This solution follows the discipline of "modify deployment code, then rebuild": changes to identity, skills, and configuration are first written into the build script or launch template, rolled out gradually, and rolled forward through rolling rebuilds, with the image manifest rolled back when problems occur, avoiding hot-modification of running virtual machines.
- This solution pushes metrics to Amazon CloudWatch, and host probes additionally expose Prometheus metrics, providing observability for virtual machine memory, disk, CPU, and health state.
- This solution covers common failures with "symptom–localization–handling" runbooks, and host probes provide two levels of self-recovery capability for virtual machine anomalies.

### Security

This section describes how this solution applies the principles and best practices of the Security pillar.

- This solution uses Amazon Cognito as the single root of trust for control-plane identity; the control plane accepts only tokens verified by Cognito, and downgrades to least privilege (read-only) whenever verification fails. The data plane does not use Cognito; it authenticates each tenant with a per-tenant `gateway_token`.
- This solution enables role-based access control (RBAC) by default, enforcing per-route role checks and resource ownership checks.
- This solution provisions a dedicated-kernel Firecracker microVM for each tenant, and drops cross-tenant east-west traffic at host iptables, as well as traffic from the virtual machine to the Instance Metadata Service (IMDS) and to host management ports.
- The agent tool execution layer of this solution vetoes dangerous actions such as credential exfiltration and sensitive file reads before the tool actually executes, and the content layer intercepts jailbreaks and harmful content through Amazon Bedrock Guardrails.
- The virtual machine of this solution is a zero-credential golden image that holds no long-term AWS credentials; identity and skills are baked into the read-only disk and cannot be tampered with at runtime.
- All data stores (Amazon S3 buckets and Amazon DynamoDB tables) are encrypted by default; the Amazon S3 asset bucket blocks all public access and enforces HTTPS.

### Reliability

This section describes how this solution applies the principles and best practices of the Reliability pillar.

- This solution uses AWS serverless services (AWS Lambda, Amazon API Gateway, Amazon S3, Amazon DynamoDB) wherever possible to improve availability and recover from service failures.
- The hosts of this solution are managed by an Amazon EC2 Auto Scaling group, host probes provide two levels of self-recovery for virtual machine failures, and the control plane health check function restarts agents for unreachable tenants.
- This solution enables point-in-time recovery (PITR) for the control plane metadata table, retaining 35 days of continuous backups by default; tenant data is periodically archived by the backup function to an Amazon S3 bucket with Object Lock enabled.
- This solution enforces a synchronous backup before deleting tenant data, and aborts deletion if the backup fails (fail-closed), avoiding irreversible data loss.

### Performance Efficiency

This section describes how this solution applies the principles and best practices of the Performance Efficiency pillar.

- This solution runs Firecracker with native KVM on Amazon EC2 bare metal instances; the microVM pure startup takes approximately 1.74s (bare metal measured, p50), and from startup to gateway availability takes approximately 6.48s (measured).
- This solution uses bare metal instance types with AWS Graviton processors (Arm64) as the production foundation, running microVMs with native KVM to avoid the performance overhead of nested virtualization.
- This solution uses an Amazon SQS load-leveling queue to flatten large-scale lifecycle operations into a controlled concurrency rate, avoiding the AWS Systems Manager per-instance concurrency quota becoming a bottleneck.
- This solution supports memory overcommit reclamation (balloon, disabled by default, enabled on demand); a single bare metal instance measured to carry 187 fully healthy nodes (constrained by a disk bottleneck, measured).

### Cost Optimization

This section describes how this solution applies the principles and best practices of the Cost Optimization pillar.

- The control plane of this solution uses a serverless architecture, so customers pay only for actual usage; Amazon DynamoDB is billed on demand with no provisioned cost.
- The hosts of this solution can mix Savings Plans and Spot Instances to reduce compute cost; measured at approximately $8.36/tenant/month under a mix of 80% Savings Plans plus 20% Spot (cost estimate).
- This solution splits large model invocation spend by tenant through per-tenant billing virtual keys, facilitating cost attribution and budget control.
- Idle hosts of this solution are reclaimed in a controlled manner by the scaling function after two rounds of confirmation, avoiding idle-running cost.

### Sustainability

This section describes how this solution applies the principles and best practices of the Sustainability pillar.

- This solution uses dedicated-kernel microVMs to carry hundreds of tenants on a single bare metal instance, reducing the per-tenant resource footprint through high-density reuse.
- This solution uses managed services that scale on demand and hosts that are reclaimed in a controlled manner, so resource consumption tracks actual load and idle resources are reduced.
- This solution uses AWS Graviton processors, which have better energy efficiency than comparable compute under equivalent workloads.
