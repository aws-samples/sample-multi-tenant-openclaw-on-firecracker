# 实施指南目录

本目录是 OpenClaw on Firecracker 多租户 AI Agent 平台的实施指南章节源。出版物位于 [`../OpenClaw-on-Firecracker-实施指南.pdf`](../OpenClaw-on-Firecracker-实施指南.pdf)。

## 章节一览

| 章节                                                                          | 主题                                                           |
| ----------------------------------------------------------------------------- | -------------------------------------------------------------- |
| [00 快速上手与部署运行手册](00-quickstart-and-runbook.md)                     | 一份自包含的"从零到跑通"，含结论、系统价值、部署清单与常见排障 |
| [01 解决方案概述](01-overview.md)                                             | 场景、卖点、关键概念、边界                                     |
| [02 架构概述](02-architecture.md)                                             | 参考架构、组件、高层数据流、Well-Architected 原则              |
| [03 架构详情](03-architecture-details.md)                                     | 逐组件、逐数据流、五层纵深防护展开                             |
| [04 规划部署](04-plan.md)                                                     | 成本、安全、区域、服务配额                                     |
| [05 部署解决方案](05-deploy-use-troubleshoot.md)                              | 完整部署 → 使用 → 排障                                         |
| [更新解决方案](05-update-solution.md)                                        | host、镜像、Lambda 与配置更新                                  |
| [卸载解决方案](05-uninstall-solution.md)                                     | 数据保护、stack 删除与残留资源                                 |
| [客户部署与删除手册](06-customer-deploy-and-teardown.md)                     | 独立 source-only runbook；复用网络与详细清理                    |
| [06 开发人员指南](06-developer-guide.md)                                      | 认证模型、控制面 API 设计、授权模型                            |
| [07 功能详细说明](07-features.md)                                             | Agent 面向终端用户的能力清单与控制面功能                       |
| [08 参考与术语](08-reference.md)                                              | 数据收集说明、贡献者、术语表、修订记录                         |
| [09 控制面 API 对接](09-api-integration.md)                                   | 可拷贝的调用参考、逐端点示例、错误码                           |
| [自带 LLM 网关](09-custom-llm-gateway.md)                                     | LiteLLM / OpenAI-compatible 接入                               |
| [11 组件运维手册](11-ops-maintenance.md)                                      | 日常维护、监控、告警、扩缩容、故障排查                         |
| [12 控制面 API Gateway 加固 + 凭据 KMS 加密注入](12-private-api-hardening.md) | 生产附录，两件事 + 可跑 demo                                   |
| [13 数据面两级路由](13-data-plane-redesign.md)                                | 2026-07-08 转型后的实时聊天链路                                |
| [14 十万级规模化](14-scale-100k.md)                                           | 测试 / 上线 / 生产三阶段规模化红线                             |
| [15 交付边界与责任矩阵](15-delivery-boundary-and-responsibility.md)           | 部署后必配项清单 + 配置责任矩阵                                |
| [15 镜像构建快速上手](15-image-build-getting-started.md)                      | 第一次给项目构建镜像的入门                                     |
| [补充 API 接入手册（中文备份）](API-接入手册.md)                             | source-only 备份，正式对接以第 9 章为准                         |
| [Host 脚本、镜像与配置边界](16-hot-swap-vs-baked-and-host-rebuild.md)          | 生效矩阵、host 重建与诊断性热补                                |
| [17 可观测性运维手册](17-observability-ops.md)                                | 部署后维护：三层可观测性巡检、容量、告警、排查路径             |
| [18 Host/Edge S3 user-hook](18-s3-user-hooks.md)                             | 客户扩展脚本契约、安全边界、发布与排障                         |

`00-quickstart-and-runbook.md`、`06-customer-deploy-and-teardown.md` 与
`API-接入手册.md` 是独立 source-only 手册，不重复装订进 PDF。PDF 按
“概述 → 架构 → 规划 → 部署/使用/排障 → 更新 → 卸载 → 开发 → 专题 →
参考 → 术语 → 修订”排序。

## 推荐阅读路径

- **新手快速上手**：[00 快速上手](00-quickstart-and-runbook.md) → [15 镜像构建入门](15-image-build-getting-started.md) → [05 部署解决方案](05-deploy-use-troubleshoot.md)
- **API 对接方**：[01 解决方案概述](01-overview.md) → [09 控制面 API 对接](09-api-integration.md) → [06 开发人员指南](06-developer-guide.md)
- **生产运维**：[11 组件运维手册](11-ops-maintenance.md) → [17 可观测性运维手册](17-observability-ops.md) → [12 API Gateway 加固](12-private-api-hardening.md) → [13 数据面两级路由](13-data-plane-redesign.md) → [14 十万级规模化](14-scale-100k.md)

## 权威数字与工程知识源

对外一切性能/容量/隔离/拦截数字以 `engineering/02-system-constraints/FACT-BASELINE.md` 为唯一真相源，本指南各章引用的数字均可回查该表。架构现状读 `engineering/00-knowledge-base/map.md`。
