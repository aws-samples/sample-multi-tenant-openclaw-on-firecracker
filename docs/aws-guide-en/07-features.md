# Features in detail

This section describes the capabilities that the agent of this solution can provide to end users, as well as the list of orchestration functions exposed externally by the control plane.

## Layering of platform core and business sample

The platform core of this solution is decoupled from any specific business. The control plane (AWS Lambda and Amazon DynamoDB), the data plane two-tier edge route (OpenResty edge ASG + host iptables DNAT, routing to the OpenClaw-native gateway inside each microVM), and the host lifecycle scripts themselves contain no business content; they are solely responsible for provisioning and managing isolated AI agent microVMs at large scale. The agent that runs in each virtual machine is determined by a replaceable sample: the build script selects the sample through the `SAMPLE` environment variable, defaulting to `finance-agent`. Replacing the sample directory replaces the entire set of agent capabilities without changing the platform core.

The `finance-agent` sample published with the repository is a **minimal skeleton**: it demonstrates how a sample should be organized (identity persona `persona/`, capability skills `skills/`, standard guardrails `security/`, deployment configuration `config/`), but ships only one `weather` demonstration skill, leaving the "business capability" layer for the deployer to populate according to their own scenario. In this way, the open-source release preloads no business skills for any specific industry, while the platform core and security layer are fully usable.

**Standard security layer (shipped with every sample, guaranteed by the platform)**

Every sample image ships with two categories of guardrails (under `security/`) that do not vary with business skills: `sentinel-guard` (tool execution layer ACL, default-deny that intercepts reads of credentials / IMDS / sensitive paths) and `acl-guard` (command allowlist). These are layered on top of the read-only golden image (EROFS), auditd, and file integrity monitoring. This layer is the on-the-ground implementation of the L2 tool guardrails and the L5 read-only / monitoring layer; see the Architecture and security chapter for details.

**Demonstration skill and supply chain governance**

`weather` is a demonstration skill released with the sample that shows the skill directory structure (`SKILL.md` + scaffold) and how skills are loaded; it does not represent production capability. Adding new skills goes through supply chain governance: a skill is subject to mandatory offline review before entering the read-only golden image, is not hot-installed on a running virtual machine, and takes effect on the next image rebuild or `refresh-rootfs`.

**How deployers add their own business skills**

Under `samples/<your-brand>/skills/`, place your own skill directory following the structure of `weather`, and declare each skill's purpose, defense objective, and whether it is `always=true` (force-loaded per session) in the sample's `MANIFEST.md`. Baking the image (`SAMPLE=<your-brand> ./build-rootfs.sh`) then cold-injects this set of skills into the read-only disk.

> **Note**
>
> A recommended security principle for deployers: keep the vast majority of skills read-only; do not execute any action involving funds or that is irreversible directly within the agent, but route it to a dedicated skill guarded by a CONFIRM gate, placing the hard veto at the tool execution layer (`sentinel-guard`) so that even prompt injection cannot bypass it. The specific capabilities of a sample are as defined by the description fields in its skill definition files and by `MANIFEST.md`.

## List of externally exposed control plane functions

Beyond the capabilities of the agent inside the virtual machine, the control plane of this solution exposes a set of tenant orchestration functions externally through a REST API. For the complete endpoint contract, see the Developer guide; the following table provides a function map. All endpoints go through Amazon API Gateway, require `x-api-key`, and have RBAC enabled by default.

| Function domain                  | Capability                                                                                                                   | Primary endpoints                                   |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Tenant registration              | Register a tenant (operator level); end users self-provision their own node (viewer level, capped)                           | `POST /tenants`, `POST /tenants/self`               |
| Lifecycle                        | Start/stop, restart, pause and resume, reset, hot-add vCPU, offline disk expansion, cross-host migration                     | `POST /tenants/{id}/{action}`                       |
| Deletion and data protection     | Delete a tenant (mandatory synchronous backup before destroying data, abort on failure; reclaim billing key)                 | `DELETE /tenants/{id}`                              |
| Backup and restore               | Asynchronous backup, list all tenant backups, restore from backup                                                            | `POST /tenants/{id}/backup`, `GET /backups`         |
| Query                            | List tenants (tag filtering, pagination, key redaction), query a single tenant, query authorization                          | `GET /tenants`, `GET /tenants/{id}`                 |
| Batch operations                 | Batch start/stop, delete, backup by ID list or tag filter; large batches turn into async jobs with progress polling          | `POST /batch/tenants`, `GET /batch/jobs/{id}`       |
| Node management by business user | Manage all nodes owned by a single business user: list nodes, aggregate, batch actions                                       | `GET/POST /users/{tenant_user_id}/*`                |
| Authorization                    | Explicit authorization grant/revoke; external authorization writes authoritative state to the business backend (HMAC-signed) | `POST /tenants/{id}/access`, `POST /external/authz` |
| Host management                  | Register, list, decommission hosts; refresh golden image; query image version                                                | `POST /hosts`, `POST /hosts/refresh-rootfs`         |
| Skill distribution               | Add/delete skill groups, read/write/delete skill definitions, control skill scope by tenant or group                         | `GET/POST /groups`, `GET/PUT/DELETE /skills/{name}` |
| System and audit                 | Feature snapshot, AgentCore status and tools, audit log (retained 90 days by default)                                        | `GET /system/info`, `GET /audit-log`                |
| Real-time chat                   | Two-tier edge route to the microVM-native gateway (Bearer `gateway_token` auth)                                              | `POST /ws/{tenant_id}/v1/chat/completions` (SSE)    |

> **Note**
>
> The "Node management by business user" function in the table above depends on a secondary index (`gsi_tenant_user`), which is controlled by a configuration switch and is not created by default. "Self-service registration" and "External authorization" are gated by deployment switches. The switch state and default values for each function are as authoritatively defined in the corresponding sections of the Developer guide and Plan your deployment.
