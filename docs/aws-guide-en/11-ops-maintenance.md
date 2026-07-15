# 11 · Operations & Component Maintenance

> Per-component runbook: routine tasks, monitored metrics, alert thresholds,
> scaling notes, and troubleshooting. This chapter is the operational floor
> for 100k-tenant scale. Chinese counterpart lives at
> `docs/aws-guide/11-ops-maintenance.md`; both stay in sync.
> Source of truth for the data-plane redesign:
> `internal-docs/00-knowledge-base/the data-plane design/` + the arch review
> in `internal-docs/progress/aws-architect.md`.

---

## 11.1 CloudFront distribution

**Role**: Global edge; TLS termination; S3 origin for static assets; data-
plane `/ws/*` forwarded to the ALB.

**Routine**: AWS-managed. Only changes via CDK. Quarterly review of
`security_headers_behavior` (CSP/HSTS) against the security baseline.

**Metrics**: `4xxErrorRate`, `5xxErrorRate`, `OriginLatency`,
`TotalErrorRate`. If WAF is attached, watch `BlockedRequests` bursts
(usually a scanner).

**Thresholds**:

- 5xx 5min-avg > 1% → warning; > 5% → critical (usually origin/ALB, not
  CloudFront itself).
- OriginLatency p99 > 5s → investigate the microVM gateway.

**Long-idle WS constraint**: CloudFront HTTP origin `readTimeout` is hard-
capped at **180s** — this comes from CDK source
`aws-cdk-lib/aws-cloudfront-origins` `HttpOrigin` (line varies by CDK version) where
`validateSecondsInRangeOrUndefined('readTimeout', 1, 180, ...)` is enforced.
A WebSocket that goes idle > 180s will be cut by CloudFront regardless of
the ALB (3600s) or OpenResty (3600s) timeouts. Clients MUST ping every
~30s. Publish this in the client SDK integration guide.

**Troubleshooting**:

- 502 from CloudFront: check `OriginLatency` for spikes → ALB access logs
  for the request → certificate chain / origin health.
- SSE disconnects: confirm origin `read_timeout` is 180s and the client
  emits a 30s heartbeat.

---

## 11.2 Application Load Balancer

**Role**: L7 data-plane entry. LOR routing to the OpenResty edge ASG
(the transitional `/hub/*` → host TG rule was removed with #187 P5).

**Routine**: AWS-managed across 3 AZs. On CDK deploy verify
`idle_timeout=3600s` (SSE/WS) and that the SG only accepts the CloudFront
origin-facing prefix list (exposure red line). ACM certs auto-renew.

**Metrics**: `RequestCount`, `TargetResponseTime`, `HTTPCode_ELB_5XX_Count`,
`TargetConnectionErrorCount`, TG `HealthyHostCount` / `UnHealthyHostCount`.
Non-zero `TargetConnectionErrorCount` almost always means backend SG or TG
port is misconfigured.

**Thresholds**:

- HealthyHostCount < N-1 (N = ASG desired) for 2min → warning; = 0 →
  critical (data-plane down).
- HTTPCode_ELB_5XX_Count 5min > 100 → warning; likely ALB-layer errors
  (SG rejects, all backends unhealthy).
- TargetResponseTime p95 > 3s → backend slow.

**Scaling**: ALB scales itself. LCU pricing (25 new conn/s, 3000 active
conn/min, or 1GB/LCU-hour). 300k active WS ≈ 100 LCU/min ≈ $584/month at
list rates — verify against the actual bill.

**Troubleshooting**:

- Customer reports a WebSocket that "hangs but doesn't fail": confirm
  `idle_timeout=3600s` is in the latest IaC deployment; check TG
  HealthyHostCount and that edge instances are live.
- 5xx spike: enable ALB access logs to S3 (currently off) and grep for
  the failing status code.

---

## 11.3 OpenResty edge ASG (data-plane routing)

**Role**: Look up `tenant_id` in Redis to resolve `host:port`, then
`proxy_pass` to the host DNAT or the local microVM gateway. This is the
new data-plane entry — the load-bearing tier for 100k tenants.

**Shape**: Dedicated ASG, 3 AZs, min=3 desired=3 (N-1), max sized by load
tests. c6in.xlarge (x86) or c7g.xlarge (arm64). Userdata is
`deploy/edge/install-edge.sh`.

**Routine**:

- Rolling instance refresh every 30 days for OpenResty patches + kernel
  updates. `aws autoscaling start-instance-refresh --auto-scaling-group-name
openclaw-edge-asg --preferences MinHealthyPercentage=66` keeps 2 of 3
  alive for N-1 safety.
- OpenResty version bumps happen by editing `install-edge.sh`'s
  `OPENRESTY_VERSION` and rolling.
- Verify `/etc/sysctl.d/99-openclaw-edge.conf` survives AMI base changes.

**Metrics**: Prometheus scrape at `:8080/metrics` (stub in P2; wired up in
P6 with cache-hit rate, route source distribution, Redis latency).
CloudWatch Agent tails journald for the `claw-edge` unit — grep `WARN` or
`ERR`. ELB TG `HealthyHostCount` should equal `min_capacity` at rest.

**Thresholds**:

- HealthyHostCount < 3 for 5min → critical.
- Edge log lines matching `redis transport err` 5min > 10 → warning:
  the data plane fell into fail-static; investigate Redis immediately.
- Long-term /healthz 503 → ASG replaces the instance; three refresh cycles
  in a row failing means ElastiCache isn't ready or SGs are wrong.

**Scaling**:

- Trigger: `RequestCountPerTarget` p95 > 2000 rps for 3min or CPU > 70% →
  desired += 1.
- Trim: CPU < 30% for 30min → desired -= 1 (never below min).

**Cold-start wall clock (SPEC §6)**:

- `health_check_grace_period` recommended **300s**. Measurement at P7 will
  refine: EC2 boot ~60s + apt install openresty ~90s + nginx <5s +
  route.lua async Redis warmup up to 30s (15 attempts × 2s) + buffer.
- `install-edge.sh` already polls `/healthz` at userdata tail until it
  returns 200; the ASG lifecycle hook only continues after that. Repeated
  warmup failures should be triaged via `journalctl -u claw-edge`.

**Troubleshooting**:

- Some tenants 404, others 200: three-tier cache inconsistency. SSH the
  edge and `curl 127.0.0.1:8080/healthz`; grep the `route.lua` log for the
  tenant id; check Redis directly with
  `redis-cli -h <primary-endpoint> get route:<tid>`.
- Burst of 503: usually a Redis brownout — `route.lua` entered fail-static.
  Check the ElastiCache event log for a failover.

---

## 11.4 Host ASG (metal Firecracker pool)

**Role**: Each r8g.metal-24xl runs 380 microVMs — the "fortress" (project
iron law #3).

**Routine**:

- Identity/skill/config changes require re-baking the golden image and a
  rolling replace. Never modify a running host or VM (iron law #3).
- Capacity headroom via `config.yml:asg.max_capacity` + a scaling event.
- v4 → v5 image upgrades go through the P3 re-bake + `instance refresh`.
  Roll one host at a time and roll back the launch-template version on
  failure.

**Metrics**: host-agent `:8899/metrics` (Prometheus): `active_vm_count`,
`disk_usage_gb`, `dnat_rule_count`, `port_bitmap_usage`,
`descriptor_drift_count`. CloudWatch Agent for host system metrics.
`descriptor_drift_count` > 0 for 5min → warning: port bitmap, DNAT, and
DDB descriptors drifted, indicating a possible resource leak.

**Thresholds**:

- `active_vm_count / host_max` > 95% → capacity warning; add hosts.
- `disk_usage_gb` > 800 (of the 900GB data volume) → warning (100GB
  headroom).
- Host heartbeat missing > 5min → critical (the whole host is down; all
  tenants on it are affected; AZ failover — `config.yml:health_check.
az_failover` — should trigger).

**Scaling**:

- `scaler` Lambda (`deploy/lambda/scaler`) terminates idle hosts per
  `idle_timeout_minutes`.
- **100k tenant creation MUST route through SQS dispatch**
  (`dispatch.enabled=true`; see `config.yml:139-148`). Synchronous
  fan-out saturates SSM at ~40 concurrent SendCommand.

**Troubleshooting**:

- Single host down while the AZ is fine: verify AZ failover was NOT
  falsely triggered; check EC2 status checks; pull kernel oops with
  `aws ec2 get-console-output`. With `keep_data_volume=true` tenant data
  survives; a fresh host attaches and recovers it.
- Whole AZ down: `health_check.az_failover` migrates running tenants to
  other AZs with a 30min cool-down against repeated fires.
- iptables DNAT drift (`descriptor_drift_count > 0`): SSH the host,
  `iptables -t nat -L PREROUTING -n --line-numbers`, compare against the
  DDB `host_port` for every tenant descriptor. Use the host-agent
  reconciler to fix — never hand-edit tables.

**100k-scale notes**:

- 380 tenants per host (2GB/VM × 380 = 760GB matches the 768GB metal) is
  a hard ceiling. Raising `mem_overcommit_ratio` above 1.5 lands you in
  the narrow `free_page_reporting` balloon window and risks OOM.
- 300 hosts must spread evenly across AZs. Skewed placement (all in one
  AZ) magnifies the blast radius of an AZ event.
- `lifecycle_hook_timeout=1200s` is already generous (842MB golden image
  download + decompress + mount, see `config.yml:71`). When starting a
  new region from cold, ramp in waves (e.g. 20 hosts per 3-5 min) so SSM
  doesn't hit its per-instance concurrency limit.

---

## 11.5 ElastiCache Redis (route cache authority)

**Role**: `tenant_id → {host, port, guest_ip}` — the authoritative routing
map. host-agent writes; edge reads. Both are one-way.

**Shape**: Multi-AZ replication group. 3 nodes (1 primary + 2 replicas
across AZs), `automatic_failover_enabled=true`, Redis 7.x, private subnet,
SG accepts only host + edge.

**Routine**:

- Memory growth is trivial: ~200B per route × 100k tenants = 20MB.
  cache.r7g.large (13GB) is comfortable.
- Daily snapshot with 7-day retention (defense in depth — real recovery
  is via host-agent replaying from DDB).
- Engine maintenance window off-peak (04:00 UTC).

**Metrics**: `EngineCPUUtilization`, `DatabaseMemoryUsagePercentage`,
`Evictions` (should be zero — routes have no EXPIRE), `ReplicationLag`,
`ReplicationBytes`, `CacheHits` / `CacheMisses` (>99% hit expected under
host-agent double-write). App-side: edge log matches on
`redis transport err` should be 0 outside failover windows.

**Thresholds**:

- `ReplicationLag` > 5s for 5min → warning.
- `Evictions` > 0 → critical (routes must never be evicted; eviction =
  data loss = tenant 404s).
- `EngineCPUUtilization` > 70% → warning; scale up node type or shard.
- CloudWatch event `AWS ElastiCache Failover` → info-level record;
  correlate with edge fail-static duration (should be < 30s given the
  60s L2 TTL).

**Failover behavior**:

- Multi-AZ failover typically completes in 15-30s (AWS documentation:
  "usually <60s"; exact numbers still pending live verification).
  Clients must reconnect. `host-agent` uses redis-py with
  `retry_on_error=[ConnectionError]` and `health_check_interval=30`. The
  edge relies on `resolver ... valid=30s` + L2 stale TTL of 60s.
- Manual failover drill each quarter:
  `aws elasticache test-failover --replication-group-id openclaw-routes
--node-group-id 0001`.

**Scaling**:

- Memory > 70% → upgrade node type (add a replica, promote, drop the old
  primary). Disruptive — schedule a maintenance window.
- QPS is a non-issue below ~50k ops/s. Peak in this system is ~10k
  GETs/s.

**Troubleshooting**:

- Edge storming fail-static with 5xx: confirm connectivity
  (`redis-cli -h <primary-endpoint> ping` from an edge box) and DNS
  resolution (`dig +short <primary-endpoint>` — see whether the AZ moved).
- Single tenant 404: host-agent didn't write to Redis. SSH the host,
  grep the host-agent log for that tenant id, and check the key with
  `redis-cli`.

---

## 11.6 API Lambda + DynamoDB (control plane)

**Role**: Tenant CRUD + host lifecycle. Unchanged by the data-plane
redesign.

**Routine**:

- Lambda: `update-function-code` must ship the full dependency wheel —
  Python-only updates without deps have broken PyJWT in production (see
  `memory/e2e--passed-and-pyjwt-lesson`).
- DynamoDB: PITR enabled (35 days). audit table WORM archive controlled
  by `config.yml:audit.worm_archive_enabled`. Snapshot before any delete
  (iron law #4).

**Metrics**: Lambda `Errors`, `Throttles`, `Duration`,
`ConcurrentExecutions`. DDB `ThrottledRequests`, `UserErrors`,
`ConsumedRead/WriteCapacityUnits`. SQS dispatch queue
`ApproximateNumberOfMessagesVisible`, DLQ depth.

**Thresholds**:

- Lambda `Errors` 5min > 5 → warning.
- DDB `ThrottledRequests` > 0 for 3min → critical; switch to provisioned +
  auto-scaling.
- SQS DLQ depth > 0 → critical; fail-loud on any message that couldn't be
  delivered.

**Scaling for bulk tenant creation**: enable
`scaler.lifecycle_queue_enabled=true` + `scaler.create_via_queue=true` +
`dispatch.enabled=true`. Without those, 40 concurrent `POST /tenants`
requests will hit SSM's `TimedOut` cliff (documented in
`memory/loadtest-380-ssm-concurrency-bottleneck`).

---

## 11.7 KMS CMK

**Role**: envelope-encrypt gateway tokens (EncryptionContext=tenant_id)
and injected credentials (EncryptionContext=owner_id) + optional audit
CMK.

**Routine**:

- CMK auto-rotation on (yearly). CDK: `enable_key_rotation=True`.
- Quarterly key policy audit: host role only Decrypts (never Encrypt or
  GenerateDataKey); API Lambda holds the reverse.
- **Never delete a CMK.** During the 30-day pending window you can cancel;
  afterwards ciphertext is unrecoverable. Set `RemovalPolicy.RETAIN` in
  CDK.

**Metrics / alerts**: CloudWatch `KMS.Requests`, `KMS.Errors`. CloudTrail
Decrypt/Encrypt events for per-role auditing. Anything unexpected (a role
that shouldn't Decrypt calling Decrypt) → warning at once.
Decrypt errors per minute > 20 → warning (EncryptionContext mismatch, an
identity is being forged). `DisableKey` / `ScheduleKeyDeletion` in
CloudTrail → critical (someone is deleting a key).

---

## 11.8 NAT Gateway (one per AZ)

**Role**: Private-subnet outbound to Bedrock / LiteLLM / public APIs for
hosts, edge, Lambda.

**Routine**: One NAT GW per AZ, no cross-AZ sharing (cheaper + smaller
blast radius). Pre-allocate the EIP if upstream services allow-list it.
NAT GW auto-scales bandwidth (up to 100 Gbps + 55000 concurrent
connections per EIP per destination).

**Metrics**: `BytesInFromDestination`, `BytesOutToDestination`,
`ActiveConnectionCount`, `ConnectionAttemptCount`, and critically
`ErrorPortAllocation`.

**Thresholds**:

- `ErrorPortAllocation` > 0 → critical (data plane starts dropping;
  usually too many connections to a single destination IP; add EIPs or
  use a VPC endpoint).
- Per-NAT-GW egress > 50 Gbps → warning (approaching the ceiling).

**Cost / scale optimization**:

- Route AWS-service traffic (Bedrock, S3, DDB, KMS) through **VPC
  Endpoints** to skip NAT data-processing charges — one of the biggest
  levers at 100k scale. NAT is $0.045/GB processed; VPCE gateway type
  (S3/DDB) is free; VPCE interface type is $0.01/GB + $0.01/hour/AZ.
  A rough model: 100k tenants with 100 GB/day of Bedrock traffic
  ≈ $4500/month via NAT vs. hundreds via VPCE.
- Add secondary EIPs to a NAT GW if a single upstream target concentrates
  many connections (each EIP gets its own 55000-connection budget per
  destination).

**Troubleshooting**:

- Some microVMs can't reach the internet: check whether
  `security.egress_allowlist_enabled=true` DROPs the target on the tap;
  then NAT GW `ErrorPortAllocation`; then upstream throttling.

---

## 11.9 Wazuh manager (security monitoring)

**Role**: Aggregates in-guest auditd/FIM alerts, GuardDuty findings, and
host-agent metrics.

**Current shape**: Single EC2 (m7i.xlarge, AL2023) running docker-compose
for manager/indexer/dashboard. `config.yml:security.wazuh_enabled=false`
by default.

**HA gap**: Single-instance = single point of failure. Production
recommendations, in order of investment:

1. Baseline: docker-compose `restart: unless-stopped` + EBS PITR snapshot
   - Grafana provisioning versioned in git. Survives process crashes;
     still loses data if the EBS volume corrupts.
2. Robust: two-manager cluster with a shared EFS volume + OpenSearch
   cluster indexers. Recommended for real production.

**Routine**: Weekly triage of alerts to tune rule weights; quarterly
Wazuh minor upgrade via docker image tag bump.

**Alerts**: Critical Wazuh alerts route through SNS to email/Slack.

---

## 11.10 Prometheus + Grafana (self-hosted, P6)

**Role**: Aggregate host-agent `:8899`, edge `:8080/metrics`, and Lambda
CloudWatch metrics.

**Planned shape**: CDK-launched EC2 running
`deploy/monitoring/docker-compose.prom-grafana.yml`.
`config.yml:metrics.enabled=true, use_managed=false` remains the intended
architecture (avoid Amazon Managed Grafana's forced AWS_SSO).

**HA gap**: Single-instance. SPEC §6 requires "ASG or restart policy" for
the self-hosted monitoring stack.

- Minimum: systemd `Restart=always` + EBS PITR + dashboards in git.
- Preferred: 2 EC2s behind an ALB + Prometheus `remote_write` dual-write.

**Routine**: Prometheus retention sized for disk — 15 days × ~1k series
per minute ≈ 5 GB; 30 days ≈ 20 GB. Grafana dashboards + alerting rules
version-controlled; direct UI edits banned (they vanish on rebuild).

---

## 11.11 100k-scale operational baseline

**Cost levers** (Solutions Architect should model these first):

1. Route Bedrock through a VPC Interface Endpoint to skip NAT data-
   processing charges — the biggest single monthly saver at 100k
   tenants.
2. S3 (rootfs, backups) and DDB via VPC Gateway Endpoints (free).
3. SSM, KMS, CloudWatch Logs, and STS via VPC Interface Endpoints —
   reduces both cost and exposure.
4. Reserved Instances / Savings Plans for the ~80% of predictable host
   load. On-demand r8g.metal-24xl is $6.82/hr; a 3-year compute SP
   yields ~55% savings.
5. One NAT GW per AZ, no cross-AZ traffic (default route → local NAT).

**Scaling order**: hosts first (capacity ceiling), then edge (routing
capacity — 3 boxes handle ~30w rps), then ElastiCache (only if QPS
exceeds ~50k/s). ALB and CloudFront scale automatically.

**Drills**:

- ElastiCache manual failover — quarterly.
- Edge single-AZ kill — quarterly (terminate one edge in one AZ;
  observe ASG replacement time and ELB rebalance).
- Host AZ failover — semi-annually (trigger `az_failover`, verify tenant
  migration path).

---

## 11.12 Handover checklist

Run these before taking over operations:

1. `aws sts get-caller-identity` — confirm the account and profile.
2. Pull the latest `stack.py` + `config.yml`; cross-check §11.1-11.10
   against the actual deployment.
3. Load Grafana; if there are no dashboards, run the provisioning script.
4. Execute one manual failover drill (ElastiCache + host AZ).
5. Read the last 30 days of `CHANGELOG.md`.
6. Read `.claude/rules/amazon-production-safety-do-not-delete.md` (never
   delete resources without a snapshot; treat unknowns as production).
