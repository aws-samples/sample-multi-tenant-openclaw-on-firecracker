# 自建 Prometheus + Grafana 运行时监控 Runbook

> 在一台监控 EC2 上用 docker-compose 起 Prometheus + Grafana,抓各 metal host 的 host-agent `:8899/metrics`,并排展示 microVM fleet 的 VM 数、内存、CPU、磁盘、健康度。这是 AMP(Managed Prometheus)+ AMG(Managed Grafana)的**自托管替代**——AMG 强制走 IAM Identity Center(SSO),本环境没配 SSO,故自托管。每步带验证;凭据从不硬编码。

## 为什么自托管(不用 AMP/AMG)

AMG 登录强制 SSO。本环境没接 SSO,所以改用 Grafana 自带 admin 账号(密码走环境变量)。原 metrics 链路是 `host-agent → ADOT collector → AMP → AMG`(见 `deploy/userdata/init-host.sh:163-186`,`metrics.enabled=false` 时不装 ADOT)。本栈直接让 Prometheus 抓 host-agent,免 ADOT/AMP/SSO 一整条。

## 架构

```
metal host A ── host-agent :8899/metrics (openclaw_vm_* 7 个 gauge) ──┐
metal host B ── host-agent :8899/metrics ─────────────────────────────┤
   ...                                                                  ├─→ Prometheus (ec2_sd 自动发现, scrape 30s)
metal host N ── host-agent :8899/metrics ─────────────────────────────┘            │
                                                                                    ↓
                                                                              Grafana (provisioned datasource + dashboard)
监控 EC2 自身 ── node_exporter :9100 ─────────────────────────────────────→ Prometheus (自监控)
```

host-agent 在每台 metal host 的**私网 IP:8899**(与 `/health` 同端口,见 `host-agent.py:39-42`)。所以监控 EC2 必须与 metal host **同 VPC**,且 metal host 的 SG 入站放行监控 EC2 来源的 8899(见 §2)。

## host-agent 真实导出的指标(抓取源,不编造)

来源 `deploy/userdata/host-agent.py:50-93`(`_PROM_GAUGES` + `_render_metrics_text`)。全部是 gauge,host-agent 侧只打一个 `tenant` label;`instance`/`az`/`host_name` 由 Prometheus ec2_sd relabel 注入(见 `prometheus.yml`):

| 指标名                           | 含义                           | label  |
| -------------------------------- | ------------------------------ | ------ |
| `openclaw_vm_memory_used_mb`     | 单 VM 活跃内存 (MB)            | tenant |
| `openclaw_vm_memory_balloon_mib` | host 持有的 balloon 大小 (MiB) | tenant |
| `openclaw_vm_disk_used_mb`       | 单 VM 数据盘已用 (MB)          | tenant |
| `openclaw_vm_disk_total_mb`      | 单 VM 数据盘容量 (MB)          | tenant |
| `openclaw_vm_disk_used_pct`      | 单 VM 数据盘已用 (%)           | tenant |
| `openclaw_vm_cpu_pct`            | 单 VM CPU 占用 (% of vcpus)    | tenant |
| `openclaw_vm_health`             | VM ping 通=1 否则=0            | tenant |
| `openclaw_app_health`            | 租户 gateway 应答 HTTP=1 否则=0（#526 起对 chat_ep=1 的租户探 /v1/chat/completions，404=端点缺失=0） | tenant |

> 注意:host-agent **没有**导出 "VM 总数" / "健康节点数" 这类标量指标。dashboard 里这两个数是 PromQL 在每 VM 一条 `tenant` 序列上 `count()`/`sum()` 聚合出来的(VM 数 = `count(openclaw_vm_health)`,健康数 = `sum(openclaw_vm_health)`)。"各 host" 维度靠 `instance` label(ec2_sd 注入)分组。

### VM 与 Gateway 必须分开看

`openclaw_vm_health` 只表达 ICMP 可达，`openclaw_app_health` 只表达 gateway 是否应答 HTTP；两者是独立事实源，不能用 ping 推导 gateway 健康。Gateway 异常数用 `count(openclaw_app_health) - sum(openclaw_app_health)`；定位「VM 可达但 Gateway 不健康」盲区用 `openclaw_vm_health == 1 and on(job, instance, tenant) openclaw_app_health == 0`，完整匹配 `job`/`instance`/`tenant` 避免多 host 或迁移残留跨实例误配。

## 1. 起一台监控 EC2

机型 `c7i.large` 或 `t3.large`(2 vCPU/4-8GB 起步,Prometheus TSDB + Grafana 够用;留 15 天数据见 compose `retention.time`)。AMI 用 AL2023。两条路:

**A. 一键脚本(推荐)** —— `setup-monitoring-ec2.sh` 建 SG(只对 VPC CIDR 开)+ instance role(ec2:DescribeInstances 只读 + S3 只读)+ 起 EC2,userdata 自动装 docker/compose、拉资产、生成 admin 密码、compose up:

```bash
# 先把本目录资产 sync 到 S3(setup.sh 部署时会做;或手动):
aws s3 sync deploy/monitoring/ s3://<ASSETS_BUCKET>/deployment/monitoring/ \
  --exclude ".env" --exclude "*.bak*"
# 起监控 EC2(VPC/subnet 用与 metal host 同一个 VPC):
VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ASSETS_BUCKET=<bucket> REGION=ap-southeast-1 \
  bash deploy/monitoring/setup-monitoring-ec2.sh
```

> 验证:脚本打印 `launched monitoring EC2: i-xxx` 与 `SG=sg-xxx ingress 9090/3000 <- <VPC CIDR> only`。等 2-3 分钟 userdata 跑完。

**B. 手工(已有 EC2)** —— AL2023 上装 docker + compose,把本目录拷到 `/opt/monitoring/`,然后走 §3 起服务。

## 2. SG 规则(硬红线:绝不 0.0.0.0/0)

监控 EC2 一个**独立 SG**,入站只允许:

- **9090 (Prometheus) / 3000 (Grafana)**:源**只能**是 VPC CIDR、堡垒机 SG,或办公/VPN 的 /32。**绝不 0.0.0.0/0**。一键脚本默认用 VPC CIDR;想收更窄设 `ALLOW_CIDR=<办公IP>/32`,脚本检测到 `0.0.0.0/0` 直接拒绝退出。
- 出站:允许到 metal host 的 8899(同 VPC 默认通)。

metal host 侧:host SG 入站放行**监控 EC2 的 SG / 私网 IP** 来源的 8899(目前 host SG 见 `deploy/stack.py:2056+`,8899 默认只 VPC 内;若已 VPC CIDR 放行则无需改)。

> 验证:`aws ec2 describe-security-groups --group-ids <sg> --query 'SecurityGroups[0].IpPermissions'`,确认 9090/3000 的 `IpRanges` 里**没有** `0.0.0.0/0`。

生产建议:别直接暴露 3000/9090,前置 ALB+ACM(SG 只放 CloudFront prefix list),或干脆只 bind 私网走 SSH 隧道看(见 §5)。

## 3. 起服务(手工部署时)

```bash
cd /opt/monitoring
# Grafana admin 密码:强随机,写 600 .env,不提交 git、不硬编码。
echo "GRAFANA_ADMIN_PASSWORD=$(openssl rand -base64 24)" > .env
chmod 600 .env
docker compose --env-file .env -f docker-compose.prom-grafana.yml up -d
```

> 验证:`docker compose -f docker-compose.prom-grafana.yml ps` 三个容器(prometheus/grafana/node_exporter)Up;`curl -s localhost:9090/-/ready` 返回 `Prometheus Server is Ready`。

## 4. 验证抓到 metal host 指标(端到端真验)

```bash
# ① Prometheus 发现到 target 了吗(ec2_sd / file_sd):
curl -s 'http://localhost:9090/api/v1/targets' | python3 -c \
  'import sys,json; [print(t["labels"].get("instance"), t["health"]) for t in json.load(sys.stdin)["data"]["activeTargets"]]'
# 期望:每台 metal host 一行,health=up。全是 down 检查 SG/同 VPC/8899 可达。

# ② 真抓到 host-agent 指标了吗(直接查一个真实指标):
curl -s 'http://localhost:9090/api/v1/query?query=openclaw_vm_health' | python3 -c \
  'import sys,json; r=json.load(sys.stdin)["data"]["result"]; print("series:",len(r)); print(r[:2])'
# 期望:series>0,每条带 tenant + instance label。为 0 说明没抓到,回 ① 排查。

# ③ 直接从监控 EC2 探一台 host-agent(确认网络可达):
curl -s http://<metal-host-private-ip>:8899/metrics | head -20
# 期望:看到 # HELP openclaw_vm_memory_used_mb ... 等真实指标行。
```

> 这是端到端真验(看到真实 series),不是"容器起来了"自证。

## 5. Grafana 登录与看板

```bash
# admin 密码(一键脚本生成在监控 EC2 上,经堡垒/SSM 取,别外传):
sudo cat /opt/monitoring/.env   # GRAFANA_ADMIN_PASSWORD=...
```

访问(推荐 SSH 隧道,不暴露公网):

```bash
ssh -i ~/.ssh/<key>.pem -L 3000:<monitoring-private-ip>:3000 \
    -L 9090:<monitoring-private-ip>:9090 ubuntu@<堡垒机>
# 浏览器开 http://localhost:3000  用户 admin / 上面的密码
```

datasource(Prometheus)与 dashboard(`OpenClaw microVM Fleet (host-agent)`,uid `openclaw-fleet`)已 provisioning,登录即见,无需手建。看板含:VM 总数/健康数/不健康数/内存合计(stat)、Gateway 健康数/不健康数(stat)、每 host VM 数与健康数与内存与 balloon、每 host 不健康 Gateway 数(时序)、Top20 VM CPU/磁盘占用、per-VM 明细表(含 `vm_health` 与 `app_health`)。顶部 `Host (instance)` 变量可按 host 过滤。

> 验证:dashboard 各面板有数据(非 No data)。No data 时回 §4 确认 Prometheus 真抓到 series。

## 安全边界

- Prometheus 9090 / Grafana 3000 入站**绝不** 0.0.0.0/0;只 VPC CIDR / 堡垒 SG / 办公 IP。一键脚本对 `0.0.0.0/0` 直接拒绝。
- Grafana admin 密码走 `.env`(`openssl rand`,chmod 600,不提交 git);关匿名访问、关注册(compose env)。生产建议进 Secrets Manager 启动注入。
- 监控 EC2 独立 SG + 独立 instance role(只 `ec2:DescribeInstances` 只读 + S3 只读),与 metal host 隔离爆炸半径。
- EBS 加密(脚本 `Encrypted=true`)。删监控 EC2 前快照(铁律 #4)。
- host-agent 的 `/metrics` 是明文 http(私网内、无凭据),靠网络层(同 VPC + SG)隔离,不对外暴露。

## 现状标注(诚实)

- ✅ 真实可部署、本地已校验:compose / prometheus.yml / datasource+dashboard provisioning JSON 已过 YAML+JSON 合法性校验;指标名逐一对齐 `host-agent.py:50-65`(grep 实读,非编造)。
- ⏳ 规划(未实测):未在真监控 EC2 上 `docker compose up` 跑过,未对真 metal host 的 `:8899` 抓过真实 series。§4 给出确切验证命令,真机执行时按它逐条核(target up / series>0 / dashboard 有数据)。
- ⚠️ 待核:`ec2_sd_configs` 的 tag 过滤(`tag:Project=openclaw` / `tag:Role=metal-host`)需与 metal host launch template 实际打的 tag 对齐——上真机前 `aws ec2 describe-instances` 看 metal host 真实 tag,不符就改 `prometheus.yml` 的 `filters`。host SG 是否已放行 8899 给监控 EC2 也需真机核(见 §2)。

```

```

## 5. 集成方接入(#387:外部自建 Prometheus 私网 scrape)

**前提(逐条核实,不满足走退化路径)**:
1. 集成方 Prometheus 与 ClawPool **同 VPC**(SG 按 VPC CIDR 放行)。peering 场景须显式放行 peer CIDR/SG 并从真实集成方位置验收。
2. ec2_sd 自动发现要求集成方 Prometheus 的实际身份(instance role)能调 `ec2:DescribeInstances` + `ec2:DescribeAvailabilityZones`(同账号**不自动授权**,必须实测)。不满足 → 用 file_sd(`targets/` 目录模式,见上文 job `openclaw-host-agent-file`)。

**端点清单(全部私网,SG 已放行 VPC CIDR)**:

| 端点 | 指标 | 说明 |
|---|---|---|
| host `:8899/metrics` | `openclaw_vm_*` 7 gauge + `openclaw_vm_health`/`openclaw_app_health` | per-tenant 资源与健康 |
| host `:8899/metrics` | `openclaw_host_dnat_ports_{used,total,quarantined}` | **容量红线①**:端口耗尽=新 VM 起不来。bitmap 未初始化(该 host 从未分配过路由)时三条 absent,不吐假 0 |
| host `:8899/metrics` | `openclaw_agent_loop_last_tick_epoch{loop}` / `openclaw_agent_build_info{sha}` / `openclaw_route_ensure_failures_total` / `openclaw_host_ssm_agent_up` | agent 自身健康(v4)。loop label 只吐实际启用的线程:poll/disk_gc/disk_report 常驻,dispatch/housekeeping 仅 pull 模式 |
| edge `:9145/metrics` | `edge_connections{state}` + `edge_worker_{connections_limit,processes}` + `edge_up` | **容量红线②**:连接耗尽=全部租户连不上。`edge_up` 只证 nginx 活着,业务就绪用 blackbox probe ALB `/healthz` |

**发现标签约定**:`tag:Project=openclaw` + `tag:Role=metal-host|edge`(LaunchTemplate 实例级打的,随 ASG 重建继承;存量 edge 需 instance refresh 或手工补 tag)。

**告警阈值样例**:
```yaml
# 端口水位:quarantined 与 used 一样不可分配,合并计算
- alert: HostDnatPortsHigh
  expr: (openclaw_host_dnat_ports_used + openclaw_host_dnat_ports_quarantined) / openclaw_host_dnat_ports_total > 0.8
# 连接近似压力(估算口径:active 不含 upstream,WS 主导 ×2)
- alert: EdgeConnPressure
  # 分子分母都按 instance 聚合(codex 评审:sum by 丢 job label 而分母保留 →
  # 默认向量匹配无样本、告警永不触发)
  expr: 2 * sum by (instance) (edge_connections{state="active"}) / sum by (instance) (edge_worker_processes * edge_worker_connections_limit) > 0.7
# agent loop 卡死(HTTP 还活着但后台线程 hang 的假绿场景)
- alert: AgentLoopStale
  expr: time() - openclaw_agent_loop_last_tick_epoch{loop=~"poll|disk_report"} > 60
- alert: AgentLoopStaleSlow
  expr: time() - openclaw_agent_loop_last_tick_epoch{loop=~"disk_gc|housekeeping"} > 300
# SSM agent 挂(数据面照跑但控制面失联;本 scrape 通道独立于 SSM,拉得到)
- alert: HostSsmAgentDown
  expr: openclaw_host_ssm_agent_up == 0
```

## 6. OS 级指标:平台使用者自装(对标 K8s node_exporter DaemonSet 模式)

平台只暴露自己组件才知道的指标;OS 级 CPU/Mem/Disk(`node_memory_*`/`node_filesystem_*` 等)由**平台使用者**自选 exporter(node_exporter/CW Agent/自研)并自装。样例(在你自己 checkout 的 `init-host.sh`(host,arm64)/`install-edge.sh`(edge,x86_64)末尾追加):

```bash
# ---- node_exporter 自装样例(按架构选包 + SHA256 校验 + systemd)----
NE_VER="1.8.2"
case "$(uname -m)" in
  aarch64) NE_ARCH="arm64";  NE_SHA256="<官网 sha256sums 里 arm64 包的值>" ;;
  x86_64)  NE_ARCH="amd64";  NE_SHA256="<官网 sha256sums 里 amd64 包的值>" ;;
esac
curl -sL -o /tmp/ne.tgz "https://github.com/prometheus/node_exporter/releases/download/v${NE_VER}/node_exporter-${NE_VER}.linux-${NE_ARCH}.tar.gz"
echo "${NE_SHA256}  /tmp/ne.tgz" | sha256sum -c - || exit 1
tar xzf /tmp/ne.tgz -C /opt && ln -sf /opt/node_exporter-${NE_VER}.linux-${NE_ARCH}/node_exporter /usr/local/bin/node_exporter
cat > /etc/systemd/system/node_exporter.service <<'UNIT'
[Unit]
Description=node_exporter
[Service]
ExecStart=/usr/local/bin/node_exporter --web.listen-address=:9100
Restart=always
User=nobody
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now node_exporter
```

**安全注意事项(必读,两条都是硬要求)**:
1. **SG 入站只允许 Prometheus 所在 SG(或其 /32),禁止放行 VPC CIDR。** 原因:microVM 出网经 MASQUERADE 后源地址=host VPC IP,访问**另一台** host/edge 的 :9100 时 SG 无法区分租户流量与节点流量——放 VPC CIDR 等于把宿主机 OS 指标暴露给所有租户 microVM。
2. **自装端口必须加进 guest→host 的 INPUT DROP 清单。** 平台已预防性把 9100 加进 `launch-vm.sh`/`migrate-vm.sh` 的 DROP 清单(本机维度);若你用非标准端口,须自行在两个脚本的 `for _port in ...` 行追加。

**ec2_sd job 样例**(复用平台实例标签,把 port 换成你的 exporter 端口):照抄上文 `openclaw-edge-nginx` job,`port: 9100`、`Role` 按 host/edge 选。

**升级责任声明**:修改 `init-host.sh`/`install-edge.sh` 属于你维护自己的脚本分支;平台升级这两个脚本时的合并冲突由使用者自行解决——它们不是平台的稳定扩展接口。
