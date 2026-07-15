// Config templates: load, save (JSON-validated), delete.
//
// Config Template = 整份 openclaw.json 快照替换（launch-vm.sh 是 aws s3 cp 覆盖，
// 不与内置默认做 merge）。所以「基于 default 增量改」的正确姿势是：把下面的 DEFAULT
// 全量基线拷进编辑框，在它上面加 MCP / 改模型，另存成完整模板。基线本身只读展示，
// 不可编辑（防误改污染基底）。litellm 的 baseUrl/apiKey 由 host 启动时补，模板里不写死。
window.ocTemplates = {
  templates: [],
  editTpl: null,

  // 内置默认 openclaw.json 全量基线（= deploy 的 templates/openclaw.json，只读参考）。
  // 界面在 New Template 弹窗里原样展示，用户点「以默认基线填充」把它灌进可编辑框再加 MCP。
  DEFAULT_TPL_BASELINE: JSON.stringify(
    {
      agents: {
        defaults: {
          workspace: "/home/agent/.openclaw/workspace",
          model: { primary: "litellm/claude-sonnet-4-6" },
          heartbeat: { every: "30m", target: "last" },
          contextPruning: {
            mode: "cache-ttl",
            ttl: "3m",
            keepLastAssistants: 3,
            softTrimRatio: 0.3,
            hardClearRatio: 0.5,
            minPrunableToolChars: 5000,
            softTrim: { maxChars: 2000, headChars: 600, tailChars: 600 },
            hardClear: {
              enabled: true,
              placeholder: "[Old tool result content cleared to save tokens]",
            },
          },
          compaction: {
            mode: "safeguard",
            reserveTokens: 100000,
            keepRecentTokens: 100000,
            reserveTokensFloor: 100000,
            maxHistoryShare: 0.4,
            memoryFlush: { enabled: true, softThresholdTokens: 6000 },
          },
          maxConcurrent: 4,
          subagents: { maxConcurrent: 8 },
        },
      },
      gateway: {
        mode: "local",
        auth: { mode: "token" },
        port: 18789,
        bind: "lan",
        controlUi: { enabled: false },
        http: {
          endpoints: {
            chatCompletions: { enabled: true },
            responses: { enabled: true },
          },
        },
      },
      session: { dmScope: "per-peer" },
      tools: {
        profile: "coding",
        loopDetection: {
          enabled: true,
          historySize: 30,
          warningThreshold: 5,
          criticalThreshold: 10,
          globalCircuitBreakerThreshold: 15,
          detectors: {
            genericRepeat: true,
            knownPollNoProgress: true,
            pingPong: true,
          },
        },
      },
      models: {
        providers: {
          litellm: {
            api: "openai-completions",
            models: [
              {
                id: "claude-sonnet-4-6",
                name: "Claude Sonnet 4.6 (via LiteLLM→Bedrock)",
                reasoning: true,
                contextWindow: 200000,
                maxTokens: 64000,
                input: ["text", "image"],
              },
              {
                id: "claude-haiku-4-5",
                name: "Claude Haiku 4.5 (via LiteLLM→Bedrock)",
                reasoning: false,
                contextWindow: 200000,
                maxTokens: 32000,
                input: ["text", "image"],
              },
            ],
          },
        },
      },
    },
    null,
    2,
  ),

  // 把只读基线灌进可编辑框，用户在此之上加 MCP / 改模型（"基于 default 增量改"入口）。
  useBaseline() {
    if (this.editTpl) this.editTpl.content = this.DEFAULT_TPL_BASELINE;
  },

  async loadTemplates() {
    try {
      const r = await this.api("GET", "templates");
      this.templates = r.templates || [];
    } catch {}
  },
  async saveTemplate() {
    if (!this.editTpl?.name) return;
    let content;
    try {
      content = JSON.parse(this.editTpl.content);
    } catch (e) {
      alert("Invalid JSON: " + e.message);
      return;
    }
    // #269 fail-loud:PUT 非 2xx(WAF 403 / 后端 5xx)必须报出来,不能静默关弹窗
    // 骗用户"保存成功"。用 apiStatus 拿到真实 status;失败保留弹窗 + 内容不丢。
    const res = await this.apiStatus(
      "PUT",
      "templates/" + this.editTpl.name,
      content,
    );
    if (!res.ok) {
      alert(
        "保存失败 (HTTP " +
          res.status +
          "): " +
          (res.body?.error || res.body?.message || "请重试或检查网络/权限"),
      );
      return; // 保留 editTpl,内容不丢,用户可重试
    }
    this.editTpl = null;
    this.loadTemplates();
  },
  async deleteTemplate(name) {
    if (!confirm('Delete template "' + name + '"?')) return;
    const res = await this.apiStatus("DELETE", "templates/" + name);
    if (!res.ok) {
      alert(
        "删除失败 (HTTP " +
          res.status +
          "): " +
          (res.body?.error || res.body?.message || "请重试"),
      );
      return;
    }
    this.loadTemplates();
  },
};
