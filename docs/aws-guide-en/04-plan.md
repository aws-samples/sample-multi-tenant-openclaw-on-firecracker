# Plan your deployment

This section describes the cost, security, supported AWS Regions, and service quotas you should consider before deploying this solution.

## Cost

You are responsible for the cost of the AWS services used while running this solution. As of the writing of this guide, based on 80% Savings Plans plus 20% Spot Instances, the compute cost of this solution is estimated at about $8.36/tenant/month (cost estimate, not measured). Actual cost depends on the host instance type, tenant density, large language model call volume, and whether the optional monitoring components are enabled.

> **Important**
>
> This estimate is a cost estimate and does not include large language model inference, third-party exchange APIs, data transfer, and similar charges, nor does it include the optional monitoring platforms (Prometheus, Grafana, and Wazuh on self-managed Amazon EC2) and optional managed security services (Amazon GuardDuty, Amazon OpenSearch Service). Use the AWS Pricing Calculator with your target Region and actual usage as the source of truth. We recommend that you create a budget for this solution and track actual spending through AWS Cost Explorer.

The primary cost components of this solution are as follows.

| AWS service              | Billing dimension                     | Description                                                                                          |
| ------------------------ | ------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Amazon EC2 (metal hosts) | Instance hours (Savings Plans / Spot) | Primary cost item. Carries all tenant microVMs; production uses Graviton Arm64 metal                 |
| Amazon EBS               | Provisioned capacity (gp3, encrypted) | Each host mounts data volumes carrying the sparse disks and image overlays of all microVMs           |
| AWS Lambda               | Number of requests + runtime duration | Control plane API, health checks, scaling, and backup functions                                      |
| Amazon DynamoDB          | On-demand read/write + storage + PITR | Tenant, host, skill group, and audit metadata                                                        |
| Amazon S3                | Storage + requests                    | Golden image, tenant backups, and log archives                                                       |
| Amazon API Gateway       | Number of requests                    | Control plane REST API                                                                               |
| Amazon CloudFront        | Data transfer + requests              | Sole public entry point, reverse-proxying the chat UI and hub                                        |
| Elastic Load Balancing   | Load balancer hours + LCU             | Application Load Balancer, receiving CloudFront origin traffic                                       |
| Amazon CloudWatch        | Log ingestion + storage + metrics     | Control plane logs and metrics; the log group default retention is described in Architecture details |

> **Note**
>
> The per-tenant billing virtual key (LiteLLM virtual key) of this solution splits large language model call spend by "the Amazon Cognito identity corresponding to the tenant", making it easy to attribute inference cost to a specific tenant. This capability is off by default and takes effect only after the billing section is enabled in the configuration.

## Security

When you build systems on AWS, security responsibilities are shared between you and AWS. This shared responsibility model reduces your operational burden. For more information, see the AWS shared responsibility model.

This section describes the security design of this solution in the areas of identity, network, encryption, and public exposure. For the complete five-layer security model, see Architecture details.

### IAM roles

AWS Identity and Access Management (IAM) roles allow customers to assign fine-grained access policies and permissions to AWS services and to resources on those services. This solution creates least-privilege IAM roles separately for the control plane Lambda, the host instances, and the monitoring components:

- The control plane Lambda role is granted `secretsmanager:GetSecretValue` permission on the large language model master key only when the billing section is configured, and the resource scope is narrowed by secret name.
- The host instance role has the minimum permissions needed to read the golden image, query CloudFormation outputs, and issue lifecycle commands; there are zero credentials inside the VM, holding no long-lived AWS credentials.
- The monitoring component instance role has only the permissions to write to its own log groups and notification topics.

### Identity and access control

This solution uses Amazon Cognito as the single root of trust for identity. Both the control plane and the data plane accept only tokens issued and verified through Cognito, and verification failure always degrades to the least privilege (read-only viewer). This solution enables role-based access control (RBAC) by default, enforcing per-route role checks and resource-owner checks.

### Network and public exposure

This solution uses Amazon CloudFront as the sole public entry point (CloudFront origins to an Application Load Balancer) and does not expose the backend directly to the internet. Security group inbound rules are not open to `0.0.0.0/0`: inbound for host probes, the monitoring platform, databases, and management ports is allowed only from the VPC CIDR, the CloudFront managed prefix list, or the bastion host security group.

This solution enforces three classes of drop rules on the host iptables for each microVM: drop cross-tenant east-west traffic, drop VM access to the instance metadata service (IMDS), and drop VM access to host management ports. Firecracker itself does not filter traffic; cross-tenant network isolation is entirely borne by the host iptables.

> **Important**
>
> This solution explicitly removes OpenClaw's native OpenAI-compatible `chatCompletions` HTTP endpoint. This endpoint is a bypass that goes straight to the large language model and circumvents the control UI's device authentication; enabling it globally adds an external attack surface to every tenant. If an external chat endpoint is genuinely required, the correct approach is a per-tenant switch, enabled at rebuild only for the tenants that truly need it, rather than enabling it globally. The current code has no path that injects this endpoint, and the per-tenant switch is planned (not implemented).

### Encryption

All data stores in this solution are encrypted by default: the Amazon S3 asset bucket blocks all public access (all 4 public-access-block switches on) and enforces HTTPS (rejecting non-TLS requests), and Amazon DynamoDB tables are encrypted at rest.

> **Note**
>
> The audit log table currently uses an AWS owned key for encryption; the customer managed key (CMK) is planned only when a brand-new account is first deployed, because switching encryption online on an existing retained (RETAIN) table would force a replace and lose audit data. When planning for disaster recovery, treat it as "the audit table is currently not CMK".

### VPC

This solution is deployed inside an Amazon Virtual Private Cloud (Amazon VPC). VPC Flow Logs (`traffic_type=ALL`) are enabled by default, delivered to an Amazon CloudWatch log group with limited retention, used to detect cross-tenant east-west anomalies and verify that the iptables isolation is truly in effect. Amazon Route 53 Resolver DNS Firewall can be optionally enabled to intercept, at the DNS resolution layer, VMs resolving known malicious domains (off by default).

## Supported AWS Regions

The core AWS services used by this solution (AWS Lambda, Amazon API Gateway, Amazon S3, Amazon DynamoDB, Amazon Cognito, Amazon CloudFront, Amazon EC2) are available in most AWS Regions. The production foundation of this solution requires that the target Region provide AWS Graviton (Arm64) metal instance types and provide Amazon Bedrock and its Guardrails capability.

> **Note**
>
> Before deploying, confirm that the target Region supports both the required Graviton metal instance types and Amazon Bedrock Guardrails. The Region availability of Amazon Bedrock and certain instance types changes over time; use the AWS Regional Services List as the source of truth.

## Quotas

Certain AWS services used by this solution have quota limits. Confirm that the following quotas satisfy your target scale before deploying and scaling.

| Service / resource        | Quota of concern                                              | Description                                                                                                                |
| ------------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Amazon EC2                | Instance quota for the target metal instance type, Spot quota | Determines the number of hosts that can be launched and total tenant capacity                                              |
| AWS Systems Manager       | Concurrent commands per instance                              | The real bottleneck for large-scale tenant creation; enable an Amazon SQS smoothing queue to avoid it                      |
| Application Load Balancer | Number of rules per load balancer                             | Each tenant occupies one forwarding rule; the relationship between the rule-count hard wall and capacity is to be verified |
| AWS Lambda                | Concurrent executions                                         | Concurrency of the control plane and lifecycle functions                                                                   |
| Amazon DynamoDB           | On-demand throughput                                          | On-demand billing scales automatically with traffic                                                                        |

> **Important**
>
> The real bottleneck for large-scale tenant creation is the AWS Systems Manager per-instance concurrency, not compute capacity. Creating a full host in one shot instantly exceeds the Systems Manager per-instance concurrency quota, causing some requests to remain permanently in the `creating` state. Before scaling, you must enable the Amazon SQS smoothing queue (`scaler.lifecycle_queue_enabled=true`) to flatten lifecycle operations into a controlled concurrency rate (about 5–10 concurrent per metal host is recommended), and you should not call the registration API directly with high client-side concurrency.

> **Note**
>
> The single-tenant resource quota check (`QUOTAS_ENABLED`) of this solution is off by default. Capacity is adjusted per the configuration file; the measured fully healthy capacity of a single host is 187 nodes (constrained by the disk bottleneck), and a higher-density steady-state capacity requires additional load testing to conclude; 380 tenants/host is a capacity estimate.
