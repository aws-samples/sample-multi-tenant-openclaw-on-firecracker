// Formatting + RBAC helpers (mostly pure; no own state beyond JWT inspection).
window.ocFormat = {
  formatTs(ts) {
    if (!ts) return '-';
    // API returns ISO 8601 like "2026-04-27T19:00:39Z"
    const d = new Date(ts);
    return isNaN(d) ? ts : d.toLocaleString();
  },
  formatBytes(n) {
    if (!n) return '-';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + units[i];
  },
  healthClass(v, status) {
    // Only running tenants have meaningful live health; others show grey.
    if (status && status !== 'running') return 'health-unknown';
    return v === 'up' ? 'health-up' : v === 'down' ? 'health-down' : 'health-unknown';
  },
  // 1.2.9 — host-agent now samples cpu_pct from /proc/<fc_pid>/stat
  // and memory_used_mb from VmRSS (the previous balloon-stats path was
  // unreliable: available_memory often returned 0). All three metrics
  // are now meaningful — DDB returns Number attributes as strings so
  // we coerce here.
  _n(v) { return Number(v) || 0; },
  metricLevel(pct) {
    return pct >= 90 ? 'metric-crit' : pct >= 70 ? 'metric-warn' : 'metric-ok';
  },
  diskText(t) {
    const m = t.metrics;
    if (!m) return '';
    const fmt = mb => mb >= 1024 ? (mb/1024).toFixed(1)+'G' : mb+'M';
    const used = this._n(m.disk_used_mb);
    const total = this._n(m.disk_total_mb);
    const pct = this._n(m.disk_used_pct);
    return `${fmt(used)}/${fmt(total)} (${pct}%)`;
  },
  cpuText(t) {
    // "12% / 2 vCPU" — pct is of the allocated vcpu count; show the
    // raw vcpu allocation for context so 50% on 1 vcpu vs 2 vcpu reads right.
    const pct = this._n(t.metrics?.cpu_pct);
    return `${pct}% / ${t.vcpu} vCPU`;
  },
  memText(t) {
    // "1.2G/4G (30%)" — used is from VmRSS; total is the configured mem_mb.
    const used = this._n(t.metrics?.memory_used_mb);
    const total = this._n(t.mem_mb);
    const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
    const fmt = mb => mb >= 1024 ? (mb / 1024).toFixed(1) + 'G' : mb + 'M';
    return `${fmt(used)}/${fmt(total)} (${pct}%)`;
  },
  memPctOfTenant(t) {
    const used = this._n(t.metrics?.memory_used_mb);
    const total = this._n(t.mem_mb);
    return total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  },
  role() {
    // #217 — BFF 架构:真实角色由 BFF 从 ALB x-amzn-oidc-data 解出,经 config.js 注入
    // window.OC_ROLE(前端拿不到那个 header)。优先读它——BFF 登录门下 localStorage 无
    // oc_id_token,下面老 JWT 分支会误判成 admin,导致 viewer 也看到写操作入口。
    if (window.OC_ROLE) return window.OC_ROLE;
    // RBAC (issue #14): inspect the JWT id_token, return the most-privileged
    // group name. No JWT (e.g. when console_auth.enabled: false) → admin.
    var token = localStorage.getItem('oc_id_token');
    if (!token) return 'admin';
    try {
      var p = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
      var groups = p['cognito:groups'] || [];
      if (typeof groups === 'string') groups = [groups];
      var rank = { viewer: 0, operator: 1, admin: 2 };
      var best = null, bestRank = -1;
      groups.forEach(function(g) {
        if (rank[g] !== undefined && rank[g] > bestRank) { best = g; bestRank = rank[g]; }
      });
      return best || 'viewer';
    } catch (e) { return 'viewer'; }
  },
  canWrite() { return this.role() !== 'viewer'; },
  // #394 — promote/rollback/discard canary are admin-only (backend x-rbac: admin).
  // Note: the API-key path resolves is_admin server-side regardless; this only gates
  // UI visibility so non-admins don't see buttons that would 403.
  isAdmin() { return this.role() === 'admin'; },
};
