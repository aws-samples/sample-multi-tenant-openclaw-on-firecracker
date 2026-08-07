# 更新解决方案

本章定义 host、镜像、Lambda、配置和文档的更新路径。运行中的 host 或 microVM
不会因为 Amazon S3 对象变化自动更新；最终修复必须回到权威源并通过部署或重建收敛。

## 更新前

1. 记录当前 `gitlab/bb` SHA、部署区域、CloudFormation stack、Launch Template
   版本、host 镜像槽和租户状态。
2. 确认受影响资源、回滚点和验证计划。
3. 涉及数据、KMS、DynamoDB、RemovalPolicy 或删除时，先创建并确认所需快照为
   `available`。
4. 运行针对性测试、`cdk synth` 和文档/API 契约检查。

## Host 与基础设施

`init-host.sh` 由 CDK 发布到不可变 S3 key
`deployment/bootstrap/host/<sha256>/init-host.sh`。Launch Template bootstrap
下载后验证 SHA-256，再原子执行。修改 host bootstrap 或 host 脚本的流程：

```text
change source
  -> cdk deploy / setup.sh uploads immutable assets
  -> new Launch Template version
  -> replace one canary host
  -> verify
  -> ASG instance refresh in batches
```

仅修改 S3 latest 对象不会更新已运行 host。详见
[Host 脚本、镜像与配置的生效边界](16-hot-swap-vs-baked-and-host-rebuild.md)。

## 黄金镜像

镜像更新走 live/canary 双槽：

1. 构建版本化镜像。
2. `POST /create-image-snapshot` 创建非空 label 的快照。
3. `POST /hosts/{id}/pull-image?slot=canary` 拉取到一台测试 host。
4. 轮询 `pull-image-progress`，并读取 `image-slots`。
5. 创建固定到该 canary snapshot 的测试 tenant。
6. 完成应用健康、身份、网络、日志、数据保留和回滚验证。
7. `promote-canary` 后分批扩展。

回滚不是独立 API：把已保留的旧 snapshot pull 到 `slot=live`。未提升 canary 可由
下次 canary pull 覆盖；无人引用的版本由 `reclaim-images` 回收。

## Lambda 与控制面

Lambda、API Gateway、DynamoDB 索引和 IAM 更新通过 AWS CDK 部署。更新前后核对：

- OpenAPI 与实际路由一致；
- API-key-only 默认角色、Cognito/IAM authorizer 和 platform scope 未放宽；
- 异步 consumer、API alias 和 event source 指向同一发布版本；
- 表和索引状态为 `ACTIVE`；
- 失败重试不会重复创建或丢失租户操作。

不可逆表结构、RemovalPolicy、KMS、SG/IAM 与凭据边界变更需要人工评审。

## 配置

`config.yml` 变化按其交付面生效：

- CDK 资源、Lambda env、ASG 或网络变化：重新部署。
- host bootstrap/脚本变化：重新部署并替换 host。
- 镜像、skill、persona、guest 配置：重烤并按 canary 发布。
- per-tenant 冷注入配置：在受控 restart/rebuild 时生效。

不要把运行中资源的手工修改作为最终状态。

## 更新后验证

验证证据绑定合并后的 commit 和部署版本：

- CloudFormation / Launch Template / image snapshot 与目标版本一致；
- 新 host bootstrap SHA 与仓库产物一致；
- 至少一个真实 tenant 完成 create、chat、restart/rebuild 和 delete/backup 相关回归；
- 多租户场景不串数据，重试和并发可收敛；
- 监控、日志、告警和回滚入口可用。

任何失败、跳过或版本不一致都保持更新未完成。
