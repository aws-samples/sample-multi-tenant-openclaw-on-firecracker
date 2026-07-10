# 13 · 数据面两级路由(2026-07-08 转型后)

> 本章描述 2026-07-08 数据面去中枢化改造后的实时聊天链路。**代替** [03-架构详情](03-architecture-details.md) 里"实时聊天中枢 claw-hub"一节的旧模型(hub-WS + claw-channel 出站拨号 + Cognito 三处身份 + HMAC channel_secret 等)。改造缘由与影响面见项目决策记录;权威接口契约见项目接口契约文档。运维手册(监控、告警、故障排查)见 [第 11 章 · 组件运维手册](11-ops-maintenance.md),本章不重复。

## 13.1 端到端数据面链路

外部聊天流量走一条两级路由链,不经过任何 hub 中转,microVM 内的 OpenClaw gateway 是聊天的唯一后端:

```
浏览器 ── wss ─▶ 平台后端 /gw/ws ── HTTP+SSE ─▶ Amazon CloudFront ─▶ Application Load Balancer(LOR 单默认规则)
                                                                                                │
                                                                              (跨 3 AZ)         ▼
                                                                                       OpenResty edge ASG(3 台)
                                                                                                │
                                                                                 查 Amazon ElastiCache Redis
                                                                                        route:{tenant_id}
                                                                                        {host, port, guest_ip}
                                                                                                │
                                                                                     ┌──────────┴──────────┐
                                                                                     ▼                     ▼
                                                                          host:host_port(跨机)      guest_ip:18789(本机)
                                                                          iptables PREROUTING DNAT   直投 tap 网卡
                                                                                     └──────────┬──────────┘
                                                                                                ▼
                                                                                    microVM OpenClaw gateway :18789
                                                                                    POST /v1/chat/completions(SSE)
```

代码 file:line 落地:OpenResty 侧 `deploy/edge/nginx.conf`(80 行 server 段 + 三级缓存 + 单一 `microvm_gateway` 上游)、`deploy/edge/route.lua`(rewrite/balancer/init_worker 三阶段)、`deploy/edge/lib/backend.lua`(L1 lrucache + L2 shared_dict + L3 Redis 三级缓存,fail-static 60s 兜住 ElastiCache failover);host 侧 `deploy/userdata/host-agent.py`(端口位图 10000-10400 + iptables DNAT + descriptor 双写 Redis);IaC 侧 `deploy/stack.py`(EdgeASG + ElastiCache Multi-AZ replication group + ALB LOR)。

## 13.2 身份认证(token-only,唯一凭据)

OpenClaw gateway 端 `gateway.auth.mode=token` + `gateway.controlUi.enabled=false`(templates/openclaw.json:10-17);唯一鉴权 = per-租户 `Authorization: Bearer <gateway_token>`。token 生命周期:

1. **铸造(mint)**:控制面 `POST /tenants` 时,`deploy/lambda/api/services/tenant_service.py:162-214 mint_gateway_token` 走 KMS GenerateRandom 32B → base64url 编码 → `kms_envelope.encrypt_with_tenant(plaintext, tenant_id, ClawPoolCMK)` 信封加密,EncryptionContext={"tenant_id":<id>} → 存 `openclaw-tenant-secrets` DDB 表(schema 见 stack.py),`expires_at=now+900`。
2. **注入 microVM**:密文以 launch-vm 位置 12 参数传入 `deploy/userdata/launch-vm.sh`(#187 P1),host 侧 kms:Decrypt(EC={tenant_id})取明文,写入只读盘上 `openclaw.json .gateway.auth.token`。**明文永不落在 host 磁盘,永不进 SSM 命令,永不入 CloudTrail**。
3. **调用方拿密文**:平台后端调 `GET /tenants/{id}`,response.status=`running` 时响应体自动带 `gateway_token` 字段(base64 KMS 信封密文;`handler.py:346-357` 引用 `tenant_service.read_gateway_token_ct`)。这是取 token 的唯一途径(专用 `GET /tenants/{id}/token` 端点已删除)。API Lambda 不 decrypt、也无 kms:Decrypt 权限;调用方拿到密文后本地 Decrypt(EncryptionContext={"tenant_id":<id>})取明文。
4. **窗口**:密文表 DDB TTL 900s(15min);过期后 `gateway_token` 字段从 GET 响应消失,需重铸(SPEC §7.1 开放问题)。

**已删除**:hub-WS(claw-hub) · claw-channel 出站拨号 · Cognito 三处身份(user pool + 两个 app client + `custom:tenant_user_id`)· POST /chat/sign HMAC 会签 · `channel_secret`。

## 13.3 边缘路由:OpenResty edge ASG

**部署形态**:独立 ASG(不与 host ASG 混),跨 3 AZ,min=3 desired=3(N-1 容灾),`health_check_type=ELB`。userdata=`deploy/edge/install-edge.sh`:装 OpenResty(Ubuntu apt / AL2023 dnf,arm64/x86 都支持)→ 从 IMDS 拿 local-ipv4 templated 进 nginx.conf → sysctl 调优 → systemd `claw-edge.service` 起 → **userdata 尾部轮询 `/healthz` 到 200 才 exit 0**,ASG lifecycle hook 只在 warmup 完成后 CONTINUE(SPEC §6 硬要求)。

**三级路由缓存**(`deploy/edge/lib/backend.lua`):

| 层  | 存储                                      | TTL     | 用途                                                      |
| --- | ----------------------------------------- | ------- | --------------------------------------------------------- |
| L1  | worker-local `resty.lrucache`(4000 cap)   | 5s 抖动 | 每 worker 热路径纳秒命中                                  |
| L2  | `lua_shared_dict route_cache 128m`        | 60s     | 跨 worker 共享 + fail-static 兜 ElastiCache failover 窗口 |
| L3  | ElastiCache Redis `GET route:{tenant_id}` | —       | 权威源(host-agent 双写)                                   |

L3 miss 时 `resty.lock` 单飞回源(防 stampede);Redis 不可达时 L2 保底服务旧值(fail-static)。**L2 TTL 60s 是量化下限**——INTERFACE-CONTRACT §8 明确 "≥ 预期最长 failover 窗口(建议 ≥30-60s)",ElastiCache Multi-AZ automatic failover 通常 15-30s。

**DNS 与连接层**:nginx.conf 有 `resolver 169.254.169.253 valid=30s ipv6=off`;`lua-resty-redis` `set_keepalive(60000ms, 100)`(池化短生命)。禁止硬编码 Redis 节点 IP;必须用 primary endpoint DNS(AWS 侧 failover 时更新 CNAME)。

## 13.4 宿主 DNAT + 端口位图

`deploy/userdata/host-agent.py`(host-agent worker 串行):

- **端口段**:10000-10400(401 个槽,匹配单机 ≤400 microVM;`INTERFACE-CONTRACT §3`)。
- **分配**:本机位图 + `iptables -C` 冲突检测三步原子(mutex 串行,防并发撞端口)。
- **DNAT 建**:`iptables -t nat -A PREROUTING -p tcp --dport <host_port> -j DNAT --to-destination <guest_ip>:18789`。
- **descriptor 双写**:VM 探活 + gateway token 验证通过后,`先写 DDB 后写 Redis`;delete/migrate 时 `DEL route:{tenant_id}` + 对称撤 DNAT + 释放位图槽。
- **\_probe_all 对账**:VM 进程集 vs DDB descriptor vs iptables DNAT 规则集三方差集告警/修复,防长期漂移。

## 13.5 超时链(SSE / WS 长连接)

三层齐平不是"3600s 全链路",AWS 有硬约束:

| 层                                                    | 值    | 依据                                                                                                                                             |
| ----------------------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| CloudFront origin `readTimeout`                       | 120s  | 账号配额 L-AECE9FA7(Response timeout per origin)默认 120s,写 180 真机 CreateDistribution 400(2026-07-08 实撞);CDK 校验上限 180s,要用满须先提配额 |
| CloudFront origin `keepaliveTimeout`                  | 60s   | AWS 硬上限;同 CDK 源码 :77                                                                                                                       |
| ALB `idle_timeout`                                    | 3600s | 可配 1-4000s,SPEC §6 设 3600s(SSE/WS 长连接需要)                                                                                                 |
| OpenResty `proxy_send_timeout` / `proxy_read_timeout` | 3600s | `deploy/edge/nginx.conf` :171-172                                                                                                                |

**关键**:CloudFront 180s 是天花板。SSE 场景常规不会 180s 无 token,业务侧不用担心;**WS 长静默(两条消息间隔 >180s)会经 CloudFront 断,ALB/OpenResty 无法补救**——客户端必须 **30s 内发心跳**(比如 `type:"ping"` 帧)。

## 13.6 十万级规模化基线(设计目标)

10 万 microVM · 约 300 host(r8g.metal-24xl,每台 380 稳态)· 30 万并发 WS · 3 台 c6in.xlarge/c7g.xlarge edge 起步。核心约束:

- **NFR-3 内核**:单机 400 microVM 时 `nf_conntrack_max = 1048576`(默认 262144 会撞),edge + host 都设。edge 在 `install-edge.sh:131`,host 在 `init-host.sh:85-99`。
- **NFR-2 首字节时延**:T0(VM 启动完)→T2(SSE 200 首字节)≤ 1.5s,由 route.lua L1 命中主导。
- **NFR-1 接管**:单 edge 挂,余 2 台 LOR 平滑接管;客户端重连 ≤ 15s。ASG `health_check_grace_period=300s` 给冷启 openresty apt 装 + Lua warmup 全过程用。
- **NFR-4 隔离**:跨租户 100% 丢包(SPEC + FACT-BASELINE 实测);edge 路径不引入新越权面,唯一鉴权还是 gateway token。
- **NFR-6 内存**:OpenResty / host-agent 72h 浸泡 RSS 上升后拉平,波动 ≤ 5%。
- **iptables O(n) 匹配预警**:单机 380 条 DNAT 规则,`iptables -t nat -L PREROUTING` 顺序匹配,常态 400 条内 O(n) 走完在 μs 级,不是热路径瓶颈;超过 1000 条(未来密度)考虑迁 `nftables sets` 常数复杂度查找。**当前不做,记 backlog**。
- **CloudFront 客户端心跳**:30s 一次(避 180s 断连)。SDK 接入指南必须写清。

上线纪律、监控告警、故障排查、扩缩容触发条件、AZ failover 演练等运维内容全部在 [第 11 章 · 组件运维手册](11-ops-maintenance.md),不在本章重复。

## 13.7 与旧描述的差异对照(升级参考)

| 项              | 旧(2.x hub-WS)                                                                                | 新(3.x 数据面重构)                                                                                |
| --------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 前端 → microVM  | wss `/hub/ws` → hub → claw-channel 出站 → agent                                               | wss `/gw/ws`(平台后端) → HTTP+SSE → CloudFront → ALB → OpenResty → microVM gateway                |
| microVM 到 host | claw-channel 主动出站(零入站)                                                                 | 接入 `iptables PREROUTING DNAT` 从 host_port 转发到 :18789(microVM 只对 host 内部 tap 暴露 18789) |
| 身份            | Cognito × 3(user pool + 入口/channel appclient + custom:tenant_user_id) + HMAC channel_secret | gateway token(KMS 密文调用方自解 + microVM 冷注入明文)                                            |
| 中转层          | claw-hub(自建 WS 中枢,EKS 上 3 pod)                                                           | OpenResty edge ASG(3 台 EC2,独立 ASG)                                                             |
| 路由权威源      | hub 内存 + owner 校验                                                                         | ElastiCache Redis `route:{tenant_id}` 三级缓存                                                    |
| 出图/看图链路   | claw-hub presign S3                                                                           | 待重设计(留 SPEC §7.2 开放问题;当前样本 chat demo 不含图)                                         |

**已合入 bb 的 phase MR**(2026-07-08):P1 控制面预铸 token(MR !… 见 `progress/p1-controlplane.md`) · P2-edge 三件套(nginx.conf/route.lua/install-edge.sh) · P2b-host(host-agent 端口位图 + DNAT) · P2b-iac(stack.py EdgeASG + ElastiCache + ALB) · P3 镜像 v5(去 channel) · P4-①② demo/前端切换。P4-③(edge admin console)与 P5-P7 在后续 phase 落地。
