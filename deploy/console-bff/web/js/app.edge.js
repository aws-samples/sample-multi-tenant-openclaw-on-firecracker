// P4-③ (#187) — Edge admin tab: OpenResty ASG instances + /metrics stub.
// Data comes from GET /admin/edge/instances + GET /admin/edge/metrics
// (operator+); no polling — loads on tab click + manual refresh button.
window.ocEdge = {
  edgeInstances: null, // {asg_name, asg, instances:[], generated_at, notes}
  edgeMetrics: null,   // {generated_at, metrics_source, instances, notes}
  edgeError: "",
  edgeLoading: false,

  async loadEdgeInstances() {
    if (!this.apiUrl || !this.apiKey) return;
    this.edgeLoading = true;
    this.edgeError = "";
    try {
      const r = await this.api("GET", "admin/edge/instances");
      if (r && r.error) {
        this.edgeInstances = null;
        this.edgeError = r.error;
      } else {
        this.edgeInstances = r;
      }
    } catch (e) {
      this.edgeInstances = null;
      this.edgeError = String(e);
    } finally {
      this.edgeLoading = false;
    }
  },

  async loadEdgeMetrics() {
    if (!this.apiUrl || !this.apiKey) return;
    try {
      const r = await this.api("GET", "admin/edge/metrics");
      if (r && r.error) {
        this.edgeMetrics = null;
      } else {
        this.edgeMetrics = r;
      }
    } catch {
      this.edgeMetrics = null;
    }
  },

  async refreshEdge() {
    await this.loadEdgeInstances();
    await this.loadEdgeMetrics();
  },
};
