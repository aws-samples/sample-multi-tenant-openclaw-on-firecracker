// Core: API client, settings, refresh orchestration, toast/connection state.
// Loaded before app.js. Merged into the Alpine component via mergeModules().
window.ocCore = {
  apiUrl: '', apiKey: '', connected: false, toast: '',
  page: 'tenants',
  showApiKey: false,
  saving: false,

  init() {
    this.apiUrl = localStorage.getItem('oc_api_url') || window.OC_DEFAULT_API_URL || '';
    this.apiKey = localStorage.getItem('oc_api_key') || window.OC_DEFAULT_API_KEY || '';
    // Restore the last-viewed tab so a refresh doesn't snap back to Tenants.
    // Whitelist guards against a stale/garbage value pointing at a dead tab.
    const pages = ['tenants', 'app', 'monitoring', 'backups', 'settings'];
    const saved = localStorage.getItem('oc_page');
    if (pages.includes(saved)) this.page = saved;
    this.$watch('page', v => localStorage.setItem('oc_page', v));
    if (this.apiUrl && this.apiKey) {
      this.refresh();
      // refresh() doesn't cover the tabs that lazy-load on click. When we restore
      // straight onto one of them, fire its loader so the page isn't empty.
      if (this.page === 'app') this.loadAgentCoreTools();
      if (this.page === 'backups') this.loadBackups();
    }
  },
  saveSettings() {
    localStorage.setItem('oc_api_url', this.apiUrl);
    localStorage.setItem('oc_api_key', this.apiKey);
  },
  async api(method, path, body) {
    const url = this.apiUrl.replace(/\/+$/, '') + '/' + path.replace(/^\/+/, '');
    const headers = { 'x-api-key': this.apiKey, 'Content-Type': 'application/json' };
    // 1.5.0: the API Lambda verifies a Cognito id_token's signature and reads cognito:groups for RBAC.
    // Without this header every write is downgraded to viewer and 403s.
    const token = localStorage.getItem('oc_id_token');
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const opts = { method, headers };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(url, opts);
    this.connected = r.ok;
    return r.json();
  },
  async refresh() {
    await Promise.all([this.loadHosts(), this.loadTenants(), this.loadTemplates(), this.loadSystemInfo()]);
  },
  copyCmd(cmd) {
    navigator.clipboard.writeText(cmd);
    this.toast = '✓ Copied: ' + cmd;
    setTimeout(() => this.toast = '', 2000);
  },
};
