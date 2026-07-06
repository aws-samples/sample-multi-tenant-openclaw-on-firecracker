# 控制面 API 对接文档

本节面向要把本平台集成进自有系统的对接方(客户后端 / 运营控制台 / 自动化脚本)。每个接口示例均经真实调用验证(直接对部署环境 curl,响应为真机返回、凭据已脱敏)。照本节从「创建租户 → 查到运行中 → 实时通道发消息 → 收到回复」可顺序跑通全链路。

> 本文所有 `{BASE}` 指控制面 API 网关地址(形如 `https://<api-id>.execute-api.<region>.amazonaws.com/v1`,部署后由 `console/config.js` 的 `OC_DEFAULT_API_URL` 给出)。`{HUB}` 指数据面 hub 的对外地址(经 CloudFront `/hub/*` 反代)。真实账号 ID / 域名 / 凭据请以你自己的部署为准,本文用占位符。

---

## 1. 认证模型(授权三件套)

控制面对每个请求做三层校验,对接前必须理解:

1. **API Key(网关层,`x-api-key` 头)** — API 网关 usage plan 校验。缺失或错误一律 `403 {"message":"Forbidden"}`,请求根本不进业务逻辑(真机验证:缺 key 与错 key 返回一致,不区分,防枚举)。所有控制面调用都要带 `x-api-key`。
2. **Cognito JWT(身份层,`Authorization: Bearer <id_token>` 头)** — 面向"以某个登录用户身份"操作的路由(如自助注册 `POST /tenants/self`)。网关侧 Cognito authorizer + Lambda 内 JWKS 验签(RS256,校验 issuer + 过期 + client_id)。纯后端自动化可只用 API Key 走管理员路径(`owner_id = API_KEY_OWNER`,全量可见)。
3. **RBAC + owner 归属门控(授权层,Lambda 内)** — 校验过的 Cognito `sub` 作为 `owner_id` 落到租户记录;每个 per-tenant 路由强制 `owner == caller`(或 admin / api-key)。非 owner 的跨用户访问返 `403`。

判据一句话:**纯后端集成用 `x-api-key`;要按平台用户身份建/管各自的 openclaw,叠加 `Authorization: Bearer <Cognito id_token>`。**

数据面(实时对话)另有独立的 token 兑换,见 §5。

### 1.1 RBAC 角色与路由权限(config-gated)

RBAC 由环境变量 `RBAC_ENABLED` 控制(默认开)。角色分三级 `viewer < operator < admin`,来自 Cognito `cognito:groups` claim(取最高级)。授权在路由命中之后校验,因此未知路径仍返 `404` 而非 `403`。失败安全:无 Bearer token 落到 `DEFAULT_NO_JWT_ROLE`(默认 `viewer`);token 验不过 → `403`。

- **跳过 RBAC(自带认证)**:`POST /external/authz`(HMAC 签名)、`GET /tenantmatch`(登录前的 IdP 路由查询)。
- **viewer 即可**:所有只读 `GET`(列表/详情/系统信息/审计/镜像/分组读/技能读)+ `POST /tenants/self`(自助注册)+ `POST /chat/sign`(路由层 viewer,函数内另做 owner 门控)。
- **operator 及以上**:所有写操作(创建/生命周期/宿主管理/技能写删/分组写删/批量)。
- **admin 专属**:`POST /hosts/fleet-power`(全舰队启停,路由层要 operator,函数内再校验 admin)。

per-tenant 路由(带 `{id}`)在 RBAC 之上再做 owner 门控:非 admin / 非 api-key 调用者只能操作 `owner_id == 自己 sub` 的租户,否则 `403`(防 IDOR)。

---

## 2. 通用约定

- **Content-Type**:写操作 `application/json`。
- **错误码(结构化)**:客户端错误返 `4xx` + `{"error":"<人读消息>","code":"<机读码>"}`。已上线的机读码:`VALIDATION`(入参非法)、`CONFLICT`(幂等键重放撞已存在 id)。用 `code` 分支,别解析 `error` 文案。
- **幂等**:创建类支持可选 `client_token`(4–128 位可打印 ASCII)。同 `client_token`(同 owner)重放得同一 id、第二次返 `409 CONFLICT`,不会双开。不传则每次新建(等价 EC2 RunInstances 无 ClientToken)。
- **分页**:列表类支持可选 `?limit=N`(正整数,上限见响应)。**不传 `limit` 返裸数组(全量,旧兼容形态);传 `limit` 返分页对象 `{items…, next_token, count}`**。非法 `limit`(负 / 0 / 非整数)或被篡改的 `next_token` 返 `400 VALIDATION`(不静默降级)。翻页把上次响应的 `next_token` 原样带回。
- **异步写**:重操作(创建 / 生命周期启停 / 大批量)走 SQS 削峰,返 `202` + 状态 `queued` / 作业 id,再轮询查结果,不阻塞 30s API 网关超时。
- **数值字段为字符串**:`/hosts`、`/tenants` 里的 `vcpu`/`mem_mb`/`vm_count` 等为字符串型(如 `"95"`),解析时按 string 处理。

---

## 3. 端点参考(每条经真机验证)

### 3.1 系统与容量(只读)

**`GET {BASE}/system/info`** — 系统能力开关快照。对接前先打这个确认环境形态。

```bash
curl -s -H "x-api-key: $KEY" "{BASE}/system/info"
```

真机响应(节选):

```json
{
  "version": "1.5.5",
  "region": "<region>",
  "agentcore": { "enabled": false, "gateway_url": null },
  "metrics": { "enabled": true, "grafana_url": "<占位>" },
  "multi_az": { "enabled": true, "az_count": 2 },
  "waf": { "enabled": true },
  "cognito": {
    "enabled": true,
    "user_pool_id": "<占位>",
    "rbac_enabled": true
  },
  "notifications": { "enabled": true, "topic_arn": "<占位>" },
  "quotas": { "enabled": false },
  "host_config": {
    "cpu_overcommit_ratio": 6.0,
    "mem_overcommit_ratio": 1.5,
    "vm_default_vcpu": 2,
    "vm_default_mem_mb": 4096
  }
}
```

**`GET {BASE}/hosts`** — 宿主机列表与容量。返裸数组(排除 `__*` 合成记录)。

```json
[
  {
    "instance_id": "i-xxx",
    "status": "active",
    "total_vcpu": "95",
    "used_vcpu": "8",
    "vm_count": "18",
    "total_mem_mb": "770018",
    "used_mem_mb": "21504",
    "rootfs_version": "v1.0",
    "az": "<az>",
    "private_ip": "<ip>",
    "next_vm_num": "36",
    "last_seen": "<ISO8601>"
  }
]
```

**`GET {BASE}/hosts/rootfs-version`** — 返 `{"version":"v1.0"}`(当前 live 镜像版本号,即新宿主开机启动的镜像)。
**`GET {BASE}/hosts/rootfs-drift`** — 各宿主镜像版本对账 `{"current_version","up_to_date","unknown","stale_count","stale":[...]}`,看哪些宿主还没滚到最新镜像(滚动升级追踪)。

三者区别:`/images`(见 §3.4)看 S3 里烤了哪些制品、哪个版本 live;`/hosts/rootfs-version` 只回 live 版本号;`/hosts/rootfs-drift` 看宿主机实际运行的镜像版本是否对齐。

### 3.2 租户生命周期

**`POST {BASE}/tenants`** — 创建租户(建一个独立 openclaw microVM)。管理员/后端路径(`x-api-key`,RBAC operator+)。

```bash
curl -s -X POST -H "x-api-key: $KEY" -H "content-type: application/json" \
  -d '{"name":"acme","vcpu":1,"mem_mb":2048,"client_token":"acme-idem-0001"}' \
  "{BASE}/tenants"
```

入参(除 `name` 外全部可选):

- `name`(必填,标识符正则 `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`)
- `vcpu`/`mem_mb`/`data_disk_mb`(正整数;非整数/负数/0 → `400 VALIDATION`,不再 500)
- `image_id`(默认 `v2`)、`config_template`(DNS-label 正则)
- `security`(嵌套加密配置 Map,见 §3.10)
- `client_token`(幂等键,`^[\x21-\x7e]{4,128}$`)
- `chat_endpoint_enabled`(**JSON 布尔**,默认 false;传字符串会 400)
- `skills`(字符串数组,per-tenant 技能范围)、`group`(组名,继承组级技能)
- `tags`(Map)、`ttl_hours` + `on_expiry`(`delete`/`stop`)、`schedule`(定时启停)
- `restore_from` / `clone_from`(从备份或已有租户派生)、`preferred_host_id`

> **Note** `owner_id` 由调用者身份自动落定(Cognito `sub` 或 `API_KEY_OWNER`),body 里传 `owner`/`owner_id` 会被忽略。`tenant_user_id`(外部联邦用户稳定 id)来自 JWT 的 `custom:tenant_user_id` claim,同样不从 body 取——外部平台集成时靠 Pre-Token-Generation 注入该 claim(见外部平台集成章)。

返回码取决于部署是否开启创建削峰队列(`CREATE_VIA_QUEUE`,`config.yml` 的 `scaler.create_via_queue`,**默认关**):

- **默认(同步)**:有宿主容量返 `201 {"id":"acme-xxxx","status":"creating",...}`,无容量返 `201 {"id":"...","status":"pending"}`(自动触发扩容,后台处理)。
- **开启削峰队列后(异步)**:入 SQS 削峰,返 `202 {"id":"acme-xxxx","status":"queued","message":"create accepted; provisioning asynchronously"}`。大规模建租户时推荐开(见规划部署章的 SSM 并发说明)。

带 `client_token` 时 id 由 `(owner, client_token)` 决定,同键重放返 `409 CONFLICT`。两种模式都**随后轮询 `GET /tenants/{id}` 直到 `status:running`**。

**`POST {BASE}/tenants/self`** — 自助注册(以登录用户身份建自己的节点)。需 `Authorization: Bearer <Cognito id_token>` + `x-api-key`(RBAC viewer+)。`owner_id` 强制为调用者校验过的 `sub`(body 不能替别人指定 owner),受 per-user 节点上限 `SELF_MAX_NODES_PER_USER`(默认 1,0=不限)门控,超限返 `409`。`EXTERNAL_AUTHZ` 开启时该端点直接拒绝(授权判定权交外部后端)。这是外部 SaaS 平台用户各自建 openclaw 的入口(联邦方案见外部平台集成章)。

**`GET {BASE}/tenants`** — 租户列表(RBAC viewer+;非 admin 只见自己 owner 的)。

- 无参:裸数组(全量)。每条为该租户 DDB 记录去除服务端凭据字段后的投影,含 `id/name/status/owner_id/host_port/guest_ip/vm_health/app_health` 等字段(`vm_health`/`app_health` 由 health_check Lambda 写入,新建租户在首次健康检查前可能尚未出现)。
- 分页:`GET {BASE}/tenants?limit=5` → `{"tenants":[…],"next_token":"<opaque>","count":<本页条数>}`。
- 边界(真机验证):`?limit=-1` → `400 {"code":"VALIDATION","error":"limit must be a positive integer (>= 1)"}`;`?next_token=garbage` → `400 {"code":"VALIDATION","error":"next_token is invalid or expired"}`。
- 可选 `?tag=key:value` 按标签过滤(可多个,AND 语义)。
  > **Important** 列表响应当前可能包含租户级凭据字段,对接方**不得把响应原样透传到不可信前端**;凭据只在服务端使用。

**`GET {BASE}/tenants/{id}`** — 单租户详情(owner 门控)。

**`POST {BASE}/tenants/{id}/{action}`** — 生命周期动作(RBAC operator+ + owner 门控)。支持的 `action`:

| action                          | 语义                                                          | 返回                                         |
| ------------------------------- | ------------------------------------------------------------- | -------------------------------------------- |
| `start`                         | 唤醒(~3.7s)                                                   | 202 异步(配 LIFECYCLE_QUEUE 时入队)          |
| `stop`                          | 休眠(~6.0s)                                                   | 202 异步                                     |
| `restart` / `reset` / `rebuild` | 重启 / 重置 / 用新镜像重建                                    | 202 异步                                     |
| `pause` / `resume`              | 暂停 / 恢复                                                   | 202 异步                                     |
| `backup`                        | 备份到 S3(~6.6s)                                              | 202,异步调 backup Lambda                     |
| `resize`                        | 热改 vCPU(需 body `vcpu`)                                     | 200 `{old_vcpu,new_vcpu}`                    |
| `resize-disk`                   | 离线扩数据盘(需 body `new_size_mb`)                           | 200                                          |
| `migrate`                       | 迁到目标宿主(需 body `target_host_id`)                        | 202 `{status:"migrating",snapshot_uri,poll}` |
| `access`                        | 授权账本读写(body `principal`+`op:grant\|revoke`+可选 `role`) | 200                                          |

对已删租户有状态守卫防回生。未知 action 返 `400`。

**`GET {BASE}/tenants/{id}/{action}`** — 租户维度只读(RBAC viewer+ + owner 门控)。支持的 `action`:

- `backups` — 该租户的备份清单。
- `data` — 租户数据快照(**仅元数据、零凭据**:`id/status/host_id/guest_ip/vm_num/vcpu/mem_mb/data_disk_mb/rootfs_version/effective_skills/group/schedule/ttl_hours/expires_at/owner_id/tenant_user_id/has_billing_vkey/backup_count/created_at/updated_at/tags`;`has_billing_vkey` 仅报计费密钥是否存在、绝不返回值),让运维不进 guest 就看清租户态。
- `access` — 显式授权账本 `{owner_id, authorized_users:{sub:{role,granted_at,expire_at}}}`。

**`DELETE {BASE}/tenants/{id}`** — 注销(~12.2s,RBAC operator+ + owner 门控)。

- 默认 `?keep_data=true`:软删,**保留数据盘**。
- `?keep_data=false`:删数据盘,且删前**自动同步备份到 S3**(除非再带 `?skip_backup=true` 显式跳过)。
- 删前平台侧对宿主容量做条件回退。真机返 `200 {"id":"...","status":"deleted"}`。对已删租户幂等(重复删返已删态)。

### 3.3 批量与用户维度

**`POST {BASE}/batch/tenants`** — 批量对多个租户施加一个动作(RBAC operator+)。body:`{action:"start|stop|delete|backup", ids:[...] 或 filter:{tag:"k:v"}, async?:true}`。`ids` 与 `filter` 二选一;`ids` 上限 100000。同步(≤100 且非 async)返 `200 {succeeded,failed}`;超 100 或 `async:true` 返 `202 {job_id,status:"queued"}`。非 admin 的 filter 只解析自己 owner 的租户。
**`GET {BASE}/batch/jobs/{job_id}`** — 查异步作业进度(RBAC viewer+):`{job_id,action,status,total,done,succeeded:[...],failed:[{id,error}],created_at,updated_at}`。
**`GET {BASE}/users/{tenant_user_id}/tenants`** — 按平台用户查其名下所有节点(GSI 索引查询,非全表扫,分页同 §2)。
**`GET {BASE}/users/{tenant_user_id}/summary`** — 该用户节点数 + 按状态分桶 `{total, by_status, truncated}`。
**`POST {BASE}/users/{tenant_user_id}/action`** — 对该用户全部节点批量 `start`/`stop`,返 `{succeeded:[...],failed:[...],truncated}`。

这三个 `/users/*` 是"按平台用户关联管理成千上万 openclaw"的核心接口:目标集合来自 GSI(不是客户端传 id 列表),后端说"停这个用户",平台解析其节点。owner 门控:联邦用户只能管自己的 fleet,admin/api-key 管全部。

### 3.4 备份、审计、镜像

**`GET {BASE}/backups`** — 全量备份清单(跨所有租户,左连接租户表标记孤儿备份 `exists:false`)。
**`GET {BASE}/audit-log`** — 审计日志(时间倒序,`?limit=`默认 50 上限 500,可选 `?since=<ISO8601>`)。每条含 `method/resource/actor/target_id/status_code/ts/error`。所有写操作(POST/PUT/DELETE)自动落审计。

**`GET {BASE}/images`** — 黄金镜像制品清单 + 当前 live 版本。只读:枚举 S3 `rootfs/` 前缀下的所有制品(rootfs / data-template / kernel / 完整性基线 / manifest),并报告 `manifest.json` 当前指向哪个版本。**只返制品元数据(名字/大小/时间/分类),不下载也不暴露镜像字节**,让运维不 SSH 宿主就能看清烤了什么、哪个版本 live。

```bash
curl -s -H "x-api-key: $KEY" "{BASE}/images"
```

```json
{
  "live_version": "v1.0",
  "manifest": { "version": "v1.0", "...": "manifest.json 原样内容" },
  "artifact_count": 5,
  "artifacts": [
    {
      "name": "openclaw-rootfs-v1.0.erofs",
      "kind": "rootfs",
      "size_bytes": 707788800,
      "last_modified": "2026-06-30T12:00:00+00:00",
      "is_backup": false
    }
  ]
}
```

字段:`live_version`(`manifest.json` 指向的版本,取不到为 `"unknown"`)、`manifest`(原样内容,取不到为 `{}`)、`artifact_count`、`artifacts[]` 按 `(kind, name)` 排序,每条含 `name`、`kind`、`size_bytes`、`last_modified`(ISO8601,缺失为 `null`)、`is_backup`(名字含 `.bak` 为 `true`)。`kind` 枚举:`rootfs`(根文件系统只读盘)、`data-template`(数据盘模板)、`kernel`(`vmlinux`)、`integrity-baseline`(`golden-image.sha256`)、`manifest`、`other`。错误:`ASSETS_BUCKET` 未配 → `503`;S3 列举失败 → `500`。

### 3.5 宿主机管理(运维)

**`POST {BASE}/hosts`** — 把一台 EC2 实例注册为 Firecracker 宿主(RBAC operator+)。body `{instance_id:"i-..."}`。平台侧 DescribeInstances 拿 vCPU/内存/AZ,扣除 `HOST_RESERVED_*` 后记账,返 `201 {instance_id,status:"active",az}`。
**`DELETE {BASE}/hosts/{instance_id}`** — 宿主下线(RBAC operator+)。不直接删,标 `status:draining` 并触发 ASG 生命周期钩子,由钩子清理其上所有租户(mark-deleted)后终止实例。返 `200 {instance_id,status:"draining"}`。
**`POST {BASE}/hosts/refresh-rootfs`** — 从 `manifest.json` 把最新 rootfs + 数据模板 + 只读盘经 SSM 下发到所有活跃/空闲宿主(RBAC operator+),异步更新各宿主 `rootfs_version`。无 body。返 `{message:"refresh started",version,hosts:[...]}`。
**`POST {BASE}/hosts/fleet-power`** — **全舰队启停**:跨所有活跃宿主经宿主本地 fan-out 一次性启/停其上每个 microVM(1 分钟舰队启停目标)。body `{action:"start|stop"}`。**admin 专属**(路由层要 operator,函数内再校验 admin,双层防御)。返 `202 {action,hosts,command_id,reconciled,status:"dispatched"}`,并自动对稳态租户做状态对账(start:stopped→running;stop:running→stopped),不触及过渡态。

### 3.6 分组与技能(控制台运维)

**`GET {BASE}/groups`** — 分组列表(viewer+):`{groups:[{name,skills:[...],description,created_at}]}`。
**`POST {BASE}/groups`** — 建分组(operator+):body `{name(正则同 tenant),skills:[...],description?}`,返 `201`,重名 `409`。
**`POST {BASE}/groups/{name}/skills`** — 往分组加一个技能(operator+):body `{skill}`,幂等,返更新后的技能列表。
**`DELETE {BASE}/groups/{name}/skills/{skill}`** — 从分组移除一个技能(operator+),返剩余技能列表。

**`GET {BASE}/skills/{name}`** — 读技能 `SKILL.md` 内容(viewer+):`{name,content,size,last_modified}`,不存在 `404`。
**`PUT {BASE}/skills/{name}`** — 写/建技能(operator+):body `{content}`,须 UTF-8、≤256KB、含至少一条顶级 `# 标题`;返 `200`(已存在)或 `201`(新建);超限 `413`,格式不符 `400`。
**`DELETE {BASE}/skills/{name}`** — 删技能(operator+):返 `{name,deleted:<删除文件数>}`。

> **Note** 技能名限小写字母 + 数字 + 连字符,非法名返 `400`。技能改动落到镜像制品层,随下次镜像重建 / refresh-rootfs 生效,不热改运行中 VM(呼应架构铁律"改镜像重建、不热改活 VM")。

### 3.7 AgentCore(只读,config-gated)

**`GET {BASE}/agentcore/status`**(viewer+)— AgentCore 网关启用状态 `{enabled, gateway_url}`。
**`GET {BASE}/agentcore/tools`**(viewer+)— 注册到 AgentCore 网关的 MCP 工具清单 `{enabled, tools:[{name,description,input_schema}]}`。未启用时 `enabled:false`。

### 3.8 联邦 IdP 路由(登录前查询)

**`GET {BASE}/tenantmatch?platform_id=<id>`** — 外部平台 → Cognito 上游 IdP 路由查询(**跳过 RBAC**,登录前无身份)。给定平台标识返 `{platform_id, idp_provider_name, issuer_url}`,前端据此把用户直接跳到对应上游 IdP。`platform_id` 正则 `[a-zA-Z0-9._-]{1,128}`,非法 `400`;未配 IdP 联邦(`TENANT_IDP_TABLE` 未设)`404 NOT_CONFIGURED`;平台未注册 `404 NOT_FOUND`;DDB 故障 `502`。**登录前查询,不泄露任何租户数据,只做平台→IdP 路由。**

### 3.9 授权对接(外部后端)

**`POST {BASE}/external/authz`** — 外部后端推送"用户 ↔ 租户"授权映射(**跳过 RBAC,自带 HMAC 签名认证**,`EXTERNAL_AUTHZ` 开启时生效)。用于把授权判定权交给客户自有平台:平台说"用户 X 可访问租户 Y",写入 `authorized_users`,数据面 hub 据此放行。

- 签名头:`x-claw-authz-signature`(= HMAC-SHA256(secret, `"{timestamp}.{raw_body}"`))+ `x-claw-authz-timestamp`(unix 秒,±`EXTERNAL_AUTHZ_TS_WINDOW` 默认 300s,防重放)。
- body:`{tenant_id, principal, op:"grant"|"revoke", role?, expire_at?}`。
- 返 `200 {id,op,principal}`。错误:`EXTERNAL_AUTHZ` 未开 `404`;secret 未配 `503`;签名/时间戳无效 `401`;入参缺失/非法 `400`;租户不存在 `404`。

> **Note** 真机现状:该路由在未启用 `EXTERNAL_AUTHZ` 的默认部署返 `404`。要用需在栈里开 `EXTERNAL_AUTHZ` + 配 `EXTERNAL_AUTHZ_SECRET`。文档标此为 config-gated 能力。

### 3.10 `security` 嵌套加密配置(创建时可选)

`POST /tenants` 的 `security` 字段是一个命名子对象(对齐 S3 ServerSideEncryptionConfiguration 范式,不用 `env`——`env` 在 AWS 惯例专指环境变量):

```json
{
  "security": {
    "storage_encrypted": true,
    "encryption_type": "tenant_cmk",
    "kms_key_arn": "arn:aws:kms:<region>:<acct>:key/<id>",
    "cert_arn": "arn:aws:acm:...",
    "secret_ref": "arn:aws:secretsmanager:...:secret:..."
  }
}
```

不变量(违反返 `400 VALIDATION`):`storage_encrypted:false` 不能带 key;`encryption_type:tenant_cmk` 必须给 `kms_key_arn`;引用外部资源必须是**完整 ARN**(裸 id/alias 跨账号会解析到错误 key)。`secret_ref` 存 Secrets Manager ARN(引用,不存 secret 值)。

---

## 4. 数据面:换取前端令牌 + 消息签名(hub)

数据面把"浏览器/前端 ⇄ 用户自己的 openclaw"经 hub(WS 中枢)打通。前端不直连 microVM(VM 不开入站端口),而是双方各自向 hub 出站汇合。

**`POST {HUB}/hub/token`** — 前端用 Cognito `id_token` 换 hub 短 token。

```bash
curl -s -X POST -H "Authorization: Bearer <cognito_id_token>" \
  -H "content-type: application/json" -d '{"tenant_id":"acme-xxxx"}' "{HUB}/hub/token"
```

hub 侧:JWKS 验签(`token_use=id` + audience)+ `authorizeSubForTenant(sub, tenant_id)` 查 `owner_id`/`authorized_users`。通过则返 `{"token":"<前端短token>","expires_in":300}`。短 token claim=`{role:"frontend", sub:<验证过>, tenant, access, exp:+300s}`,HMAC 签(密钥多副本经 Secrets Manager 共享)。403 表示该 sub 无权访问该租户。

**`POST {HUB}/channel-token`** — microVM 出站侧(claw-channel)证明自己是某租户:Cognito machine-user access token(`username` claim = 租户,不可伪造),换等价 `{role:"channel"}` 短 token。对接方通常无需直接调用(由 VM 内 channel 自动完成)。

**`POST {BASE}/chat/sign`** — 控制面侧为一条 C 端消息信封签名,投给 per-VM webhook 的备用旁路(RBAC viewer+,函数内做 owner/admin 门控)。需 `Authorization: Bearer <Cognito id_token>` + body `{tenant_id, text}`(`text` ≤8000 字符)。返 `{path:"/chat/{tenant_id}/inbound", body:"<签名信封>", headers:{x-claw-signature, x-claw-random, x-claw-timestamp}}`,由 HMAC 派生的 `channel_secret` 签。租户 channel 密钥未就绪(VM 还在启动)返 `409`。日常实时对话走 §5 的 WebSocket,不需要直接调本端点。

**文件**:`POST {HUB}/files/upload-url`(MIME 白名单 + size)返 S3 预签 PUT;`GET {HUB}/files/download-url?fileKey=` 带租户段守卫(fileKey 第二段必须 == 调用者租户,防跨租户 IDOR)返预签 GET。

---

## 5. WebSocket 实时对话

1. 前端按 §4 拿到前端短 token。
2. 建连:`wss {HUB}/hub/ws?token=<前端短token>`(经 CloudFront `/hub/*` → ALB → hub)。hub 校验 token、盖 `_tenant/_sub`、注册到 frontends 表。连接后 hub 每 25s 发协议级 PING keepalive(扛 agent 冷启动期间空闲断连)。
3. 发消息(帧 shape):

```json
{
  "operationType": "msg_create",
  "parts": [{ "kind": "TEXT", "text": "你好" }],
  "threadId": "<会话id,正则 ^[A-Za-z0-9._:-]+$ 长度≤80>",
  "clientMessageId": "<前端关联id>"
}
```

hub 把 `senderId` 设为**服务端验证过的 Cognito sub**(不信客户端自报,防冒充),投递给该租户的 channel → microVM 内 openclaw 推理。4. 收回复:`type:"reply_delta"` 或 `operationType:"msg_update"` 流式增量(前端按 `clientMessageId` 替换气泡)。hub 只投给 `_tenant` 匹配的该 sub 的所有 tab(跨租户隔离)。5. 历史:发 `type:"history_request"` → 收 `type:"history_reply"`(messages 数组)。

**跨租户隔离(结构性,不信客户端自报)**:① 前端短 token 绑单一 tenant ② channel 用 Cognito access token 的 username claim 证明租户身份 ③ 撮合仅在 `fws._tenant === frame._tenant` 时发生 + senderId 用服务端验过的 sub + 授权查 `owner_id`/`authorized_users`。

---

## 6. 快速上手:端到端跑通(客户视角)

```bash
export KEY="<你的 x-api-key>"
export BASE="https://<api-id>.execute-api.<region>.amazonaws.com/v1"

# 1) 确认环境
curl -s -H "x-api-key: $KEY" "$BASE/system/info" | python3 -m json.tool

# 2) 建租户(带幂等键)
curl -s -X POST -H "x-api-key: $KEY" -H "content-type: application/json" \
  -d '{"name":"quickstart","client_token":"qs-0001"}' "$BASE/tenants"
# → 默认同步: 201 {"id":"quickstart-xxxx","status":"creating"}
# → 开削峰队列: 202 {"id":"quickstart-xxxx","status":"queued"}

# 3) 轮询到 running
curl -s -H "x-api-key: $KEY" "$BASE/tenants/quickstart-xxxx"
# → 直到 {"status":"running","vm_health":"up","app_health":"up"}

# 4) 前端换 hub token(浏览器/前端,需 Cognito 登录拿 id_token)
#    POST {HUB}/hub/token  Bearer <id_token> + {"tenant_id":"quickstart-xxxx"}
#    → {"token":"<前端短token>","expires_in":300}

# 5) 建 wss 发消息收回复
#    wss {HUB}/hub/ws?token=<前端短token>
#    发 {operationType:msg_create, parts:[{kind:TEXT,text:"..."}], threadId, clientMessageId}
#    收 reply_delta 流式回复
```

控制面步骤(1–3)纯 `curl` 可跑;数据面(4–5)需 Cognito 登录态 + WS 客户端(参考 chat UI)。端到端首回复实测约 27s(含 agent 冷启动)。

---

## 7. 附:端点速查表

| 端点                                                   | 方法           | 认证 / RBAC             | 用途                                                                                   |
| ------------------------------------------------------ | -------------- | ----------------------- | -------------------------------------------------------------------------------------- |
| `/system/info`                                         | GET            | api-key · viewer        | 系统能力快照                                                                           |
| `/hosts` `/hosts/rootfs-version` `/hosts/rootfs-drift` | GET            | api-key · viewer        | 宿主与镜像对账                                                                         |
| `/hosts`                                               | POST           | operator                | 注册宿主                                                                               |
| `/hosts/{instance_id}`                                 | DELETE         | operator                | 宿主下线(draining)                                                                     |
| `/hosts/refresh-rootfs`                                | POST           | operator                | 下发最新镜像到宿主                                                                     |
| `/hosts/fleet-power`                                   | POST           | **admin**               | 全舰队启停                                                                             |
| `/images`                                              | GET            | viewer                  | 镜像制品清单                                                                           |
| `/tenants`                                             | GET            | viewer(仅自己)          | 租户列表(裸数组/分页双形态)                                                            |
| `/tenants`                                             | POST           | operator                | 创建租户                                                                               |
| `/tenants/self`                                        | POST           | Bearer · viewer         | 自助注册(用户身份)                                                                     |
| `/tenants/{id}`                                        | GET            | owner 门控              | 单租户详情                                                                             |
| `/tenants/{id}/{action}`                               | GET            | owner 门控              | backups / data / access(只读)                                                          |
| `/tenants/{id}/{action}`                               | POST           | operator + owner        | start/stop/restart/pause/resume/reset/rebuild/backup/resize/resize-disk/migrate/access |
| `/tenants/{id}`                                        | DELETE         | operator + owner        | 注销(keep_data 可选)                                                                   |
| `/batch/tenants` `/batch/jobs/{id}`                    | POST/GET       | operator / viewer       | 批量与作业进度                                                                         |
| `/users/{uid}/tenants` `/summary` `/action`            | GET/POST       | viewer(仅自己)          | 按平台用户管理 fleet                                                                   |
| `/backups` `/audit-log`                                | GET            | viewer                  | 备份与审计                                                                             |
| `/groups` `/groups/{name}/skills`                      | GET/POST       | viewer / operator       | 分组与组级 skill                                                                       |
| `/groups/{name}/skills/{skill}`                        | DELETE         | operator                | 移除组级 skill                                                                         |
| `/skills/{name}`                                       | GET/PUT/DELETE | viewer / operator       | 技能内容 CRUD                                                                          |
| `/agentcore/status` `/agentcore/tools`                 | GET            | viewer                  | AgentCore 网关状态/工具(config-gated)                                                  |
| `/tenantmatch`                                         | GET            | 无(登录前)              | 平台→IdP 路由查询                                                                      |
| `/external/authz`                                      | POST           | HMAC                    | 外部授权推送(config-gated,默认未启用)                                                  |
| `/chat/sign`                                           | POST           | Bearer · viewer + owner | C 端消息签名(备用旁路)                                                                 |
| `{HUB}/hub/token` `/channel-token`                     | POST           | Bearer / Cognito access | 数据面令牌兑换                                                                         |
| `{HUB}/hub/ws`                                         | WSS            | 前端短 token            | 实时对话                                                                               |
| `{HUB}/files/upload-url` `/download-url`               | POST/GET       | hub token               | 文件预签(租户段守卫)                                                                   |

> 验证来源:控制面路由与 RBAC 分级来自 `deploy/lambda/api/handler.py` 路由表(`routes` 字典)+ `_VIEWER_OK`/`_RBAC_SKIP`/`_rbac_check` 定义;端点行为经真机 curl 部署环境验证(证据 `engineering/00-knowledge-base/evidence/`);hub/wss 参数来自 `SPEC/03-HUB-SPEC.md` + `deploy/hub/server.mjs`。分页页大小语义、AgentCore 工具清单以实际部署配置为准。
