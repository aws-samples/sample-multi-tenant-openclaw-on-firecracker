// #236 — Audit & Ops tab: surfaces swagger endpoints that had no console UI.
//   GET /audit-log                     → recent audit trail (items[], newest first)
//   GET /images                        → golden rootfs version manifest + live pointer
//   GET /users/{uid}/tenants|summary   → per-user fleet listing + status rollup
//   POST /users/{uid}/action {action}  → bulk start/stop of every node a user owns
// All read-only except the user bulk action (admin/operator, confirm-gated).
window.ocAudit = {
  auditItems: null, // audit-log rows (array)
  auditError: "",
  auditLoading: false,
  images: null, // /images response
  imagesError: "",
  userQuery: "", // tenant_user_id to look up
  userTenants: null, // {tenants:[], count, next_token}
  userSummary: null, // {total, by_status, truncated}
  userError: "",
  userLoading: false,

  async loadAudit() {
    if (!this.apiUrl || !this.apiKey) return;
    this.auditLoading = true;
    this.auditError = "";
    try {
      const r = await this.api("GET", "audit-log?limit=100");
      // /audit-log returns a bare array (items[:limit]); tolerate {error}.
      if (r && r.error) {
        this.auditItems = null;
        this.auditError = r.error;
      } else {
        this.auditItems = Array.isArray(r) ? r : r.items || [];
      }
    } catch (e) {
      this.auditItems = null;
      this.auditError = String(e);
    } finally {
      this.auditLoading = false;
    }
  },

  async loadImages() {
    if (!this.apiUrl || !this.apiKey) return;
    this.imagesError = "";
    try {
      const r = await this.api("GET", "images");
      if (r && r.error) {
        this.images = null;
        this.imagesError = r.error;
      } else {
        this.images = r;
      }
    } catch (e) {
      this.images = null;
      this.imagesError = String(e);
    }
  },

  async lookupUser() {
    const uid = (this.userQuery || "").trim();
    if (!uid || !this.apiUrl || !this.apiKey) return;
    this.userLoading = true;
    this.userError = "";
    this.userTenants = null;
    this.userSummary = null;
    try {
      const enc = encodeURIComponent(uid);
      const [t, s] = await Promise.all([
        this.api("GET", `users/${enc}/tenants`),
        this.api("GET", `users/${enc}/summary`),
      ]);
      if (t && t.error) this.userError = t.error;
      else this.userTenants = t;
      if (!(s && s.error)) this.userSummary = s;
    } catch (e) {
      this.userError = String(e);
    } finally {
      this.userLoading = false;
    }
  },

  async userBulkAction(action) {
    const uid = (this.userQuery || "").trim();
    if (!uid) return;
    if (
      !confirm(
        `Apply "${action}" to EVERY node owned by user "${uid}"? This is a bulk lifecycle action.`,
      )
    )
      return;
    try {
      const enc = encodeURIComponent(uid);
      const r = await this.api("POST", `users/${enc}/action`, { action });
      if (r && r.error) {
        alert("Bulk action failed: " + r.error);
      } else {
        alert(
          `Bulk ${action} dispatched` +
            (r && r.succeeded ? ` (${r.succeeded.length} node(s))` : ""),
        );
        this.lookupUser();
      }
    } catch (e) {
      alert("Bulk action error: " + String(e));
    }
  },
};
