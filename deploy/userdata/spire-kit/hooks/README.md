# 自动部署:用平台既有的 user hook 装 broker(零改 ClawPool)

> **⚠️ 已被二期方案取代(2026-08-19,#516)。** broker 现在走平台标准通道自动安装:
> 文件随 `clawpool-deploy.sh` 同步进 assets 桶,`init-host.sh` Step 4c 拉取执行
> `../spire-kit-setup.sh`(fail-open + 告警 marker)。见 `../README.md` §自动安装。
> 本目录保留两个用途:① `user_hooks.host`(#390)通道的参考实现(客户在**没有**
> 本仓部署流水线的环境里仍可用它);② 一期真机验证的历史对照。新环境不要再配
> `user_hooks.host` 指向这里。

`install.sh` 需要人 `sudo` 手动跑一次。ASG 新起的 host 上没人跑,broker 就不存在 ——
**那台 host 会照常接租户,而它上面的每台 VM 都静默没有身份**。这个目录解决这件事。

做法是挂到平台**已经存在**的 `user_hooks.host`(#390)钩子上:填一段 config + 放一个
S3 对象,`launch-vm.sh` / `init-host.sh` / `host-agent.py` / IAM 一行都不动。

---

## 数据流

```
config.yml: user_hooks.host.s3_uri  ──┐
                                      │ host 首 boot(注册 active 之前)
                            init-host.sh 自动下载 + 校验 sha256 + 以 root 执行
                                      ▼
                          hooks/host-user-hook.sh
                                      │ ① 读 SSM /openclaw/spire-kit/*
                                      │ ② 必填校验(缺一个就拒装)
                                      │ ③ 取 kit tar.gz + 校验 sha256
                                      ▼
                               install.sh --registrar-url … --trust-domain …
                                      │ systemctl enable --now
                                      ▼
                         spire-join-broker.service(常驻)
```

配置放 SSM 而不是写死在脚本里,是为了**同一份 hook 脚本(同一个 sha256)走遍
dev/staging/prod**:SSM 参数天生按 region/账号隔离,改地址只改参数值,不用重算 sha256、
不用换 Launch Template。

---

## 部署步骤

### 1. 写 SSM 参数(每个环境一次)

必填:

```bash
R=us-west-2                     # 你的 region
P=/openclaw/spire-kit
aws ssm put-parameter --region $R --name $P/enabled              --type String --value true            --overwrite
aws ssm put-parameter --region $R --name $P/trust-domain         --type String --value <你的 trust domain> --overwrite
aws ssm put-parameter --region $R --name $P/spire-server-address --type String --value <SPIRE Server 地址> --overwrite
aws ssm put-parameter --region $R --name $P/registrar-url        --type String --value <Entry Registrar URL> --overwrite
aws ssm put-parameter --region $R --name $P/kit-s3-uri           --type String --value s3://<你的桶>/spire-kit.tar.gz --overwrite
aws ssm put-parameter --region $R --name $P/kit-sha256           --type String --value <上面对象的 sha256> --overwrite
```

可选(有默认值):`registrar-backend`(默认 `http`,可选 `exec`/`local`)、
`registrar-cmd`(`exec` 后端必填)、`spire-server-port`(默认 `8081`)、
`broker-port`(默认 `8877`)。

> **`spire-server-address` 填真实地址,不要填 `127.0.0.1`。** guest 是在自己的 netns 里
> 连这个地址,loopback 指向 guest 自己 → attestation 永远失败。hook 与 broker 都会显式
> 拒绝这个值,就是因为它是最难排查的错配(host 侧一路全绿)。

### 2. 打包并上传 kit

```bash
tar -czf spire-kit.tar.gz -C engineering/poc spire-kit
shasum -a 256 spire-kit.tar.gz          # 填进上面的 kit-sha256
aws s3 cp spire-kit.tar.gz s3://<你的桶>/spire-kit.tar.gz
```

顶层带 `spire-kit/` 目录或直接平铺都可以,hook 自己找 `install.sh`。

### 3. 上传 hook 脚本并填 config

```bash
aws s3 cp deploy/userdata/spire-kit/hooks/host-user-hook.sh s3://<你的桶>/spire-kit/host-user-hook.sh
shasum -a 256 deploy/userdata/spire-kit/hooks/host-user-hook.sh
```

```yaml
user_hooks:
  host:
    s3_uri: s3://<你的桶>/spire-kit/host-user-hook.sh
    sha256: <上一步算出的 64 位 sha256>
    timeout_seconds: 300
    failure_policy: fail
```

部署时平台会自动给 host role 加上**这个精确对象**的 `s3:GetObject`。

### 4. 让存量 host 生效

`user_hooks.host` 只对**用新 Launch Template 起的实例**生效,不热改现有 host。
存量 host 要么走 ASG instance refresh,要么手动跑一次 `install.sh`。

---

## 为什么不需要改 IAM

hook 需要两种权限,host role **都已经有了**:

| 需要 | 已有来源 |
| --- | --- |
| 读 `/openclaw/spire-kit/*` | `ssm:GetParameter` on `/openclaw/*`(原本给 `CLOUDFRONT_ORIGIN` 用) |
| 读 kit tar.gz | assets/backup 桶的读写授权;放自管桶则由 user hook 机制自动授精确对象 |

⚠️ **所以 hook 刻意用 `aws ssm get-parameter`(单数)逐个读参数。**
`GetParameters`(复数)和 `GetParametersByPath` 是**不同的 IAM action**,host role 没有,
用它们会 AccessDenied —— 那就得改 IAM,"零改 ClawPool"随之不成立。
acceptance 里有一条断言守着这件事,防止有人"顺手优化"成批量读。

---

## 开关

三层,从外到内:

| 层 | 怎么关 | 效果 |
| --- | --- | --- |
| 总开关 | SSM `enabled` != `true` | 什么都不装。host 上不存在任何 spire 相关文件与进程 |
| 运行时 | `/etc/spire-kit/broker.env` 里 `SPIRE_KIT_ENABLED=false` + 重启 unit | 进程还在、规则还在,领证一律 503。回切最快 |
| 卸载 | `install.sh --uninstall` | 删 unit/二进制/规则,保留台账便于审计 |

guest 侧另有独立开关:`~/.spire-kit/enabled` 标记文件不存在 → unit 的
`ConditionPathExists` 不满足 → agent 不启动、bootstrap 直接退 0。

## 插件口(客户自己换掉整段 SPIRE 逻辑)

| 想换什么 | 怎么换 | 要改 ClawPool 吗 |
| --- | --- | --- |
| 发证逻辑整段(换 registrar / 换鉴权 / 不用 SPIRE) | `registrar-backend=exec` + `registrar-cmd` 指向自己的程序(JSON 走 stdin/stdout) | 不用 |
| guest 侧怎么取 token | `SPIRE_KIT_TOKEN_CMD` | 不用 |
| attest 前后的动作 | `SPIRE_KIT_PRE_HOOK` / `POST_HOOK` | 不用 |

`exec` 契约(退出码有语义):`0` = 成功;`2` = 明确拒绝(broker 归还本次配额);
其它非 0 = 结果不确定(**保留**配额,避免重试放大成多发一枚 token)。

---

## `failure_policy` 怎么选

推荐 **`fail`**:SPIRE 配错的 host 直接 ABANDON、不注册 active,不会带着错配置接租户。

理由是失败模式不对称。`warn` 的失败是**静默**的 —— VM 起得来、业务正常、只是没身份,
要一直跑到下游拒绝它的请求才被发现;而 `fail` 的失败是刺眼的、部署当场就看见。
不希望 SPIRE 故障挡住 host 上线的话可以改 `warn`,但要自己盯巡检。

`timeout_seconds` 不会连坐 kill broker:平台用
`timeout --signal=TERM --kill-after=10s Ns bash <hook>`,只对 hook 的直接子进程发信号,
而 `systemctl enable --now` 拉起的 unit 是 systemd(PID 1)的子进程、独立 cgroup,
不在 timeout 的进程组里。所以 hook 装完 unit 就退出是安全的。

---

## 怎么确认装好了

```bash
# 1. hook 跑过没、装成功没
grep spire-kit /var/log/cloud-init-output.log

# 2. broker 活着 + 发证依赖可达(ok 现在包含 registrar 可达性)
curl -s http://127.0.0.1:8877/healthz | python3 -m json.tool

# 3. 重启后还在不在(healthz 只证明此刻活着)
systemctl is-enabled spire-join-broker.service

# 4. 全景(unit / healthz / iptables / 源地址伪造防护生效值 / 开关 / 插件 / 台账)
sudo ./install.sh --status      # 在解包后的 kit 目录里跑

# 5. broker 日志(结构化 JSON;不含 token 明文)
journalctl -u spire-join-broker -n 50
```

`/healthz` 的 `ok` 为 true 表示**进程活着且发证依赖此刻可达**。
`ok: false` 时看 `registrar` 字段的 `error`/`target` —— 最常见是 registrar URL 填错、
跨 VPC 路由/SG 不通、DNS 解析不了。

`ok` 早期只反映"进程活着 + 台账没坏",于是 registrar 完全不通时 healthz 照样绿,
真正的失败要等第一台 VM 起来才暴露 —— 那个假绿灯已经修掉。

---

## 边界(诚实说明)

- **rootfs 里的 guest kit 由客户自己烤。** 平台不碰 rootfs 构建。对应地,`rebuild`
  和 `restore` 都不需要平台额外做什么:`restore` 只换 `data.ext4`,rootfs 不动;
  只要每个 rootfs 版本都烤进 guest kit,四个 API(create/restart/restore/rebuild)
  全部覆盖 —— 它们最终都会走到 `launch-vm.sh` 建 /30 tap 再起 VM,而 broker 是常驻
  pull 模型,VM 一起来自己来要 token。
- **`migrate` 不覆盖。** 它自建 tap + snapshot/load,不过 `launch-vm.sh`;而 snapshot
  restore 是 resume 不是 boot,bootstrap 不重跑、不会来要 token,旧 join_token 也已过
  TTL。需求方已确认该 API 移除;若重开需单独设计续发。
- **hook 只对新 Launch Template 起的实例生效**,不热改存量 host(见步骤 4)。
- **Firecracker 无 vTPM**,SVID 私钥能被 SSH 进 VM 的合法租户读走。本 kit 不改变这条,
  它提供的是"权威签发 + 短命 + 可批量撤销"的身份,不是防御 VM 内合法用户。
