# 自带 LLM 网关(litellm / OpenAI-兼容)接入说明

> 面向客户/接入方:如何让某个租户用**你自己的 LLM 网关地址**(尤其自建 `http://` 的
> litellm / OpenAI-兼容网关),而不是平台默认的共享网关。

## TL;DR(一句话)

改某个租户的 LLM `baseUrl`,**唯一稳定入口是「创建租户时」在请求体传
`injected_parameters.items.llm_base_url`**。**不要**登进 microVM 手改 `openclaw.json`
——那个改动每次 VM 开机都会被平台还原。

## 1. 正确做法:创建时传参

`POST /tenants` 请求体带 `injected_parameters`:

```json
{
  "name": "my-agent",
  "injected_parameters": {
    "items": {
      "llm_base_url": "http://litellm.my-company.internal:4000/v1"
    }
  }
}
```

- 字段路径固定是 `injected_parameters.items.llm_base_url`(注意有 `.items.` 这一层)。
- `http://` 原样保留,**平台不会把它改成 `https://`**(代码里没有任何 http→https 强制;
  你传什么 scheme 就是什么)。
- 该值会被冻结进租户的注入计划(`frozen_injection_plan`),随租户每次重建/唤醒继承,
  最终写进 microVM 的 `openclaw.json` 的 `models.providers.litellm.baseUrl`。
- 想同时传自带的 key,用同一 `items` 里的 `llm_key`(那个是敏感值,走加密通道)。
- **不传 `llm_base_url`**:租户回退平台默认共享网关(行为与以前一致)。

同理可用于 OpenAI-兼容的任意网关地址(只要 litellm/OpenClaw 认这个 baseUrl 形态)。

## 2. 为什么不能登进 VM 手改 openclaw.json

平台每次 VM 启动(全新创建 + 唤醒都算)都会跑一次配置**收敛**(`oc_harden_config`),把
`models.providers.litellm.baseUrl` 收敛回"该租户应有的值":

- 如果该租户**有** `frozen_injection_plan`(即创建时传了 `llm_base_url`)→ 收敛到**你传的值**
  (注入在收敛之后执行,你的值胜出,随重建继承)。
- 如果该租户**没有** `frozen_injection_plan`(创建时没传)→ 收敛到**平台默认** `LITELLM_HOST`。

所以:**登进 VM 手改 `openclaw.json` 的 baseUrl,会在下一次开机被上述收敛还原**。这不是
bug,是刻意的部署纪律(运行态不留手改漂移——LLM 网关 IP 变了、镜像重建了,都靠这套
收敛保证配置和部署代码一致)。"改成 http 却反复被覆盖回去"的现象,根因就是这个:改了
运行态文件、但没通过创建参数落进 `frozen_injection_plan`,每次开机被还原。

## 3. 存量(老)租户想换网关怎么办

`frozen_injection_plan` 是**创建时冻结、之后不可变**(目前没有"改注入计划"的 API)。所以:

- **老租户**(创建时没传 `llm_base_url`,或传的是旧地址)要用自带网关 → **重建租户**:删掉
  旧租户,新建时带上 `injected_parameters.items.llm_base_url`。
- 需要保数据的,先按备份/恢复流程做(删除默认会同步备份,见部署手册)。

## 4. 排查:我看到的 baseUrl 是 https,谁改的?

平台侧**不会**产生 https(已核实:收敛脚本 `oc_normalize_litellm_baseurl` 只在地址**无
scheme** 时才补 scheme,且默认补 `http`;不存在任何把 http 改 https 的逻辑)。如果你反复
看到 https:

1. 确认你**创建租户时**传的 `llm_base_url` 是不是 https(传什么就是什么)。
2. 若你没传(走平台默认),检查**你自己部署环境**里平台默认网关的来源:SSM 参数
   `/openclaw/litellm-host` 的值、以及你所说的"含 HTTPS 配置的 env 文件"具体是哪个文件
   ——https 只可能来自这些你可控的源,不是平台代码凭空生成。
3. 若你是登进 VM 看到的 https 且它"改不掉",见 §2:手改会被还原;要固定成 http,走 §1
   的创建参数(或 §3 重建)。

## 5. 一次性自检清单

- [ ] 我是在**创建租户**时传 `injected_parameters.items.llm_base_url`,不是登进 VM 改文件。
- [ ] 字段路径带 `.items.` 这一层。
- [ ] 老租户换网关我走的是**重建**,不是原地改。
- [ ] 看到 https 时,先查我传的值 + 我环境的 `/openclaw/litellm-host`,再怀疑平台。

---

_本文基于最新 bb 分支代码核对(`core/utils.py` `_normalize_injected_parameters`、
`core/envelope.py` `_validate_injected_parameters_v2`、`services/registry_service.py`
`llm_base_url` registry 项、`services/tenant_service.py` `_resolve_injection_plan`、
`deploy/userdata/lib/harden-config.sh` `oc_harden_config`/`oc_inject_config_from_plan`)。
"自带 baseUrl 解耦" = feat/litellm-baseurl-decouple,已在 bb。_
