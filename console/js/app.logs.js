// #266 — Per-tenant log viewer. Core use case: a tenant goes unhealthy between
// create and running → pull its firecracker start logs (source=vm, the fc.log
// AOS index) plus launch diagnostics (source=host) and control-plane Lambda
// trail (source=lambda, CloudWatch Insights). Backed by BFF /capi/logs.
//
// name→id: reuses this.tenants (already loaded by ocTenants). Operators type or
// pick a tenant name; we resolve to tenant_id before querying (VM/host logs are
// only keyed by tenant_id — name never reaches the host layer).

window.ocLogs = {
  logsLoading: false,
  logsError: "", // "" | "unavailable" | "timeout"
  logRows: [],
  logApproximate: false,
  logFilter: {
    tenant: "", // name OR id — resolved to id at query time
    source: "vm", // vm | host | lambda
    minutes: 60,
  },

  // Resolve a typed value to a tenant_id: exact id match wins, else name match.
  resolveTenantId(input) {
    const v = (input || "").trim();
    if (!v) return "";
    const list = this.tenants || [];
    const byId = list.find((t) => (t.id || t.tenant_id) === v);
    if (byId) return byId.id || byId.tenant_id;
    const byName = list.find((t) => t.name === v);
    if (byName) return byName.id || byName.tenant_id;
    return v; // fall back to treating the input as an id (BFF re-validates shape)
  },

  async loadLogs() {
    if (!this.apiUrl) return;
    const tenantId = this.resolveTenantId(this.logFilter.tenant);
    if (!tenantId) {
      this.logsError = "unavailable";
      this.logRows = [];
      this.toast = "✗ 先选或填一个租户 name / id";
      setTimeout(() => (this.toast = ""), 3000);
      return;
    }
    this.logsLoading = true;
    this.logsError = "";
    this.logApproximate = false;
    try {
      const now = Math.floor(Date.now() / 1000);
      const params = new URLSearchParams({
        tenant: tenantId,
        source: this.logFilter.source,
        start: String(now - Math.max(1, this.logFilter.minutes) * 60),
        end: String(now),
      });
      const r = await this._logFetch("logs?" + params.toString());
      if (!r.ok) {
        this.logsError = r.status === 504 ? "timeout" : "unavailable";
        this.logRows = [];
        return;
      }
      const body = await r.json();
      this.logRows = body.rows || [];
      this.logApproximate = !!body.approximate;
    } catch {
      this.logsError = "unavailable";
      this.logRows = [];
    } finally {
      this.logsLoading = false;
    }
  },

  // Shares apiKey/id_token with ocCore/ocTraces.
  async _logFetch(path) {
    const url = this.apiUrl.replace(/\/+$/, "") + "/" + path;
    const headers = { "x-api-key": this.apiKey };
    const token = localStorage.getItem("oc_id_token");
    if (token) headers["Authorization"] = "Bearer " + token;
    return fetch(url, { method: "GET", headers });
  },
};
