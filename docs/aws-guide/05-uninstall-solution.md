# 卸载解决方案

卸载会触及持久化数据、备份和加密密钥。执行前先确认目标账号、区域、stack、数据
保留合同和恢复责任。未知环境按生产处理。

## 卸载前

1. 列出 active tenant、host、备份、镜像 snapshot、DynamoDB 表、Amazon S3 桶和
   KMS keys。
2. 为 EC2/EBS、RDS（如启用）和其他有状态资源创建快照，并等待状态为
   `available`。
3. 验证至少一次恢复路径。
4. 导出审计和合规所需证据。
5. 取得资源 owner 的明确删除确认。

> **Important**
>
> Amazon S3 Object Lock COMPLIANCE 保留期内，连 root 用户也不能删除对象版本或
> 缩短保留期。不要尝试绕过；保留桶并等待到期。

## 选择卸载范围

| 范围 | 行为 |
| --- | --- |
| 停止业务流量 | 停止新建和入口流量，保留全部数据 |
| 删除可重建计算 | 回收 edge/host/Lambda 等计算，保留数据资源 |
| 删除 stack | 按 RemovalPolicy 删除或保留资源 |
| 完全退役 | 在保留合同允许后删除残留表、桶、版本和密钥 |

先执行最小范围并验证，再进入下一层。不要把 `DELETE_FAILED` 当成功。

## Stack 卸载

使用仓库的 `scripts/destroy.sh` 或 AWS CDK 标准流程。运行前先查看帮助，确认
profile、region 与 stack 名称。命令完成后检查 CloudFormation 事件，而不是只看
shell exit code。

数据保留区中的 DynamoDB 表和 WORM 桶可能使用 `RETAIN`，因此 stack 删除后仍存在。
可重建区可能使用 `DESTROY` 与自动清空，但仍需确认实际模板。

## 残留资源

按资源类型读取当前状态：

- DynamoDB：列出 `openclaw-*` 表、PITR 与 GSI 状态。
- Amazon S3：列出 bucket versioning、Object Lock、所有版本和 delete markers。
- KMS：确认 key policy、依赖密文和 deletion state。
- EC2：确认 ASG、Launch Template、EBS、ENI 与安全组。
- CloudWatch/OpenSearch：确认日志保留与导出责任。

只有 owner 确认且恢复证据成立后才删除。版本化 S3 bucket 必须同时处理对象版本和
delete markers；WORM 对象等保留期结束。

## KMS

在安排 key deletion 前，证明所有依赖密文已不需要恢复。KMS key deletion 有等待
窗口；窗口内可取消，过期后密文不可恢复。记录 key ARN、等待窗口、批准人和恢复影响，
不要在文档或日志中记录密钥材料。

## 完成判据

- 目标 stack 不存在或达到预期终态；
- 所有保留资源都有 owner、保留原因和后续日期；
- 不存在非预期 host、EBS、ENI、NAT、OpenSearch 或日志成本；
- 备份与审计满足合同；
- KMS key 状态与数据恢复决定一致；
- 最终清单记录命令、退出码、时间和证据，不含凭据或主机坐标。

详细 imported VPC 与手工残留排查见独立
[客户部署与删除手册](06-customer-deploy-and-teardown.md)。
