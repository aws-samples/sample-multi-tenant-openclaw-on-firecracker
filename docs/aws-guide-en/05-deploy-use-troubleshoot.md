# Deploy the Solution

Before you deploy this solution, review the architecture and the planning considerations in this guide. This solution provisions one dedicated-kernel Firecracker microVM per tenant on AWS, running an OpenClaw AI agent equipped with an identity, skills, and guardrails. The control plane (AWS Lambda, Amazon DynamoDB, and Amazon API Gateway) handles registration, lifecycle, backup, and deregistration, and injects no business data into tenant microVMs after they are running. This section describes the deployment process and the required steps.

## Deployment process overview

This section describes the deployment entry points, configuration files, and recommended order of execution for the solution.

The deployment code for this solution comprises four parts: an infrastructure definition built on the AWS Cloud Development Kit (AWS CDK) (`deploy/app.py` and `deploy/stack.py`), the host and microVM lifecycle scripts (`deploy/userdata/*.sh`), the golden image build script (`build-rootfs.sh`), and the data-plane WebSocket hub claw-hub (`deploy/hub/`).

The AWS CDK entry point `deploy/app.py` reads `region` from context (defaults to `us-east-1`), instantiates `OpenClawOrchestratorStack`, and takes the account from the `CDK_DEFAULT_ACCOUNT` environment variable. The central configuration file `config.yml` defines, in one place, the host specification, microVM defaults, the Auto Scaling Group (ASG), Balloon memory reclamation, health checks, Multi-AZ, and Amazon Cognito authentication. The deployment commands are wrapped in `setup.sh`.

The recommended deployment order is:

1. Review `config.yml`, paying particular attention to the region (`region`), instance type (`instance_type`), and ASG capacity.
2. Run `setup.sh` to complete the AWS CDK deployment and upload the host scripts and the `deploy/hub/` assets to the Amazon Simple Storage Service (Amazon S3) assets bucket.
3. The ASG launches the first host, which runs `init-host.sh` to bootstrap itself and register with the hosts table.
4. Register the first tenant through the control plane API and verify end-to-end connectivity.

## Step 1: Deploy the infrastructure

This section describes how to deploy the control plane and network infrastructure of the solution.

The entire infrastructure is defined in the `OpenClawOrchestratorStack` class in `deploy/stack.py`. The core of the deployment command is a single `cdk deploy` line with a region parameter:

```bash
cdk deploy -c region="<region>" --profile "<profile>" --require-approval never
```

After deployment completes, retrieve the runtime coordinates such as the assets bucket, backup bucket, and backup key from the Outputs of the AWS CloudFormation stack. At runtime, the host queries these Outputs through `aws cloudformation describe-stacks` (retrying up to 20 times, 15 seconds apart) rather than receiving them through user-data, to avoid exceeding the 16 KB limit on EC2 user-data.

> **Note**
>
> In the AWS CloudFormation console, confirm that the stack status is `CREATE_COMPLETE`, and that the Outputs contain `AssetsBucket`, `BackupBucket`, and `BackupCmkKeyId`. The assets bucket should have all four public-access block switches enabled and HTTPS enforced.

## Step 2: Build the golden image

This section describes how to build the read-only golden image used by the solution.

`build-rootfs.sh` runs on Linux only, because it depends on `debootstrap` and `chroot`. When run on macOS, the script explicitly directs the operator to the remote Amazon Elastic Compute Cloud (Amazon EC2) builder script `build-rootfs-on-ec2.sh`. The preflight check before running requires that dependencies are present (debootstrap, aws, mkfs.ext4, curl, pigz, e2fsck, resize2fs), that `/tmp` has at least 10 GB of free space, and that at least 2 GB of memory is available (4 GB or more is recommended).

A single build produces three independent ext4 images:

- **rootfs (read-only golden image)**: pulled through debootstrap for Ubuntu Noble (arm64 from ports.ubuntu.com, amd64 from ec2.archive.ubuntu.com), with Node.js 22.x, the OpenClaw CLI, uv, auditd, and the GitHub CLI installed inside the chroot. Identity files and skills are baked into the rootfs at build time.
- **data-template**: the data-disk template, serving as the baseline for the writable data disk on the microVM's first boot.
- **immutable (read-only authoritative disk)**: contains 7 identity files and the 31 skills listed in `IMMUTABLE_SKILLS`, all of which are hashed with SHA-256 to produce the `golden-image.sha256` baseline.

The read-only semantics of the three images do not depend on the file system type; they are guaranteed by three overlaid layers: Firecracker's virtio write barrier (`is_read_only:true`), an in-guest `mount -o ro`, and a ro-bind.

Canary release is controlled by the `SKIP_MANIFEST` environment variable. When baking a new version, set `SKIP_MANIFEST=1`; the script publishes a versioned image without updating `manifest.json`, and the old image continues to serve as the live version for newly launched microVMs. The subsequent flow is: run the new version on a small number of test nodes, and only after verification passes, update `manifest.json`, then perform a rolling rebuild.

> **Note**
>
> Confirm that three ext4 images — rootfs, data-template, and immutable — are produced in Amazon S3. When `SKIP_MANIFEST=1` is set, confirm that `manifest.json` is not updated and that new microVMs still use the old image.

> **Note**
>
> The full S3 path prefix of the three ext4 images is to be verified; the upload target should be confirmed when changing environments (see the annotation in the source operations runbook).

## Step 3: Register the first tenant to verify

This section describes how to register the first tenant through the control plane API to verify the end-to-end path.

Send a `POST /tenants` request to the control plane API to register a tenant. The control plane API is fronted by Amazon API Gateway (named `openclaw-orchestrator`, stage `v1`), forwarding to an AWS Lambda function that runs on ARM_64 with 256 MB and a 120-second timeout. On successful registration, the control plane writes the tenant record into DynamoDB, schedules it onto a host, and that host's `launch-vm.sh` completes all cold injection before the Firecracker `InstanceStart` and launches the microVM.

> **Note**
>
> Confirm that `POST /tenants` returns (measured at about 1.7 seconds), and that the tenant status changes from `creating` through `running` to `vm_health` up within about 4.0 seconds (measured). Then send a message over the real-time chat path to confirm the agent replies (end-to-end first reply measured at about 27 seconds): sign in with Amazon Cognito to obtain the id_token → `POST {CloudFront}/hub/token` with `Authorization: Bearer {id_token}` and `{"tenant_id":"<id>"}` to exchange for a frontend token → `wss {CloudFront}/hub/ws?token=<frontend token>` to send `{"text":"..."}` → receive `{"type":"reply"}`. Chat messages travel over the claw-hub WebSocket hub and the claw-channel outbound connection inside the VM, and do not pass through any gateway HTTP endpoint. See "Developer Guide — Real-time chat integration".

## Core resources created by the CDK deployment

This section describes the core resources created by the AWS CDK deployment and their built-in security hardening.

The solution creates three primary tables in DynamoDB, all billed `PAY_PER_REQUEST` with `RETAIN` deletion protection:

- **`openclaw-tenants`**: the tenants table, primary key `id`. It includes two global secondary indexes (GSIs), both `ProjectionType=ALL`. `gsi_owner` (partition key `owner_id`) reverse-looks-up nodes by owner and is already ACTIVE; `gsi_tenant_user` (partition key `tenant_user_id`) reverse-looks-up a fleet of nodes by external business user, supporting the three `GET/POST /users/{tenant_user_id}/*` endpoints. `gsi_tenant_user` is controlled by `scaler.add_gsi_tenant_user` (default false) and is not created by default; because DynamoDB can add only one GSI per update, it must be deployed separately once after `gsi_owner` is ACTIVE. When it is not created, the three endpoints above degrade but core tenant CRUD is not blocked.
- **`openclaw-hosts`**: the hosts table, primary key `instance_id`.
- **`openclaw-groups`**: the skill groups table, primary key `name`, used for per-tenant and per-group skill distribution.

Beyond the primary tables, the solution creates two auxiliary tables:

- **Audit table `openclaw-audit-log-<8-hex>`**: the name carries a per-deploy suffix (the first segment of the STACK_ID UUID) to avoid colliding with a RETAIN residual table after a destroy-and-rebuild; TTL field `expires_ttl`, retained 90 days by default.
- **Async batch job table `openclaw-batch-jobs`**: primary key `job_id`, TTL field `expires_ttl` (completed job rows are cleaned up automatically after 7 days), `PAY_PER_REQUEST` paired with `DESTROY`. Large batches (more than 100 nodes) or `async:true` `POST /batch/tenants` requests are recorded in this table, executed in batches by a worker that the API Lambda self-invokes and that incrementally refreshes progress; the client polls through `GET /batch/jobs/{job_id}`.

### Point-in-time recovery

The four RETAIN tables — tenants, hosts, groups, and audit — all have Point-in-Time Recovery (PITR) enabled, with DynamoDB maintaining 35 days of continuous backups and the ability to restore to any second within the recovery window. This capability is controlled by config's `dynamodb.point_in_time_recovery` (default true) and `recovery_period_in_days` (default 35, the upper limit). The transient `openclaw-batch-jobs` table (`DESTROY` paired with TTL) does not have PITR enabled.

> **Note**
>
> PITR protects the control-plane metadata tables themselves. Tenant business data is separately backed up as a fallback by a backup Lambda that delivers to an Amazon S3 WORM bucket.

### Built-in security hardening

The following security hardening is hard-coded in the deployment code and takes effect on deployment; it is not subject to `config.yml` switches:

- **Amazon S3 assets bucket fully blocks public access and enforces HTTPS**: the assets bucket (which carries tenant data disks, skills, backups, and images) is set to `block_public_access=BLOCK_ALL` (all four public-access block switches on) and `enforce_ssl=True` (AWS CDK automatically appends a Deny on `aws:SecureTransport=false` to the bucket policy). Amazon CloudFront accesses it through signed Origin Access Control (OAC) without breaking the path.
- **VPC Flow Logs**: enabled by default (`flow_logs.enabled` defaults to true), delivered to the Amazon CloudWatch log group `/openclaw/vpc/flow-logs`, `traffic_type=ALL`, used to detect east-west cross-tenant anomalies and to verify that the iptables isolation is in effect. Retention defaults to 90 days.
- **Two AWS WAF baseline rules cannot be trimmed by config**: regardless of how `config.yml`'s `waf.managed_rules` is set, the code side always merges `AWSManagedRulesSQLiRuleSet` and `AWSManagedRulesAmazonIpReputationList` into the rule set (deduplicated in order via `dict.fromkeys`). The current config additionally configures `AWSManagedRulesCommonRuleSet` and `AWSManagedRulesKnownBadInputsRuleSet`, for 4 rules in total after merging.

> **Note**
>
> A customer-managed key (AWS Key Management Service (AWS KMS) CMK) for the audit table is a to-do item; it currently still uses an AWS-owned key. CIS 2.2.2 recommends a customer-managed CMK, but the existing audit table is `RETAIN`, and switching encryption online would force a replace and lose audit data, so it is added only on the first deployment of a fresh account. When planning disaster recovery for a new environment, operations should treat the audit table as "not currently a CMK".

### Control plane API Lambda specification

The control plane API Lambda runs on ARM_64 architecture, 256 MB of memory, and a 120-second timeout, with all configuration injected through environment variables. Amazon API Gateway is named `openclaw-orchestrator`, with stage `v1`, and routes are defined in the `add_resource` and `add_method` blocks (about 37 `add_method` calls, including GET/POST on the same resource).

---

# Use the Solution

This section describes day-to-day operation of the solution, including lifecycle management of hosts and tenant microVMs, image upgrade and canary release, monitoring and alerting and disaster recovery, and scaling and instance-type capacity.

## Host and tenant microVM lifecycle

This section describes host bootstrap, the tenant microVM launch flow, and the responsibilities of the lifecycle scripts for stopping, backing up, migrating, scaling, and cloning.

### Host bootstrap

After a new host is launched by the ASG, `init-host.sh` bootstraps in the following order: configure KVM permissions, harden the host, install tools and Firecracker, mount the data volume, download images (rootfs and vmlinux), sync shared skills from S3, deploy the lifecycle scripts, and finally register with the `openclaw-hosts` table.

EC2 user-data has a hard 16 KB limit, whereas `init-host.sh` is about 23 KB after injection. AWS CDK embeds `base64(gzip(init-host.sh))` into a small pure-ASCII bootstrap that decodes the script to `/tmp/init-host.sh` and executes it. When troubleshooting bootstrap failures, the file actually running is `/tmp/init-host.sh`, not the user-data original; `/var/log/cloud-init-output.log` records the bootstrap decode and init-host execution logs.

Key points of host bootstrap:

- **Firecracker version is pinned to v1.15.1** (overridable through the `FC_VERSION` environment variable), because latest may lack a CI-validated guest kernel.
- **Shared skills sync every 5 minutes via cron**: `*/5 * * * * root aws s3 sync s3://<bucket>/skills/ /data/shared-skills/`.
- **Per-host SSH key**: each host generates an ed25519 key pair once, keeps the private key in `/etc/openclaw/host_vm_key`, and injects the public key into each microVM's data disk under `.ssh/authorized_keys`. Each microVM trusts only its own host's key.
- **Lifecycle hook protection**: `init-host.sh` binds an EXIT trap, returning CONTINUE on success and ABANDON on failure, to prevent a broken host from stalling the ASG; if DDB registration fails after 10 retries, it exits with ABANDON.

### Launch a tenant microVM

`launch-vm.sh` receives 9 arguments (defaults in brackets): `tenant_id`, `vm_num`, `vcpu[2]`, `mem_mb[4096]`, `config_template`, `restore_backup_key`, `scoped_skills`, `litellm_vkey`, `channel_secret`. Of these, the 8th, `litellm_vkey` (the per-tenant billing key), and the 9th, `channel_secret` (the per-tenant hub HMAC secret), are minted and passed in by the API Lambda at registration time, with fallbacks when empty (vkey falls back to a shared key, channel_secret is self-generated).

> **Note**
>
> The script's default vCPU 2 / memory 4096 MB in `launch-vm.sh` match the microVM defaults in `config.yml` (`default_vcpu: 2` / `default_mem_mb: 4096`); the arguments passed in by the caller (the API Lambda) prevail in practice. Capacity is estimated at a 2 GB-per-microVM memory baseline (r8g.metal-24xl ≈ 760 GB ÷ 2 GB ≈ 380/host, an estimate; the per-host measured healthy density of 187 nodes is in the capacity section).

All injection in this solution completes before the Firecracker `InstanceStart`; this is where "zero runtime operations" is implemented. The launch flow is:

1. Mount data.ext4, inject shared skills, generate the gateway token, and inject the SSH public key.
2. Modify `openclaw.json` through jq: write a random `NEW_TOKEN` as `gateway.auth.token`, write the claw-channel HMAC secret, set `claw-channel.enabled=true`, and point hubUrl/wsUrl at port 8790 of the host IP; in the same jq block, delete chatCompletions and dangerouslyDisableDeviceAuth and narrow allowedOrigins to a single CloudFront origin; if the API minted a per-tenant vkey, additionally write `.models.providers.litellm.apiKey`.
3. Mount the `/dev/vdd` immutable read-only disk and start Firecracker.

After `InstanceStart`, the script disables strict mode, performs only an nginx reload and ssh-keygen cleanup, and no longer pushes data to the running microVM.

Each tenant microVM uses four virtual disks: `vda` rootfs (read-only), `vdb` overlay (read-write copy-on-write), `vdc` data (read-write persistent), and `vdd` immutable (read-only authoritative disk). The three-layer stack is a read-only rootfs base + a per-microVM sparse writable overlay layer + a writable persistent data disk, with both rootfs and immutable set `is_read_only:true`.

The solution overlays three firewall isolation layers on each tenant microVM. The rules are all inserted with `-I` at the top of the FORWARD/INPUT chains, ahead of ACCEPT, with one copy inserted per microVM by tap interface:

- DROP guest traffic to IMDS (169.254.169.254 and IMDSv6 169.254.169.253), preventing theft of host credentials.
- DROP guest traffic to the entire tenant supernet (SUBNET_PREFIX/16), preventing east-west microVM interconnection.
- INPUT DROP guest traffic to the host's ports 8899/9090/22, preventing access to the management plane.

The hardened target state is 100% cross-tenant packet loss (the hardening code is in place and the static rules have been reviewed as consistent; the vulnerable state measured 0% packet loss and a cross-tenant RTT of 0.187 milliseconds. A fresh, timestamped bare-metal re-test is to be verified).

### Stop, back up, migrate, scale, and clone

The solution provides a set of lifecycle scripts, each with the following responsibility:

- **`stop-vm.sh`** stops in four steps: send Ctrl-Alt-Del for a graceful shutdown, wait 2 seconds, SIGTERM first and then SIGKILL after a sleep, and clean up the network and nginx. The script comments emphasize not to `pkill firecracker`, because after `InstanceStart` succeeds the microVM is running normally, a subsequent nginx race should not clear it in reverse, and crash recovery is left to the host-agent's automatic recovery.
- **`backup-data.sh`** pauses the microVM, compresses data.ext4 in parallel with pigz, resumes the microVM, and uploads to S3, with a key format of `backups/{tenant}/{timestamp}.gz`; a `cleanup()` trap ensures the microVM is resumed even on failure.
- **`migrate-vm.sh`** provides two modes, snapshot and restore: in snapshot mode it pauses, creates a snapshot, resumes, and uploads snapshot.vm, snapshot.mem, vm.json, data.ext4, and overlay.ext4 to S3; in restore mode it downloads all disks, starts Firecracker, and POSTs `/snapshot/load` to restore and wake automatically. A Firecracker snapshot records only disk paths, so the actual files must be transferred as well, or a cross-host restore reports `os error 2`.
- **`resize-disk.sh`** resizes online: pause, truncate to enlarge the sparse ext4, e2fsck, resize2fs, resume, without a reboot or a partition adjustment.
- **`clone-data.sh`** clones on the same host: pause the source, copy data and overlay with `cp --sparse=always`, resume the source, and verify with `e2fsck -fy`. The script receives four arguments — src_tenant, src_vm_num, dst_tenant, dst_vm_num — and after cloning completes the caller must run `launch-vm.sh` to start the target.

> **Important**
>
> Before deleting a tenant microVM, confirm that it has no real data or that a backup has completed. `DELETE /tenants/{id}?keep_data=false` synchronously backs up to S3 before deleting the data disk; if the backup fails it returns 502 and aborts the deletion (fail-closed); only `?skip_backup=true` skips the backup. Deletion also reclaims the tenant's LiteLLM vkey to prevent orphan keys. Before deleting a host, verify which tenants are mounted on it to avoid accidentally deleting nodes used for demonstration.

> **Note**
>
> The robustness of several lifecycle scripts in edge cases is to be hardened: `resize-disk.sh` assumes no bad blocks, and after a backup restore, if the preceding e2fsck reports code 4 and is not re-checked, the file system may be corrupted after resizing; `clone-data.sh` has no return-code check after e2fsck; `migrate-vm.sh` restore mode tolerates a missing disk, and cross-host, if the snapshot's referenced path does not match, it fails at the `/snapshot/load` stage; the space-reclamation mechanism for temporary files after `stop-vm.sh` is not explicitly documented in the script.

## Image upgrade and canary release

This section describes the image upgrade discipline and the canary release mechanism of the solution.

The correct way to upgrade OpenClaw, modify configuration, or replace an identity is to modify the deployment code and rebuild, not to hot-modify a running microVM. Modifying a tenant's identity requires rebuilding the image, not calling a runtime API. The specific path is: modify `build-rootfs.sh` or `launch-vm.sh`, bake a new image or adjust the launch template, canary and then perform a rolling rebuild, and roll back `manifest.json` if a problem occurs. Manually modifying a running microVM is only for validating a hypothesis; after validation it must be landed back into the deployment code.

This discipline is the foundation of the whole isolation design: identity, skills, credentials, and configuration go through cold injection before launch, and no batch hot-injection channel from host to microVM is opened after launch — one fewer live channel means one fewer lateral-movement surface.

Canary release is implemented through `SKIP_MANIFEST` (see "Deploy the Solution — Step 2: Build the golden image"). The flow is: bake a new image with `SKIP_MANIFEST=1`, verify on a small number of test nodes, update `manifest.json` after verification passes, and perform a rolling rebuild.

> **Important**
>
> A rolling rebuild rebuilds the microVMs on a host with the new image, one host at a time. The rebuild regenerates each microVM's gateway token and channel secret and starts it from the new image. Before executing, confirm that the new image has passed verification on the test nodes; roll back by pointing `manifest.json` back to the old version if a problem occurs.

## Monitoring, alerting, and disaster recovery

This section describes the solution's scheduled control-plane tasks, health determination, AZ failover, runtime monitoring, and self-hosted monitoring platform.

### Scheduled control-plane Lambdas

The solution's disaster recovery and operations are driven by three scheduled Lambda functions:

| Lambda       | Frequency                       | Responsibility                                                    |
| ------------ | ------------------------------- | ----------------------------------------------------------------- |
| health_check | Every 5 minutes                 | Determine stale, restart agent, AZ failover, migration monitoring |
| scaler       | Every 3 minutes                 | Idle-host reclamation, TTL-expired tenant handling                |
| backup       | Scan cadence `rate(30 minutes)` | Run `backup-data.sh` through AWS Systems Manager RunShellScript   |

The backup `backup_cron` is a scan cadence rather than a unified backup time: each trigger backs up only a batch that is due (more than `backup_interval_hours` since the last backup, default 24 hours), up to `backup_batch_limit` (default 20), staggering and limiting concurrency, with the full set rolling over within `backup_interval_hours`.

### Health determination and restart

A tenant with no health update for more than 120 seconds is determined to be stale, and its host agent may be down. Host-agent restart has a 600-second (10-minute) cooldown to prevent frequent restarts. The 120-second threshold pairs with the host-agent refreshing its timestamp every 15 seconds: only after about 8 consecutive periods without a refresh is it considered stale.

### AZ failover

In the current `config.yml`, `multi_az.enabled=false` and `az_failover.enabled=false`; during the test period, it runs on a single AZ to save cross-AZ traffic costs. Once enabled, the logic is: if all hosts in an AZ are unhealthy for more than 10 consecutive minutes, failover is triggered, with a 30-minute cooldown to prevent repeated triggers. Recovery has a precondition gate — a backup must exist, otherwise the migration is refused and marked `failover_blocked`. The post-failover microVM is verified with three-layer detection: the Firecracker process exists, the nginx configuration exists, and a local HTTP probe returns less than 500; on success it records the audit log `AZ_FAILOVER_TENANT_RECOVERED`. The migration process is monitored with a 15-minute timeout, rolling back to running automatically on timeout; after failover and migration, it also verifies through the public path (through the ALB) that the dashboard is truly reachable, rolling back if unreachable.

### Runtime monitoring

Inside each tenant microVM runs a layer of runtime monitoring that watches for two kinds of action: one, the microVM establishing a reverse connection outbound (a typical reverse shell); and two, modification of key system and identity files. A hit produces an alert. The monitoring itself runs with the system's highest privilege, invisible to, uncloseable by, and unreadable by the ordinary agent user, so the "turn off monitoring first, then act" path is blocked. This monitoring layer is resident with controlled overhead: file and behavior auditing takes about 11.7 MB, and file integrity monitoring takes about 42 MB of memory (measured).

### Self-hosted monitoring platform

Monitoring is fixed to self-hosted Prometheus, Grafana, and Wazuh on Amazon EC2, not relying on Amazon Managed Service for Prometheus or Amazon Managed Grafana. Both monitoring assets are optional, deployed on demand, and do not start automatically with the main stack. The managed monitoring in AWS CDK (Amazon Managed Service for Prometheus, Amazon Managed Grafana, Amazon GuardDuty, Amazon Simple Notification Service (Amazon SNS) notifications) is config-gated and off by default. Both self-hosted monitors are deployed on dedicated EC2 (to isolate the blast radius, not running on the metal host), with security group inbound open only to the VPC CIDR or the bastion security group, never to 0.0.0.0/0:

- **Prometheus and Grafana**: scrape each metal host's host-agent `:8899/metrics` (microVM memory, Balloon, disk, CPU, health, and other gauges), auto-discovered through ec2_sd and accompanied by a dashboard. The collection path depends on two accompanying conditions: the host instances must carry the `Project=openclaw` and `Role=metal-host` tags, and the host security group must allow port 8899 inbound from the VPC CIDR.
- **Dual-EC2 Wazuh**: EC2-1 is the manager all-in-one (manager, indexer, dashboard), and EC2-2 is the agent (wazuh-agent, auditd, and real-time file integrity monitoring). Neither EC2 has a public IP, and the dashboard is accessed through a bastion SSH tunnel.

To avoid alerts landing only on the manager itself, the deployment script configures a least-privilege instance role for the manager and mirrors alerts in real time to a separate CloudWatch log group and an Amazon SNS notification topic. Optionally, a separate Amazon OpenSearch Service domain can be enabled (off by default, incurring continuous cost) to land another copy of the alerts in a separate trust domain. Runtime alerts from inside microVMs can be aggregated to this manager for unified viewing.

## Scaling and instance-type capacity

This section describes the solution's ASG elastic scaling, instance-type selection, capacity configuration, and concurrency control for large-scale scale-out.

### ASG elastic scaling and instance types

Host scaling is managed by Amazon EC2 Auto Scaling. The solution uses an Auto Scaling Group and a launch template to manage the launch and rolling rebuild of the entire host fleet. Hosts are launched by the ASG and bootstrap-register; idle timeout is reclaimed by the scaler under control after two rounds of confirmation; the whole pool's capacity is adjusted through `config.yml`, with defaults `min_capacity: 2` / `max_capacity: 8` hosts.

Production instance types use the metal series (Graviton4 ARM64, running Firecracker on native KVM, not x86 nested virtualization). `config.yml` configures the instance type through `arch: arm64` and `instance_type`, and capacity derivation is computed by the deployment code by looking up the size token in a table combined with the memory ratio. The metal instance type uses native KVM and does not enable nested virtualization; the production foundation is fixed at metal native KVM.

If config's `host.instance_types` provides multiple equal-capacity instance types (no fewer than 2), the ASG uses a `MixedInstancesPolicy` to launch hosts across instance types, improving availability and Spot resilience. The hard constraint is that all instance types in the pool must be equal capacity (same vCPU and memory), otherwise synth errors out directly.

### Capacity configuration

Capacity is adjusted through `config.yml`:

- Each tenant microVM defaults to 2 vCPU / 4096 MB, controlled by `vm.default_vcpu` and `vm.default_mem_mb`; capacity is still estimated at a 2 GB-per-microVM memory baseline.
- The overcommit ratio is controlled by `cpu_overcommit_ratio` and `mem_overcommit_ratio`. Allocatable capacity is computed as `allocatable_vcpu = total_vcpu × CPU_OVERCOMMIT_RATIO`, and the API side schedules based on each host's remaining capacity.
- Each host is configured with a 600 GB gp3 encrypted EBS data disk (`/dev/sdf` mounted at `/data`), carrying the sparse disks and rootfs overlays of all microVMs. A single microVM's sparse disk actually occupies about 187 MB to 1.3 GB.
- microVM addressing tops out at vm_num 480 (one /30 point-to-point link per microVM).
- Allocating vm_num uses a DynamoDB ConditionExpression optimistic lock, with 8 CAS retries.

### Concurrency bottleneck and throttling queue

The real bottleneck for creating tenants at scale is AWS Systems Manager concurrency, not capacity. On the synchronous path, each create or start sends an independent `ssm.send_command` to the target metal, and filling a single host all at once instantly exceeds the Systems Manager per-instance concurrency quota, leaving some requests permanently stuck in `creating`.

The solution is to enable `scaler.lifecycle_queue_enabled=true`, which first writes create/start/stop/delete into an Amazon Simple Queue Service (Amazon SQS) queue to throttle, with the consumer executing in batches at a controlled concurrency (set to about 5 to 10 per metal, corresponding to the sustainable rate per Systems Manager instance, rather than the default 50). Enable this throttling queue before large-scale scale-out; do not have the client POST at N-way concurrency directly.

### Measured performance and capacity numbers

| Metric                         | Value                                                                 | Source            |
| ------------------------------ | --------------------------------------------------------------------- | ----------------- |
| microVM pure boot              | 1.74 s (p50)                                                          | metal measured    |
| launch to gateway available    | 6.48 s (Firecracker 1.7 s + gateway cold start 4.7 s)                 | metal measured    |
| blank microVM RSS              | about 609 MB                                                          | metal measured    |
| whole-machine ramp average     | 669 MB                                                                | metal measured    |
| single-host fully-healthy load | 187 nodes (disk bottleneck, not a memory ceiling)                     | metal measured    |
| steady-state load per host     | 380 tenants (r8g.metal-24xl, 760 GB ÷ 2 GB) (estimated)               | capacity estimate |
| monthly cost                   | about 8.36 USD/tenant/month (80% Savings Plan + 20% Spot) (estimated) | cost estimate     |

> **Note**
>
> Several capacity and scaling items remain uncertain and should be treated as not yet settled during capacity planning: whether the 187-node disk bottleneck is IOPS or capacity is unclear; the ASG auto-scale trigger threshold is to be confirmed and currently appears to be manual SetDesiredCapacity only; the relationship between the ALB rules hard wall and capacity is to be confirmed; the detailed allocation model for the about 8.36 USD/tenant/month cost is not provided. The cost estimate excludes third-party services and data-transfer charges.

---

# Troubleshoot

This section describes common problems and their solutions, log entry points, and support channels for the solution.

## Solutions to known problems

The "symptom, locate, remedy" items below are based on the actual error branches and self-recovery logic in the code. All host operations go through SSH; AWS Systems Manager is used only in production-operations scenarios.

### Symptom A: A tenant's health flips to stale and the chat connection drops

Determination: a tenant with no health update for more than 120 seconds (`STALE_SECONDS=120`) is determined to be stale.

Locate: SSH to the host, check whether the Firecracker process exists, and view the microVM's `fc.log` (`--log-path ${VM_DIR}/fc.log`, where VM_DIR is `/data/firecracker-vms/<tenant>`).

Remedy: the host-agent provides two levels of self-recovery, and in most cases no manual intervention is needed. When `vm.json` exists but the process has disappeared, `_recover_vm` restarts it; when Firecracker is alive but the guest network is continuously unreachable up to the threshold, `_force_relaunch_vm` rebuilds through stop plus launch. If all tenants on an entire host go stale at once, the host-agent itself is most likely down, and the health_check Lambda issues `systemctl restart host-agent` through Systems Manager to bring it up, with a 600-second cooldown (`RESTART_COOLDOWN_SECONDS=600`). Do not manually restart repeatedly during the cooldown; wait out the cooldown per `agent_restart_at`.

Chat-specific troubleshooting (health is normal but the user's chat cannot connect): the chat path and the in-VM gateway control UI are two orthogonal paths; chat travels over the claw-hub WebSocket hub plus the in-VM claw-channel outbound connection, unrelated to the control UI. Troubleshoot in this order — (1) whether the claw-hub service (`claw-hub.service`) is healthy, whether `/hub/healthz` returns normally, and whether the host nginx `/hub/` reverse proxy is in effect; (2) the in-VM claw-channel outbound connection log (`claw-channel.log`), looking for decisions such as `token-fail`, `ws-unexpected-response`, and `ws-close`; (3) whether `channel_secret` in the Amazon DynamoDB tenant record is mirrored and ready (the hub verifies the VM's channel registration signature against it).

### Symptom B: microVM launch errors, the microVM does not come up

Locate: view the `launch-vm.sh` log (`log()` prints to stdout with the prefix `[oc:launch]`; the ERR trap prints `FAIL line=<line number>`).

Remedy: several hard-failure branches that `exit 1` directly — on backup restore, `e2fsck` returning code 4 or 16 (file system corrupted, not fixed) is judged `FATAL: backup filesystem check failed` and refuses to start, and only return codes 0/1/2/8 are accepted, so an earlier backup key can be retried; `tuntap add` reporting EBUSY forces a tap cleanup and retries (not fatal); when Firecracker `InstanceStart` returns a non-empty error, it `exit 1`s and prints `ERROR: <RESULT>`.

### Symptom C: After a cross-host migration or failover, the microVM does not come up, and fc.log shows `os error 2`

Cause: a Firecracker snapshot records only disk paths, so a cross-host restore must transfer the snapshot.vm, snapshot.mem, vm.json, data.ext4, and overlay.ext4 actual files as well.

Remedy: confirm that these actual files are complete in S3 before restoring. `migrate-vm.sh` restore mode tolerates a missing disk with `2>/dev/null || true`, so a missing file does not error at the download stage but fails at the `/snapshot/load` stage.

### Symptom D: Failover is triggered but refused, and the tenant is marked failover_blocked

Cause: failover recovery has a precondition gate — a backup must exist, otherwise the migration is refused.

Remedy: first run a backup for the tenant (see the backup scripts in "Use the Solution — Host and tenant microVM lifecycle"), then retry the failover.

### Symptom E: An idle host is not reclaimed, or reclamation is too aggressive

Logic: the scaler confirms in two rounds — idle for more than 10 minutes is marked idle first, and only if it is still idle in the next round and the ASG allows is it terminated, protected by the ASG MinSize, skipping when at min. Not reclaiming at MinSize is expected behavior, not a defect.

## Log entry points

| What to look at                                                 | Where                                                        | How to get                                                                         |
| --------------------------------------------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| host-agent probes and self-recovery                             | the systemd service `host-agent` on the host                 | after SSH, `journalctl -u host-agent -n 200`                                       |
| Firecracker log of a single microVM                             | `${VM_DIR}/fc.log` (`/data/firecracker-vms/<tenant>/fc.log`) | after SSH, read the file                                                           |
| nginx reverse proxy                                             | the `nginx` service on the host                              | `journalctl -u nginx` and `/var/log/nginx/*`                                       |
| control-plane Lambdas (register, schedule, health, backup)      | Amazon CloudWatch Logs, grouped by each Lambda function name | console or `aws logs tail`                                                         |
| in-guest runtime alerts (reverse shell, sensitive file changes) | in-microVM analyzer, root:root 0700, invisible to the agent  | through the host-agent or the monitoring pipeline, not readable in guest userspace |

## Contact support

If you encounter a problem not covered in this section, contact the support team through the deployer's technical support channel. When submitting a support request, it is recommended to attach the following diagnostic information to speed up localization:

- The `tenant_id` of the affected tenant and the `instance_id` of the host it is on.
- The host-agent logs (`journalctl -u host-agent`) and the corresponding microVM's `fc.log` for the relevant time window.
- Log snippets in CloudWatch Logs from the relevant control-plane Lambda functions.
- Steps to reproduce the failure, the expected behavior and actual behavior, and any remedial actions already attempted.

> **Note**
>
> Before submitting diagnostic information, redact it by removing credentials (gateway token, API key, JWT, and so on), real account IDs, and real domain names.
