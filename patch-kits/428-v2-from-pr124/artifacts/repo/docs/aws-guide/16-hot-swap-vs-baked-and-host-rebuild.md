# Host 脚本、镜像与配置的生效边界

本章说明修改后何时生效。核心原则：运行中的 host 和 microVM 不会因为 Amazon S3
对象变化而自动更新。热补只用于验证假设，最终修复必须回到仓库、CDK、镜像或
bootstrap，并通过重建收敛。

## 1. 交付矩阵

| 改动 | 权威源与交付方式 | 已运行资源是否自动更新 | 正确生效路径 |
| --- | --- | --- | --- |
| `init-host.sh` | CDK 渲染后发布到 `deployment/bootstrap/host/<sha256>/init-host.sh`；Launch Template 小 bootstrap 下载并校验 | 否 | `cdk deploy` 生成新不可变 key/LT 版本，再滚动重建 host |
| `host-agent.py`、`route_ops.py`、`launch-vm.sh`、`stop-vm.sh` 等 host 脚本 | `setup.sh` 上传到 assets bucket；新 host 的 `init-host.sh` 下载到 `/home/ubuntu/` | 否 | 上传新资产后重建 host；不能只改 S3 就声称现有 host 已更新 |
| rootfs、data-template、immutable 盘 | 版本化镜像快照与 host live/canary 槽 | 否 | build → snapshot → pull canary → 验证 → promote/rollout |
| config template | S3 `templates/openclaw/<name>/openclaw.json` | 已运行 VM 否 | 新建或 rebuild 时重新注入 |
| tenant gateway/device/vkey/skills | DynamoDB/KMS/S3 状态，`launch-vm.sh` 启动前冷注入 | 已运行 VM 否 | restart/rebuild 走受控生命周期；不得向运行中 VM 热灌 |
| S3 user-hook | config 指定的 versioned object + SHA-256，bootstrap 时下载执行 | 否 | 更新 version/SHA 后重建目标 host/edge |

`launch-vm.sh` 在已有 host 上从本地路径执行。它不会在每次起 VM 前重新下载自己的
脚本。因此“覆盖 S3 后下次 VM 启动自然用新版”是错误操作口径。

## 2. Host 重建

1. 在仓库修改权威源。
2. 运行相关静态/单元测试和 `cdk synth`。
3. 执行 `cdk deploy` / `setup.sh`，确认 bootstrap 与 host 资产上传成功。
4. 记录当前 Launch Template 版本、目标 ASG、host 与 tenant 清单。
5. 对一台测试 host 做 canary replacement。
6. 验证 bootstrap SHA、DynamoDB host 注册、host-agent、Firecracker、路由、
   日志和至少一个真实 tenant 生命周期。
7. 通过后使用 ASG instance refresh 分批滚动，并设置健康百分比控制爆炸半径。

新 host 的 bootstrap 日志是 `/var/log/openclaw-bootstrap.log`。它下载
`init-host.sh` 后校验完整 SHA-256，原子安装到
`/var/lib/cloud/init-host.sh`。下载、摘要或脚本失败必须触发 lifecycle
`ABANDON`，不能注册为 active。

## 3. 镜像发布

不要用 host replacement 代替镜像版本管理。镜像变更走：

```text
build image
  -> POST /create-image-snapshot
  -> POST /hosts/{id}/pull-image?slot=canary
  -> GET pull-image-progress + image-slots
  -> create pinned canary tenant
  -> verify
  -> POST promote-canary
  -> staged rollout
```

回滚不是独立接口：将已保留的旧 snapshot pull 到 `slot=live`。未提升 canary 可被
下一次 canary pull 覆盖；无人引用的版本由 `reclaim-images` 回收。

## 4. 诊断性热补

`copy-file-from-s3` 或 SSH/SSM 临时替换文件只用于复现和验证假设。使用时必须：

- 记录原文件摘要和备份；
- 限定单台测试 host；
- 记录命令、退出码和恢复步骤；
- 不把临时状态当完成证据；
- 随后把相同修复落回权威源并走重建。

涉及删除、回收或替换数据盘时，先创建并确认快照为 `available`，再执行任何不可逆
步骤。

## 5. 验收

完成声明必须绑定本次部署的 commit、Launch Template 版本、bootstrap SHA、host
实例、镜像 snapshot/slot 和验证时间。只看到 S3 对象已更新、CDK 已返回成功或进程
已启动，都不足以证明新资源使用了新版本。
