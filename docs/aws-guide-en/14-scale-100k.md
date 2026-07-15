# 14 · 100k-Scale Operations (Test / Rollout / Production)

> This chapter is arranged by **test → rollout → production**, covering the hard constraints that must be respected when running toward 100k microVMs. It **does not repeat** [Chapter 13 · Data-plane two-tier routing](13-data-plane-redesign.md) (architecture) or [Chapter 11 · Component Ops Manual](11-ops-maintenance.md) (day-to-day metrics). It answers three questions: how to make a load test count, how to roll out safely, and which lines never move in production.
>
> Scale baseline: `internal-docs/00-knowledge-base/the data-plane design/the requirements doc.md § 2` (100k microVMs · ~300 hosts · 300k concurrent WS).

---

## 14.1 Test phase · full-load 380 per host + adversarial cases

**Hard requirement** : real-host tests must drive **380 microVMs per host at full load** and include **adversarial cases**, not just happy path.

### Full-load pressure

- **Per-host ceiling**: `r8g.metal-24xl` supports 380 microVMs (2 GB/VM × 380 = 760 GB, matches the 768 GB memory). Test plan: `internal-docs/00-knowledge-base/the data-plane design/the test plan.md`.
- **Four tests that block issue-close** (missing any goes to backlog, not "done"):
  1. **Steady state** — 380 microVMs running, SSE holds 30 min without dropouts.
  2. **Bursty create** — 300 create/s into SQS dispatch; verify the SSM per-instance concurrency stays under the ceiling ( measured: 40 concurrent create hit `TimedOut`, `memory: loadtest-380-ssm-concurrency-bottleneck`).
  3. **Single-AZ down** — kill one AZ of edge + host mixed load; verify the other two AZs absorb traffic and `az_failover` migrates tenants (`config.yml:health_check.az_failover`).
  4. **conntrack near cap** — drive edge + host to `nf_conntrack_max=1048576` neighborhood; verify no packet loss. Edge: `install-edge.sh:131`. Host: `init-host.sh:85-99`.

### Adversarial cases (same rigor as happy path)

Iron law #11 mandates test intensity scales with blast radius on isolation / delete / auth-affecting changes. Minimum coverage:

- **Cross-tenant** — A presents B's tenant_id with A's gateway_token → gateway 401 (EncryptionContext mismatch, `kms:Decrypt` rejects).
- **Token expiry** — after the 900 s TTL, `GET /tenants/{id}/token` returns 410; no plaintext obtainable.
- **Redis brownout** — kill primary; during the 15-30 s failover the edge serves stale from L2 (60 s TTL). Live drill: `aws elasticache test-failover --replication-group-id openclaw-routes --node-group-id 0001`.
- **Port bitmap race** — two host-agent workers alloc concurrently; the `route_ops.alloc_and_dnat_atomic` three-step atomic must prevent collisions.
- **Descriptor drift** — construct DDB descriptor without matching iptables DNAT (or vice versa); `_probe_all` must alarm and self-heal.

### Evidence

All test results must land in `internal-docs/00-knowledge-base/evidence/` — no traces = not tested (the ops guide test discipline).

---

## 14.2 Rollout phase · canary rolling + staged bring-up

**Core discipline** : do not `min_capacity=1` and start hosts before the golden image is ready. **Correct order: min=0 → bake image → then scale up.**

### Cold-start order (fresh region)

1. **VPC and networking first** — `./setup.sh <region> <profile>` runs `deploy/stack.py:_build_vpc(mode=self_managed)` (self-managed /20 + 3 AZ + 3 NAT GW). At this point `host_asg` **min=0**, `edge_asg` **min=0**.
2. **Bake the image** — `build-rootfs.sh --arch arm64`, or in-stack CodeBuild (`image.build_in_stack=true`). Wait for the rootfs in S3.
3. **Scale hosts to minimum** — set `config.yml:asg.min_capacity=2`, re-run `setup.sh`. The rootfs is already in S3, so hosts won't Heartbeat-Timeout-replace (burned on this: `memory: uswest2-deploy-deadlock-and-fixes`).
4. **Scale edge to min=3** — set `config.yml:edge.enabled=true` + `edge.min_capacity=3`, re-run `setup.sh`. Edge userdata polls `/healthz` to 200 before CONTINUE (`install-edge.sh:170-183` warmup gate); the ASG lifecycle hook only lets the instance take traffic once it truly routes.

### Rolling upgrade (image change / stack.py change)

**Image change** (identity, skills, config, guardrail model) = re-bake + rolling refresh (iron law #3):

1. `build-rootfs.sh` bakes new image; bump `image.version` (e.g., `v5.0 → v5.1`).
2. `setup.sh` deploys the CDK update → `aws autoscaling start-instance-refresh --auto-scaling-group-name openclaw-hosts-asg --preferences MinHealthyPercentage=66` (≥ 3-node ASGs keep 2/3 serving during refresh).
3. Watch that `az_failover` does not misfire on refresh jitter (threshold: `config.yml:health_check.az_failover.unhealthy_threshold_minutes=10`).

**stack.py change** (IaC structure, DDB schema, IAM):

- Edit `stack.py` → `setup.sh` runs a CFN update.
- **Irreversible changes** (drop a DDB table / flip RemovalPolicy · alter a security guardrail SG/IAM/credential · alter Guardrail) go through the shared-files protocol serial + human review.
- All DDB tables are `RETAIN` by default (especially `tenants` / `audit` / `tenant-secrets`); take a snapshot before drop (iron law #4).

### Cutting over the data plane (hub-WS → two-tier route)

**Parallel canary landed in bb** (P2 · MR !168 P2b-iac): both paths coexist for now.

- The legacy `/hub/*` rule, HubTargetGroup, and CloudFront `/hub/*` behavior have all been removed from the stack (#187 P5).
- ALB rule priority 20 points `/vm/*` + `/ws/*` → EdgeTG (`deploy/stacks/ha_edge.py:1034-1036`).

The cutover is complete: the data plane's only path is ALB `/vm/*` + `/ws/*` → EdgeTG (OpenResty edge), with the client SDK on `wss /gw/ws` (see `docs/aws-guide-en/13-data-plane-redesign.md § 13.1`).

---

## 14.3 Production phase · six lines that never move

Production = 100k microVMs steady state. Break any of these six lines and you have an incident.

### R1 · conntrack table — hard gate for 400 microVMs/host

- **Value**: `nf_conntrack_max = 1048576` on both edge (`install-edge.sh:131`) and host (`init-host.sh:85-99`).
- **Why**: Ubuntu 22.04 aarch64 kernel default is 262144; a host with 380 microVMs × 5-10 stateful conns/VM + cross-host DNAT + LLM egress trips the default long before the design ceiling.
- **Switchover point** (future density): when per-host DNAT rules exceed ~1000, sequential `iptables PREROUTING` becomes a hot-path bottleneck; migrate to **`nftables sets`** for constant-time lookup. Under 400 rules today the O(n) scan is μs-level and safe. **Backlog only for now.**
- **Monitoring**: `cat /proc/sys/net/netfilter/nf_conntrack_max` (ceiling) + `wc -l /proc/net/nf_conntrack` (usage); usage > 80% ceiling is a warning.

### R2 · Edge ASG elasticity — N-1 floor + 300 s grace

- **Values**: `config.yml:edge.min_capacity=3` (one per AZ, N-1) · `edge.health_check_grace_period_seconds=300` (cold start = apt install openresty + nginx start + Lua warmup + Redis probe retries).
- **Elasticity**: `RequestCountPerTarget` p95 > 2000 rps for 3 min or CPU > 70% → desired += 1; CPU < 30% for 30 min → desired -= 1 (never below min).
- **Failure mode**: three refresh cycles that fail warmup in a row means ElastiCache is not ready or SGs are misconfigured (see `docs/aws-guide-en/11-ops-maintenance.md § 11.3`).

### R3 · Redis primary-endpoint DNS — 30 s TTL enforced

- **Values**: clients (edge nginx + host-agent redis-py) refresh DNS every 30 s. Edge: `deploy/edge/nginx.conf:47-50` `resolver 169.254.169.253 valid=30s ipv6=off`.
- **Not allowed**: hardcoding Redis node IPs. ElastiCache Multi-AZ automatic failover promotes a replica in 15-30 s and updates the primary-endpoint CNAME; a client that doesn't refresh keeps hitting the demoted primary.
- **App-side backstop**: edge fail-static L2 TTL 60 s (`deploy/edge/lib/backend.lua:60` `L2_TTL_SEC=60`) covers the failover window.

### R4 · CloudFront 180 s cap — mandatory 30 s client heartbeat

- **Value**: CloudFront origin `readTimeout` is hard-capped at 180 s (validated by CDK source; unreachable via any config), so ALB idle 3600 s and OpenResty proxy 3600 s cannot rescue an idle WS after 180 s.
- **Requirement**: client SDKs must send a **≤ 30 s heartbeat** (WebSocket ping or SSE keepalive). Otherwise idle WS gets cut by CloudFront invisibly to ALB.
- **Docs**: publish this rule in the customer SDK integration guide; do not assume default clients honor it.

### R5 · KMS permissions minimized — API has no Decrypt

- **Value**: `deploy/stacks/lambdas.py:406-425` — API Lambda role gets `kms:GenerateRandom + Encrypt` only, **no `Decrypt`**. The caller (platform BE) decrypts locally with `EncryptionContext={tenant_id}`.
- **Why**: if the API could `Decrypt`, any accidental delegation of that role would expose all gateway_tokens. Splitting the permission means every CloudTrail `Decrypt` event is guaranteed to come from a caller IAM identity — a policy scope, not a data question.
- **Monitoring**: CloudTrail `Decrypt` events outside the expected IAM principals alarm at critical.

### R6 · NAT GW vs VPC Endpoint split — 100k-scale cost lever

- **Value**: one NAT GW per AZ (`deploy/stacks/_helpers.py:52 nat_gateways=3`), no cross-AZ egress.
- **Cost knife-edge at 100k**: route Bedrock / S3 / DDB / KMS through **VPC Interface / Gateway Endpoints** to skip NAT data-processing charges (0.045 USD/GB). S3 and DDB Gateway Endpoints are free; Bedrock / KMS Interface Endpoints charge per-AZ hourly + per-GB. For 100k tenants at 100 GB/day Bedrock: NAT ≈ $4500/month, VPCE ≈ few hundred.
- **Monitoring**: NAT GW `ErrorPortAllocation > 0` is critical (data plane starts dropping; add a secondary EIP or a VPC Endpoint).

---

## 14.4 Drill cadence (semi-annual rotation)

Once in production, establish drills — don't wait for real incidents. Recommended cadence:

| Scenario | Frequency | Command / Steps |
|---|---|---|
| ElastiCache manual failover | **Quarterly** | `aws elasticache test-failover --replication-group-id openclaw-routes --node-group-id 0001` — observe fail-static trigger duration < 60 s |
| Edge single-AZ kill | **Quarterly** | Terminate one AZ's edge instance — observe ASG replacement + ELB rebalance time |
| Host AZ failover | **Semi-annual** | Trigger `az_failover` (manually disable host status in one AZ) — verify tenant migration path |
| CloudFront long-idle cap | **Quarterly** | Hold an idle WS 200 s — observe whether CloudFront cuts at 180 s — verify client-heartbeat SDK deployment |
| Guardrail intercept sampling | **Monthly** | Sample OWASP top-10 through the guardrail — 14/14 blocked is baseline — any regression escalates |

All drill results land in `internal-docs/00-knowledge-base/evidence/`.

---

## 14.5 Cross-references

- Architecture: [Chapter 13 · Data-plane two-tier routing](13-data-plane-redesign.md).
- Component ops (alert thresholds, scaling triggers, troubleshooting): [Chapter 11 · Component Ops Manual](11-ops-maintenance.md).
- Private API hardening: [Chapter 12 · Private API Gateway](12-private-api-hardening.md).
- HA audit (15 components, fixed vs still-single): [`internal-docs/00-knowledge-base/the data-plane design/HA-AUDIT-DRAFT.md`](../../internal-docs/00-knowledge-base/the data-plane design/HA-AUDIT-DRAFT.md).
- Handover (new-engineer onramp): [`internal-docs/03-collaboration/HANDOVER.md`](../../internal-docs/03-collaboration/HANDOVER.md).
