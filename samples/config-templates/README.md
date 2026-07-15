# Config 模板样例（三套，不同 MCP）

三套开箱即用的 `openclaw.json` 模板，都以 deploy 的 default 全量（`templates/openclaw.json`）为基底，只在 `mcpServers` 段不同。演示「基于 default 增量改 → 另存为完整模板」的正确姿势。

> **模板是整份替换，不是合并**（`launch-vm.sh:244-246` 是 `aws s3 cp` 覆盖整个 `openclaw.json`）。所以每套都是完整可跑的一份，不是补丁。litellm 的 `baseUrl`/`apiKey` 故意不写——host 启动时用 `oc_harden_config` 补（`harden-config.sh:79-84`），别在模板里写死真实 LiteLLM IP（重建后会 401）。

| 模板文件                | MCP 组合                                                              | 场景                                            |
| ----------------------- | --------------------------------------------------------------------- | ----------------------------------------------- |
| `mcp-filesystem.json`   | `filesystem`（stdio，npx）                                            | agent 读写自己 workspace 的文件                 |
| `mcp-fetch-http.json`   | `fetch`（stdio）+ `remote-tools`（SSE 远程端点）                      | 抓网页 + 接一个远程 MCP 服务                    |
| `mcp-git-postgres.json` | `git`（stdio）+ `postgres`（stdio，env 引用注入的 `${DATABASE_URL}`） | 版本控制 + 数据库，DB 连接串走凭据注入（§11.8） |

## 怎么用

三套都通过 §7 `PUT /templates/{name}` 上架，或在控制台 Agent Config → New Template 里粘贴保存：

```bash
# 上架一套（name 就是建租户时传的 config_template）
curl -X PUT "$API/templates/mcp-filesystem" -H "x-api-key: $APIKEY" \
  -H 'content-type: application/json' --data @samples/config-templates/mcp-filesystem.json

# 建租户时选用它
curl -X POST "$API/tenants" -H "x-api-key: $APIKEY" -H 'content-type: application/json' \
  -d '{"name":"agent-fs","config_template":"mcp-filesystem"}'
```

不同租户用不同 MCP 组合 = 建租户时 `config_template` 选不同模板。

## MCP 字段名的注意

OpenClaw 的 MCP 配置字段名，不同版本 / 文档写法有两种：

- `mcpServers`（顶层，`{name:{command,args,env}}` 或远程 `{url,transport}`）—— 本目录三套用的是这个。
- `mcp.servers`（嵌在 `mcp` 下）—— 部分 OpenClaw 版本 / 官方 setup 文档用这个。

**以你实际部署的 openclaw 版本 schema 为准**（本仓 `templates/openclaw.json` 目前没带 MCP 示例段，未固化字段名）。harden 只碰 `gateway.controlUi`/`chatCompletions`/`litellm` 四类键，**不动你的 MCP 段**，所以两种写法哪个对就用哪个，改这里即可。
