// Core: API client, settings, refresh orchestration, toast/connection state.
// Loaded before app.js. Merged into the Alpine component via mergeModules().
window.ocCore = {
  apiUrl: "",
  apiKey: "",
  connected: false,
  toast: "",
  page: "tenants",
  showApiKey: false,
  saving: false,
  pollMs: 5000, // live-status poll cadence; a host's active→upgrading→active shows without a manual refresh
  _pollTimer: null,

  init() {
    this.apiUrl =
      localStorage.getItem("oc_api_url") || window.OC_DEFAULT_API_URL || "";
    this.apiKey =
      localStorage.getItem("oc_api_key") || window.OC_DEFAULT_API_KEY || "";
    // Restore the last-viewed tab so a refresh doesn't snap back to Tenants.
    // Whitelist guards against a stale/garbage value pointing at a dead tab.
    const pages = [
      "tenants",
      "app",
      "monitoring",
      "backups",
      "poolops",
      "loadtest",
      "edge",
      "traces",
      "logs",
      "settings",
    ];
    const saved = localStorage.getItem("oc_page");
    if (pages.includes(saved)) this.page = saved;
    this.$watch("page", (v) => localStorage.setItem("oc_page", v));
    if (this.apiUrl && this.apiKey) {
      this.refresh();
      // refresh() doesn't cover the tabs that lazy-load on click. When we restore
      // straight onto one of them, fire its loader so the page isn't empty.
      if (this.page === "app") this.loadAgentCoreTools();
      if (this.page === "backups") this.loadBackups();
      this.startPolling();
    }
  },
  // #217 — quietly re-poll host+tenant status so a host's active→upgrading→active
  // cycle (e.g. after a snapshot Pull) shows up without hitting refresh. Pauses
  // when the tab is backgrounded so we don't burn API calls no one is watching.
  startPolling() {
    if (this._pollTimer) return;
    const tick = () => {
      if (document.hidden) return; // backgrounded → skip this beat, keep the timer
      this.pollHosts();
      this.pollTenants();
      this.pollUpgradingProgress(); // #309 — live pull-image status for upgrading hosts
    };
    this._pollTimer = setInterval(tick, this.pollMs);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tick(); // catch up immediately when the tab returns
    });
  },
  saveSettings() {
    localStorage.setItem("oc_api_url", this.apiUrl);
    localStorage.setItem("oc_api_key", this.apiKey);
  },
  async api(method, path, body) {
    const url =
      this.apiUrl.replace(/\/+$/, "") + "/" + path.replace(/^\/+/, "");
    const headers = {
      "x-api-key": this.apiKey,
      "Content-Type": "application/json",
    };
    // 1.5.0: the API Lambda verifies a Cognito id_token's signature and reads cognito:groups for RBAC.
    // Without this header every write is downgraded to viewer and 403s.
    const token = localStorage.getItem("oc_id_token");
    if (token) headers["Authorization"] = "Bearer " + token;
    const opts = { method, headers };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(url, opts);
    // Issue #16 — on 401 (token expired), attempt silent refresh then retry once.
    if (r.status === 401 && window.refreshIdToken) {
      const newToken = await window.refreshIdToken();
      if (newToken) {
        opts.headers["Authorization"] = "Bearer " + newToken;
        const r2 = await fetch(url, opts);
        this.connected = r2.ok;
        return r2.json();
      }
      // Refresh failed — force re-login by clearing tokens and reloading.
      localStorage.removeItem("oc_id_token");
      localStorage.removeItem("oc_refresh_token");
      localStorage.removeItem("oc_token_exp");
      window.location.reload();
      return {};
    }
    this.connected = r.ok;
    return r.json();
  },
  // #269 — 写操作 fail-loud 版:和 api() 同样构造(url/header/token/401 刷新),但
  // 返回 {ok, status, body} 而非吞掉 status。api() 为读路径保留(不改其行为,避免
  // 波及全站调用方);写操作(saveTemplate/deleteTemplate)用这个才能把 WAF 403 /
  // 后端 5xx 报给用户,不静默关弹窗骗"成功"。body 解析失败(非 JSON)时 body=null。
  async apiStatus(method, path, body) {
    const url =
      this.apiUrl.replace(/\/+$/, "") + "/" + path.replace(/^\/+/, "");
    const headers = {
      "x-api-key": this.apiKey,
      "Content-Type": "application/json",
    };
    const token = localStorage.getItem("oc_id_token");
    if (token) headers["Authorization"] = "Bearer " + token;
    const opts = { method, headers };
    if (body !== undefined) opts.body = JSON.stringify(body);
    let r = await fetch(url, opts);
    if (r.status === 401 && window.refreshIdToken) {
      const newToken = await window.refreshIdToken();
      if (newToken) {
        opts.headers["Authorization"] = "Bearer " + newToken;
        r = await fetch(url, opts);
      }
    }
    this.connected = r.ok;
    let parsed = null;
    try {
      parsed = await r.json();
    } catch {
      /* 非 JSON 响应(如 WAF 的 HTML/空 body):body 留 null */
    }
    return { ok: r.ok, status: r.status, body: parsed };
  },
  async refresh() {
    await Promise.all([
      this.loadHosts(),
      this.loadTenants(),
      this.loadTemplates(),
      this.loadSystemInfo(),
    ]);
  },
  copyCmd(cmd) {
    navigator.clipboard.writeText(cmd);
    this.toast = "✓ Copied: " + cmd;
    setTimeout(() => (this.toast = ""), 2000);
  },
};

// R15.2:Settings 页 SSM 平台默认值面板。独立 alpine data,只走 BFF /capi/system/defaults,
// 不接触 admin key。GET 读全部(vkey 类字段掩码只回尾 4 位),POST 只发用户改过的字段。
window.sysDefaults = function () {
  return {
    FIELDS: {
      litellm_host: {
        label: "LiteLLM Gateway URL",
        secure: false,
        masked: false,
      },
      litellm_shared_vkey: {
        label: "LiteLLM Shared vkey (SecureString, 掩码回显)",
        secure: true,
        masked: true,
      },
      config_template: {
        label: "config_template (registry pointer)",
        secure: false,
        masked: false,
      },
      rootfs_manifest_version: {
        label: "rootfs manifest version",
        secure: false,
        masked: false,
      },
    },
    values: {},
    edits: {},
    saving: false,
    feedback: "",
    loadError: "",
    async load() {
      this.feedback = "";
      this.loadError = "";
      try {
        const r = await fetch("/capi/system/defaults");
        if (!r.ok) {
          this.loadError = `读取失败 (${r.status}) — 请确认已登录 + BFF 有 SSM 读权限`;
          return;
        }
        const j = await r.json();
        this.values = j.values || {};
        this.edits = {};
      } catch (e) {
        this.loadError =
          "网络错误: " + (e && e.message ? e.message : String(e));
      }
    },
    async save() {
      this.feedback = "";
      const payload = {};
      for (const [k, v] of Object.entries(this.edits)) {
        if (typeof v === "string" && v.length > 0) payload[k] = v;
      }
      if (Object.keys(payload).length === 0) {
        this.feedback = "无待更新字段";
        return;
      }
      this.saving = true;
      try {
        const r = await fetch("/capi/system/defaults", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        });
        const j = await r.json();
        if (r.ok) {
          this.feedback = "OK — 已更新: " + (j.updated || []).join(", ");
          await this.load();
        } else {
          this.feedback =
            "失败 (" +
            r.status +
            "): " +
            (j.error || "unknown") +
            (j.details ? " " + JSON.stringify(j.details) : "");
        }
      } catch (e) {
        this.feedback = "网络错误: " + (e && e.message ? e.message : String(e));
      } finally {
        this.saving = false;
      }
    },
  };
};
