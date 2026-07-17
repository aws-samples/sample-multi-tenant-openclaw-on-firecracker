# Patch #311: Post-266 Rollup (deployment + data-plane fixes)

承接 `patch/266-token-drift-fix/`。266 修了 gateway token 漂移(恢复路径)。**本 patch 打包
266 之后的全部客户运行时修复**,让你拿一份就能把这批修复一次应用上。

## 这些 patch 修了什么

| 修复 | 现象 | 根因 | 层 |
| ---- | ---- | ---- | -- |
| #306 | VM 创建时 `launch-vm.sh` 报 `log: command not found` (rc=127),VM 卡 creating/down | token 回读段调 `log()`,但 `log()` 定义在更靠后;`set -e` 下调用返 127 立即退出——恰好卡在回读成功的路径上 | host 脚本 |
| #303/#304/#305 | 升级 rootfs 后 VM 仍跑旧代码 / 升级 rebuild 误删存量数据盘 / 版本号谎报 | restart 保留旧 overlay(半新半旧);rebuild 遇数据盘尺寸漂移会重建盘丢数据;版本号不验真起在新 rootfs 就标新 | host 脚本 + Lambda |
| #298 | 纯私有 API(`{proxy+}` 集成)下除 `/ping` 外所有路由 404,数据面基准跑不通 | handler 按 resource 模板分发,但私有 API 的 `event["resource"]` 恒为 `/{proxy+}`,对不上 | Lambda |
| #300 | host 开机 `init-host.sh` 卡 step2 → host 全 ABANDON、0 healthy host | `_stack_output` 查了个被 CDK 前缀化后不存在的 output key,每次开机干烧 5min 静默重试 | host 脚本 |
| #307 | 纯 CDK 部署时 host 起 VM 报 AccessDenied → 卡 creating | #290 DDB fallback 要 host_role 读 `openclaw-tenant-secrets`,CDK 没授这个权限 | CDK(IAM) + host 脚本 |
| #310 | `cdk deploy` 因 secretsmanager VPCE `private-dns-enabled ... conflicts` 整栈 ROLLBACK | 无条件建开 private DNS 的 secretsmanager VPCE;VPC 已有一个时冲突(AWS 同服务同 VPC 只许一个) | CDK |

## 三层应用机制(关键:不是所有改动都能"替换文件")

266 只碰 host 脚本,能纯替换。**本 patch 跨三层**,按你的部署能力选择:

1. **host 脚本层(可热替换)** — `host-scripts/`:`launch-vm.sh.patched`(含 #306+#303-305+#307 的 launch 侧)、`init-host.sh.patched`(含 #300)。scp 到 host 直接换 + 上传 S3 供未来 host。
2. **Lambda 代码层(需重部署函数)** — `lambda/APPLY-LAMBDA.md`:#298 + #303-305 的控制面代码,靠 `cdk deploy` 或 `update-function-code`。
3. **CDK / 权限层(需 deploy 或手工 inline policy)** — `cdk/APPLY-CDK.md` + `iam/`:#307 host_role 读权限(fail-closed 前提)、#310 VPCE 幂等门。

## ⚠️ 依赖顺序(硬约束,顺序错会中途失败)

```
① IAM 授权(#307)——最先做,fail-closed 前提
   纯 CDK 走 cdk deploy;来不及则 iam/apply-iam.sh 打 inline policy
      ↓
② host 脚本替换(#306/#300/#303/#307 launch 侧)——换文件 + 上传 S3
      ↓
③ Lambda 重部署(#298/#303-305 控制面)——cdk deploy 或 update-function-code
      ↓
④ CDK deploy(#310 VPCE)——deploy 前先查 VPC 有没有现存 secretsmanager VPCE,
   有则 config 设 create_secretsmanager_vpce: false(见 cdk/APPLY-CDK.md)
```

**最省事**:①(或确认已有权限)后,直接一次 `cdk deploy` 把 ②③④ 全带上(deploy 会重打 Lambda 代码 + 更新 IAM + VPCE + 触发 host 拉新脚本)。分步是给"只想热补一部分"的场景。

## Files

| 路径 | 用途 |
| ---- | ---- |
| `README.md` | 本文件:问题清单 + 分层机制 + 依赖顺序 |
| `APPLY-INSTRUCTIONS.md` | AI 可执行的分步应用指南(IAM 前置、按依赖顺序) |
| `host-scripts/launch-vm.sh.patched` | 含 #306/#303-305/#307 的完整 launch-vm.sh,直接替换 |
| `host-scripts/init-host.sh.patched` | 含 #300 的完整 init-host.sh,上传 S3 供未来 host |
| `iam/host-role-tenant-secrets.json` | #307 host_role 读 tenant-secrets 的 inline policy(占位需填 region/account) |
| `iam/apply-iam.sh` | 幂等打上述 inline policy(非 CDK 部署时的热补) |
| `lambda/APPLY-LAMBDA.md` | #298 + #303-305 Lambda 代码怎么重部署 |
| `cdk/APPLY-CDK.md` | #307 grant + #310 VPCE 幂等门 + 残留 VPCE 冲突处理 |

## 验证总纲

- host 起 VM 不再 rc=127(#306)、不再卡 step2 ABANDON(#300)。
- get-item `openclaw-tenant-secrets` 探针不再 AccessDenied(#307)。
- `cdk deploy` 到 CREATE_COMPLETE,不因 VPCE 冲突 ROLLBACK(#310)。
- 私有 API 非 `/ping` 路由正常(#298);rebuild 保留数据盘 + 版本号如实(#303-305)。
- 逐项验证步骤见各层的 APPLY 文档。
