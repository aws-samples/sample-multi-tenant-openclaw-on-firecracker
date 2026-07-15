# 关键脚本:哪些热替换、哪些烤死、各自生效路径 + host 一键重建

> 面向运维:改一个脚本后"下次 VM 启动就继承"还是"必须重建 host"?本文把边界钉死,避免"改了没生效"或"以为要重建其实热拉即可"。覆盖 R18 的 14.2(热替换边界文档 + init-host 一键重建路径)。

## 一、两类脚本的边界

### A. 热拉脚本(改 S3 → 下次 VM 启动继承,不需重烤镜像/不需重建 host)

`init-host.sh` 启动时从 `s3://${ASSETS_BUCKET}/deployment/scripts/` 拉这些(走 `_s3_get` 重试骨架 `init-host.sh:46`):

| 脚本                         | 拉取点                           | 改它怎么生效                                           |
| ---------------------------- | -------------------------------- | ------------------------------------------------------ |
| `host-agent.py`              | `init-host.sh:289-290`           | S3 覆盖 → host 重启(或下次 host 起)拉新版              |
| `route_ops.py`               | `init-host.sh:294-295`           | 同上;host-agent import 它,host 重启生效                |
| `launch-vm.sh`               | `init-host.sh:413-414`           | S3 覆盖 → **下次 VM 启动**即用新版(每次 launch 都热拉) |
| `harden-config.sh`           | launch-vm 内热拉(同 launch 路径) | 下次 VM 启动继承                                       |
| `setup-egress-allowlist.sh`  | `init-host.sh:277-278`           | S3 覆盖 → host 重启拉新版                              |
| `openclaw.json` / restore 盘 | launch-vm 内热拉                 | 下次 VM 启动继承                                       |

**改法**:`aws s3 cp <新版> s3://${ASSETS_BUCKET}/deployment/scripts/<脚本>` → 影响下次 VM/host 启动。不需重烤黄金镜像、不需改 launch template。

### B. 烤死脚本(改它必须"改 launch template → 滚动重建 host")

| 脚本                                     | 为什么烤死                                                                                                                                        | 改它的路径                                                |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `init-host.sh` 本身                      | 烤进 launch template user-data(16KB 硬限,`init-host.sh:270-272` 注释自认脚本外置正是为避免撑爆 user-data);它是"拉其它脚本的那个脚本",不能自己热拉 | 改 launch template → 更新版本 → ASG 滚动/单台重建(铁律#3) |
| 黄金镜像内容(rootfs/skills/persona/护栏) | 冷注入只读盘,启动即成品                                                                                                                           | 改 build-rootfs → CodeBuild 烤新镜像 → 滚动重建(铁律#3)   |

判据:**这个脚本是不是被 init-host 从 S3 拉的?** 是 → 热拉(改 S3 即可);否(它本身在 user-data 里,或在只读镜像盘里)→ 烤死(改部署代码 → 重建)。

## 二、host 一键重建运维路径(纪要2:切镜像/跑脚本全靠手工无 API)

改烤死脚本(init-host.sh)或修 host AMI 时的可复现操作序列,避免"只有原作者知道怎么重建":

1. **改 user-data**:改 `init-host.sh`(或 launch template 里引用的 user-data 模板)。
2. **更新 launch template 版本**:`cdk deploy` 会渲染新 user-data 生成 launch template 新版本;或手工 `aws ec2 create-launch-template-version`。
3. **滚动/单台重建**:
   - 单台修复:`aws autoscaling terminate-instance-in-auto-scaling-group --instance-id <id> --should-decrement-desired-capacity false`(ASG 用新 launch template 版本拉新)。
   - 全量滚动:`aws autoscaling start-instance-refresh --auto-scaling-group-name openclaw-hosts-asg`(灰度滚动,min healthy % 控爆炸半径)。
4. **验证**:新 host 注册 DDB(`init-host.sh` 注册段)+ lifecycle hook CONTINUE(`asg.lifecycle_hook_timeout` 内),再 chat/dashboard 数据面端到端可达。

> host AMI 修复(R18-E N13)复用同一序列:改镜像 → CodeBuild 烤 → 更 launch template image → instance-refresh。

## 三、本文档边界

- 本文只文档化既有机制(热拉/烤死已在 `init-host.sh` 实现),**未改任何脚本**。
- 去 host nginx(14.1)、死代码清理(14.3)、日志→CloudWatch(14.4,碰 host IAM)等 R18 改动碰网络/IAM,留独立 MR + 人工评审,拓扑核实证据见 `internal-docs/00-knowledge-base/evidence/r18-topology-confirm-2026-07-12.md`。
