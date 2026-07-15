// Tenants: create/restore form, CRUD, actions, filtering, token/descriptor view.
window.ocTenants = {
  tenants: [],
  selectedHost: null,
  loadingTenants: false,
  showModal: false,
  form: {
    name: "",
    vcpu: null,
    memory_mb: null,
    config_template: "",
    preferred_host_id: "",
    group: "",
    tags_text: "",
  },
  statusFilter: "all", // filter for tenants list
  tagFilter: "", // filter expression: "k1:v1,k2:v2" — AND across pairs

  async loadTenants() {
    if (!this.apiUrl || !this.apiKey) return;
    this.loadingTenants = true;
    try {
      this.tenants = await this.api("GET", "tenants");
      this.connected = true;
    } catch {
      this.connected = false;
    }
    this.loadingTenants = false;
  },
  // Mirror of API _NAME_RE in deploy/lambda/api/handler.py — keep in sync.
  get nameError() {
    const n = (this.form.name || "").trim();
    if (!n) return "";
    if (n.length > 32) return "Name exceeds 32 characters";
    if (!/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(n)) {
      return "Name must be lowercase letters, digits, or hyphens (no leading/trailing hyphen)";
    }
    return "";
  },
  async createTenant() {
    if (!this.form.name || this.nameError) return;
    const body = { name: this.form.name };
    if (this.form.vcpu) body.vcpu = this.form.vcpu;
    if (this.form.memory_mb) body.mem_mb = this.form.memory_mb;
    if (this.form.config_template)
      body.config_template = this.form.config_template;
    if (this.form.preferred_host_id)
      body.preferred_host_id = this.form.preferred_host_id;
    // Group scopes which shared skills the VM gets; empty = broadcast (all skills).
    if (this.form.group) body.group = this.form.group;
    // Parse tags from textarea — supports "key:value" per line, also comma-separated.
    const tags = this.parseTagsInput(this.form.tags_text);
    if (Object.keys(tags).length > 0) body.tags = tags;
    if (this.restoreSource) {
      body.restore_from = {
        tenant_id: this.restoreSource.tenant_id,
        timestamp: this.restoreSource.timestamp,
      };
    }
    const r = await this.api("POST", "tenants", body);
    if (r && r.error) {
      this.toast = "✗ " + r.error;
      setTimeout(() => (this.toast = ""), 4000);
      return;
    }
    const wasRestore = !!this.restoreSource;
    this.closeCreateModal();
    if (wasRestore) {
      this.page = "tenants"; // jump to Tenants tab to watch the new VM come up
    }
    this.loadTenants();
  },
  closeCreateModal() {
    this.showModal = false;
    this.restoreSource = null;
    this.form = {
      name: "",
      vcpu: null,
      memory_mb: null,
      config_template: "",
      preferred_host_id: "",
      group: "",
      tags_text: "",
    };
  },
  parseTagsInput(text) {
    // Accepts "key:value" pairs separated by newline or comma. Empty → {}.
    const tags = {};
    if (!text) return tags;
    const parts = text
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
    for (const p of parts) {
      const idx = p.indexOf(":");
      if (idx <= 0) continue;
      const k = p.slice(0, idx).trim();
      const v = p.slice(idx + 1).trim();
      if (k && v) tags[k] = v;
    }
    return tags;
  },
  async deleteTenant(id) {
    if (!confirm("Delete tenant " + id + "?")) return;
    // #263 — delete 走队列后返 202 {status:"queued"}(不再是同步 deleted)。乐观
    // 把本地这行标 deleting,让用户立刻看到反馈、避免重复点;真正的终态由后台
    // consumer 收敛,几秒后 loadTenants 刷新会显示 deleting→消失,失败则靠 DLQ 告警
    // (运维可见)兜底。不引入轮询。同步路径(队列没开)返 deleted,行为不变。
    const r = await this.api("DELETE", "tenants/" + id);
    const t = this.tenants.find((x) => x.id === id);
    if (t && r && (r.status === "queued" || r.status === "deleting")) {
      t.status = "deleting";
    }
    this.loadTenants();
  },
  async tenantAction(id, action) {
    // 暂停能力(pause/resume)本期未落地,标 Disabled 时按钮已灰,防御性拦一层。
    if (action === "pause" || action === "resume") {
      this.toast = "✗ 暂停/恢复本期未开(SPEC 标 Disabled)";
      setTimeout(() => (this.toast = ""), 3000);
      return;
    }
    this.toast = action + "…";
    await this.api("POST", "tenants/" + id + "/" + action);
    this.toast = "✓ " + action;
    setTimeout(() => (this.toast = ""), 2000);
    this.loadTenants();
  },
  async copyText(s) {
    try {
      await navigator.clipboard.writeText(s || "");
      this.toast = "✓ 已复制";
    } catch {
      this.toast = "✗ 复制失败,请手动选中";
    }
    setTimeout(() => (this.toast = ""), 2000);
  },
  get filteredTenants() {
    let list = this.tenants;
    if (this.selectedHost)
      list = list.filter((t) => t.host_id === this.selectedHost);
    if (this.statusFilter !== "all")
      list = list.filter((t) => t.status === this.statusFilter);
    const tagPairs = this.parseTagsInput(this.tagFilter);
    const required = Object.entries(tagPairs);
    if (required.length) {
      list = list.filter((t) => {
        const tags = t.tags || {};
        return required.every(([k, v]) => tags[k] === v);
      });
    }
    return list;
  },
  get tenantStatuses() {
    return [
      ...new Set(this.tenants.map((t) => t.status).filter(Boolean)),
    ].sort();
  },
};
