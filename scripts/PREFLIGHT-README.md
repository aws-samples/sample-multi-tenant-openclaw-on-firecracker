# preflight-check.sh — 部署前配置冲突预检(使用说明)

> 面向对象:运维人员 **或 Claude Code(AI 助手)**。若你是 AI,把本文件当操作 SOP 逐步执行,
> 遵守文末的「判读纪律」和「删除安全红线」——这些是真机反复踩坑后沉淀的,违反会造成部署失败或误删生产资源。

## 这个脚本是干什么的
客户写好自己的 `config.yml` 后,在 `cdk deploy` / `setup.sh` **之前**跑一次,提前把"这次部署会撞什么"列出来,
避免部署 30 分钟后才 CREATE_FAILED / 回滚。它**完全 base 客户自己的 config**(VPC/子网/证书/redis/logging 全从 config 读),
不含任何特定环境的硬编码,可直接对外。

它检查两类问题:
1. **config 静态契约**——配置违反 CDK 栈的 fail-loud 规则(部署时 synth 直接报错的那些),纯读 config,不调 AWS。
2. **环境冲突/残骸**——上一轮部署留下的、会挡这次部署的东西(ROLLBACK 空壳栈、RETAIN/WORM S3 桶、DDB 表、
   冲突的 VPC Endpoint),以及证书是否存在/区域对、配额够不够——读 config 值 + `aws describe` 实查。

**它只读,绝不删任何东西。** 需要删的残骸,它会打印【建议删除命令】,由你确认后手动执行。

## 前置条件
- `python3`(带 pyyaml:`python3 -c 'import yaml'` 不报错;缺则 `pip install pyyaml`)
- `aws` CLI + 有效凭据(能访问目标账号/区域)
- 客户的 `config.yml`

## 用法

### 模式 1:完整预检(部署前跑)
```bash
bash preflight-check.sh <config.yml> <region> [--profile <p>|-]
```
- `<region>`:如 `ap-southeast-1`
- `--profile <p>`:AWS profile 名;若用 instance role / 环境变量凭据,传 `-` 或省略
- 例:`bash preflight-check.sh config.yml ap-southeast-1 -`

**退出码**:`0`=无 BLOCK(可部署,可能有 WARN 需确认);`1`=有 BLOCK(先解决);`2`=用法/凭据错误。

### 模式 2:只列残骸 + 删除命令(删旧环境时跑)
```bash
bash preflight-check.sh <config.yml> <region> [--profile|-] --list-residue
```
跳过 config 契约/证书/配额,只查"会挡下一轮部署的残骸"并给出删除命令。**删旧测试环境后跑这个确认清干净了没。**

## 输出怎么读
- `🔴 BLOCK` —— 不解决必然导致部署失败/回滚。**必须先处理。**
- `🟡 WARN` —— 可能有问题、静默失效、或需人工确认。逐条看,确认无碍再部署。
- `✅ PASS` —— 该项通过。
- 末尾「建议处理命令」段:每条 BLOCK 残骸对应一条可照抄的删除命令(区分 WORM 桶的特殊处理)。

## 典型工作流(客户下午部署)
```
1. 写好 config.yml
2. bash preflight-check.sh config.yml <region> <profile>      # 完整预检
3. 若有 BLOCK:
     - 残骸类 → 按末尾「建议处理命令」删除,删完再跑一次直到无残骸
     - config 类 → 改 config 里对应的键,再跑
4. 全部 PASS(或 WARN 已确认)→ cdk deploy / setup.sh
```

## 删旧测试环境的完整流程(重点:客户环境常是 protect_stateful_resources: true)
客户测试环境若用 `deploy.protect_stateful_resources: true` 部署过,DDB 表 + 备份桶是 **RETAIN + WORM Object Lock**,
`cdk destroy` **删不掉它们**,会留残骸挡下次部署。正确顺序:

```
1. cd <repo>; nohup cdk destroy --all --force -c cf_origin_facing_prefix_list=<pl> > /tmp/destroy.log 2>&1 &
2. 轮询直到两栈都 does-not-exist(关键!别发起就当删完;OpenSearch/Redis 慢,20-40min):
     watch -n 60 'aws cloudformation describe-stacks --stack-name OpenClawOrchestrator --region <region> 2>&1 | tail -1'
     # 直到报 "Stack ... does not exist";DELETE_FAILED 则看 events,RETAIN 资源用
     #   aws cloudformation delete-stack --stack-name <s> --deletion-mode FORCE_DELETE_STACK  或  --retain-resources <那些资源>
3. bash preflight-check.sh config.yml <region> <profile> --list-residue    # 确认残骸清单
4. 按清单删剩余残骸(孤儿 VPCE / RETAIN DDB 表 / 非 WORM 桶);WORM 桶删不掉 → config 设 s3.backup_bucket_suffix 换名避开
5. 再跑 --list-residue 直到「无残骸」→ 才开始新部署
```

## 判读纪律(避免误判,真机踩坑沉淀)
1. **CFN 安静期 <40min 不算"卡住"**:正常 deploy 20-29min,某些资源(AOS 域/EdgeASG 后)有正常静默期,别急着判失败。
2. **host 健康看 DDB 真实字段** `instance_id`/`total_mem_mb`,`status=active` 或 `idle` 都是健康(不是 `host_id`/`ip`)。
3. **判"残骸"前先核时间戳**是不是本轮自己产生的(别把正在跑的这轮的资源当成旧残骸)。
4. **api.mode=private 从 VPC 外 curl 失败(DNS 解析/Missing Authentication Token)是正常私有姿态**,不是部署故障。
5. **发起 ≠ 完成**:任何 destroy/delete 是异步,必须轮询到资源真消失才算删净——不能发起就报"已删除"。

## 删除安全红线(客户账号 = 可能含生产资源,务必遵守)
- **只删 `openclaw-*` 命名、且确认是本套测试部署残骸的资源**;不确定是不是生产数据 → 先问人,别删。
- **绝不删 imported VPC / 子网本身**(那是客户自己的网络,栈只是 imported 进来;删栈不该删它们)。
- **RETAIN 的 DDB 表可能含真实租户/审计数据**——删 `delete-table` 前必须确认是可丢弃的测试残骸。
- WORM/Object Lock 桶 root 都删不掉,别硬删,换 `s3.backup_bucket_suffix`。
- 每个删除命令**由人确认后手动执行**,脚本只建议不执行。

## 检查覆盖的坑(来源)
残骸撞名(坑A)· Secrets Manager + execute-api VPCE 私有 DNS 冲突(坑F/坑F++)· imported 子网契约(数量/归属/AZ)·
ACM 证书区域(bff=区域内 / cloudfront=us-east-1)· config fail-loud(api.mode / redis.engine / master_nodes /
bff_ingress 禁 0.0.0.0/0 / dispatch 互斥 / edge 需 redis / node_type 键名)· metal vCPU 配额 · CloudFront prefix list ·
ROLLBACK 空壳栈。**边界**:代码级 IAM/逻辑 bug(如某版本 SetDefaultLTVersion 漏 ec2:ModifyLaunchTemplate 权限)
预检查不出,只能靠 `cdk synth` + 用经过验证的 commit。
