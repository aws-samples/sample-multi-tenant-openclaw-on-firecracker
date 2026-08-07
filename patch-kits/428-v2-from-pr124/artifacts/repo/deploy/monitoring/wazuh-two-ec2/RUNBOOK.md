# Wazuh on two EC2 — real-time FIM (server + agent separated)

Security-hardened build of the reference article
[Setting Up Wazuh on AWS: Two EC2 Instances, Real-Time File Monitoring, and What Actually Went Wrong](https://medium.com/@nikhilshakya0905/setting-up-wazuh-on-aws-two-ec2-instances-real-time-file-monitoring-and-what-actually-went-wrong-b491ef570622)
(Nikhil Shakya, Apr 2026). Same architecture and install path, but the lab's
`0.0.0.0/0` security groups are replaced with the project's SG red line.

> Verified end-to-end on account `<AWS_ACCOUNT_ID>` / `ap-southeast-1`, 2026-06-30.
> What was verified is marked **[verified]**; design notes are unmarked.

## Architecture (two EC2)

```
EC2-1  Wazuh-Manager (m7i-flex.large, Ubuntu 22.04, all-in-one)
        wazuh-manager + wazuh-indexer + wazuh-dashboard   (wazuh-install.sh -a)
        listens: 1514/1515 (agent), 55000 (API), 443 (dashboard), 9200 (indexer, localhost)
                         ▲ 1514 events / 1515 enroll  (private VPC IP)
                         │
EC2-2  Wazuh-Agent-one (t3.micro, Ubuntu 22.04)
        wazuh-agent  +  auditd
        real-time FIM + whodata on /etc  ->  ships file events to the manager
```

The article installs manager + indexer + dashboard on EC2-1 with the all-in-one
installer, and a lightweight agent on EC2-2. **File Integrity Monitoring (FIM)**
on the agent's `/etc` is the point: any create/modify/delete fires an alert on the
manager within ~1s, and `whodata` (backed by `auditd`) attaches the user/process.

## What differs from the article (and why)

|                           | Article (throwaway lab)                 | This deployment                           |
| ------------------------- | --------------------------------------- | ----------------------------------------- |
| SG inbound                | 22/80/443/1514/1515 open to `0.0.0.0/0` | **zero `0.0.0.0/0`** — see below          |
| Public IP                 | both instances public                   | **none** — private subnet, NAT egress     |
| Dashboard access          | browser straight to public IP           | **SSH tunnel through bastion**            |
| Manager address for agent | Elastic IP (to survive public-IP churn) | **private VPC IP** (stable, never public) |

The article's headline failure ("agent shows _Unknown_ after restart, fixed with
an Elastic IP") only happens because the agent talked to the manager's **public**
IP, which changes on stop/start. Here the agent uses the manager's **private**
`172.31/16` IP, which never changes and never leaves the VPC — so no EIP is needed
and nothing is exposed.

## Security groups — the red line **[verified: zero 0.0.0.0/0]**

`wazuh-manager-sg` inbound:

- `1514-1515` TCP **and** UDP ← `wazuh-agent-sg` (agent events + enrollment)
- `55000` (API), `443` + `5601` (dashboard), `22` (SSH) ← bastion SG only

`wazuh-agent-sg` inbound:

- `22` (SSH) ← bastion SG only

Every rule is a **security-group reference**, no CIDR ranges. The manager process
binds `0.0.0.0:1514/1515/55000` (Wazuh default), but the network boundary is the
SG, which only admits the agent SG and the bastion SG. `setup-wazuh-two-ec2.sh`
asserts no `0.0.0.0/0` inbound and aborts if it finds any.

## Deploy

Run from the bastion (has admin + `aws`):

```bash
cd deploy/monitoring/wazuh-two-ec2
VPC_ID=<vpc-id> PRIVATE_SUBNET=<subnet-id> BASTION_SG=<sg-id> \
  AMI_ID=<ubuntu-ami-id> KEY_NAME=<ec2-key-name> ./setup-wazuh-two-ec2.sh
```

Defaults target the verified `ap-southeast-1` reference env; override via env vars
(`REGION`, `VPC_ID`, `PRIVATE_SUBNET`, `BASTION_SG`, `AMI_ID`, `KEY_NAME`, ...).
The manager installer runs unattended for ~5-10 min.

### Credentials (never committed)

The all-in-one installer **generates** the admin/indexer passwords into
`/root/wazuh-install-files.tar`. Pull once, store in a secret manager, rotate
before non-lab use:

```bash
sudo tar -O -xf /root/wazuh-install-files.tar \
  wazuh-install-files/wazuh-passwords.txt | grep -A1 "username: .admin."
```

## Verify (real machine)

**1. Manager stack up** **[verified]**

```bash
for s in wazuh-manager wazuh-indexer wazuh-dashboard; do systemctl is-active $s; done
# -> active / active / active
sudo ss -tlnp | grep -E ':(443|55000|1514|1515|9200)'
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:443/    # -> 302 (login)
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:55000/  # -> 401 (auth)
```

**2. Agent registered** **[verified]**

```bash
sudo /var/ossec/bin/agent_control -l
#   ID: 000, Name: ip-172-31-52-173 (server), IP: 127.0.0.1, Active/Local
#   ID: 001, Name: Agent-one, IP: any, Active        <-- agent online
```

**3. Real-time FIM trigger — the core test** **[verified]**

On the **agent** node:

```bash
sudo touch /etc/fim_test
echo backdoor | sudo tee -a /etc/fim_test
sudo rm /etc/fim_test
```

On the **manager**, within the same second:

```bash
sudo grep fim_test /var/ossec/logs/alerts/alerts.json
```

Observed 2026-06-30 (all at `02:32:23`, agent `Agent-one`, `whodata` user `root`):

| time     | rule | level | event    | path          | who  |
| -------- | ---- | ----- | -------- | ------------- | ---- |
| 02:32:23 | 554  | 5     | added    | /etc/fim_test | root |
| 02:32:23 | 550  | 7     | modified | /etc/fim_test | root |
| 02:32:23 | 553  | 7     | deleted  | /etc/fim_test | root |

This is the article's result reproduced: create/modify/delete on a monitored
agent directory, captured on the manager in real time, with the acting user
attached via `whodata`/`auditd`.

## Custom OpenClaw rules

`deploy/monitoring/wazuh-rules/openclaw_local_rules.xml` (FIM identity-file changes
100110, reverse-shell 100210, privesc 100220, GuardDuty 100300) is copied into
`/var/ossec/etc/rules/` on the manager and loaded after `systemctl restart
wazuh-manager` **[verified: manager active, no rule-load error]**.

## Dashboard access (no public exposure)

From your laptop, tunnel through the bastion (dashboard SG admits the bastion SG):

```bash
ssh -i <bastion-key>.pem -L 8443:<manager-private-ip>:443 ubuntu@<bastion-public-ip>
# open https://localhost:8443  (admin / generated password; accept self-signed cert)
```

## 告警外发与留存(别让告警只躺 manager 本地)

默认 Wazuh 告警只落 manager 本地 `/var/ossec/logs/alerts/alerts.json` + 本地 indexer。
那台 EC2 挂了 / 被删 / 被攻陷先删日志,告警历史全没 —— 等于没监控。生产级要求
告警出本机、汇到独立信任域、防删(对标生产级中心化 HIDS,监控数据不在
被监控方信任域内)。本部署把这步落进代码:

- `setup-wazuh-two-ec2.sh` 阶段 1b 建 manager instance role(最小权限:只写本告警
  log group + publish 本 topic)+ SNS topic `openclaw-wazuh-alerts` + CloudWatch
  log group `/openclaw/wazuh/alerts`(保留 30 天)。
- `server-userdata.sh` 装 CloudWatch agent,把 alerts.json 实时镜像到该 log group。
  manager 被删/删本地日志后,CloudWatch 那份仍在。
- **更强防删(生产建议)**:再叠一个 S3 + Object Lock(COMPLIANCE)做 WORM,把告警
  归档到连 root 都删不掉的桶(同租户备份桶那套),CloudWatch Logs 订阅 → Firehose → S3。
- **实时外发(待接订阅渠道)**:role + SNS topic 已就绪;高危告警(level≥10)经 Wazuh
  integratord 调脚本 publish 到 topic,运维订阅邮箱/钉钉/PagerDuty 即实时收。订阅渠道
  由运维定,故脚本只建 topic + 授权,不写死订阅端。

> 验证:manager 上 `cat /var/log/wazuh-install-done.marker` 见 `CW_AGENT_DONE`;
> 改 agent `/etc` 触发告警后,CloudWatch Logs `/openclaw/wazuh/alerts` 应几秒内出现该条。

### 生产形态:独立 Amazon OpenSearch Service 域(治 all-in-one 单点全丢)

CloudWatch 兜底解决"告警证据不丢",但查询/看板体验差。**生产推荐**把告警从 manager
本地 indexer 改送一个**独立托管**的 OpenSearch 域 —— all-in-one 是 manager+indexer+
dashboard 挤一台,那台没了告警数据库一锅端;独立域让告警落在独立信任域,查询用
OpenSearch Dashboards(SIEM 该有的体验)。对标生产级实践:监控数据不在被监控方信任域。

**⚠ 成本**:Amazon OpenSearch Service 按小时持续计费、停不掉(不像 EC2 能 stop)。
最小 `t3.small.search` 单节点约 $26/月起,multi-AZ 翻倍。所以 `setup` 脚本默认
`ALERTS_OPENSEARCH_ENABLED=false`,明确接受持续成本再开。

建域(setup 脚本阶段 1c,需先 `export ALERTS_OPENSEARCH_ENABLED=true`):

```bash
# demo 最小单节点(默认):
ALERTS_OPENSEARCH_ENABLED=true ./setup-wazuh-two-ec2.sh
# 生产 multi-AZ:
ALERTS_OPENSEARCH_ENABLED=true OPENSEARCH_INSTANCE_COUNT=2 OPENSEARCH_MULTI_AZ=true \
  OPENSEARCH_INSTANCE_TYPE=m6g.large.search ./setup-wazuh-two-ec2.sh
```

脚本建:VPC 内私网域(不公网)+ 域 SG 入站 443 只对 manager SG(零 0.0.0.0/0)+
细粒度访问控制 + 静态/传输/节点间加密 + 强制 TLS1.2。域约 15-20 分钟变 active。

**改 Filebeat output 指向域(进 manager SSH 跑,带回滚)**:域 active 后取 VPC endpoint,
改 manager 的 `/etc/filebeat/filebeat.yml` 的 `output.elasticsearch.hosts` 从本地
`127.0.0.1:9200` 指向 `https://<域 endpoint>:443`,填域的 master 用户/密码(细粒度访问
控制建域时设),`systemctl restart filebeat`。

> 验证:OpenSearch Dashboards(经堡垒机隧道)能查到 `wazuh-alerts-*` 索引的新告警。
> 回滚:把 `output.elasticsearch.hosts` 改回 `127.0.0.1:9200` + restart filebeat,即恢复本地 indexer。

## Teardown

Snapshot first (project rule #4), then terminate both instances and delete the two
SGs. Instances carry tag `project=openclaw-monitoring`. 另需清理:CloudWatch log group
`/openclaw/wazuh/alerts`、SNS topic `openclaw-wazuh-alerts`、IAM role/profile
`openclaw-wazuh-manager-role`(确认无其它依赖再删)。若开过 OpenSearch:**先
`opensearch create-snapshot`/手动快照确认再** `opensearch delete-domain --domain-name
openclaw-wazuh-alerts`(持续计费,不用务必删)+ 删 `openclaw-opensearch-sg`。

## Honest status

- **[verified]** SGs (zero 0.0.0.0/0), both EC2 launched, manager stack active,
  dashboard :443→302, API :55000→401, agent registered Active, real-time FIM +
  whodata reproduced (rule 554/550/553 with who=root), custom rules loaded.
- **Not done / out of scope**: dashboard TLS still self-signed (article uses
  "Advanced → Proceed"; production = ACM/Let's Encrypt); GuardDuty→Wazuh integration
  (see ../WAZUH-RUNBOOK.md §5); connecting the actual microVM in-guest agents
  (this deployment proves the path with a plain Ubuntu endpoint).
