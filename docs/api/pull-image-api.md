# 镜像版本与按 VM 灰度 API

本文按**作用域**分为两个部分，避免把“管理全局镜像目录”和“在一台 EC2 Host 上测试镜像”混为同一个动作：

- **Part A · Global Image Ops**：发布、查询、下架全局镜像快照。一个全局目录供所有 Host 使用；创建或软删除目录记录后，所有 Host 看到的可拉取版本集合一致。
- **Part B · Test on one EC2 Host**：选择一台 EC2 Host，把全局快照安装到它的 canary 槽，创建固定到该版本的验证 VM，再在这台 Host 上提升或回滚，并回收无人引用的旧版本释放磁盘。

> “把镜像 push 到一台 EC2”是调用方视角的说法。实际数据流由 `POST /hosts/{instance_id}/pull-image` 触发，**目标 Host 按 `snapshot_time` 从 S3 pull** 镜像；控制面不把镜像字节经 Lambda 推送到 EC2。

## 0. API Gateway / OpenAPI 文档约定

仓库控制面定义保留 **OpenAPI 3.1**（`engineering/backend/openapi-control-plane.yaml`，机读单一真相源）；本文是面向调用方的规范化集成说明，按 Amazon API Gateway 的 **Resource + Method / OpenAPI Operation** 结构展开,并与该 YAML 保持一致。

- 每个接口以 `HTTP method + resource path + operationId` 唯一标识；
- 鉴权头、path/query/header 参数、`requestBody` 和 `responses` 分开描述；
- `x-api-key` 由 API Gateway usage plan 校验；启用 RBAC 时，`Authorization: Bearer <id_token>` 由 Lambda 校验；
- API Gateway/Lambda proxy 返回的 HTTP 状态码和 JSON body 共同构成契约，客户端应优先根据稳定的 `code` 分支；
- 本文中的 Global/Host-local 是业务作用域，不改变 API Gateway resource hierarchy。

## 1. 两个作用域与 API 总览

### 1.1 Part A · Global Image Ops（全局镜像目录）

| # | Method + resource path | `operationId` | 作用域 | 执行方式 | 语义 |
|---:|---|---|---|---|---|
| A1 | `POST /create-image-snapshot` | `createImageSnapshot` | 全局 | 同步 | 发布一个不可变快照到全局目录，随后所有 Host 都可按该 `snapshot_time` 拉取 |
| A2 | `GET /list_image_versions` | `listImageVersions` | 全局 | 同步查询 | 查询所有 Host 共用的全局快照目录 |
| A3 | `POST /delete-image-snapshot` | `deleteImageSnapshot` | 全局 | 同步 | 软删除并下架快照(body `{snapshot_time}`);任一 Host 槽位或租户仍引用时拒绝 |

“全局生效”是指**目录可见性和可拉取性**对所有 Host 一致，不是 fan-out 安装或删除：

- create 不会主动修改任何 Host 的 `live/canary/previous_live`，也不会启动 VM；
- delete 成功后，DynamoDB 记录保留但标记 `status=deleted` 并写入 `deleted_at`；`list_image_versions` 会过滤该记录，所有 Host 都不能再发起该快照的新 pull；
- delete 是可审计、可重复的软删除，不删除 DDB 记录、S3 `deployment/` 对象或 Host 已落盘的版本目录；
- delete 先以条件写把目录状态从 `active` 切到 `deleting`，从而阻止新 Pull 入队；随后检查所有 Host 的 `live/canary/previous_live`、所有非 deleted 租户固定的 `image_snapshot_time` 以及非终态 Pull Job。任一引用存在即回滚为 `active` 并返回 `409 IMAGE_VERSION_IN_USE`；Host mirror 缺失或超过 freshness 窗口也 fail-closed。

### 1.2 Part B · Test on one EC2 Host（单 Host 镜像测试）

| # | Method + resource path | `operationId` | 作用域 | 执行方式 | 语义 |
|---:|---|---|---|---|---|
| B1 | `POST /hosts/{instance_id}/pull-image` | `pullImage` | 指定 Host | **异步** | 目标 Host 将全局快照 pull 到 live 或 canary 槽，返回 `job_id` |
| B2 | `GET /hosts/{instance_id}/pull-image-progress?job_id=...` | `pullImageProgress` | 指定 Host | 同步查询 | 查询该 Host 的指定 pull Job |
| B3 | `POST /hosts/{instance_id}/promote-canary` | `promoteCanary` | 指定 Host | 同步 | 将验证通过的 canary 提升为该 Host 的 live |
| B4 | `POST /hosts/{instance_id}/reclaim-images` | `reclaimImages` | 指定 Host | 同步 | 删除该 Host 上无人引用的 `versions/` 版本目录，释放磁盘 |
| B5 | `GET /hosts/{instance_id}/image-slots` | `hostImageSlots` | 指定 Host | 同步查询 | 读该 Host 磁盘上真实的镜像槽位状态(live/canary/previous_live + 已装版本),排障用 |
| — | `POST /tenants` | `createTenant` | 指定 Host/VM | 现有 `201` / `202` 流程 | 用 `preferred_host_id` 在该 EC2 创建固定版本的 canary 验证租户 |
| — | `POST /tenants/{id}/rebuild` | `tenantAction` | 已存在租户 | 现有 lifecycle 流程 | **#416** 带可选 `image_channel` body 把一个【已存在】租户显式换到该 Host 当前 canary 槽的候选版本(或切回 live),复用 create 同一套字段名/CAS/错误码;换版前强制备份 fail-closed。见 §2.4。 |

完整镜像 surface 是 **3 个全局 API + 5 个 Host-local API = 8 个镜像 API**,另增量扩展现有 `POST /tenants`。只有 B1 pull 涉及下载、解压和校验,因此只有它使用异步镜像 Job;全局目录变更和 Host 指针/查询操作均同步返回。**回滚不是独立 API**:回滚 = 用 `pull-image` 把老版本装到 live —— 若该版本目录在本机【已完整装好】,pull 走快路径跳过下载、秒级翻指针(见 5.1)。**放弃 canary 也不是独立 API**:验证失败/不提升时无需显式清指针——下次 `pull-image?slot=canary` 覆盖该槽,promote 成功也会清空它;磁盘由 `reclaim-images`(B4)回收。Part B 的 pull、验证和槽位变更只作用于选中的 `instance_id`,其他 Host 的本地槽位和运行中 VM 不变。

## 2. 集成概念

### 2.1 全局快照目录与 Host-local 槽位

全局目录以 `snapshot_time` 标识可供任意 Host 拉取的不可变快照；每台 Host 的 `versions/` 与 `slots.json` 则是本地安装状态。目录发布/下架是 fleet-wide catalog operation，pull/promote/reclaim 是 single-Host operation。全局目录没有一个会自动覆盖所有 Host 的“当前 live 指针”。

### 2.2 live、canary 与 previous_live

每台 Host 独立维护三个版本引用：

- `live`：普通租户新启动或重建时使用的版本；
- `canary`：等待验证的候选版本；
- `previous_live`：最近一次提升前的 live 版本(纯展示信息)。回滚请用 `pull-image` 装老版到 live,不依赖此字段。

提升只影响指定 Host，不改变其他 Host 或全局默认版本。提升不会立即重启已在运行的 VM。

### 2.3 snapshot_time 与 generation

- `snapshot_time` 唯一标识一个不可变镜像快照，例如 `2026-07-24T02:15:30Z`。
- `generation` 标识某台 Host 的槽位配置版本，每次 pull 或 promote 成功后递增。

promote 必须回传验证过的 canary `snapshot_time` —— **版本 CAS 以 `snapshot_time` 相等为准**:只有当前 canary 的 `snapshot_time` 与你回传的不一致时才返回 `409 CANARY_CHANGED`(证明"提升的就是你验证过的那个版本")。`generation` 可选回传,仅作可观测/审计用途,**不是拒绝门**:一个滞后的 `generation`(控制面镜像与盘上真值之间常有正常漂移)【不会】导致拒绝,以免产生假冲突。创建 canary 租户时,`expected_image_snapshot_time` 是必需前置条件,`expected_image_generation` 同为可选的创建期并发提示。

### 2.4 canary 租户的版本固定

`image_channel=canary` 只用于创建时选择候选槽位，不替代现有 `POST /tenants` 的其他字段。服务端验证 `expected_image_snapshot_time` 和 `expected_image_generation` 后，把解析出的具体 `image_snapshot_time` 固定到租户记录；后续 restart、reset 或自动恢复仍使用该版本，不会跟随 canary 槽位的后续变化。

`expected_image_generation` 只参与创建时的并发校验，不是租户后续生命周期的版本选择条件。创建成功后，即使 Host generation 因 promote 增加，也不会使该租户失效。

**创建后切换版本**：管理员可调用 `POST /tenants/{id}/rebuild`，将已有租户
重建到该 Host 的 `live` 或 `canary` 槽位。

- `image_channel` 可省略；省略与显式传 `live` 完全等价。
- 只有一个 rebuild 流程；API 返回 `202` 后由异步 worker 执行，不按 channel 拆分入口。
- `live` 和 `canary` 都必须在当前 Host 上存在。缺 `live` 返回
  `409 NO_LIVE_VERSION`；缺 `canary` 返回 `409 CANARY_NOT_READY`，不会回落到另一版本。
- 槽位校验通过后，服务端会先完成强制备份。备份失败返回
  `502 REPIN_BACKUP_FAILED`，不会切换版本或重建 VM。
- `canary` 可带 `expected_image_snapshot_time` 和
  `expected_image_generation` 作为并发保护；不匹配返回 `409 CANARY_CHANGED`。

### 2.5 pull-image 异步任务

`POST /hosts/{instance_id}/pull-image` 返回 `202 Accepted` 和 `job_id`。调用方随后使用：

```http
GET /hosts/{instance_id}/pull-image-progress?job_id=<job_id>
```

查询任务，直到任务进入终态。

| `state` | 是否终态 | 说明 |
|---|---:|---|
| `NONE` | 是 | 该 Host 没有任何 pull Job；仅用于 progress 空态，不是持久 Job 状态 |
| `QUEUED` | 否 | 已受理，等待执行 |
| `STAGING` | 否 | 正在下载和准备镜像文件 |
| `VALIDATING` | 否 | 正在校验镜像完整性 |
| `COMMITTING` | 否 | 正在提交目标槽位 |
| `SUCCEEDED` | 是 | 安装成功 |
| `FAILED` | 是 | 安装失败，错误详情见 `error` |
| `RECOVERY_REQUIRED` | 是 | 服务端无法自动确认最终状态，需要平台管理员介入 |

同一台 Host 同时只执行一个镜像变更操作。已有**有效期内**的 lease 时，新请求返回 `409 IMAGE_OPERATION_IN_PROGRESS`；owner 字段残留但 lease 已过期时允许接管，并由 fence 阻止旧 worker 提交。canary pull 不会停止该 Host 上普通 live 租户的创建和运行。

进度文件 `/tmp/<job_id>.txt` 仅是实时信号，不是唯一终态来源。文件丢失或 Job 终态写回失败时，reconciler 会对 live 使用 Host `status + snapshot_time`，对 canary 使用已结束的 lease + host-agent 新鲜同步的 `slots.json` mirror。成功收敛后 `ProcessingJobStatus` 与 `state/phase/result/error` 来自同一终态；终态无法持久化时返回 `503 JOB_RECORD_UNAVAILABLE`，不会返回字段互相矛盾的 `200`。

worker 在 slots rename 落盘后发现 lease 已被接管时，Job 进入
`RECOVERY_REQUIRED`，`error.code=POST_COMMIT_FENCED`。`error.reason` 与 SSM detail
说明本地旧指针是否恢复成功；无论本地恢复结果如何，都由当前 lease owner 权威覆盖收敛。
兼容字段 `ProcessingJobStatus` 仍返回 `Failed`，旧客户端无需识别新 Job state 或错误码。

### 2.6 同步槽位操作

promote 和 reclaim-images 在确认 Host 更新成功后返回 `200 OK`，不需要轮询 progress。

这些接口支持 `Idempotency-Key`。如果客户端在收到响应前发生网络超时，可使用相同请求体和 Idempotency-Key 重试：

- 操作已完成：返回之前的成功结果；
- 操作仍在执行：返回 `409 IMAGE_OPERATION_IN_PROGRESS`；
- 服务端无法在同步时限内确认结果：返回 `503 OPERATION_STATUS_UNKNOWN`，客户端应复用原 Idempotency-Key 重试。

要释放磁盘，显式调用 `POST /hosts/{instance_id}/reclaim-images`（B4）：它删除该 Host 上不再被 `live/canary/previous_live` 或任何非 deleted 租户引用的 `versions/<snapshot>/` 目录，保护名单由服务端计算后作为白名单下发，Host 只做减法，读不到完整版本清单时 fail-loud 不删任何东西。放弃未提升的 canary 无需额外动作:下次 `pull-image?slot=canary` 覆盖该槽,promote 成功也会清空它(没有独立的清 canary 指针接口)。

## 3. 通用约定

### 3.1 鉴权与权限

所有请求均需携带：

```http
x-api-key: <api-key>
```

启用 RBAC 的环境还需根据操作提供 Cognito JWT：

| 操作 | 最低角色 |
|---|---|
| 查询快照、查询 pull 进度、读 host image-slots 状态 | viewer |
| 创建/删除全局快照、pull live/canary、创建 canary 租户 | operator |
| promote、reclaim-images | admin |

缺失或错误的 API key 可能由 API Gateway 直接返回：

```http
403 Forbidden
```

```json
{"message":"Forbidden"}
```

### 3.2 幂等与重试

Host-local 镜像变更 `pull-image`、promote 和 reclaim-images 支持：

```http
Idempotency-Key: <caller-generated UUID>
```

建议每次业务操作生成一个新的 UUID，并在网络重试时复用同一个值。

- 相同调用方、路径、请求体和 Idempotency-Key：返回原 pull Job 或原同步槽位结果；
- 相同 Idempotency-Key 对应不同请求体：返回 `409 IDEMPOTENCY_KEY_REUSED`。

全局目录接口使用自身的一致性规则：create 以 `snapshot_time` 条件写避免覆盖同秒快照；delete 使用 `active → deleting → deleted` 条件状态机。Pull admission 通过单个 DynamoDB `TransactWriteItems` 同时校验 snapshot 仍为 active 并写入 Job，因此 delete 与新 Pull 之间没有 scan-then-write 窗口。引用扫描失败会回滚 deleting gate；超时遗留的 deleting gate 可在 stale 阈值后被安全接管。重复软删除返回 `200`，只有记录从未存在时才返回 `404 NOT_FOUND`。当前实现不为这两个全局接口持久化 Idempotency-Key 结果。

### 3.3 responses 与错误信封

Lambda 应用错误使用当前 `_err(...)` 信封：

```json
{
  "error": "canary slot no longer matches the requested snapshot",
  "code": "CANARY_CHANGED"
}
```

客户端应先按 HTTP status 分类，再根据稳定的 `code` 分支，并将 `error` 用于日志和诊断。请求未进入 Lambda 时，API Gateway 可能直接返回不同信封，例如无效 API key：

```json
{"message":"Forbidden"}
```

除异步 pull 的 `202` 外，本文所有成功响应都代表该 operation 已同步完成；不能仅凭 response body 文本推断状态。

## 4. Part A · Global Image Ops

### 4.1 POST /create-image-snapshot

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `createImageSnapshot` |
| 业务分组 | Global Image Ops |
| `security` | `x-api-key`；RBAC 开启时需要 operator Bearer token |
| `parameters` | 无 |
| `requestBody` | 可选 `application/json` 对象：`label` |
| `responses` | `200`, `400`, `403`, `409`, `500`, `503` |

创建一个不可变镜像快照并发布到全局目录；发布后所有 Host 都能从同一目录发现并按 `snapshot_time` 拉取，但不会自动安装到任何 Host。

```http
POST /create-image-snapshot
Content-Type: application/json
x-api-key: <api-key>
Authorization: Bearer <operator-token>
```

```json
{"label":"v1.4"}
```

`label` 可选，格式为 `^[A-Za-z0-9._-]{1,128}$`。未提供时，服务端从 rootfs manifest 的 version 派生。

成功：

```http
200 OK
```

```json
{
  "snapshot_time": "2026-07-24T02:15:30Z",
  "label": "v1.4",
  "file_count": 66
}
```

常见错误：

- `400 VALIDATION`
- `409 CONFLICT`：同一秒创建了相同主键的快照
- `500 EMPTY_SNAPSHOT`：`deployment/` 下没有可快照化文件
- `503 NOT_CONFIGURED`

### 4.2 GET /list_image_versions

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `listImageVersions` |
| 业务分组 | Global Image Ops |
| `security` | `x-api-key`；RBAC 开启时最低 viewer |
| `parameters` | query `show_deleted`（可选,默认 false;true 时含软删条目,带 `status`) |
| `requestBody` | 无 |
| `responses` | `200`, `403`, `503` |

查询所有 Host 共用的**可拉取版本目录**(读 DDB `version-snapshots` 表),返回按 `snapshot_time` 倒序排列的 JSON 数组,每条 `{snapshot_time, label, file_count, status}`。这是"能 pull 哪些版本"的菜单——`pull-image?snapshot_time=` 的取值就来自这里。默认过滤软删(`status=deleted`)条目;`?show_deleted=true` 看全量(供 Image Snapshot 面板显示被引用却误软删的版本)。

> **`/list_image_versions` 与 `/images` 的区别(容易混淆,务必分清):**
> - **`GET /list_image_versions`(A2)**:读 **DDB 快照目录表**,列的是【版本目录/可 pull 的候选清单】——每个 `snapshot_time` 是一个可选的、不可变的历史版本。用途:**选一个版本去 pull**。
> - **`GET /images`**:读 **S3** `rootfs/` 前缀 + `rootfs/manifest.json`,列的是【S3 里物理黄金镜像工件 + 当前 manifest 指向的那一版】(即新 Host 开机装的版本)。用途:**看 S3 里现在烤好的是什么、当前 golden/live 是哪版**,不是可 pull 版本菜单。
> 一句话:`list_image_versions` = 可选版本的【目录/菜单】(DDB);`images` = S3 里【当前物理镜像 + 当前指针】。选版本 pull 用前者;查 S3 现状用后者。

```http
GET /list_image_versions
x-api-key: <api-key>
```

成功：

```json
[
  {
    "snapshot_time": "2026-07-24T02:15:30Z",
    "label": "v1.4",
    "file_count": 66
  }
]
```

### 4.3 POST /delete-image-snapshot

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `deleteImageSnapshot` |
| 业务分组 | Global Image Ops |
| `security` | `x-api-key`；RBAC 开启时需要 operator Bearer token |
| `parameters` | 无 path 参数 |
| `requestBody` | 必填,`{ "snapshot_time": "YYYY-MM-DDTHH:MM:SSZ" }`(与 `create-image-snapshot` 对称) |
| `responses` | `200`, `400`, `403`, `404`, `409`, `503` |

从全局目录软删除并下架一个快照。与 `create-image-snapshot` 一一对应:`snapshot_time` 走 **JSON body** 而不是 path(避开 ISO 时间戳里冒号在 path segment 的编码坑)。该操作对所有 Host 的后续版本发现和新 pull 生效,但保留 DDB 审计记录,也不 fan-out 删除 S3 或 Host 本地文件。

```http
POST /delete-image-snapshot
x-api-key: <api-key>
Authorization: Bearer <operator-token>
Content-Type: application/json

{ "snapshot_time": "2026-07-24T02:15:30Z" }
```

删除前服务端先执行 `active → deleting` 条件门（阻止新 Pull admission），再 fail-closed 检查：

1. Host `snapshot_time` 以及由 host-agent 持续从权威 `slots.json` 对账的 `image_slots.live/canary/previous_live`；mirror 缺失或过期即拒绝；
2. 所有 `status != deleted` 租户的 `image_snapshot_time`；
3. 所有目标为该版本的非终态 Pull Job。

Pull 的 Job Put 与 snapshot `status=active` ConditionCheck 在同一个 DynamoDB transaction 中：事务先赢时 delete 扫描能看到 Job；delete gate 先赢时 Pull 不会入队。

任一引用命中时：

```http
409 Conflict
```

```json
{
  "code": "IMAGE_VERSION_IN_USE",
  "message": "snapshot 2026-07-24T02:15:30Z is still in use (...)"
}
```

成功：

```http
200 OK
```

```json
{
  "message": "snapshot record marked deleted (soft delete)",
  "snapshot_time": "2026-07-24T02:15:30Z",
  "label": "v1.4",
  "status": "deleted",
  "note": "soft delete: DDB record marked status=deleted (not removed); image files under deployment/ were not removed"
}
```

常见错误：

- `400 VALIDATION`：`snapshot_time` 不是 `YYYY-MM-DDTHH:MM:SSZ`；
- `404 NOT_FOUND`：快照记录从未存在；已是 `status=deleted` 的记录会幂等返回 `200`；
- `409 IMAGE_VERSION_IN_USE`：任一 Host 槽位、非 deleted 租户或在飞 Pull Job 仍引用；Host slot mirror 不够新也保守拒绝；
- `409 DELETE_IN_PROGRESS` / `DELETE_STATE_CHANGED`：另一个删除者持有状态门或状态并发变化；
- `503 JOB_RECORD_UNAVAILABLE`：未部署 Job 表，无法提供可串行化删除保护；
- `503 DELETE_REFERENCE_CHECK_FAILED` / `DELETE_ROLLBACK_FAILED`：引用检查或状态门回滚失败；
- `503 NOT_CONFIGURED`。

## 5. Part B · Test on one EC2 Host

以下步骤只改变路径中 `instance_id` 指定的 EC2 Host。调用方所谓“push 镜像到 EC2”，在实现上是触发该 Host 从全局目录所指向的 S3 VersionId 拉取并安装。

### 5.1 POST /hosts/{instance_id}/pull-image

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `pullImage` |
| 业务分组 | Per-Host Image Test |
| `security` | `x-api-key`；RBAC 开启时需要 operator Bearer token |
| `parameters` | path `instance_id`；query `snapshot_time`（必填）、`slot`（可选） |
| `requestBody` | 无 |
| `responses` | `202`, `400`, `403`, `404`, `409`, `500`, `503` |

将指定快照安装到目标 Host 的 live 或 canary 槽位。

```http
POST /hosts/{instance_id}/pull-image?snapshot_time=<ISO8601>&slot=live|canary
Idempotency-Key: <uuid>
x-api-key: <api-key>
Authorization: Bearer <operator-token>
```

参数：

| 参数 | 必填 | 说明 |
|---|---:|---|
| `instance_id` | 是 | 目标 EC2 Host ID |
| `snapshot_time` | 是 | `GET /list_image_versions` 返回的快照标识 |
| `slot` | 否（见下文） | `live` 或 `canary` |

缺省或空 `slot` 会在 API 边界规范化为 `live`；Job、异步 payload、响应和 Host
安装脚本都使用规范化后的值。新的集成代码仍应显式传递 `slot`，避免运维意图不清：
正式安装用 `slot=live`，候选验证用 `slot=canary`。两者都安装到不可变版本目录并更新
`slots.json`，不会再因省略参数进入旧扁平布局。

#### "目标版本与当前槽位相同"时的行为矩阵(重要)

pull 是否允许"目标版本 == 该 Host 已有的 live/canary"取决于 `slot`,规则如下:

| `slot` | 目标 `snapshot_time` vs 该 Host 现状 | 行为 |
|---|---|---|
| `live` | **== 当前 live** | ✅ **允许**。走完整性快路径:盘上带 `.complete` → 秒级翻指针(等价重新确认/自愈);半装 → 重下。用于"重新校验/修复 live"。 |
| `live` | != 当前 live(更老或更新的版本) | ✅ **允许**。这就是【回滚】(pull 老版到 live)和【升级】的统一路径。老版通常已在本机 → 快路径秒级;不在则正常下载。 |
| `canary` | **== 当前 live** | ❌ **拒绝,`409 CANARY_EQUALS_LIVE`**。canary 与 live 同版 = 没有可验证的差异,建金丝雀租户毫无意义。请 pull 一个【不同于 live】的候选。 |
| `canary` | == 当前 canary(重复 pull 同一候选) | ✅ **允许**。不静默跳过——仍走完整性判定:`.complete` → 秒级翻指针;半装 → 重下自愈。 |
| `canary` | 其它版本 | ✅ **允许**。正常装候选到 canary 槽。 |

**为什么 live 允许同版、canary 不允许同版(有意的不对称):**

- **快路径 / 完整性判据**:任何 pull(不分 slot)若目标版本目录在盘上【已完整装好】(带 `.complete` 标记 + 各盘齐全)→ 跳过下载解压,直接翻 `slot` 指针,秒级完成;若本机没有或只装了一半(下载中断/盘满,无 `.complete`)→ 正常重新下载安装。**完整性判据只看盘上真值,不看控制面指针镜像**,故【绝不】对半装版本谎报"已就位"跳过自愈——这是"重复 pull 同版是安全幂等操作"的底层保证。
- **`slot=live` 允许同版**:因为"重新 pull live 当前版本"是有用的——重新校验 `.complete`、修复半装、以及【回滚就是 pull 老版到 live】都靠这条路径,不能封。
- **`slot=canary` 禁止 == live**:canary 的唯一用途是"在与 live 不同的候选版本上验证",与 live 同版则无可验证内容,故 `409 CANARY_EQUALS_LIVE` 直接挡住(这是准入前基于控制面镜像的建议性校验;镜像滞后的极端情况最坏也只是漏挡后落到无害的幂等 pull)。

成功受理：

```http
202 Accepted
Content-Type: application/json
```

```json
{
  "message": "pull-image started (async; poll pull-image-progress)",
  "instance_id": "i-0123456789abcdef0",
  "snapshot_time": "2026-07-24T02:15:30Z",
  "slot": "canary",
  "status": "active",
  "job_id": "pull-8a1f0b2c3d4e5f60"
}
```

当前实现不返回 `Location` 或 `Retry-After` header；客户端从 body 读取 `job_id`，并按第 5.2 节轮询。

pull 成功后，progress 响应的 `result` 提供创建 canary 租户或 promote 所需的版本信息：

```json
{
  "snapshot_time": "2026-07-24T02:15:30Z",
  "slot": "canary",
  "generation": 17
}
```

常见错误：

- `400 VALIDATION`：`snapshot_time` 必须放在 query 中，取值来自
  `GET /list_image_versions`；示例
  `?snapshot_time=2026-07-24T02%3A15%3A30Z&slot=live`
- `404 HOST_NOT_FOUND`
- `404 SNAPSHOT_NOT_FOUND`
- `409 IMAGE_OPERATION_IN_PROGRESS`
- `409 CANARY_EQUALS_LIVE`（`slot=canary` 且目标版本已是该 Host 的 live）
- `422 SNAPSHOT_INVALID`
- `503 DEPENDENCY_UNAVAILABLE`

### 5.2 GET /hosts/{instance_id}/pull-image-progress

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `pullImageProgress` |
| 业务分组 | Per-Host Image Test |
| `security` | `x-api-key`；RBAC 开启时最低 viewer |
| `parameters` | path `instance_id`；query `job_id`（新客户端应传，兼容路径可省略） |
| `requestBody` | 无 |
| `responses` | `200`, `400`, `403`, `404` |

查询指定 pull-image 任务。新客户端应始终传递 `job_id`：

```http
GET /hosts/i-0123456789abcdef0/pull-image-progress?job_id=pull-8a1f0b2c3d4e5f60
x-api-key: <api-key>
```

参数：

| 参数 | 必填 | 说明 |
|---|---:|---|
| `instance_id` | 是 | pull-image 请求中的 Host ID |
| `job_id` | 否（兼容旧客户端；新客户端必填） | `POST pull-image` 返回的任务 ID |

执行中：

```json
{
  "job_id": "pull-8a1f0b2c3d4e5f60",
  "instance_id": "i-0123456789abcdef0",
  "target_slot": "canary",
  "requested_snapshot_time": "2026-07-24T02:15:30Z",
  "state": "VALIDATING",
  "ProcessingJobStatus": "InProgress",
  "phase": "verify-checksums",
  "progress_percent": 75,
  "created_at": "2026-07-28T12:00:00Z",
  "updated_at": "2026-07-28T12:01:03Z",
  "result": null,
  "error": null
}
```

成功终态：

```json
{
  "job_id": "pull-8a1f0b2c3d4e5f60",
  "instance_id": "i-0123456789abcdef0",
  "state": "SUCCEEDED",
  "ProcessingJobStatus": "Completed",
  "result": {
    "snapshot_time": "2026-07-24T02:15:30Z",
    "slot": "canary",
    "generation": 17
  },
  "error": null
}
```

失败终态：

```json
{
  "job_id": "pull-8a1f0b2c3d4e5f60",
  "instance_id": "i-0123456789abcdef0",
  "state": "FAILED",
  "ProcessingJobStatus": "Failed",
  "result": null,
  "error": {
    "code": "CHECKSUM_MISMATCH",
    "message": "downloaded artifact checksum does not match the snapshot"
  }
}
```

无任务空态：

```json
{
  "instance_id": "i-0123456789abcdef0",
  "host_status": "active",
  "job_id": null,
  "snapshot_time": null,
  "state": "NONE",
  "ProcessingJobStatus": null,
  "last_status": null,
  "message": "no pull-image job for this host"
}
```

`state` 是新客户端使用的规范字段。`ProcessingJobStatus` 为兼容既有客户端保留，映射如下：

| `state` | `ProcessingJobStatus` |
|---|---|
| `NONE` | `null` |
| `QUEUED` / `STAGING` / `VALIDATING` / `COMMITTING` | `InProgress` |
| `SUCCEEDED` | `Completed` |
| `FAILED` / `RECOVERY_REQUIRED` | `Failed` |

兼容行为：

- 传入 `job_id`：精确查询该 Host 的指定 pull 任务；任务不存在或不属于该 Host 时返回 `404 JOB_NOT_FOUND`；
- 未传 `job_id`：返回该 Host 最近一次 pull 任务；如果该 Host 从未执行过 pull，返回
  `state=NONE`、`ProcessingJobStatus=null`，且不回填 Host 当前镜像版本到
  `snapshot_time`；
- 新客户端不应依赖“未传 job_id”的行为。

轮询建议：优先使用响应中的 `Retry-After`；未返回时从 5 秒开始，并采用指数退避和抖动。

## 6. 创建 canary 验证租户

### 6.1 POST /tenants（现有接口的增量扩展）

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `createTenant` |
| 业务分组 | Canary Validation Tenant |
| `security` | `x-api-key`；RBAC 开启时需要 operator Bearer token |
| `parameters` | 无 |
| `requestBody` | 必填 `application/json`；canary 场景增加 `preferred_host_id`, `image_channel`, `expected_image_*` |
| `responses` | `201`, `202`, `400`, `403`, `404`, `409` |

canary pull 进入 `SUCCEEDED` 后，继续调用现有 `POST /tenants`。该接口仍只有 `name` 必填，`vcpu`、`mem_mb`、`client_token`、标签、模板等既有字段及校验规则保持不变；canary 场景只增加镜像选择字段。

```http
POST /tenants
Content-Type: application/json
x-api-key: <api-key>
Authorization: Bearer <operator-token>
```

```json
{
  "name": "canary-check-v14",
  "vcpu": 2,
  "mem_mb": 4096,
  "client_token": "canary-check-v14-attempt-1",
  "preferred_host_id": "i-0123456789abcdef0",
  "image_channel": "canary",
  "expected_image_snapshot_time": "2026-07-24T02:15:30Z",
  "expected_image_generation": 17
}
```

新增字段及 canary 约束：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `image_channel` | 否 | `live` 或 `canary`；缺省为 `live`，保持现有创建行为 |
| `expected_image_snapshot_time` | 是（canary 时） | `pull-image-progress` 成功结果中的 `snapshot_time`，仅作为创建前置条件 |
| `expected_image_generation` | 是（canary 时） | `pull-image-progress` 成功结果中的 `generation`，仅作为创建前置条件 |
| `preferred_host_id` | 是（canary 时） | 现有的严格定向调度字段，不会自动回退到其他 Host |

服务端以 `preferred_host_id + expected_image_snapshot_time + expected_image_generation` 校验目标 Host 的 canary 槽位，并将实际解析出的不可变 `image_snapshot_time` 固定到租户记录。调用方不能直接写入租户记录中的固定版本字段。

`POST /tenants` 保持现有响应模型：完成校验并开始创建时可返回 `201 Created` 和 `status=creating`；启用创建队列时可返回 `202 Accepted` 和 `status=queued`。无论使用哪种模式，canary 槽位校验和版本引用都必须在响应前原子持久化，调用方随后通过现有 `GET /tenants/{id}` 查询租户状态。

示例响应：

```http
201 Created
```

```json
{
  "id": "canary-check-v14-a1b2",
  "host_id": "i-0123456789abcdef0",
  "status": "creating",
  "image_channel": "canary",
  "image_snapshot_time": "2026-07-24T02:15:30Z"
}
```

其他规则：

- `image_channel=live` 时不得传 `expected_image_snapshot_time` 或 `expected_image_generation`；
- canary 槽位不存在或未就绪时返回 `409 CANARY_NOT_READY`；
- canary 已变化时返回 `409 CANARY_CHANGED`，不会回落到 live；
- 创建成功后，租户始终使用已固定的 `image_snapshot_time`，Host generation 后续变化不影响该租户。

### 6.1 canary 租户迁移限制

V1 中，canary 租户只能迁移到已经存在相同 `snapshot_time` 的 Host。目标 Host 不具备该版本时返回 `409 TARGET_IMAGE_UNAVAILABLE`，且服务端不会自动复制镜像。

## 7. 提升(promote)

> 回滚没有独立接口:用 `pull-image?slot=live` 把要回到的老版本装到 live —— 该版本本机已完整装好时走快路径,秒级翻指针不重新下载(见 5.1)。

### 7.1 POST /hosts/{instance_id}/promote-canary

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `promoteCanary` |
| 业务分组 | Per-Host Image Test |
| `security` | `x-api-key`；RBAC 开启时需要 admin Bearer token |
| `parameters` | path `instance_id`；header `Idempotency-Key`（可选） |
| `requestBody` | 必填；`expected_canary_snapshot_time` 必填，generation 可选 |
| `responses` | `200`, `400`, `403`, `404`, `409`, `500`, `503` |

将已验证的 canary 同步提升为该 Host 的 live。必须回传验证时的 `expected_canary_snapshot_time`(版本 CAS 判据);`expected_canary_generation` 可选,仅作可观测用途,不作拒绝门(见 2.3)。

```http
POST /hosts/{instance_id}/promote-canary
Idempotency-Key: <uuid>
Content-Type: application/json
x-api-key: <api-key>
Authorization: Bearer <admin-token>
```

```json
{
  "expected_canary_snapshot_time": "2026-07-24T02:15:30Z",
  "expected_canary_generation": 17
}
```

成功：

```http
200 OK
```

```json
{
  "message": "canary promoted to live",
  "instance_id": "i-0123456789abcdef0",
  "live_snapshot_time": "2026-07-24T02:15:30Z",
  "previous_live_snapshot_time": "2026-07-23T15:52:09Z",
  "generation": 18,
  "already_promoted": false
}
```

如果 canary 在验证后被更新，返回 `409 CANARY_CHANGED`。如果相同幂等请求此前已经成功，服务端返回相同结果；live 已是目标版本时，返回 `200` 且 `already_promoted: true`。

### 7.2 回滚 = pull 老版到 live(无独立接口)

不提供独立的 rollback 接口。要把 live 换回某个更早的版本,直接:

```http
POST /hosts/{instance_id}/pull-image?snapshot_time=<要回到的老版本>&slot=live
x-api-key: <api-key>
Authorization: Bearer <operator-token>
```

该老版本目录通常已在本机(此前 promote/pull 装过,未被 `reclaim-images` 回收)→ pull 走快路径**秒级翻 live 指针,不重新下载**(见 5.1)。这与 Lambda alias / K8s revision 的"选定一个已保留版本重指指针"模型一致:回滚不是特殊操作,只是"把 live 指到你要的版本"。若该版本本机没有,pull 会正常下载安装。

## 8. 回收磁盘

放弃一个未提升的 canary 候选无需显式接口：下一次向该槽 `pull-image?slot=canary` 会覆盖它，`promote-canary` 成功后也会清空 canary 指针。被顶下来或被覆盖的旧版本文件通过下面的 `reclaim-images` 统一回收。

### 8.1 POST /hosts/{instance_id}/reclaim-images

| OpenAPI Operation 字段 | 契约 |
|---|---|
| `operationId` | `reclaimImages` |
| 业务分组 | Per-Host Image Test |
| `security` | `x-api-key`；RBAC 开启时需要 admin Bearer token |
| `parameters` | path `instance_id`；header `Idempotency-Key`（可选） |
| `requestBody` | 无 |
| `responses` | `200`, `403`, `404`, `409`, `503` |

回收该 Host 上不再被引用的版本目录，释放磁盘。保留名单 = `{live, canary, previous_live}` ∪ `{所有非 deleted 租户仍固定的 image_snapshot_time}`；不在名单里的 `versions/<snapshot>/` 目录被删除。

```http
POST /hosts/{instance_id}/reclaim-images
Idempotency-Key: <uuid>
x-api-key: <api-key>
Authorization: Bearer <admin-token>
```

成功：

```json
{
  "message": "reclaimed unreferenced image versions",
  "instance_id": "i-0123456789abcdef0",
  "kept_versions": ["2026-07-28T09:00:00Z", "2026-07-24T02:15:30Z"],
  "reclaimed_versions": ["2026-07-20T10:21:17Z"],
  "reclaimed_count": 1
}
```

安全行为：

- **无 live 一律拒绝**：`slots.json` 没有 live（扁平/损坏 Host）时返回 `409 NO_LIVE_VERSION`，不删除任何目录——此时回收可能删掉该 Host 唯一可启动的底盘。请先 pull 一个镜像建立 live 再回收；
- 保留名单由服务端计算后作为**白名单**下发，Host 只删不在名单内的目录（绝不删在用版本，防止运行中 VM 下次启动丢失只读底盘）；
- Host 无法列全 `versions/` 清单时 fail-loud，不删除任何目录；
- 不跟随 `versions/` 之外的符号链接；
- 与 pull/promote 互斥（持同一把 Host 镜像锁），进行中返回 `409 IMAGE_OPERATION_IN_PROGRESS`；
- 幂等：重复调用无害，`reclaimed_count` 可能为 0。

常见错误：

- `404 HOST_NOT_FOUND`
- `409 NO_LIVE_VERSION`
- `409 IMAGE_OPERATION_IN_PROGRESS`
- `503 OPERATION_STATUS_UNKNOWN`（Host 未确认，可安全重试）

## 9. 两部分的推荐编排

### 9.1 Part A：发布或下架全局镜像

发布：

```text
POST /create-image-snapshot
  → 全局目录出现 snapshot_time
  → GET /list_image_versions 可被所有 Host 的集成流程发现
```

安全下架：

```text
先确保所有 Host 的 live/canary/previous_live 和所有非 deleted 租户都不再引用该版本
  → POST /delete-image-snapshot
  → 200：记录保留并标记 status=deleted，从全局目录下架，所有 Host 均不能再新 pull
  → 409 IMAGE_VERSION_IN_USE：引用仍存在，不做任何删除
```

下架不等于物理擦除：DDB 行保留用于审计，S3 对象和 Host 本地文件仍由各自保留/GC 策略处理；重复 DELETE 幂等返回 `200`。

### 9.2 Part B：把候选镜像测试在一台 EC2 并提升

```text
GET /list_image_versions，选择 snapshot_time
  → 选择一个 instance_id
  → POST /hosts/{instance_id}/pull-image?snapshot_time=...&slot=canary
  → GET /hosts/{instance_id}/pull-image-progress?job_id=...，等待 SUCCEEDED
  → POST /tenants，携带 preferred_host_id=instance_id、image_channel=canary 与 expected_*
  → 在固定到该 snapshot_time 的验证 VM 中执行业务验证
  → POST /hosts/{instance_id}/promote-canary，返回 200
  → DELETE 验证租户
```

### 9.3 Part B：验证失败并放弃候选

```text
DELETE 验证租户
  → 无需显式清理 canary 指针:下次 pull canary 会覆盖它,promote 成功也会清空它
  → （可选）POST /hosts/{instance_id}/reclaim-images 回收该 Host 上不再被引用的版本目录，释放磁盘
```

### 9.4 Part B：回滚(提升出问题时换回老版)

```text
GET /list_image_versions，选出要回到的老 snapshot_time
  → POST /hosts/{instance_id}/pull-image?snapshot_time=<老版>&slot=live
  → 该版本本机已完整装好 → 快路径秒级翻 live 指针,不重新下载
  → 返回 202 + job_id;GET pull-image-progress 到 SUCCEEDED(快路径几乎立即完成)
  → 其他 Host 不变
```

## 10. 兼容性说明

### 10.1 既有接口

以下既有镜像接口继续保持兼容：

- `GET /images`：**保持不变**。列举 S3 assets bucket 的 `rootfs/` 前缀下黄金镜像各盘工件(按文件名归类 rootfs / data-template / kernel / integrity-baseline(sha256) / manifest),每条给 `name / kind / size_bytes / last_modified / is_backup`;并读 `rootfs/manifest.json` 返回完整 `manifest` 与 `live_version`(= manifest.version,即新 Host 开机会装的那版)。只读,只列清单+完整性基线是否在,**不下载/暴露镜像字节**。响应形如 `{live_version, manifest, artifact_count, artifacts[]}`。三个"看镜像"的接口别混:`/images`=【S3 物理黄金镜像 + 当前 manifest 指针】;`/list_image_versions`(A2)=【DDB 可 pull 版本目录/菜单】(详见 4.2 的对比框);`GET /hosts/{id}/image-slots`(B5)=【某台 Host 盘上真实槽位状态】。三者数据源和用途都不同。
- `POST /create-image-snapshot`
- `GET /list_image_versions`
- `POST /hosts/{instance_id}/pull-image`：缺省 `slot=live`
- `GET /hosts/{instance_id}/pull-image-progress`：允许暂时不传 `job_id`

### 10.2 新增与调整

- `pull-image` 增加 `slot=canary`，响应继续返回 `job_id`；
- `pull-image-progress` 增加可选查询参数 `job_id`，新客户端应始终传入；
- 新增全局 `POST /delete-image-snapshot` 软删除下架接口（DDB 行保留，重复删除返回 `200`）；
- 新增同步 `promote-canary` 和 `reclaim-images` API；
- `pull-image` 加快路径:目标版本本机已完整装好则跳过下载、秒级翻指针(回滚即靠此,无独立 rollback 接口);
- `POST /tenants` 保留现有请求字段和 `201` / `202` 响应模型，新增 `image_channel` 与 canary 创建前置条件字段；
- promote、reclaim-images 不需要调用 progress。

## 11. 主要错误码

| HTTP | code | 建议处理 |
|---:|---|---|
| 400 | `VALIDATION` | 修正请求参数 |
| 403 | `FORBIDDEN` | 检查 JWT 角色和 API key |
| 404 | `HOST_NOT_FOUND` | 刷新 Host 列表 |
| 404 | `SNAPSHOT_NOT_FOUND` | 刷新镜像版本列表 |
| 404 | `NOT_FOUND` | 全局快照记录从未存在；已软删除记录会幂等返回 `200` |
| 404 | `JOB_NOT_FOUND` | 检查 job_id 与 instance_id |
| 409 | `IMAGE_OPERATION_IN_PROGRESS` | 等待当前镜像操作完成后重试 |
| 409 | `CANARY_NOT_READY` | 目标 Host 尚无可用于创建验证租户的 canary 槽位 |
| 409 | `CANARY_CHANGED` | 重新读取当前 canary 的 snapshot_time/generation |
| 409 | `IMAGE_VERSION_IN_USE` | 全局下架前先清除所有 Host 槽位和租户引用 |
| 409 | `TARGET_IMAGE_UNAVAILABLE` | V1 不支持自动复制；选择已有同版的 Host |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 为新的业务操作生成新的幂等键 |
| 422 | `SNAPSHOT_INVALID` | 检查构建和快照完整性 |
| 500 | `SLOTS_CORRUPT` | Host 的 slots.json 无法解析(fail-loud,不按空处理);需人工介入修复该 Host |
| 503 | `DEPENDENCY_UNAVAILABLE` | 按退避策略重试并告警 |
| 503 | `OPERATION_STATUS_UNKNOWN` | 使用相同 Idempotency-Key 重试同步操作 |

### 11.1 Job 级错误码

`POST_COMMIT_FENCED` 是 **Job 级错误码（不是 HTTP 状态码）**，出现在异步 pull-image Job
的 `error.code` 中。兼容字段 `ProcessingJobStatus` 对它仍返回 `Failed`，旧客户端无需识别
该错误码。当前 image-lease owner 必须权威覆盖收敛；本地恢复结果见 Job reason / SSM detail。
