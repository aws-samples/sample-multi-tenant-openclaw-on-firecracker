# Host/Edge S3 user-hook 运维手册

`user_hooks` 用于在新 Host 或 Edge 首次启动时安装客户自管扩展，例如
`node_exporter`、安全 agent 或资产清点 agent。它不是远程命令通道：hook 必须是
私有 S3 中的不可变脚本，并由 `config.yml` 固定完整 SHA256。

## 稳定契约

```yaml
user_hooks:
  host:
    s3_uri: "s3://customer-hooks/prod/host/node-exporter-v1.sh"
    sha256: "<64 位小写 SHA256>"
    timeout_seconds: 300
    failure_policy: fail
  edge:
    s3_uri: ""
    sha256: ""
    timeout_seconds: 300
    failure_policy: fail
```

- `s3_uri` 为空即关闭；关闭时不生成 hook 命令或 hook 专属 IAM 权限。
- URI 必须指向一个精确私有对象，不接受 prefix、通配符、query 或 fragment。
- `sha256` 是下载内容的完整摘要；`timeout_seconds` 范围为 1–3600 秒。
- `failure_policy: fail` 是生产默认。Host 失败时不注册 active，并由现有 lifecycle
  trap 返回 `ABANDON`；Edge 失败时停止 `claw-edge.service`，保持 ELB unhealthy。
  `warn` 只记录错误并继续启动，应仅用于不影响节点正确性的附加能力。
- hook 以 root 运行，并显式获得 `OC_REGION` 和 `OC_NODE_ROLE`（`host` 或
  `edge`）。Host hook 还可读取已生成的 `/etc/platform.env`；不要改写其中平台字段。
  Edge 不保证存在该文件。
- 下载先落临时文件，通过 SHA256 后才以同文件系统 rename 原子安装到
  `/var/lib/openclaw/user-hooks/{host|edge}.sh`（root 所有、`0700`），然后执行稳定
  路径。下载或摘要失败不会覆盖上一个已验证版本。
- Host hook 在 DDB active 注册前执行；Edge hook 在 `install-edge.sh` 成功后执行。
  它们只作用于使用新 Launch Template 启动的实例，不会热改现有节点。

部署自动给对应 EC2 role 增加精确对象的 `s3:GetObject`，不会授予 bucket wildcard。
若对象使用客户管理的 KMS key（SSE-KMS），还需由客户另外授予该 role
`kms:Decrypt`，并在 key policy 中允许该 role；当前 `user_hooks` 配置不会自动修改
客户 KMS key policy。

## 安全要求

1. 每次发布使用新对象 key，例如 `host/node-exporter-v2.sh`，禁止覆盖旧 key。
2. 在可信构建环境生成 SHA256；先上传对象，再把 URI 和摘要提交到配置。
3. 脚本必须幂等、固定依赖版本、非交互，并对失败返回非零。
4. 不在脚本、URI、日志或 `config.yml` 中放凭据。运行时凭据使用 instance role、
   Secrets Manager 或 SSM Parameter Store，并保持最小权限。
5. 不从未校验的公网 URL pipe 到 shell。新增监听端口时同步收窄 Security Group，
   默认只绑定 loopback 或明确的私网地址。
6. 发布前做 shell 静态检查和隔离环境测试；生产先滚一台，再按 ASG instance
   refresh 逐步替换。

## node_exporter 示例（Host）

以下脚本在 Ubuntu Host 上使用发行版包幂等安装，并只监听 loopback。远端
Prometheus 抓取时，应另行改成明确的私网地址并只向 scraper Security Group 放行
9100，禁止向公网开放。

```bash
#!/bin/bash
set -euo pipefail

test "${OC_NODE_ROLE}" = "host"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y prometheus-node-exporter

install -d -m 0755 /etc/systemd/system/prometheus-node-exporter.service.d
cat >/etc/systemd/system/prometheus-node-exporter.service.d/listen.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus-node-exporter --web.listen-address=127.0.0.1:9100
EOF

systemctl daemon-reload
systemctl enable --now prometheus-node-exporter.service
systemctl is-active --quiet prometheus-node-exporter.service
```

发布并配置：

```bash
sha256sum node-exporter-hook.sh
aws s3 cp node-exporter-hook.sh \
  s3://customer-hooks/prod/host/node-exporter-v1.sh \
  --sse aws:kms --sse-kms-key-id <kms-key-arn>
```

将输出摘要和对象 URI 写入 `user_hooks.host`，补齐上述 KMS 权限后执行常规
`./setup.sh` 部署。先替换一台 Host，确认
`journalctl -u cloud-final` 中出现 `[oc:user-hook] PASS role=host`，再继续滚动。

## 排障与回滚

- 主日志：`/var/log/cloud-init-output.log` 或 `journalctl -u cloud-final`。
- `AccessDenied`：同时检查对象级 `s3:GetObject`、bucket policy、KMS key policy 和
  `kms:Decrypt`。
- `rc=1` 且下载成功：通常是 SHA256 不匹配；核对 S3 对象内容，不要绕过校验。
- `rc=124`：hook 超时。先优化脚本；确认确有必要后再提高上限。
- 回滚时把配置改回旧的不可变 URI 和摘要，再部署并逐台替换。稳定路径中的旧脚本
  只能保护下载/校验失败，不能替代 Launch Template 回滚。

Refs #390
