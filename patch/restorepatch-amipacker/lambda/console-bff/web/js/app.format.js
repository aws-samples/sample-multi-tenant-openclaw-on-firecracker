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
  // 为什么必须单独显示:Status 列是租户生命周期(running/stopped/…),而 rebuild 期间它恒为
  // running —— 运维从那一列看不出"这台正在换版"还是"换完了"还是"没换成"。这三个 helper 把
  // rebuild_phase(到哪一步)与 rebuild_status(终态结论)渲染出来,否则这两个字段只存在于
  // API 响应里,控制台仍是全黑。
  _REBUILD_INFLIGHT: { queued: '已排队', running: '执行中', verifying: '校验中' },
  rebuildLabel(t) {
    const st = t.rebuild_status || '';
    const ph = t.rebuild_phase || '';
    // 终态优先:它是结论,而 phase 在终态时只是同值回显。
    if (st === 'done') return 'rebuild ✓';
    if (st === 'failed') return 'rebuild ✗ 失败';
    // unconfirmed 刻意不写"失败":它的含义是"没能确认",真机很可能已经升级成功。
    // 写成失败会引导运维重试,而重试会再丢一次 overlay、抹掉两次之间的写入。
    if (st === 'unconfirmed') return 'rebuild ? 未确认';
    const zh = this._REBUILD_INFLIGHT[ph];
    return zh ? 'rebuild ' + zh + '…' : (ph ? 'rebuild ' + ph : '');
  },
  rebuildStyle(t) {
    const st = t.rebuild_status || '';
    const base = 'font-weight:600;font-size:10px;';
    // 只用 style.css 里真实存在的变量(--green/--red/--cyan);--orange / --yellow 在本
    // 控制台【没有定义】(AWS 主题的强调色是 --cyan: #ff9900),写它们会静默回落成继承色。
    if (st === 'done') return base + 'color:var(--green)';
    if (st === 'failed') return base + 'color:var(--red)';
    // 琥珀 = 不知道。与红(确认失败)刻意区分开,提醒"别急着重试"。字面量而非变量:
    // 这个语义色本主题没有对应变量,不为一处用色去改全局调色板。
    if (st === 'unconfirmed') return base + 'color:#d7a21a';
    // in-flight:用 AWS 橙(--cyan)+ 闪烁,一眼看出"正在进行"。动画名 rebuild-blink 见
    // style.css;若样式表未加载,动画失效但颜色仍在,不影响可读性。
    return base + 'color:var(--cyan);animation:rebuild-blink 1.6s ease-in-out infinite';
  },
  rebuildHint(t) {
    const st = t.rebuild_status || '';
    const parts = [];
    if (t.rebuild_phase) parts.push('阶段: ' + t.rebuild_phase);
    if (st) parts.push('结论: ' + st);
    if (st === 'unconfirmed') {
      parts.push(
        '⚠ 未确认 ≠ 失败。真机可能已升级成功,只是回执没回来。' +
        '请勿重试(重试会再删一次 overlay、抹掉两次之间的写入),等自动对账收敛。'
      );
    }
    if (t.rebuild_target_snapshot_time) parts.push('目标版本: ' + t.rebuild_target_snapshot_time);
    if (t.observed_image_snapshot_time) parts.push('宿主机上报实际版本: ' + t.observed_image_snapshot_time);
    if (t.rebuild_failed_reason) parts.push('原因: ' + t.rebuild_failed_reason);
    if (t.rebuild_op_id) parts.push('操作 id: ' + t.rebuild_op_id);
    if (t.rebuild_started_at) parts.push('发起于: ' + t.rebuild_started_at);
    return parts.join('\n');
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
    // window.OC_ROLE(前端拿不到那个 header)。优先读它——BFF 登录门下 localStorage 无
    // oc_id_token,下面老 JWT 分支会误判成 admin,导致 viewer 也看到写操作入口。
    if (window.OC_ROLE) return window.OC_ROLE;
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
  // Note: the API-key path resolves is_admin server-side regardless; this only gates
  // UI visibility so non-admins don't see buttons that would 403.
  isAdmin() { return this.role() === 'admin'; },
};
