# samples/profiles — 按场景一键部署的 config 预设(#274 层 1)

`cp samples/profiles/<name>.yml config.yml`,补齐该场景标 `<必填>` 的坐标,再 `./setup.sh <region> <aws-profile>`。
`setup.sh` 在 `cdk deploy` 之前会自动跑 `scripts/preflight-check.sh`(#489 焊死,`PREFLIGHT_SKIP=1` 是显式逃生开关)。

## 三个场景

| profile | 网络 | API / ALB | 组件 | 有状态资源 | 需要客户坐标 |
| --- | --- | --- | --- | --- | --- |
| `private-enterprise` | `imported`(客户已有 VPC) | `private` + internal ALB,无 CloudFront | edge + redis(valkey) 全开 | RETAIN + WORM | **是,8 个** |
| `public-demo` | `self_managed`(CDK 自建 /20 VPC) | `edge` + 公网 ALB + CloudFront | 最小(无 edge ASG、无 AOS 日志) | DESTROY(可重建) | 否 |
| `minimal-test` | `default_vpc` | `edge` + 公网 ALB,无 CloudFront | 最小 + 单 AZ | DESTROY(可重建) | 否 |

## 设计口径(读之前先知道这三条)

1. **profile 只写「场景决定项 + CDK 硬必填键」,不复制 `config.yml.example` 的全部键。**
   `deploy/stack.py` 是 `CFG = yaml.safe_load(config.yml)`,**不做默认值合并** —— 被代码
   直接下标(`CFG["a"]["b"]`)的键缺一个就是 `cdk synth` KeyError,这些键每个 profile 都
   显式写全;其余带 `.get(key, default)` 的键留给代码默认值。
   刻意**不**把 `config.yml.example` 抄成三份:同一个数字在仓库里出现四份,只会各自漂移
   (本仓已有 5 份近重复 sample 在 `alb.internal` / `protect_stateful_resources` 上互相
   矛盾,就是这么来的)。性能/容量调优值请对照 `config.yml.example` 的同名段,那里的注释带实测依据。

2. **`cp` 即得自洽,但 `imported` 形态做不到 100%。**
   `private-enterprise` 的 `vpc_id` / `cidr` / 3 个公有 + 3 个私有 subnet id 是客户环境
   坐标,任何预设都填不出来。未填时 `preflight-check.sh` 会报 **4 条 BLOCK,且只有这 4 条**
   ——`tests/test_274_config_cross_field_gate.py` 把「只允许这 4 条」写成了断言,出现第 5 条
   就说明 profile 自身的开关组合不自洽。另两个 profile 零坐标,`cp` 完直接零 BLOCK。

3. **profile 里的 WARN 不都是错。**
   `public-demo` / `minimal-test` 的 `deploy.protect_stateful_resources: false` 会各得
   一条 WARN —— 那是刻意的(demo/测试要能反复 destroy+rebuild),生产必须改成 `true`。
   门的分级口径见 `scripts/preflight-check.sh` 文件头:BLOCK = 会 CREATE_FAILED/ROLLBACK/synth
   raise;WARN = 可能有问题 / 静默失效 / 需人工确认。

## 改了 profile 之后

`scripts/checks/config-gate.sh`(已注册进 `scripts/checks/run-all.sh`,CI 的 mechanical-gate
天然继承)会校验 `config.yml.example` 与本目录每个 `*.yml` 都带齐 16 个硬必填键。
行为回归在 `tests/test_274_config_cross_field_gate.py`。
