# pull-image API 文档

> ClawPool 控制面 —— 镜像版本快照的完整生命周期:**打快照 → 列快照 → 装到单台宿主机(host) → 轮询安装进度**。
> 生命周期:`POST /create-image-snapshot`(打)→ `GET /list_image_versions`(列)→ `POST /hosts/{id}/pull-image?snapshot_time=`(拉)→ `GET /hosts/{id}/pull-image-progress`(轮询)。
> 单一真相源:`engineering/backend/openapi-control-plane.yaml`(operationId `createImageSnapshot` / `listImageVersions` / `pullImage` / `pullImageProgress`)。
> 本文按人读文档的六要素结构组织;**返回体是本仓真实契约**(不是通用 `{code,message,data}` 信封)。

## 通用约定

- **鉴权(客户侧只需 x-api-key)**:
  - **API key(唯一必需)**:每次调用带 `x-api-key` 头(API Gateway usage-plan)。**缺/错 → API Gateway 层直接挡,返回 403 `{"message":"Forbidden"}`**(API GW 标准信封,请求根本进不到 Lambda)。这是客户唯一会遇到的 403。
  - **RBAC(取决于部署的环境配置)**:`RBAC_ENABLED=true` 时,不带 Cognito JWT 的调用角色 = 环境变量 `DEFAULT_NO_JWT_ROLE`(**代码默认 `viewer`**;**本部署实测配成 `operator`**)。这两个接口需 operator+ —— 故**在本部署**纯 api-key 调用默认就是 operator、直接过;若某部署把 `DEFAULT_NO_JWT_ROLE` 留默认 `viewer`,则纯 api-key 会被 RBAC 挡 403(需带 operator+ 的 Cognito JWT)。console 场景带 viewer 级 JWT 同样会被挡。**客户集成前请确认目标部署的 `DEFAULT_NO_JWT_ROLE`**。
- **错误信封**:本仓校验路径返回 `{"error": <人读原因>, "code": <错误码>}`;裸失败(如 5xx worker 错误)可能只有 `{"error": ...}`;**API Gateway 层的错误**(缺 api-key)是 `{"message": ...}`(不同信封,客户需分别处理)。
- **判成败**:pull-image 是【异步】——`POST` 只回 202 表示"已受理",真正成败靠轮询 `pull-image-progress` 的 `ProcessingJobStatus`。

---

## 1. POST /create-image-snapshot

### 接口描述
给 assets 桶**打一个版本快照**(生命周期第一步)。扫 S3 `deployment/` 前缀下**全部当前版对象**(rootfs 镜像 + scripts + edge + litellm + monitoring),采集每个对象的 `{path, s3_version_id, etag}`,写一条快照到 DynamoDB 表 `openclaw-version-snapshots`(主键 `snapshot_time`)。等价于运维脚本 `scripts/snapshot-version.sh`。打完即可被 `GET /list_image_versions` 列出、被 `POST /hosts/{id}/pull-image?snapshot_time=` 拉到 host。
> **bucket 不是客户参数**:后端 Lambda 从环境变量 `ASSETS_BUCKET` 自读,客户端不传 bucket/region。
> **snapshot_time 格式**:服务端按 `%Y-%m-%dT%H:%M:%SZ`(秒级 UTC + Z,如 `2026-07-23T15:52:09Z`)生成——与 pull-image 校验的格式一致,故刚打的快照立即可拉。
> (单一真相源 operationId `createImageSnapshot`,#376。)

### 请求路径与方法
```
POST /create-image-snapshot
```

### 请求参数
Body 可选(JSON 对象,可不传/传 `{}`)。RBAC:**operator+**(该路由不在 `_VIEWER_OK`,viewer 会被 403)。

| 字段 | 型态 | 必需 | 说明 |
|---|---|---|---|
| `label` | string | 否 | 人读标签(console 快照列表显示用)。留空/不传 → 后端自动读 `deployment/rootfs/manifest.json` 的 `version` 当标签。校验:须匹配 `^[A-Za-z0-9._-]{1,128}$`。 |

**curl 范例**(不含真实 key,用占位符):
```bash
# 传 label
curl -X POST "$API_URL/create-image-snapshot" \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"label":"v1.4"}'
# → 200 {"snapshot_time":"2026-07-24T02:15:30Z","label":"v1.4","file_count":66}

# 不传 label → 后端自动从 deployment/rootfs/manifest.json 的 version 填
curl -X POST "$API_URL/create-image-snapshot" -H "x-api-key: $API_KEY" -d '{}'
```

### 成功返回范例
**200 OK** —— 快照已落库:
```json
{ "snapshot_time": "2026-07-23T15:52:09Z", "label": "sg-test-376", "file_count": 66 }
```

| 字段 | 型态 | 说明 |
|---|---|---|
| `snapshot_time` | string | 本次快照的 ISO8601 UTC 主键;传给后续 pull-image。 |
| `label` | string | 生效的标签(未传时为自动派生值;manifest 无 version 时为 `""`)。 |
| `file_count` | integer | 本次采集到的对象数。 |

### 失败返回范例
**400 Validation** —— `code: VALIDATION`。三种情形:
```json
{ "error": "body must be valid JSON", "code": "VALIDATION" }
{ "error": "body must be a JSON object", "code": "VALIDATION" }
{ "error": "label must match [A-Za-z0-9._-]{1,128}", "code": "VALIDATION" }
```
- `body must be valid JSON`:body 是坏 JSON 字符串。
- `body must be a JSON object`:合法 JSON 但是标量/数组(非对象)。
- `label ...`:label 不符字符集/长度。

**409 Conflict** —— `code: CONFLICT`。同一墙钟秒内并发/重放两次 → 主键(秒级 `snapshot_time`)撞键,第二次拒写(快照不可变),稍后重试(下一秒即不同键):
```json
{ "error": "a snapshot at 2026-07-23T15:52:09Z already exists; retry in a moment", "code": "CONFLICT" }
```

**500 Empty snapshot** —— `code: EMPTY_SNAPSHOT`。fail-loud:`deployment/` 下扫到 0 个文件(桶/前缀不对或权限问题)→ 拒写空快照:
```json
{ "error": "no files found under deployment/ — bucket/prefix incorrect or permission issue", "code": "EMPTY_SNAPSHOT" }
```

**503 Not configured** —— `code: NOT_CONFIGURED`。部署缺配置:
```json
{ "error": "VERSION_SNAPSHOTS_TABLE not configured", "code": "NOT_CONFIGURED" }
{ "error": "ASSETS_BUCKET not configured", "code": "NOT_CONFIGURED" }
```

**403 Forbidden** —— 缺/错 x-api-key(API GW `{"message":"Forbidden"}`);或低于 operator 的 JWT(RBAC)。

> **真机证据**(新加坡环境 2026-07-23):POST 返 200 `{"snapshot_time":"2026-07-23T15:52:09Z","label":"sg-test-376","file_count":66}`,直接读 DDB 确认落库,`GET /list_image_versions` 列表置顶可见。

---

## 2. GET /list_image_versions

### 接口描述
列出黄金镜像的**各版本快照**(供 pull 流程选一个时间点)。返回 version-snapshots 表的元数据——`snapshot_time` + `label` + `file_count`,按 `snapshot_time` 倒序(最新在前)。**不返回**每个快照的大 `files` 列表(那在 pull 时才逐文件读)。
> 与 `GET /images` 区别:`/images` 列 S3 里的镜像【文件】产物(rootfs/data-template/... + 当前 manifest);`/list_image_versions` 列【版本快照时间点】。选出的 `snapshot_time` 用于下一步 `POST /hosts/{id}/pull-image?snapshot_time=`。
> (曾用名 `GET /snapshots`,#337 改名避免与 `/images` 混淆。)

### 请求路径与方法
```
GET /list_image_versions
```

### 请求参数
无(Path/Query/Body 都没有)。RBAC:viewer 即可(只读)。

### 成功返回范例
**200 OK** —— 快照元数据数组,最新在前:
```json
[
  { "snapshot_time": "2026-07-20T10:21:17Z", "label": "v1.2", "file_count": 46 },
  { "snapshot_time": "2026-07-13T16:59:03Z", "label": "v1.1", "file_count": 46 }
]
```

| 字段 | 型态 | 说明 |
|---|---|---|
| `snapshot_time` | string | ISO8601 UTC 快照主键;传给 pull-image 的 `snapshot_time` 参数。 |
| `label` | string | 人读标签(打快照时自动从 manifest.json rootfs version 填);无则 `""`。 |
| `file_count` | integer | 该快照记录的文件数。 |

### 失败返回范例
**403 Forbidden** —— 缺/错 x-api-key(API GW `{"message":"Forbidden"}`);或低于 viewer 的 JWT。
**503 Service Unavailable** —— 后端未配置版本快照表(`code: NOT_CONFIGURED`)。
```json
{ "error": "VERSION_SNAPSHOTS_TABLE not configured", "code": "NOT_CONFIGURED" }
```

---

## 3. POST /hosts/{instance_id}/pull-image

### 接口描述
把某个**版本快照**装到指定的一台宿主机。读该快照的 `manifest.json`,只装 manifest 点名的盘(rootfs / data-template / immutable)+ manifest 本身,按各自**精确 S3 VersionId** 拉取、校验 ETag,再装到 live。**异步**:先把 host 从 active/idle 原子 CAS 成 `upgrading`(pull 期间不接新租户),然后自调用后台 worker,**立即返回 202**;长链(下载+解压+装 live,可能数分钟)在后台跑,进度走下面的 `pull-image-progress`。失败只报错,不自动回滚。

### 请求路径与方法
```
POST /hosts/{instance_id}/pull-image?snapshot_time=<ISO8601>
```

### 请求参数

| 位置 | 参数 | 型态 | 必填 | 说明 |
|---|---|---|---|---|
| Path | `instance_id` | string | ✅ | 目标宿主机的 EC2 instance id(如 `i-0c3843987566fbcd9`)。 |
| Query | `snapshot_time` | string | ✅ | 快照主键,ISO8601 UTC 格式 `YYYY-MM-DDTHH:MM:SSZ`(从 `GET /list_image_versions` 拿),如 `2026-07-20T10:21:17Z`。 |
| Body | — | — | — | 无请求体。 |

### 成功返回范例

**202 Accepted** —— 已受理,异步安装已启动。拿 `job_id` 去轮询进度。

```json
{
  "message": "pull-image started (async; poll pull-image-progress)",
  "instance_id": "i-0c3843987566fbcd9",
  "snapshot_time": "2026-07-20T10:21:17Z",
  "status": "upgrading",
  "job_id": "pull-476ffa9179294d5a"
}
```
> ⚠️ 202 只代表"已开始",**不代表装成功**。成败必须轮询 `pull-image-progress`。

### 失败返回范例

**400 Bad Request** —— `snapshot_time` 缺失或格式非法(`code: VALIDATION`)。
```json
{ "error": "snapshot_time required (version mode removed)", "code": "VALIDATION" }
```
```json
{ "error": "snapshot_time must be ISO8601 UTC (YYYY-MM-DDTHH:MM:SSZ); got 'garbage'", "code": "VALIDATION" }
```

**403 Forbidden** —— 两种,信封不同:
- 缺/错 `x-api-key`(API Gateway 挡,请求进不到 Lambda):
  ```json
  { "message": "Forbidden" }
  ```
- 带了 viewer 级 JWT(Lambda RBAC 挡;纯 API-key 无 JWT 默认 operator,不会触发):
  ```json
  { "error": "forbidden", "rbac": { "role": "viewer", "required": "operator" } }
  ```

**404 Not Found** —— 快照不存在。
```json
{ "error": "snapshot 2026-07-20T10:21:17Z not found", "code": "NOT_FOUND" }
```

**409 Conflict** —— host 当前不可 pull(状态非 active/idle,已在 upgrading),或快照不自洽(manifest 点名的盘在快照里缺失)。
```json
{ "error": "host i-0c3843987566fbcd9 not available for pull (status must be active/idle; already upgrading or missing)", "code": "CONFLICT" }
```

**500 Internal Server Error** —— 异步 worker 自调用没发出去(此时 host 状态已复位回原态)。
```json
{ "error": "failed to dispatch pull-image worker: <detail>", "code": "DISPATCH_FAILED" }
```

**503 Service Unavailable** —— 后端未配置(`VERSION_SNAPSHOTS_TABLE` / `ASSETS_BUCKET`)。
```json
{ "error": "VERSION_SNAPSHOTS_TABLE not configured", "code": "NOT_CONFIGURED" }
```

---

## 4. GET /hosts/{instance_id}/pull-image-progress

### 接口描述
轮询某台宿主机上 pull-image 任务的进度。返回 **SageMaker ProcessingJob 风格**的三态:`Completed` / `Failed` / `InProgress`。读 host 记录的 `pull_command_id`,再 tail host 上的进度文件末行判态、取最近一条 `ERROR:<CODE>` 行作失败原因。若终态已持久化到 DDB(`last_pull_error`)但进度文件读不到(worker 早退),会强制判 `Failed`,不会一直卡 `InProgress`。

### 请求路径与方法
```
GET /hosts/{instance_id}/pull-image-progress
```

### 请求参数

| 位置 | 参数 | 型态 | 必填 | 说明 |
|---|---|---|---|---|
| Path | `instance_id` | string | ✅ | 目标宿主机 EC2 instance id。 |

### 成功返回范例

**200 OK —— InProgress**(phase2 正解压第 2/4 个盘,真机 2026-07-20 取样):
```json
{
  "instance_id": "i-0c3843987566fbcd9",
  "host_status": "upgrading",
  "job_id": "pull-491daf780b7d484a",
  "snapshot_time": "2026-07-20T10:21:17Z",
  "last_pull_error": null,
  "ProcessingJobStatus": "InProgress",
  "last_status": "2026-07-20T15:10:46Z phase2 [2/4]: unzipping openclaw-data-template-v1.2.ext4.gz to live",
  "command_id": "4cdd0e05-6a6b-4cfc-ad2a-c20f75c8a68d"
}
```

**200 OK —— Completed**(装成功,带 `ExitCode: 0`;host 复位回**原态**,原是 active 就 active、原是 idle 就 idle):
```json
{
  "instance_id": "i-0c3843987566fbcd9",
  "host_status": "active",
  "job_id": "pull-476ffa9179294d5a",
  "snapshot_time": "2026-07-20T10:21:17Z",
  "last_pull_error": null,
  "ProcessingJobStatus": "Completed",
  "last_status": "2026-07-21T03:00:40Z SUCCESS",
  "command_id": "7b4e7629-2020-4060-931a-3736d643329e",
  "ExitCode": 0
}
```

**200 OK —— Failed**(装失败;带 `ErrorCode` + `FailureReason`。注:phase2 装 live 失败 host 留 `upgrading` 待运维,phase1/下发失败会复位回原态):
```json
{
  "instance_id": "i-0c3843987566fbcd9",
  "host_status": "upgrading",
  "job_id": "pull-491daf780b7d484a",
  "snapshot_time": "2026-07-20T10:21:17Z",
  "last_pull_error": "2026-07-20T15:12:00Z ERROR:UNZIP_FAILED pigz decompress failed for openclaw-rootfs-v1.2.ext4.gz",
  "ProcessingJobStatus": "Failed",
  "last_status": "2026-07-20T15:12:00Z FAIL",
  "command_id": "8a1f...",
  "ErrorCode": "UNZIP_FAILED",
  "FailureReason": "2026-07-20T15:12:00Z ERROR:UNZIP_FAILED pigz decompress failed for openclaw-rootfs-v1.2.ext4.gz"
}
```
> 注:`ErrorCode` 在 Failed 但进度文件无 `ERROR:` 行时可能为 `null`(此时靠 `FailureReason` / `last_pull_error` 定位);`command_id`/`snapshot_time` 也可能为 `null`(如 SSM 未成功下发)。

**200 OK —— 从没 pull 过这台 host**(无 job):
```json
{
  "instance_id": "i-0c3843987566fbcd9",
  "host_status": "active",
  "job_id": null,
  "snapshot_time": "2026-07-20T10:21:17Z",
  "last_pull_error": null,
  "ProcessingJobStatus": "InProgress",
  "last_status": null,
  "message": "no pull-image job for this host"
}
```

### 字段说明

| 字段 | 型态 | 说明 |
|---|---|---|
| `ProcessingJobStatus` | string | `Completed` / `Failed` / `InProgress`。**判成败只看这个**。 |
| `host_status` | string\|null | host DDB 的 `status` 原值,**不要**和 pull 状态混。常见 active/idle/upgrading,也可能是 draining/其它或 null(直接透传 DDB 值,不做归一)。 |
| `job_id` | string\|null | 本轮 pull 的 id(`pull-<hex>`);null=从没 pull 过。 |
| `snapshot_time` | string\|null | host 当前记录的快照(装成功后更新);从没装过为 null。 |
| `last_pull_error` | string\|null | DDB 持久化的上次失败原因(base 字段,恒返回);无则 null。 |
| `last_status` | string\|null | 进度文件末行原文(带时间戳+当前动作);无 job 时 null。 |
| `command_id` | string\|null | tail 进度文件用的 SSM 命令 id;有 job 时恒返回(SSM 未下发可能 null)。无 job 时不出现。 |
| `ExitCode` | int | 仅 `Completed` 时出现,恒 `0`。 |
| `ErrorCode` | string\|null | 仅 `Failed` 时出现;进度文件有 `ERROR:<CODE>` 行时是下方枚举之一,**无 ERROR 行时为 `null`**(靠 `FailureReason` 定位)。 |
| `FailureReason` | string | 仅 `Failed` 时出现,失败详情(优先进度文件 ERROR 行 > DDB `last_pull_error` > 末行原文)。 |

### 失败返回范例(接口本身的错误,非 job 失败)

**400 Bad Request** —— 缺 `instance_id`。
```json
{ "error": "missing instance_id", "code": "VALIDATION" }
```

**403 Forbidden** —— 同 pull-image:缺/错 x-api-key → API GW `{"message":"Forbidden"}`;viewer 级 JWT → Lambda `{"error":"forbidden","rbac":{...}}`(纯 API-key 默认 operator 不触发)。

**404 Not Found** —— host 不存在。
```json
{ "error": "host i-0c3843987566fbcd9 not found", "code": "NOT_FOUND" }
```

---

## 5. 状态码速查表

### HTTP 状态码

| HTTP | 接口 | 含义 |
|---|---|---|
| 200 | create-image-snapshot | 快照已落库(返回 `snapshot_time` + `label` + `file_count`) |
| 202 | pull-image | 已受理,异步安装启动(≠ 装成功) |
| 200 | pull-image-progress | 进度快照(具体成败看 `ProcessingJobStatus`) |
| 400 | 三者 | 参数校验失败(`code: VALIDATION`);create 另含 body 非 JSON / 非对象 / label 非法 |
| 403 | 三者 | 缺/错 x-api-key(API GW `{message}`)或角色不足(RBAC `{error,rbac}`);create/pull 需 operator+ |
| 404 | pull | 快照 / host 不存在(`code: NOT_FOUND`) |
| 409 | create / pull | create:同秒撞键(`code: CONFLICT`,快照不可变);pull:host 非 active/idle 或快照不自洽(`code: CONFLICT`) |
| 500 | create / pull | create:`deployment/` 扫到 0 文件拒写(`code: EMPTY_SNAPSHOT`);pull:异步 worker 自调用失败,host 已复位(`code: DISPATCH_FAILED`) |
| 503 | create / pull | 后端未配置(表/桶)(`code: NOT_CONFIGURED`) |

> **错误信封统一**:pull-image 链路所有失败(400/404/409/500/503)都返回 `{"error": <原因>, "code": <码>}`——`VALIDATION` / `NOT_FOUND` / `CONFLICT` / `DISPATCH_FAILED` / `NOT_CONFIGURED`。客户按 `code` 判即可。唯一例外是缺 x-api-key 的 403(API Gateway 层 `{"message":"Forbidden"}`,不进 Lambda)。

### ProcessingJobStatus(pull-image-progress 的业务状态)

| 值 | 含义 | 伴随字段 |
|---|---|---|
| `InProgress` | 正在下载/解压/装 live,或从没 pull | `last_status` |
| `Completed` | 装成功 | `ExitCode: 0` |
| `Failed` | 装失败 | `ErrorCode` + `FailureReason` |

### ErrorCode(Failed 时的失败码)

| ErrorCode | 含义 |
|---|---|
| `DOWNLOAD_FAILED` | S3 get-object 拉取失败(权限/网络/VersionId 不存在/盘满写不下) |
| `ETAG_MISMATCH` | 拉下来的内容与快照记录 etag 不符(内容被改/传输损坏) |
| `MANIFEST_MISMATCH` | manifest 版本指针与拉下来的盘文件名不一致(快照不自洽) |
| `UNZIP_FAILED` | pigz 解压失败或产物为空(.gz 损坏 / 磁盘满) |
| `INSTALL_MV_FAILED` | 解压后 mv 到 live 失败(盘满/权限/目标占用) |
| `OWNERSHIP_CHECK_FAILED` | 持锁后无法从 DDB 读回 pull_command_id 确认所有权(DDB 读失败) |
| `UNKNOWN` | 未标注的意外退出(见 SSM stderr) |

---

## 6. 典型调用流程

```
0. POST /create-image-snapshot                       → 200 + snapshot_time(打一版,label 可省)
1. GET  /list_image_versions                         → 选一个 snapshot_time
2. POST /hosts/{id}/pull-image?snapshot_time=<...>   → 202 + job_id (host → upgrading)
3. 轮询 GET /hosts/{id}/pull-image-progress(每 5s):
     ProcessingJobStatus == InProgress → 继续轮询(看 last_status 里 phase1/phase2 进度)
     ProcessingJobStatus == Completed  → 完成(host 复位回原态 active/idle,snapshot_time 已更新)
     ProcessingJobStatus == Failed     → 失败(看 ErrorCode + FailureReason):
         · phase2(装 live)失败 → host 留 upgrading 待运维(live 可能半写坏,不谎报);
         · pre-dispatch / phase1 / ownership-check 失败 → host 复位回原态(live 未碰)。
```

> **兜底 500(无 code)**:上述错误码是 pull-image 主动返回的。若后端发生**未预期异常**(DDB/S3/SSM 查询报错等),handler 顶层会兜成通用 `500 { "error": "<异常信息>" }`——**没有 `code`、没有 `ProcessingJobStatus`**。客户端不能假设"所有失败都有 code";遇 5xx 且无 code 时按通用错误处理。
