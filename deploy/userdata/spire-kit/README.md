# spire-kit —— 零侵入的 per-microVM SPIRE 身份落地包

> **#516 二期(2026-08-19)已从 `engineering/poc/` 迁入 `deploy/userdata/`**:host 侧
> broker 走平台标准通道自动安装(与 host-agent.py 同一条 S3 sync + 首 boot 拉取链路)。
> guest 侧(`guest/`)仍按客户 rootfs 交底文档交付,不随 host 通道分发。

## 自动安装(#516 二期)

```
clawpool-deploy.sh scripts        ← 部署时:deploy/userdata/ sync 到 assets 桶
        ▼
init-host.sh Step 4c(host 首 boot,root)
  ① SSM /openclaw/spire-kit/enabled != "true" → 整段跳过(零下载零副作用)
  ② 从 assets 桶拉 4 个文件到 /opt/openclaw/spire-kit/
     (spire-kit-setup.sh + install.sh + spire-join-broker.py + .service)
  ③ 跑 spire-kit-setup.sh:读 SSM 配置 → 必填校验 → install.sh → 自检 enabled
  ④ 失败 fail-open:host 照常接租户,落告警接口(见下),人工介入
```

**SSM 参数**(host role 已有 `ssm:GetParameter` on `/openclaw/*`,单数 API):

```bash
R=ap-southeast-1
P=/openclaw/spire-kit
aws ssm put-parameter --region $R --name $P/enabled              --type String --value true --overwrite
aws ssm put-parameter --region $R --name $P/trust-domain         --type String --value <trust domain> --overwrite
aws ssm put-parameter --region $R --name $P/spire-server-address --type String --value <SPIRE Server 地址,禁止 loopback> --overwrite
aws ssm put-parameter --region $R --name $P/registrar-url        --type String --value <Entry Registrar URL> --overwrite
# 可选:registrar-backend(默认 http|exec|local)、registrar-cmd(exec 必填)、
#       spire-server-port(默认 8081)、broker-port(默认 8877)、spire-server-socket(local 用)
```

**告警接口**(装失败时留下,告警系统后续接入,不在本期范围):

- marker 文件:`/var/lib/openclaw/spire-kit.install-failed`(内容 = UTC 时间戳;
  安装成功或开关未开时会被清掉,存在即"最近一次尝试失败")
- 日志令牌:`SPIRE-KIT-INSTALL-FAILED`,在 `/var/log/openclaw-init.log` 与串口输出

**人工介入 / 存量 host 补装**(同一份脚本,幂等):

```bash
# 在目标 host 上(或包成 SSM send-command 对存量机队批量执行):
sudo install -d /opt/openclaw/spire-kit
for f in spire-kit-setup.sh install.sh spire-join-broker.py spire-join-broker.service; do
  sudo aws s3 cp s3://<assets桶>/deployment/scripts/spire-kit/$f /opt/openclaw/spire-kit/$f --region <region>
done
sudo bash /opt/openclaw/spire-kit/spire-kit-setup.sh && sudo rm -f /var/lib/openclaw/spire-kit.install-failed
```

**一句话**:给每台 Firecracker microVM 发一个 SPIRE 签发的身份,**不改 `launch-vm.sh`、
不改 `build-rootfs.sh`、不改 rootfs 镜像、不改 OpenClaw 代码**。全部落在新文件里,
host 一个守护进程 + guest 一段引导脚本(装数据盘用 user unit,烤 rootfs 用系统 unit,二选一)。

设计取舍与三份客户说明的逐条核对见
`engineering/security/SPIRE-非侵入落地-架构变动核对-2026-08-17.md`。

---

## 它替换了什么

上游方案把 join token 经 **MMDS** 注入,必须在 VM 启动前 `PUT /mmds/config` ——
也就是必须改 `launch-vm.sh`(2122 行、全租户启动路径)。

本 kit 换用**已经存在**的通道:每台 VM 独占的 /30 tap 点对点链路。

```
guest(uid 1000)                       host                          SPIRE
────────────────                      ────                          ─────
spire-agent.service (user unit)
  └─ ~/.spire-kit/bootstrap.sh
       GET http://<默认网关>:8877/v1/join-token
                       │  目的 IP = 本 VM /30 的 host 端(内核给,伪造不了)
                       │  源  IP = 本 VM /30 的 guest 端(iptables rpfilter 规则兜)
                       ▼
                 spire-join-broker ──► Entry Registrar / spire-server
                       │                 (node entry + workload entry + 一次性 token)
                       ▼
       {join_token, trust_domain, server_address, spiffe_id}
  └─ 渲染 ~/.spire-kit/agent.conf → exec spire-agent run -joinToken …
       └─ Workload API: ~/.spire-kit/run/agent.sock  ← OpenClaw 取 JWT/X.509-SVID
```

四层判定,任一不过即拒发:目的 IP 属于某条 /30 · 源 IP 是同一条 /30 的 guest 端 ·
**源地址伪造防护在位**(见「源地址伪造防护」一节) · 同一次开机发放次数没超上限(默认 3,给 agent
崩溃恢复留窗口)。

---

## 目录

| 文件 | 跑在哪 | 作用 |
| --- | --- | --- |
| `spire-join-broker.py` | host | join-token 代发器(仅标准库);注册表来自 `vm.json`,只读 |
| `spire-join-broker.service` | host | systemd unit(ProtectSystem=strict) |
| `install.sh` | host | 装/卸/查:broker + 自有 iptables 规则 + 源地址伪造防护(raw 表 `rpfilter` 规则 + per-tap `rp_filter`) |
| `registrar-stub.py` | host/联调 | Entry Registrar 契约的最小实现(`spire` 真调 / `fake` 仅测试) |
| `guest/spire-bootstrap.sh` | guest | 取 token → 渲染 agent.conf → exec spire-agent |
| `guest/spire-agent.service` | guest | systemd **user** unit(uid 1000,装数据盘) |
| `guest/spire-agent-system.service` | guest | systemd **系统级** unit(root,烤进 rootfs 时用) |
| `guest/agent.conf.tmpl` | guest | agent 配置模板(server 坐标由 broker 回传,零硬编码) |
| `guest/spire-header-shim.py` | guest | 本地反代:取 JWT-SVID 注入 header 后转发(HTTP + WS 都覆盖) |
| `guest/spire-header-shim.service` / `-system.service` | guest | shim 的 user / system unit(带 `ConditionPathExists`,没配 shim.env 就不启动) |
| `guest/shim.env.example` | guest | shim 配置样例(唯一必填是真网关地址) |
| `guest/install-guest-kit.sh` | host | 把 guest kit 装进数据盘模板或某个 guest home(纯新增) |
| `hooks/host-user-hook.sh` | host | 挂 `user_hooks.host`(#390):ASG 新 host 首 boot 自动装 broker,配置从 SSM 读 |
| `hooks/README.md` | — | 上面那个 hook 的部署步骤、开关、IAM 说明(见下方"自动部署") |
| `acceptance/spire-kit-acceptance.sh` | 任意 | 分层验收命令,打印 `ASSERTIONS/FAILED/SKIPPED` |
| `acceptance/logic_probe.py` | 任意 | 判定逻辑的可执行断言(host 上没 pytest 也能跑) |

---

## 0→1 步骤

### 1. host 侧(每台 metal 跑一次)

> **生产别手跑。** ASG 新起的 host 上没人 sudo,broker 就不存在 —— 那台 host 照常接租户,
> 而它上面每台 VM 都静默没有身份。生产用 `hooks/host-user-hook.sh`(见下方**自动部署**),
> 首 boot 自动装。下面这段手工命令用于单机联调。

```bash
sudo ./install.sh \
  --registrar-backend http \
  --registrar-url <你的 Entry Registrar URL> \
  --trust-domain <你的 trust domain> \
  --spire-server-address <spire-server 或 VPCE 域名> --spire-server-port 8081
sudo ./install.sh --status
```

前三个地址**必填,没有默认值** —— 早期有默认值(其中 server-address 默认 `127.0.0.1`),
后果不是装不上而是**静默错误**:broker 起得来、healthz 绿、token 真发,而 guest 拿着
loopback 去连自己,attestation 永远失败,且 host 侧全绿会把排查方向彻底带偏。

没有 registrar 时可先用本机 spire-server:`--registrar-backend local`
(broker 直接调 `spire-server token generate` + `entry create`)。

`--dry-run` 只打印将执行的动作(参数校验在权限检查之前,非 root 也能验参数组合)。
`--uninstall` 撤 broker 与自有规则(不动平台文件)。

### 1b. 自动部署(生产路径,零改 ClawPool)

填一段 `user_hooks.host` config + 放两个 S3 对象(hook 脚本 + kit tar.gz),配置从
SSM `/openclaw/spire-kit/*` 读。**不改 IAM、不改 `launch-vm.sh` / `init-host.sh`。**
完整步骤见 **`hooks/README.md`**。

### 2. guest 侧

形态 ①(烤进 rootfs,root 跑,每台 VM 一装到底 —— 真环境验收用的就是它):

```bash
sudo ./guest/install-guest-kit.sh \
     --rootfs /home/ubuntu/firecracker-assets/openclaw-rootfs.ext4 \
     --agent-binary /path/to/spire-agent
# 活体 guest 或已挂载的 rootfs:--root-dir /
```

形态 ②(装数据盘 user unit,uid 1000 跑,零镜像改动):

```bash
# 新租户:给数据盘模板打补丁(纯新增文件)
sudo ./guest/install-guest-kit.sh \
     --template /home/ubuntu/firecracker-assets/openclaw-data-template.ext4 \
     --agent-binary /path/to/spire-agent      # aarch64 静态二进制

# 存量租户(VM 停机时):挂载它的 data.ext4 后
sudo ./guest/install-guest-kit.sh --home /mnt/tenant-data --agent-binary /path/to/spire-agent

# 存量租户(在跑):走平台已有的 host→guest SSH
./guest/install-guest-kit.sh --home /home/agent --agent-binary ~/spire-agent   # 在 guest 内以 agent 身份
systemctl --user enable --now spire-agent
```

`--print-plan` 先看将新增哪些文件,一个既有文件都不会被改。

### 3. 让 JWT-SVID 真的带上出网请求(header shim)

shim 装进 kit 时**默认不启动**(unit 上有 `ConditionPathExists`),配了才生效:

```bash
# guest 内(rootfs 形态)
sudo cp /etc/spire-kit/shim.env.example /etc/spire-kit/shim.env
sudo sed -i 's|SPIRE_SHIM_UPSTREAM=.*|SPIRE_SHIM_UPSTREAM=https://<真网关>:443|' /etc/spire-kit/shim.env
sudo systemctl enable --now spire-header-shim
# 数据盘形态:~/.spire-kit/shim.env + systemctl --user enable --now spire-header-shim

# 然后把 OpenClaw 的"网关地址"配置值指到 127.0.0.1:18888 —— 应用代码零改动
```

要点:

- 请求里客户端自带的同名 header 会被**剥掉**再换成真 SVID(不许自己伪造身份)。
- `SPIRE_SHIM_ON_MISSING=forward`(默认)取不到 SVID 时照常转发但不带 header,不制造可用性
  悬崖;要 fail-closed 就设 `reject`(返 503,且不碰上游)。
- WebSocket(`Upgrade: websocket`)注入后转裸字节双向管道,wss 长连不受影响。
- 上游用私有 CA 就设 `SPIRE_SHIM_UPSTREAM_CA`;**不要**用 `--upstream-insecure`(等于允许中间人,
  只允许联调,开了会打 error 级日志)。

### 4. 自检

```bash
# host
curl -s http://127.0.0.1:8877/healthz
journalctl -u spire-join-broker -n 50 --no-pager

# guest
systemctl --user status spire-agent
~/.spire-kit/bin/spire-agent api fetch jwt -audience bgw \
    -socketPath ~/.spire-kit/run/agent.sock
```

---

## 验收

```bash
# 任何机器:静态门 + 判定逻辑
./acceptance/spire-kit-acceptance.sh --tier static,logic

# Linux + root:真 veth /30 链路、真源地址伪造防护(直读内核 DROP 计数)、跨租户实测
sudo ./acceptance/spire-kit-acceptance.sh --tier netns

# 再加真 spire-server/spire-agent:真 attest、真 X.509/JWT-SVID、token 一次性
sudo ./acceptance/spire-kit-acceptance.sh --tier full --spire-bin-dir /usr/local/bin
```

跑不了的层会打 `[SKIP]` 并说明**没证明什么**,不当通过。证据:

- 干净 Linux + veth 模拟 /30(86 断言 0 失败):`engineering/evidence/metal-experiments/spire-kit-acceptance-2026-08-17.md`
- **真 ClawPool 环境**(真 metal / 真 microVM / 真 tap,含跨租户 403、台账上限、重启自愈):
  `engineering/evidence/metal-experiments/spire-kit-real-clawpool-2026-08-17.md`

---

## 可开关(四档)

| 档 | 动作 |
| --- | --- |
| 整个环境不装 | SSM `/openclaw/spire-kit/enabled` != `true` —— 走 hook 自动部署时,host 上不会出现任何 spire 相关文件与进程 |
| 彻底移除 | `sudo ./install.sh --uninstall`;guest 侧删 kit 目录 |
| host 侧运行时关 | `/etc/spire-kit/broker.env` 里 `SPIRE_KIT_ENABLED=false` 后重启 broker(领证一律 503,不判定、不动台账) |
| 单台/单镜像 guest 关 | 删 `enabled` 标记(`/etc/spire-kit/enabled` 或 `~/.spire-kit/enabled`)—— unit 的 `ConditionPathExists` 不满足就不启动 |

安装时即可选关闭:`install-guest-kit.sh --disabled`(烤进镜像但默认不生效,便于灰度)。

## 插件化(自己改 SPIRE 逻辑,不动 ClawPool、也不动本 kit)

```bash
# ① host 侧:整段发证逻辑换成自己的程序
#   /etc/spire-kit/broker.env
SPIRE_KIT_REGISTRAR_BACKEND=exec
SPIRE_KIT_REGISTRAR_CMD=/etc/spire-kit/plugins/issue-token
```

`issue-token` 的契约(JSON 走 stdin/stdout):

```
stdin : {"tenant_id","vm_num","guest_ip","host_tap_ip","workload_uid",
         "trust_domain","boot_marker","idempotency_key","seq"}
stdout: {"join_token","ttl"?,"spiffe_id"?,"node_spiffe_id"?}
rc    : 0=成功 · 2=明确拒绝(broker 归还配额)· 其它非 0=结果不确定(保留配额)
```

```bash
# ② guest 侧:换掉"怎么拿 token"(改回 MMDS、读挂载盘、走自家 KMS…)
#   /etc/spire-kit/shim.env 或 unit 的 Environment=
SPIRE_KIT_TOKEN_CMD=/etc/spire-kit/plugins/get-token      # stdout 与 broker 同形 JSON
SPIRE_KIT_PRE_HOOK=/etc/spire-kit/plugins/pre-render      # 渲染 agent.conf 之前
SPIRE_KIT_POST_HOOK=/etc/spire-kit/plugins/pre-exec       # exec agent 之前

# ③ agent 行为:直接换 agent.conf.tmpl(trust bundle / KeyManager / attestor 都在里面)
```

## 生命周期四条路径

**交付形态定为 rootfs**(guest kit 烤进 rootfs,由客户的 rootfs 构建负责)。这个选择把
restore / rebuild 这两条原本"未验"的路径直接消掉了:

| 路径 | rootfs 形态(交付形态) | 为什么 |
| --- | --- | --- |
| create | 覆盖 | 走 `launch-vm.sh` 建 /30 tap → VM 起来 → bootstrap 来要 token |
| restart | 覆盖(换新 token) | 同上;boot marker 变化 → 台账放新额度 → agent 自愈。真机已验 |
| restore | 覆盖 | `restore` 只换 `data.ext4`,**rootfs 不动** → kit 还在 |
| rebuild | 覆盖 | `rebuild-vm.sh` 尾部 exec 回 `launch-vm.sh`;新 rootfs 只要也烤了 kit 就一样 |
| migrate | **不覆盖** | 自建 tap + snapshot/load,不过 `launch-vm.sh`;snapshot restore 是 resume 不是 boot,bootstrap 不重跑、不来要 token,旧 token 也已过 TTL。需求方已确认该 API 移除 |

客户侧唯一的纪律是:**每个 rootfs 版本都要烤进 guest kit**。漏一个版本,用它起的 VM
会静默没有身份(VM 正常、业务正常,直到下游拒绝它的请求)。

数据盘形态(`install-guest-kit.sh --template`)仍保留,但只建议用于临时验证:它让
restore 变成"还原的是归档时的盘,归档早于装 kit 就会丢"这种需要额外纪律的路径。

## 源地址伪造防护(两条机制,满足其一)

判定 2(源 IP 必须是同一条 /30 的 guest 端)只有在"源 IP 伪造不了"时才有意义。broker
每次发证前都自检这个前提(`spoof_guard`),两条机制满足其一即放行,都不在位则拒发
(`enforce` 下 403 `spoof_guard_absent`):

| # | 机制 | 谁装的 | 说明 |
| --- | --- | --- | --- |
| ① 主 | `iptables -t raw -A PREROUTING -i tap+ -p tcp --dport <PORT> -m rpfilter --invert -j DROP` | `install.sh`(新增自有规则) | 规则级 strict 反向路径检查,**不看 `conf/all`**;`-i tap+` 通配自动覆盖装好之后新建的 tap(零改 `launch-vm.sh` 的前提下这是必要的) |
| ② 叠加 | `rp_filter` **生效值** strict | `install.sh` 设 `default` + 逐个已有 tap | 生效值是 `max(conf/all, conf/<iface>)`,不是单看 per-tap 那个文件 |

**为什么 ② 不能单独当依据**:ClawPool 的 metal host 在 `/etc/sysctl.d/10-network-security.conf`
里**主动**把 `net.ipv4.conf.all.rp_filter` 设成 `2`(loose,不是内核默认值),于是 per-tap
写 1 之后生效值仍是 `max(2,1)=2` —— sysctl 这层等于没有。本 kit **刻意不改 `conf/all`**:
那会连带把主 ENI 切 strict,多 ENI / 策略路由 host 上可能打断非对称路由,不是本 kit 能替
客户单方面决定的。要改得显式加 `install.sh --harden-all-rp-filter`(会打印拓扑警告)。

`install.sh --status` 会打印每个 tap 的生效 `max` 值,并在 `conf/all=2` 时明说"sysctl 这层
等于没有";`/healthz` 有 `spoof_guard` 字段并折进 `ok`;`--uninstall` 会撤掉 ① 那条规则。

真机实测数据(判据是 `iptables -t raw` 的 DROP 计数增量,不是"有没有拿到 token")见
`ADR-spire-noninvasive-join-broker.md` §5.19 / §7.11。

## 边界(照实说)

- **Firecracker 无 vTPM**:SVID 私钥能被 SSH 进 VM 的合法租户读走。`KeyManager "memory"`
  只是不落盘,不等于偷不走。本 kit 的价值是"权威签发 + 短命 + 来源绑定 + 可批量撤销",
  与 `ADR-spire-per-microvm-poc` 第 6 节口径一致。
- **信任根是 host 的网络拓扑**(哪条 tap / 哪条 /30 属于谁),与 MMDS 的"host 写 guest 只读"同级,
  都不是 guest 侧密码学证明。要更强的 host 侧信任根就上 `aws_iid`(broker 已把 IID 递给 registrar)。
- **源地址伪造防护是硬前提**:两条机制都不在位时 broker 默认拒发
  (`--rp-filter-policy enforce` → 403 `spoof_guard_absent`)。`warn` 只给无 tap 的测试
  环境用,生产不要开。两条机制见「源地址伪造防护」一节。
- **header 注入还没做**:让 OpenClaw 把 JWT-SVID 带上出网请求,目前还需要客户拍板走哪条路
  (guest 内本地 shim / host nginx 盖章 / 网关侧改验签),见核对文档第 3 节。
- **规模化没测**:真环境只跑过一台 metal、两个租户;380 VM 并发领证、归档/还原路径未实测。
