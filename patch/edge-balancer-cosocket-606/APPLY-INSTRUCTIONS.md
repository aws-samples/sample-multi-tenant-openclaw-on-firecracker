# edge-balancer-cosocket-606 · 施加说明

> **P0。** `balancer_by_lua*` 阶段调 Redis cosocket 被 OpenResty 硬禁止，导致任何触发
> upstream 重投的租户 WSS 全部 502。缺陷自 **2026-07-21** 起就在在役 edge bundle 里，
> 只有「租户被重建 / 恢复 / 迁移」这类会让 L1 缓存失效的场景才暴露。

---

## 0. 先读这一节：交付层与生效条件

**这个 kit 的 `apply_cli` 真的下发到在役 edge。** 它不是 `lifecycle-op-patch` 里的
`deploy-other` 层 —— 那一层的 `apply_cli` 只做 `cp -p "$REPO_ROOT/..."`，**打完在役不生效**。

edge 的交付结构决定了三件事：

| 事实 | 后果 |
|---|---|
| edge 是独立数据面 ASG | 控制面 API、Lambda overlay、host 脚本下发都碰不到它 |
| bundle 由 LaunchTemplate 钉 sha | 改 S3 对象未必影响新实例，取决于 userdata 有无 sha 校验 |
| openresty 从 `lualib` 加载，从 `/opt/openclaw-edge` bootstrap | **两处都要写**，只改 `lualib` 会在重启后丢失修复 |

所以本 kit 分两个阶段：**S3 放制品**（零在役影响）→ **SSM 写两处落点 + reload**（生效）。

---

## 1. 四个文件必须整套下发

不要逐文件下发、不要逐文件 reload。依赖关系：

- `balancer.lua` 调用 `backend.peek_cached` / `backend.mark_retry_stale` / `backend.SOURCE_L2`
  —— 这三个是本次在 `backend.lua` 新增的
- `route.lua` 提供 `on_header_filter`
- `nginx.conf` 提供 `header_filter_by_lua_block`，把 balancer 阶段 fail-closed 的 503
  从 nginx 生成的 500 改写回真实状态码

缺任一文件的表现：`attempt to call a nil value`，或者 fail-closed 时客户端拿到 500 而非 503。

---

## 2. 施加步骤

### Step 0 · 环境与前置

```bash
export REGION=<region>
export ASSETS_BUCKET=<openclaw-assets-...>
# edge 实例：按 tag 取，逐台施加，不要一次全发
export EDGE_IDS="$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Role,Values=<edge-role-tag>" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
```

施加前记录基线，逐台存档：

```bash
# 在役四个文件的 sha256（用于确认偏离程度、以及回滚核对）
sha256sum /usr/local/openresty/lualib/edge/lib/balancer.lua \
          /usr/local/openresty/lualib/edge/lib/backend.lua \
          /usr/local/openresty/lualib/edge/route.lua \
          /usr/local/openresty/nginx/conf/nginx.conf
```

如果在役 sha 与 `manifest.json` 的 `patch_sha256` 已经相同 —— 该台已修，跳过。

### Step 1 · 制品上传到 S3（零在役影响）

按 `manifest.json` 里每个 path 的 `operations[0].apply_cli` 执行。制品放在
**新前缀 `deployment/edge-606/`**，不覆盖任何现有 bundle 对象，所以这一步完全可逆
（回滚就是 `delete-object`）。

### Step 2 · 下发到在役（逐台 canary）

对**第一台** edge 执行，命令体见 `operations[1].apply_steps`。要点：

- 备份锚点 `.pre-606` **只建一次**（`[ -f ... ] ||` 守卫）。重跑若覆盖锚点，
  备份就变成已修补的内容，回滚从此无效
- 下发后 `sha256sum -c -` 逐文件核对，**两处落点都要核**
- 四个文件都写完，**才** `openresty -s reload`（worker 热重载，不断现有连接）
- `nginx -t` 可以先跑，但它**不加载 lua 模块** —— 通过不代表 lua 能载入

### Step 3 · 验证第一台（见第 3 节），通过后再铺剩余实例

### Step 4 · 新实例是否继承修复（MANUAL_CLI_REVIEW）

Step 1-3 只止血在役实例。任何扩容、健康检查替换、AZ 重平衡起来的新 edge
仍会从 S3 拉旧 bundle、带回原缺陷。处置取决于一个前置：

```bash
aws ec2 describe-launch-template-versions --region "$REGION" \
  --launch-template-name <edge-lt> --versions '$Latest' \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
  | base64 -d | grep -nE 'sha256sum|sha256 -c'
```

- **无 sha 校验** → 可直接更新现有 bundle 对象。先记下旧 `VersionId` 作回滚锚点
  （S3 versioning 使字节替换可逆）
- **有 sha 校验** → 改 bundle 内容后 sha 不匹配、新实例起不来。必须 S3 与
  LaunchTemplate 同步改，属换机级操作，**单独排期**，不要接在本次施加后面

---

## 3. 验证：只有一个探针能证明修复生效

### 必做：触发 retry 路径

**正常请求不进 retry 分支。** 所以「WSS 能连上」「对话正常」这类验证**完全没有验到这个 fix**。

必须构造 route descriptor 失效的场景：

1. 选一个测试租户，记录它当前的 `host:port`
2. 让那个 VM 不可达（停止它，或用其它方式让首连拿到 RST）
3. 从外部发起该租户的 WSS
4. 断言：**L1 缓存 TTL 内的下一次请求连到新 peer**，且 `error.log` 无 `API disabled`

修复后的行为是：balancer 发现失败 → 从 shared_dict 找一个**不同**的 peer；找不到就
`mark_retry_stale` 后 fail closed 返 503。route 变更由 L1 TTL 过期驱动，
下一次请求在 `rewrite` 阶段（cosocket 合法）读 Redis 拿到新 desc 自愈。

### 必做：fail-closed 返 503 而非 500

构造共享缓存中不存在任何不同 peer 的场景，断言客户端拿 **503**。
拿到 500 说明 `nginx.conf` 没有下发成功（`header_filter_by_lua_block` 缺失）。

### 自动检查

```bash
# 期望 0
journalctl -u openresty --no-pager --since '-10min' \
  | grep -c 'API disabled in the context of balancer_by_lua' || true

# 两处落点的 sha256 都要等于 manifest 的 patch_sha256
sha256sum <live_target> <bootstrap_source>
```

---

## 4. 陷阱清单

| 陷阱 | 说明 |
|---|---|
| **`nginx -t` 通过不代表 lua 能载入** | `-t` 不加载 lua 模块。lua 的加载与运行失败只出现在 journald 与 `error.log` |
| **只改 `lualib` 会在重启后丢** | `/opt/openclaw-edge` 是 bootstrap 源，两处都要写 |
| **只验正常请求等于没验** | 正常请求不进 retry 分支，验不到本 fix。必须主动触发 retry |
| **故障表现为「部分租户断」** | 首连即命中有效 desc 的租户不受影响，容易误判成租户个体问题或 route 数据错误 |
| **不要重用失败的 desc** | 那个坐标可能已被回收重分配给另一个在役租户，重投会连上别人的 VM。这是安全边界，不是性能优化 |
| **回滚到更早的 bundle 无效** | 本缺陷自 2026-07-21 起就在。回滚到更早版本 = 重新引入同一缺陷 |
| **`systemctl status` 可能失真** | 若 openresty 非 systemd 管理，active/inactive 判断不可靠，需同时看进程与端口监听 |

---

## 5. 回滚

**Step 2 的回滚**（每台）：

```bash
cp -p <live_target>.pre-606      <live_target>
cp -p <bootstrap_source>.pre-606 <bootstrap_source>
openresty -s reload
```

**Step 1 的回滚**：`aws s3api delete-object --key deployment/edge-606/<file>`

**注意**：回滚意味着恢复到带缺陷的版本，触发 retry 的租户会重新开始 502。
只在下发本身出问题（lua 载入失败、openresty 起不来）时回滚，
不要因为「部分租户仍连不上」就回滚 —— 那可能是另一层的问题
（例如 gateway 应用层的 Origin 校验，与本 kit 无关）。

---

## 6. 本 kit 不修什么

- **gateway 应用层的 `controlUi.allowedOrigins` 校验**。`OcSession connect rejected:
  INVALID_REQUEST: origin not allowed` 是 guest 内 openclaw gateway 的 Origin 白名单问题，
  与 edge balancer 无关。那是接入方式问题，需要单独处置
- **Redis route 键的回收**。route 键不会被自动回收，恢复租户靠新写入覆盖
- **guest 出网策略（egress）**。与本缺陷无因果关系

---

## 7. 溯源

| 项 | 值 |
|---|---|
| 上游 issue | #606（fail-closed 语义 #628，坐标回收风险 #605） |
| 修复来源 commit | `71c8cf46`、`655ea312` |
| 测试覆盖 | `deploy/edge/test/balancer_failover_adversarial_spec.lua`（含 no-cross-tenant isolation）、`balancer_spec.lua` |
| 决策记录 | `ADR-edge-balancer-retry-no-cosocket.md`、`ADR-edge-retry-no-fallback-to-failed-peer.md` |

单元测试**覆盖不到**这类缺陷：用假 redis 模块的测试会全绿通过，因为它模拟不出
`balancer_by_lua*` 的上下文限制。本次修复连带修正了原来那些基于不成立契约的断言。
