# Patch #311 · 第 2 层:Lambda 代码修复(需重部署函数)

这一层的修复在**控制面 API Lambda** 里,不能靠替换 host 脚本生效,必须更新 Lambda 函数代码。

## 涉及的修复

| 修复 | 文件 | 解决什么 | 谁需要 |
| ---- | ---- | -------- | ------ |
| #298 | `deploy/lambda/api/handler.py` | 纯私有 API(`{proxy+}` 集成)下 handler 按 resource 分发与 `/{proxy+}` 对不上,除 `/ping` 外全 404 → 数据面基准跑不通 | **只有走私有 API 姿态的部署需要**(公有 API 网关不受影响) |
| #303/#304/#305 | `deploy/lambda/api/services/tenant_service.py`、`host_service.py` | rebuild/restart 语义:升级必须走 rebuild(丢 overlay + 采用校验),restart 只软重启;升级采用校验防版本谎报;存量数据盘遇模板尺寸漂移不重建(防数据丢失) | 所有做镜像升级 / rebuild 的部署 |

## 怎么应用

这些是 CDK 管理的 Lambda(源码在 `deploy/lambda/api/`)。两种更新方式,任选其一:

### 方式 A:整栈 `cdk deploy`(推荐,一并带上第 3 层 CDK 修复)

```bash
# 从仓库根,用你的部署方式(见主 README / setup.sh):
bash setup.sh <region> <profile-or-dash>   # profile 传 "-" = 走 instance role
# 或直接: cdk deploy OpenClawOrchestrator --require-approval never -c region=<region>
```

`cdk deploy` 会把 `deploy/lambda/api/` 的最新源码重新打包上传到 API Lambda,#298 + #303-305 一起生效。**这也是第 3 层(#307 IAM grant / #310 VPCE 开关)生效的方式** —— 一次 deploy 全带上,最省事。

### 方式 B:只更新 API Lambda 函数代码(不做全栈 deploy)

若你只想更新函数、不动其它资源:

```bash
# 1. 打包 API Lambda 源码(从仓库根)
cd deploy/lambda/api && zip -r /tmp/api-lambda.zip . && cd -
# 2. 找到 API 函数名(从栈输出或控制台;通常含 "OpenClawOrchestrator" + "ApiFn")
FN=$(aws lambda list-functions --region <region> \
  --query "Functions[?contains(FunctionName,'ApiFn')].FunctionName" --output text)
# 3. 覆盖代码
aws lambda update-function-code --function-name "$FN" \
  --zip-file fileb:///tmp/api-lambda.zip --region <region>
```

> ⚠️ 方式 B 只更新代码,**不会**带上第 3 层的 IAM grant(#307)和 VPCE 开关(#310)。若你的环境还没打 host_role 读 tenant-secrets 权限,先跑 `../iam/apply-iam.sh`(见 APPLY-INSTRUCTIONS 的依赖顺序)。

## 验证

- **#298**:私有 API 下 `curl` 一个非 `/ping` 路由(如 `GET /tenants`),应正常返回而非 404。
- **#303-305**:对一个租户做 rebuild(镜像升级路径),确认:① 存量数据盘保留(没被重建清空);② 版本号只在 VM 真起在新 rootfs 后才更新(不谎报)。
