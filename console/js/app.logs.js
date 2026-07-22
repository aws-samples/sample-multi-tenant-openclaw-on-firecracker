// Logs / Activity: operations audit trail (issue #71). Reverse-chronological
// table over GET /audit-log, with client-side object/operation/actor filters
// and cursor pagination (the `before` query param pages older). Read-only —
// available to viewer+ (GET /audit-log is in the API's _VIEWER_OK set).
window.ocLogs = {
  auditEntries: [],
  loadingAudit: false,
  logFilterObject: 'all',   // all | tenant | host | group | skill | template
  logFilterText: '',        // free-text match on operation/actor
  logsExhausted: false,     // true when the last page returned < pageSize
  _logPageSize: 50,

  async loadAuditLog() {
    if (!this.apiUrl || !this.apiKey) return;
    // Fresh load: reset pagination state so re-visiting the tab doesn't append.
    this.auditEntries = [];
    this.logsExhausted = false;
    this.loadingAudit = true;
    try {
      const rows = await this.api('GET', 'audit-log?limit=' + this._logPageSize);
      this.auditEntries = Array.isArray(rows) ? rows : [];
      this.logsExhausted = this.auditEntries.length < this._logPageSize;
      this.connected = true;
    } catch (e) {
      // Degrade gracefully: a deployment with no audit table returns [] (or the
      // fetch fails). Either way the tab shows an informative empty state.
      this.auditEntries = [];
      this.logsExhausted = true;
      this.connected = false;
    }
    this.loadingAudit = false;
  },

  async loadMoreAuditLog() {
    if (this.logsExhausted || this.loadingAudit || !this.auditEntries.length) return;
    this.loadingAudit = true;
    // Cursor = oldest ts already loaded; server returns entries strictly older.
    const oldest = this.auditEntries[this.auditEntries.length - 1].ts;
    try {
      const rows = await this.api(
        'GET', 'audit-log?limit=' + this._logPageSize + '&before=' + encodeURIComponent(oldest));
      const fresh = (Array.isArray(rows) ? rows : []).filter(r => r.ts < oldest);
      this.auditEntries = this.auditEntries.concat(fresh);
      this.logsExhausted = fresh.length < this._logPageSize;
      this.connected = true;
    } catch (e) {
      this.logsExhausted = true;
    }
    this.loadingAudit = false;
  },

  // Human-friendly operation label: prefer the enriched `event` (tenant.created),
  // fall back to the legacy "METHOD /resource" operation string.
  logOperation(row) {
    return row.event || row.operation || '-';
  },
  // Typed object ref: prefer enriched `object` (tenant:<id>), else resource_id.
  logObject(row) {
    return row.object || row.resource_id || '-';
  },
  logObjectType(row) {
    const o = row.object || '';
    const i = o.indexOf(':');
    return i > 0 ? o.slice(0, i) : (o || '');
  },
  logActor(row) {
    const a = row.actor || row.api_key_id || '-';
    return row.actor_role ? a + ' (' + row.actor_role + ')' : a;
  },
  logResult(row) {
    // response_status may come back as a number or (from DynamoDB via json.dumps
    // default=str) a string; normalize for display.
    const s = row.response_status;
    return (s === null || s === undefined || s === '') ? '-' : String(s);
  },
  logResultClass(row) {
    const s = Number(row.response_status);
    if (!s) return '';
    if (s >= 500) return 'badge-inactive';
    if (s >= 400) return 'badge-warn';
    return 'badge-active';
  },

  get filteredAuditEntries() {
    let rows = this.auditEntries;
    if (this.logFilterObject !== 'all') {
      rows = rows.filter(r => this.logObjectType(r) === this.logFilterObject);
    }
    const q = (this.logFilterText || '').trim().toLowerCase();
    if (q) {
      rows = rows.filter(r =>
        this.logOperation(r).toLowerCase().includes(q) ||
        this.logActor(r).toLowerCase().includes(q) ||
        this.logObject(r).toLowerCase().includes(q));
    }
    return rows;
  },
};
