# copy-file-from-s3 API 文档

> ClawPool 控制面 —— 把 S3 上的单个文件拷贝到指定宿主机(host)的指定路径(用于更新 host 脚本)。
> 单一真相源:`engineering/backend/openapi-control-plane.yaml`(operationId `copyFileFromS3`)。
> 本文按人读文档六要素组织;**返回体是本仓真实契约**(合并了标准 `{error,code}` 信封 + `ProcessingJobStatus`)。

## 通用约定

- **鉴权(客户侧只需 x-api-key)**:
  - 每次调用带 `x-api-key`(API Gateway usage-plan)。**缺/错 → API Gateway 层直接挡,403 `{"message":"Forbidden"}`**(不进 Lambda,信封不同)。
  - RBAC:`RBAC_ENABLED=true` 时,无 JWT 调用角色 = 环境变量 `DEFAULT_NO_JWT_ROLE`(**代码默认 `viewer`**;**本部署实测 `operator`**)。本接口需 operator+ → 本部署纯 api-key 直接过;若部署把它留默认 `viewer`,纯 api-key 会被 403(需 operator+ JWT)。集成前确认目标部署配置。
- **判成败(重点)**:copy 是【同步】的(单文件秒级)。返回**合并契约**——业务失败(VALIDATION/COPY_FAILED/COPY_DISPATCH_FAILED)用标准 `{error, code}`,其中 COPY_* 那两个 body 还带 **`ProcessingJobStatus: "Failed"`**。**优先按 `ProcessingJobStatus` 判**(成功恒 `Completed`);但要兜底:400 VALIDATION 没有 `ProcessingJobStatus`(没执行到 host),**未预期异常的通用 500 既无 `code` 也无 `ProcessingJobStatus`**——所以稳妥的判法是「HTTP 2xx 且 `ProcessingJobStatus==Completed` → 成功,否则失败(有 `code` 看 code,没有就按通用错误)」。

---

## 接口描述

从 S3 拷贝**单个文件**到某台 host 的指定路径,**只用于更新 host 脚本**:

- **目标限白名单根**:`/opt/openclaw/`(host-agent 的 .py service)或 `/home/ubuntu/`(.sh + lib/*)。镜像盘目录 `/data/firecracker-assets/` **禁止**(那是 pull-image 的活)。
- **落地属主/权限**:自动 `chown ubuntu:ubuntu` + `chmod 755`(-rwxr-xr-x)。因为 host-agent 以 ubuntu 跑,脚本/二进制要能执行。
- **原子写**:先下载到同目录临时文件 → 校验 → 设属主/权限 → `mv` 原子替换。传输中断不会留半截损坏文件。
- **路径安全(两层)**:① **API 层(400 VALIDATION)**——`target` 必须绝对路径、无 `..`、在白名单根下、非尾斜杠、非裸根(纯前缀/格式校验)。② **host 脚本层(502 COPY_FAILED)**——目标已存在为目录、目标或父目录组件是软链(越权逃逸)等运行期才知道的,由 host 脚本判失败,返 502。

## 请求路径与方法
```
POST /hosts/{instance_id}/copy-file-from-s3
```

## 请求参数

| 位置 | 参数 | 型态 | 必填 | 说明 |
|---|---|---|---|---|
| Path | `instance_id` | string | ✅ | 目标宿主机 EC2 instance id。 |
| Body | `target` | string | ✅ | EC2 上的**完整文件路径**(含文件名),必须在 `/opt/openclaw/` 或 `/home/ubuntu/` 下;禁 `..`、禁尾斜杠、禁只给目录。 |
| Body | `s3_uri` | string | ✅ | 源对象 `s3://<bucket>/<key>`。API 层只校验以 `s3://` 开头且长度 > `s3://`(即 `s3://bucket` 无 key 也能过 API 校验,真正拿不到对象时由 host 侧 `aws s3 cp` 失败 → 502)。 |

请求体范例:
```json
{
  "target": "/home/ubuntu/manifest3.json",
  "s3_uri": "s3://openclaw-assets-454394050889/deployment/rootfs/manifest.json"
}
```

## 成功返回范例

**200 OK** —— 拷贝完成。判成功看 `ProcessingJobStatus == "Completed"`(不必只靠 HTTP 200)。
```json
{
  "instance_id": "i-0abc123def4567890",
  "target": "/home/ubuntu/manifest3.json",
  "s3_uri": "s3://openclaw-assets-454394050889/deployment/rootfs/manifest.json",
  "ProcessingJobStatus": "Completed",
  "ExitCode": 0
}
```

## 失败返回范例

**400 Bad Request** —— 参数校验失败(`code: VALIDATION`)。s3_uri 非 `s3://`、target 越白名单 / 是目录 / 尾斜杠 / 含 `..` / 只给根目录、body 非法 JSON,都归这里。
```json
{ "error": "s3_uri must be s3://<bucket>/<key>", "code": "VALIDATION" }
```
```json
{ "error": "target must be a full file path (no trailing slash / directory)", "code": "VALIDATION" }
```
```json
{ "error": "target must be under /opt/openclaw/ or /home/ubuntu/", "code": "VALIDATION" }
```

**403 Forbidden** —— 缺/错 `x-api-key`(API Gateway 挡,`{"message":"Forbidden"}`);或带 viewer 级 JWT(RBAC,纯 api-key 默认 operator 不触发)。

**500 Internal Server Error** —— SSM send-command 根本没下发出去(`code: COPY_DISPATCH_FAILED`)。body 带 `ProcessingJobStatus: "Failed"`。
```json
{
  "error": "SSM send-command dispatch failed",
  "code": "COPY_DISPATCH_FAILED",
  "instance_id": "i-0abc123def4567890",
  "target": "/home/ubuntu/manifest3.json",
  "s3_uri": "s3://openclaw-assets-454394050889/deployment/rootfs/manifest.json",
  "ProcessingJobStatus": "Failed"
}
```

**502 Bad Gateway** —— SSM invocation 下发了但没成功完成(`code: COPY_FAILED`):脚本失败(目标校验被拒 / 软链逃逸 / 下载失败 / 盘问题),**或** invocation 没出现 / 超时 / 被取消。`error` 是 SSM stderr 末段真实原因(无输出时是 "copy-file failed (see SSM log)"),body 带 `ProcessingJobStatus: "Failed"`。
```json
{
  "error": "[copy-file] resolved parent escapes allowed roots: /etc (symlink escape?)",
  "code": "COPY_FAILED",
  "instance_id": "i-0abc123def4567890",
  "target": "/home/ubuntu/manifest3.json",
  "s3_uri": "s3://openclaw-assets-454394050889/deployment/rootfs/manifest.json",
  "ProcessingJobStatus": "Failed"
}
```

> **兜底 500(无 code)**:上述是 copy-file 主动返回的错误。若后端发生**未预期异常**(body 非法类型、DDB/SSM 报错等),handler 顶层会兜成通用 `500 { "error": "<异常信息>" }`——**没有 `code`、没有 `ProcessingJobStatus`**。客户端遇 5xx 且无 code 时按通用错误处理,不能假设"所有失败都有 code / 只看 ProcessingJobStatus"。

## 状态码速查表

### HTTP 状态码

| HTTP | 含义 | code | ProcessingJobStatus |
|---|---|---|---|
| 200 | 拷贝完成 | — | `Completed`(+ExitCode:0) |
| 400 | 参数校验失败 | `VALIDATION` | — |
| 403 | 缺/错 x-api-key(API GW `{message}`)或 viewer JWT | — | — |
| 500 | SSM 没下发 | `COPY_DISPATCH_FAILED` | `Failed` |
| 502 | SSM 脚本跑了但拷贝失败 | `COPY_FAILED` | `Failed` |

> **判成败**:客户只需看 body 的 `ProcessingJobStatus`——`Completed`=成功;`Failed`=失败(看 `code` + `error` 定位)。400 参数错是纯 `{error,code}`(没 ProcessingJobStatus,因为压根没执行到 host)。缺 x-api-key 的 403 是 API Gateway 的 `{"message":"Forbidden"}`(不同信封)。

### 字段说明

| 字段 | 型态 | 说明 |
|---|---|---|
| `ProcessingJobStatus` | string | `Completed` / `Failed`。**判成败只看这个**。 |
| `ExitCode` | int | 仅 `Completed` 时出现,恒 `0`。 |
| `code` | string | 失败时的错误码:`VALIDATION` / `COPY_FAILED` / `COPY_DISPATCH_FAILED`。 |
| `error` | string | 失败原因(脚本失败时是 SSM stderr 末段真实报错)。 |
| `target` / `s3_uri` / `instance_id` | string | 回显请求(成功和执行类失败都带)。 |

## 落地效果

拷贝成功后,目标文件在 host 上是:
```
-rwxr-xr-x 1 ubuntu ubuntu <size> <time> <target>
```
(属主 `ubuntu:ubuntu`,权限 `755`,原子落地)。
