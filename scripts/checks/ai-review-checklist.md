# AI Reviewer Checklist — 第二层 judgment 门(oc-dev-flow review 阶段用)

> 这份 checklist 是「独立 AI reviewer」评判 diff 的唯一标准。照 AWS 内部 lalsaado
> 《Closing the Loop: Automated Code Review for AI-Generated Code》(w.amazon.com/bin/view/Users/lalsaado/FrontierFieldNotes/ClosingTheLoop/)。
> 核心:机械工具(secrets/shellcheck/ruff/bandit/cdk-nag)抓不到「设计腐烂」——这些要判断,判断正是
> 看着 AI 写代码时被压缩掉的东西。reviewer 必须是**零上下文**的(不看写代码的对话,只看 diff),
> 否则会像作者一样被锚定,把自己刚写的当「干净」。

## 评什么(机械工具覆盖不了的五类)

1. **DRY 违反** — 重复逻辑、复制粘贴块、linter 检测不到的结构性重复(3+ 处重复才算 Warning 级)。
2. **SOLID 违反** — 单一职责(一个函数/类干了两件不相关的事)、开闭(改而非扩展)、依赖倒置(该抽象处写了具体依赖)。
3. **复杂度 / code smell** — 函数 >50 行、嵌套 >3 层、feature envy、Law of Demeter 违反、data clumps、过长参数表。
4. **技术债标记** — 新代码里的 TODO/FIXME/HACK、硬编码值(该进 config)、过宽的 except/catch、隐式耦合。
5. **死代码** — 不可达分支、未用 import/变量、注释掉的代码块、没人调的函数。

## 每条 finding 的格式

- **文件:行号** — 一句话描述问题。
- **severity**:Error / Warning / Info(见下)。
- **5 Whys 根因链**:五句话,从表面症状追到架构根因。区别在于「把这个方法提取出来」(治标)和
  「这个模块缺输入解析器与校验器之间的正式数据契约」(治本)。治标反复犯,根因修一次收敛。

## severity 契约(关键 · 别改成 warning 也挡)

| severity  | 行为       | 范围                                            |
| --------- | ---------- | ----------------------------------------------- |
| **Error** | **挡提交** | bug、坏逻辑、会随时间复利的严重架构违反         |
| Warning   | 报告,不挡  | 3+ 重复的 DRY、有意义的复杂度、新代码里的技术债 |
| Info      | 观察       | 小 smell、重构建议,值得知道但不值得停           |

**只有 Error 挡。** 如果 Warning 也挡,每次都陷入 review-fix-review 循环,人会直接把门关掉——那是最快让防线失效的方式。

## 本仓特有的 Error 级红线(叠加通用五类)

除了通用设计质量,以下本仓语境的问题**升级为 Error**(它们会伤到安全基石):

- 热改运行中 VM 的代码路径(违反铁律 #3:改部署代码→重建)。
- 静默吞异常(`catch{}` / 压掉错误码),尤其鉴权/依赖加载/外部调用处(踩过 EKS hub 吞 import 失败致全 401)。
- 跨租户边界被绕过的逻辑(owner_id 门控缺失、租户 ID 来自不可信 claim)。
- SSM 下发脚本用 bash-only 语法(dash 静默失败)。
- 调度选 host 的读缺 ConsistentRead(拿陈旧容量挤同一台)。

## reviewer 的产出

给每条 finding 打 severity + 5 Whys,最后一行明确判定:

- `REVIEW_VERDICT: BLOCK`(有 ≥1 个 Error)或 `REVIEW_VERDICT: PASS`(只有 Warning/Info)。
  oc-dev-flow review 阶段据此决定挡不挡。
