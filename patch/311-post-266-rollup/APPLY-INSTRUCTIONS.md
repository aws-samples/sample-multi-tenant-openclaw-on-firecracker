# AI Execution Instructions for Patch #311 (Post-266 Rollup)

你是帮客户把 266 之后的一批修复应用到运行中的 OpenClaw on Firecracker 部署的 AI。按顺序执行,
标注处向用户要信息。**严格按依赖顺序**(IAM → host 脚本 → Lambda → CDK),顺序错会中途失败。

## Step 0: Gather Information

向用户要:
```
1. host IP(metal 实例)+ SSH key 路径?(可能多台 host)
2. AWS region?(如 ap-southeast-1)+ account id?
3. host instance-role 名字?(不知道就从 host 上 `aws sts get-caller-identity` 的 ARN 取)
4. 你的 API 是私有 API(private API Gateway)还是公有?(决定是否需要 #298)
5. 你能跑 cdk deploy 吗?(能→最省事一次 deploy;不能→走热补分步)
6. 现在有卡 creating / 连不上 / 部署 ROLLBACK 的租户或栈吗?
```

## Step 1: IAM 授权(#307,最先做 —— fail-closed 前提)

host-scripts 层的 launch-vm.sh 恢复路径会 get-item `openclaw-tenant-secrets` 自取 token,
**读不到就中止启动**。所以必须在换 host 脚本 / 重建租户**之前**确保 host_role 有读权限。

先探针(host 上跑):
```bash
aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{"tenant_id":{"S":"__probe__"}}' --region <region>
```
- 返回 `{}` / 条目 → 已有权限,跳过本步。
- `AccessDeniedException` → 授权。**能 cdk deploy** 就留到 Step 4 的 deploy 一起(#307 已在 compute.py);
  **来不及 deploy** 用热补:
  ```bash
  bash iam/apply-iam.sh <host-role-name> <region> <account-id>
  # 再跑一次探针确认不再 AccessDenied
  ```

## Step 2: host 脚本层(#306 / #300 / #303-305 / #307 launch 侧)

### 2a. 当前 host 立即热替换(先备份,后 diff)

若你的部署跑的是**定制过的** launch-vm.sh,别盲替换:先 diff 确认只差这批修复的块;有其它差异就手工合。

```bash
# launch-vm.sh
ssh -i <key> ubuntu@<host> 'cp /home/ubuntu/launch-vm.sh /home/ubuntu/launch-vm.sh.bak.311'
scp -i <key> host-scripts/launch-vm.sh.patched ubuntu@<host>:/home/ubuntu/launch-vm.sh
ssh -i <key> ubuntu@<host> 'bash -n /home/ubuntu/launch-vm.sh && grep -c "DDB fallback\|WARN(#303)\|!= \"default\"" /home/ubuntu/launch-vm.sh'

# init-host.sh(#300;仅影响未来 host 开机,当前 host 不必替换运行态,见 2b)
```
回滚:`ssh ... 'cp /home/ubuntu/launch-vm.sh.bak.311 /home/ubuntu/launch-vm.sh'`

### 2b. 未来 host:上传到 S3(init-host.sh 开机拉的路径)

```bash
# 先在 host 上确认真实 S3 路径(别猜,定制部署可能不同):
grep -o 's3://[^ ]*launch-vm.sh' /var/log/openclaw-init.log
grep -o 's3://[^ ]*init-host.sh' /var/log/openclaw-init.log   # 若有
# 上传(公开仓默认路径 s3://<assets-bucket>/deployment/scripts/):
aws s3 cp host-scripts/launch-vm.sh.patched  <real-s3-launch-vm-path>  --region <region>
aws s3 cp host-scripts/init-host.sh.patched  <real-s3-init-host-path>  --region <region>
# 回读校验含修复:
aws s3 cp <real-s3-launch-vm-path> /tmp/v.sh --region <region>; grep -c 'DDB fallback' /tmp/v.sh
```

> init-host.sh 是 baked 进 Launch Template 的(base64+gzip),换 S3 只影响"开机后从 S3 拉"的部分;
> 若某修复在 baked 的那段里,严格生效要下次 cdk deploy 重烤 Launch Template。#300 的 output-key
> 修复在从 S3 拉的 init-host.sh 里,上传即可覆盖。

## Step 3: Lambda 代码层(#298 / #303-305 控制面)

见 `lambda/APPLY-LAMBDA.md`。能 cdk deploy 就留到 Step 4 一起;否则 `update-function-code` 单独更 API 函数。
- #298 只有**私有 API 姿态**需要(Step 0 问的第 4 点);公有 API 可跳。

## Step 4: CDK 层(#307 grant + #310 VPCE)—— 能 deploy 的最省事一步到位

**deploy 前必查 #310 VPCE 冲突**(见 `cdk/APPLY-CDK.md`):
```bash
aws ec2 describe-vpc-endpoints --region <region> \
  --filters "Name=service-name,Values=com.amazonaws.<region>.secretsmanager" \
            "Name=vpc-id,Values=<vpc-id>" \
  --query 'VpcEndpoints[].[VpcEndpointId,PrivateDnsEnabled]' --output text
```
- 有现存开 private DNS 的 → `config.yml` 设 `logging.aos.create_secretsmanager_vpce: false`(复用),
  否则 deploy 会冲突 ROLLBACK。

然后:
```bash
bash setup.sh <region> <profile-or-dash>    # "-" = instance role
# 或 cdk deploy OpenClawOrchestrator --require-approval never -c region=<region>
```
一次带上 #307(IAM)+ #310(VPCE)+ Lambda 代码(#298/#303-305)+ 触发 host 拉新脚本。

## Step 5: 处理受影响的租户/栈

- **栈还在 ROLLBACK**(#310 撞过):等 ROLLBACK 完成 → 按 Step 4 处理 VPCE 冲突 → 重新 deploy。
- **卡 creating 的租户**(#306/#307 导致):修好后 rebuild:
  ```bash
  curl -X DELETE "https://<api>/tenants/<tid>" -H "x-api-key: <key>"; sleep 15
  curl -X POST   "https://<api>/tenants" -H "x-api-key: <key>" -H "Content-Type: application/json" -d '{"tenant_id":"<tid>"}'
  ```

## Step 6: 验证(逐层)

```bash
# host(#306/#300):新建/重建一个租户,host 上看 launch 日志不再 rc=127、不再卡 step2
journalctl -t claw-launch --no-pager -n 100 | grep -iE 'DDB fallback|rc=127|step2'
# 权限(#307):探针不再 AccessDenied
aws dynamodb get-item --table-name openclaw-tenant-secrets --key '{"tenant_id":{"S":"__probe__"}}' --region <region>
# CDK(#310):栈 CREATE_COMPLETE,不因 VPCE 冲突 ROLLBACK
aws cloudformation describe-stacks --stack-name OpenClawOrchestrator --region <region> --query 'Stacks[0].StackStatus'
# 私有 API(#298):非 /ping 路由正常
curl -s "https://<api>/tenants" -H "x-api-key: <key>" | head
# 全链路:新建租户 → status=running、app_health=up → wss 能连(token 一致、无手工 approve)
```
