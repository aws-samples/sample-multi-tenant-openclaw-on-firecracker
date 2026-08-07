// Tenants: create/restore form, CRUD, actions, filtering, reveal token, descriptor.
window.ocTenants = {
  tenants: [], selectedHost: null,
  loadingTenants: false,
  showModal: false,
  form: { name: '', vcpu: null, memory_mb: null, config_template: '', preferred_host_id: '', group: '', tags_text: '', image_channel: 'live' },
  statusFilter: 'all',   // filter for tenants list
  tagFilter: '',         // filter expression: "k1:v1,k2:v2" — AND across pairs
  // #187 转型:reveal token + descriptor modal 状态。
  // reveal 显示的是 KMS 密文(base64 信封,§INTERFACE-CONTRACT §5),调用方
  // 拿 tenant_id 作 EncryptionContext 自解得明文;API 侧不 decrypt。
  revealState: { open: false, tenantId: '', ciphertext: '', error: '', descriptor: null, loading: false },

  async loadTenants() {
    if (!this.apiUrl || !this.apiKey) return;
    this.loadingTenants = true;
    try { this.tenants = await this.api('GET', 'tenants'); this.connected = true; }
    catch { this.connected = false; }
    this.loadingTenants = false;
  },
  // #217 — quiet poll variant: refresh tenants WITHOUT the loading flag so the
  // 5s background poll doesn't flash the spinner or empty-state on every beat.
  async pollTenants() {
    if (!this.apiUrl || !this.apiKey) return;
    try { this.tenants = await this.api('GET', 'tenants'); this.connected = true; }
    catch { this.connected = false; }
  },
  // Mirror of API _NAME_RE in deploy/lambda/api/handler.py — keep in sync.
  get nameError() {
    const n = (this.form.name || '').trim();
    if (!n) return '';
    if (n.length > 32) return 'Name exceeds 32 characters';
    if (!/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(n)) {
      return 'Name must be lowercase letters, digits, or hyphens (no leading/trailing hyphen)';
    }
    return '';
  },
  async createTenant() {
    if (!this.form.name || this.nameError) return;
    const body = { name: this.form.name };
    if (this.form.vcpu) body.vcpu = this.form.vcpu;
    if (this.form.memory_mb) body.mem_mb = this.form.memory_mb;
    if (this.form.config_template) body.config_template = this.form.config_template;
    if (this.form.preferred_host_id) body.preferred_host_id = this.form.preferred_host_id;
    // #394 — canary tenant: boots the host's canary slot. Requires preferred_host_id
    // (the canary slot only exists on the host it was pulled to). Default 'live' →
    // omit the field so legacy create behavior is byte-identical.
    if (this.form.image_channel === 'canary') body.image_channel = 'canary';
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
    const r = await this.api('POST', 'tenants', body);
    if (r && r.error) {
      this.toast = '✗ ' + r.error;
      setTimeout(() => this.toast = '', 4000);
      return;
    }
    const wasRestore = !!this.restoreSource;
    this.closeCreateModal();
    if (wasRestore) {
      this.page = 'tenants';    // jump to Tenants tab to watch the new VM come up
    }
    this.loadTenants();
  },
  closeCreateModal() {
    this.showModal = false;
    this.restoreSource = null;
    this.form = { name: '', vcpu: null, memory_mb: null, config_template: '', preferred_host_id: '', group: '', tags_text: '', image_channel: 'live' };
  },
  parseTagsInput(text) {
    // Accepts "key:value" pairs separated by newline or comma. Empty → {}.
    const tags = {};
    if (!text) return tags;
    const parts = text.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    for (const p of parts) {
      const idx = p.indexOf(':');
      if (idx <= 0) continue;
      const k = p.slice(0, idx).trim();
      const v = p.slice(idx + 1).trim();
      if (k && v) tags[k] = v;
    }
    return tags;
  },
  async deleteTenant(id) {
    if (!confirm('Delete tenant ' + id + '?')) return;
    await this.api('DELETE', 'tenants/' + id);
    this.loadTenants();
  },
  async tenantAction(id, action) {
    // 暂停能力(pause/resume)本期未落地,标 Disabled 时按钮已灰,防御性拦一层。
    if (action === 'pause' || action === 'resume') {
      this.toast = '✗ 暂停/恢复本期未开(SPEC 标 Disabled)';
      setTimeout(() => this.toast = '', 3000);
      return;
    }
    this.toast = action + '…';
    await this.api('POST', 'tenants/' + id + '/' + action);
    this.toast = '✓ ' + action;
    setTimeout(() => this.toast = '', 2000);
    this.loadTenants();
  },
  // #187 转型:拉 gateway token 密文 + descriptor(host_private_ip/host_port/
  // guest_ip/status)进 modal。轮询响应带 gateway_token 密文由 API Lambda 从
  // openclaw-tenant-secrets 表读出原样返回,不 decrypt。调用方拷回本地自解。
  async revealToken(id) {
    this.revealState = { open: true, tenantId: id, ciphertext: '', error: '', descriptor: null, loading: true };
    try {
      // 直接 GET /tenants/{id}/token —— 与就绪轮询响应等价的独立别名,body
      // 载 { gateway_token, gateway_token_ciphertext, encryption_context, ... }。
      const r = await this.api('GET', 'tenants/' + id + '/token');
      if (r && r.error) {
        this.revealState.error = r.error;
      } else {
        this.revealState.ciphertext = r.gateway_token || r.gateway_token_ciphertext || '';
        this.revealState.descriptor = {
          host_private_ip: r.host_private_ip || r.host || '',
          host_port: r.host_port || '',
          guest_ip: r.guest_ip || '',
          status: r.status || '',
          encryption_context: r.encryption_context || { tenant_id: id },
        };
      }
    } catch (e) {
      this.revealState.error = String(e && e.message || e);
    } finally {
      this.revealState.loading = false;
    }
  },
  closeReveal() {
    this.revealState = { open: false, tenantId: '', ciphertext: '', error: '', descriptor: null, loading: false };
  },
  async copyText(s) {
    try {
      await navigator.clipboard.writeText(s || '');
      this.toast = '✓ 已复制';
    } catch {
      this.toast = '✗ 复制失败,请手动选中';
    }
    setTimeout(() => this.toast = '', 2000);
  },
  get filteredTenants() {
    let list = this.tenants;
    if (this.selectedHost) list = list.filter(t => t.host_id === this.selectedHost);
    if (this.statusFilter !== 'all') list = list.filter(t => t.status === this.statusFilter);
    const tagPairs = this.parseTagsInput(this.tagFilter);
    const required = Object.entries(tagPairs);
    if (required.length) {
      list = list.filter(t => {
        const tags = t.tags || {};
        return required.every(([k, v]) => tags[k] === v);
      });
    }
    return list;
  },
  get tenantStatuses() {
    return [...new Set(this.tenants.map(t => t.status).filter(Boolean))].sort();
  },
};
