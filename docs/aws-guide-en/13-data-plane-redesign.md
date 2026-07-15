# 13 · Data-plane two-tier routing (post-2026-07-08 redesign)

> This chapter describes the real-time chat data-plane after the 2026-07-08 decentralization redesign. It **supersedes** the "claw-hub real-time chat" section in [03 Architecture Details](03-architecture-details.md) (the old hub-WS + `claw-channel` outbound dial + triple Cognito identity + HMAC `channel_secret` model). Rationale: customer OIDC does not support headless/browserless login, so Cognito and the outbound-dial hub were dropped in favor of OpenClaw-native gateway auth. Ops (monitoring, alerts, troubleshooting) lives in [Chapter 11 Component Ops Manual](11-ops-maintenance.md); not duplicated here.

## 13.1 End-to-end data-plane

Chat traffic follows a two-tier route with no hub relay. The microVM's OpenClaw gateway is the sole backend:

```
Browser ── wss ─▶ Platform BE /gw/ws ── HTTP+SSE ─▶ Amazon CloudFront ─▶ ALB(LOR, single default rule)
                                                                                            │
                                                                             (3-AZ)         ▼
                                                                                     OpenResty edge ASG (3 nodes)
                                                                                            │
                                                                          Amazon ElastiCache Redis GET
                                                                              route:{tenant_id}
                                                                              {host, port, guest_ip}
                                                                                            │
                                                                                 ┌──────────┴──────────┐
                                                                                 ▼                     ▼
                                                                       host:host_port (cross-host)  guest_ip:18789 (local)
                                                                       iptables PREROUTING DNAT     direct to tap NIC
                                                                                 └──────────┬──────────┘
                                                                                            ▼
                                                                             microVM OpenClaw gateway :18789
                                                                             POST /v1/chat/completions (SSE)
```

Code file:line: OpenResty side in `deploy/edge/nginx.conf` (single `microvm_gateway` upstream + 3-tier cache) and `deploy/edge/route.lua` (rewrite/balancer/init_worker phases) and `deploy/edge/lib/backend.lua` (L1 lrucache + L2 shared_dict + L3 Redis + fail-static). Host side: `deploy/userdata/host-agent.py` (port bitmap 10000-10400 + iptables DNAT + descriptor double-write to Redis). IaC side: `deploy/stack.py` (EdgeASG + ElastiCache Multi-AZ replication group + ALB LOR).

## 13.2 Auth (token-only, single credential)

`gateway.auth.mode=token` + `gateway.controlUi.enabled=false` on the OpenClaw gateway (`templates/openclaw.json:10-17`). Sole authorization is per-tenant `Authorization: Bearer <gateway_token>`. Token lifecycle:

1. **Mint**: on `POST /tenants`, `deploy/lambda/api/services/tenant_service.py:162-214 mint_gateway_token` calls KMS GenerateRandom 32B → base64url → `kms_envelope.encrypt_with_tenant(plaintext, tenant_id, ClawPoolCMK)` with `EncryptionContext={"tenant_id":<id>}` → PUT the ciphertext into the `openclaw-tenant-secrets` DDB table with `expires_at=now+900`.
2. **Inject into microVM**: the ciphertext travels as launch-vm positional arg #12 to `deploy/userdata/launch-vm.sh`. On the host, `kms:Decrypt(EC={tenant_id})` recovers the plaintext into the read-only disk's `openclaw.json .gateway.auth.token`. Plaintext never touches host disk beyond that file, never appears in SSM commands, never lands in CloudTrail.
3. **Callers fetch ciphertext**: the platform backend calls `GET /tenants/{id}`; when `status=running` the response body includes a `gateway_token` field (base64 KMS ciphertext; `handler.py:346-357`, via `tenant_service.read_gateway_token_ct`). This is the only way to fetch the token — the dedicated `GET /tenants/{id}/token` endpoint has been removed. **The API Lambda never decrypts** and does not hold `kms:Decrypt` — the caller decrypts locally with the same EncryptionContext.
4. **Window**: DDB TTL is 900s (15 min). After expiry the `gateway_token` field drops out of the `GET` response and the token must be re-minted (SPEC §7.1 open question).

**Removed by this redesign**: hub-WS (`claw-hub`), `claw-channel` outbound dial, the three Cognito identities (user pool + two app clients + `custom:tenant_user_id`), `POST /chat/sign` HMAC signing, `channel_secret`.

## 13.3 Edge routing: the OpenResty edge ASG

**Shape**: dedicated ASG (not mixed with the host ASG), 3 AZs, min=3 desired=3 (N-1), `health_check_type=ELB`. Userdata `deploy/edge/install-edge.sh` installs OpenResty (Ubuntu apt / AL2023 dnf, arm64 + x86), templates `local-ipv4` from IMDS into nginx.conf, tunes sysctl, starts the `claw-edge.service` systemd unit, and **polls `/healthz` until it returns 200 before exiting 0**. The ASG lifecycle hook only signals CONTINUE after that warmup gate (SPEC §6).

**Three-tier route cache** (`deploy/edge/lib/backend.lua`):

| Tier | Storage                                   | TTL         | Purpose                                                   |
| ---- | ----------------------------------------- | ----------- | --------------------------------------------------------- |
| L1   | worker-local `resty.lrucache` (cap 4000)  | 5s ± jitter | per-worker hot path (ns hits)                             |
| L2   | `lua_shared_dict route_cache 128m`        | 60s         | cross-worker + fail-static cover for ElastiCache failover |
| L3   | ElastiCache Redis `GET route:{tenant_id}` | —           | authoritative (host-agent double-writes)                  |

On L3 miss, `resty.lock` single-flights the origin fetch (stampede shield). When Redis is unreachable, L2 serves stale (fail-static). **The 60s L2 TTL is a quantified lower bound** — it must be "≥ the longest expected failover window (recommended ≥30-60s)" and ElastiCache Multi-AZ automatic failover typically takes 15-30s.

**DNS + connection layer**: `resolver 169.254.169.253 valid=30s ipv6=off;`; `lua-resty-redis set_keepalive(60000ms, 100)`. Never hard-code Redis node IPs — always use the primary-endpoint DNS name, which AWS updates on failover.

## 13.4 Host DNAT + port bitmap

`deploy/userdata/host-agent.py` (single-worker serial):

- **Port range**: 10000-10400 (401 slots, one per microVM ≤ 400).
- **Allocation**: local bitmap + `iptables -C` conflict check under mutex — three steps atomic to prevent concurrent double-alloc.
- **DNAT insertion**: `iptables -t nat -A PREROUTING -p tcp --dport <host_port> -j DNAT --to-destination <guest_ip>:18789`.
- **Descriptor double-write**: after VM probe + gateway-token verification, DDB first, Redis second. On delete/migrate, `DEL route:{tenant_id}` + symmetric DNAT `-D` + slot release.
- **\_probe_all reconciler**: three-way diff between the running VM set, DDB descriptors, and iptables rules. Any drift → alarm + repair.

## 13.5 Timeout chain (SSE / WS long connections)

Alignment across the chain is bounded by AWS hard limits — 3600s is not achievable end-to-end:

| Layer                                                 | Value | Source                                                                                                                                                                                                                  |
| ----------------------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CloudFront origin `readTimeout`                       | 120s  | Account quota L-AECE9FA7 (Response timeout per origin) defaults to 120s; 180 fails CreateDistribution with 400 (verified on real deploy 2026-07-08). CDK validates up to 180s; request a quota increase before raising. |
| CloudFront origin `keepaliveTimeout`                  | 60s   | AWS hard cap; same CDK source, line 77.                                                                                                                                                                                 |
| ALB `idle_timeout`                                    | 3600s | Configurable 1-4000s; SPEC §6 sets 3600s for SSE/WS long connections.                                                                                                                                                   |
| OpenResty `proxy_send_timeout` / `proxy_read_timeout` | 3600s | `deploy/edge/nginx.conf:171-172`                                                                                                                                                                                        |

**Consequence**: CloudFront is the ceiling. SSE rarely idles past 180s (a token stream keeps bytes flowing), so business impact is negligible. **WS with >180s silence between messages will be cut by CloudFront regardless of downstream timeouts.** The client must send a **30s heartbeat** (for example a `type:"ping"` frame). Publish this in the client SDK guide.

## 13.6 100k-scale baseline (design targets)

100k microVMs · ~300 hosts (r8g.metal-24xl, 380 tenants each in steady state) · 300k concurrent WebSockets · 3 x c6in.xlarge / c7g.xlarge edge boxes to start. Hard constraints:

- **NFR-3 kernel**: `nf_conntrack_max = 1048576` on both edge and host at 400 microVMs/box. The kernel default of 262144 does not survive. Edge: `install-edge.sh:131`. Host: `init-host.sh:85-99`.
- **NFR-2 first-byte latency**: T0 (VM ready) → T2 (first SSE byte) ≤ 1.5s, dominated by L1 cache hits in route.lua.
- **NFR-1 handover**: one edge box down leaves 2 to LOR the traffic; client reconnect ≤ 15s. ASG `health_check_grace_period=300s` covers apt install + Lua warmup.
- **NFR-4 isolation**: 100% cross-tenant packet drop measured (SPEC + FACT-BASELINE). Edge path does not introduce a new authorization surface — the gateway token remains the sole credential.
- **NFR-6 memory**: 72h soak; OpenResty and host-agent RSS should plateau within ±5%.
- **iptables O(n) match — early warning**: 380 DNAT rules per host, sequential `PREROUTING` scan, still μs-level under 400. Above ~1000 rules (future density), migrate to `nftables sets` for constant-time lookup. **Not done now — logged as backlog.**
- **CloudFront client heartbeat**: 30s (avoids the 180s cut). Publish in the SDK integration guide.

Operational rigor (rolling deploys, monitoring, alert thresholds, scaling triggers, AZ failover drills) belongs in [Chapter 11 Component Ops Manual](11-ops-maintenance.md).

## 13.7 Delta vs. the old hub-WS model

| Aspect               | Old (2.x hub-WS)                                                                                     | New (3.x decentralized data plane)                                                                  |
| -------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Browser → microVM    | wss `/hub/ws` → claw-hub → outbound claw-channel → agent                                             | wss `/gw/ws` (platform BE) → HTTP+SSE → CloudFront → ALB → OpenResty → microVM gateway              |
| microVM ingress      | claw-channel dials out (zero inbound)                                                                | `iptables PREROUTING DNAT` from `host_port` to `:18789` (microVM only accepts on host-internal tap) |
| Identity             | 3× Cognito (user pool + entry / channel app clients + `custom:tenant_user_id`) + HMAC channel_secret | gateway token (KMS ciphertext for callers + cold-injected plaintext in microVM)                     |
| Middle tier          | claw-hub (custom WS multiplexer, 3 EKS pods)                                                         | OpenResty edge ASG (3 EC2s, dedicated ASG)                                                          |
| Route authority      | Hub in-memory + owner check                                                                          | ElastiCache Redis `route:{tenant_id}` + 3-tier cache                                                |
| Image/media pipeline | claw-hub presigned S3                                                                                | To be redesigned (SPEC §7.2 open question; current sample chat demo has no image feature)           |

**Components landed (2026-07-08)**: P1 control-plane token pre-mint, P2-edge trio (nginx.conf / route.lua / install-edge.sh), P2b-host (port bitmap + DNAT), P2b-iac (stack.py EdgeASG + ElastiCache + ALB), P3 image v5 (channel removal), P4-①② demo / frontend cutover. P4-③ (edge admin console) and P5-P7 land in follow-up phases.
