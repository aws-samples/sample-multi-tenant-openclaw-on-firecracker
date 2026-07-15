# Wazuh 监控数据平台部署 Runbook(10h-goal #20)

> 把三路安全/运维信号聚到一个平台:① microVM **in-guest** 的 auditd + FIM 告警(镜像已烤,见 `build-rootfs.sh:349-420`,reverse-shell 规则 100210 / 敏感文件改 100110 实测命中)② AWS **GuardDuty** 云层威胁检测(CDK 已启,见 `deploy/stack.py` security 段)③ **openclaw 运行时 metrics**(host-agent `/metrics` → AMP → Grafana)。每步带验证;凭据从不硬编码。

## 架构(三源聚合)

```
microVM(guest) ── auditd + openclaw-fim.sh ──┐
                                              ├─→ Wazuh agent → Wazuh manager → indexer → dashboard
host EC2 ─────── (可选 host 级 wazuh agent) ──┘                        ↑
AWS GuardDuty ── Finding → EventBridge → SNS → SQS → custom integration ┘
host-agent /metrics ── ADOT remote-write → AMP ── Grafana(并排看运行时指标)
```

in-guest HIDS(Wazuh)和 GuardDuty(云层)互补:前者看进程/文件/syscall,后者看 VPC/DNS/EC2 行为。

## 1. 起 Wazuh manager+indexer+dashboard(监控 EC2)

在一台专用监控 EC2(非 metal host,隔离爆炸半径)上:

```bash
# 凭据进 .env(不提交);用强随机,不用默认。
cat > deploy/monitoring/.env <<EOF
WAZUH_INDEXER_PASSWORD=$(openssl rand -base64 24)
WAZUH_DASHBOARD_PASSWORD=$(openssl rand -base64 24)
EOF
chmod 600 deploy/monitoring/.env
docker compose --env-file deploy/monitoring/.env -f deploy/monitoring/docker-compose.wazuh.yml up -d
```

> 验证:`docker compose ps` 三个容器 Up;`curl -sk https://localhost:55000/` 返回 Wazuh API banner;dashboard `https://<监控EC2>:443`(生产前置 ALB+ACM,SG 锁办公/VPN IP,**绝不 0.0.0.0 裸开**)。

## 2. 证书与凭据(不硬编码)

首次部署用 Wazuh 官方 `wazuh-certs-tool.sh` 生成 indexer/manager/dashboard 的 mTLS 证书(放 docker volume 或挂载),密码走上面的 `.env`(`openssl rand`)。**绝不把密码/证书写进 compose 或提交 git**。生产建议密码进 Secrets Manager,启动时注入。

> 验证:`docker compose logs wazuh-indexer | grep -i "Security is enabled"`;用默认密码登录应失败。

## 3. 把 in-guest auditd/FIM 告警接进来

microVM 镜像已烤 auditd + `openclaw-fim.sh`(build-rootfs)。让它们转发到 manager 的两条路(任选/并用):

- **A. Wazuh agent(推荐)**:在 build-rootfs 的镜像里装 `wazuh-agent`,`ossec.conf` 指向 manager `1514`,enrollment 走 `1515`。agent 跑在 guest 内、agent 进程对 OpenClaw agent 不可见(同 the reference platform Wazuh-in-ns 思路)。注册用 per-tenant key,不复用。
- **B. 无 agent(轻量)**:FIM/auditd 告警已落 guest 内日志,host-agent 旁路把这些行经 manager syslog `514/udp` 转发(不下沉凭据到 guest)。
  > 验证:在测试 microVM 内触发 reverse-shell 探测(规则 100210)或改一个受保护身份文件(规则 100110),Wazuh dashboard 的 Security events 里**几秒内出现该 alert**(rule.id 100210/100110)。这是端到端真验,不是看进程起没起。

## 4. 自定义规则(匹配我们的 in-guest 告警)

`deploy/monitoring/wazuh-rules/openclaw_local_rules.xml`(挂载进 manager)把我们 FIM 脚本的输出映射成 Wazuh rule.id + 等级,见该文件。

## 5. 接 GuardDuty Finding(云层)

CDK 已把 GuardDuty Finding 经 EventBridge 路由到 SNS(`security.guardduty_enabled=true` 时)。把 SNS 订到一个 SQS,跑一个小 integration(Wazuh `custom-` integration 或 Lambda)把 Finding JSON 灌进 manager 的 `1514`(decoder 解析为 `aws.guardduty` 事件)。

> 验证:GuardDuty 控制台生成一个 sample finding(`aws guardduty create-sample-findings`),Wazuh dashboard 出现 `aws.guardduty` 来源事件。

## 6. openclaw 运行时 metrics(已有,并排展示)

host-agent `/metrics` → ADOT → AMP → Grafana(`deploy/stack.py` AMP/AMG 段,`metrics.enabled=true`)。Grafana 与 Wazuh dashboard 分工:Grafana 看容量/内存/磁盘/balloon 运行时曲线,Wazuh 看安全告警。可在 Grafana 加 Wazuh indexer 作 OpenSearch 数据源,单屏并看。

## 安全边界

- Wazuh dashboard/API 绝不对 0.0.0.0 开;ALB+ACM+SG 锁 VPN/办公 IP(对齐项目暴露红线)。
- 监控 EC2 独立 SG,只接受 agent 1514/1515 与管理端口;与 metal host 网络隔离。
- 删监控 EC2 前快照(铁律 #4)。
- 凭据全走 `.env`/Secrets Manager,git 里零明文。

## 现状标注(诚实)

- ✅ 真实可部署:compose 栈、自定义规则、GuardDuty→SNS(CDK)、AMP/Grafana(已有)。
- ⏳ 待真机:在 146 监控 EC2 上 `docker compose up` + 测试 VM 装 wazuh-agent 跑通端到端告警上屏。这步是基础设施部署(需起监控 EC2),runbook 给确切步骤,真机执行时按 §3 验证告警上屏。
