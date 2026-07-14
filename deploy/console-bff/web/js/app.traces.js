// #221 — Trace viewer: list + waterfall + service map. Backed by BFF
// /capi/traces{,/detail,/map} → X-Ray query APIs. Deep-link "log by trace_root"
// is a placeholder URL — the AOS Discover URL is delivered by #219/#207.

window.ocTraces = {
  // ── State ─────────────────────────────────────────────────────────────────
  tracesLoading: false,
  tracesError: "", // "throttled" | "unavailable" | ""
  traceRows: [], // TraceSummaries
  traceNextToken: null,
  traceFilter: {
    minutes: 15, // relative window, converted to unix seconds on query
    tenant: "",
    errorOnly: false,
  },
  traceSelected: null, // parsed span tree of the opened trace
  traceDetailLoading: false,
  serviceMap: null, // { Services: [...] }
  serviceMapLoading: false,

  // ── List ──────────────────────────────────────────────────────────────────
  async loadTraces(nextToken) {
    if (!this.apiUrl) return;
    this.tracesLoading = true;
    this.tracesError = "";
    try {
      const now = Math.floor(Date.now() / 1000);
      const params = new URLSearchParams({
        start: String(now - Math.max(1, this.traceFilter.minutes) * 60),
        end: String(now),
      });
      if (this.traceFilter.tenant) params.set("tenant", this.traceFilter.tenant);
      if (this.traceFilter.errorOnly) params.set("error", "1");
      if (nextToken) params.set("next", nextToken);
      const r = await this._traceFetch("traces?" + params.toString());
      if (!r.ok) {
        this.tracesError = r.status === 429 ? "throttled" : "unavailable";
        return;
      }
      const body = await r.json();
      const rows = body.TraceSummaries || [];
      this.traceRows = nextToken ? this.traceRows.concat(rows) : rows;
      this.traceNextToken = body.NextToken || null;
    } catch {
      this.tracesError = "unavailable";
    } finally {
      this.tracesLoading = false;
    }
  },

  loadMoreTraces() {
    if (this.traceNextToken) this.loadTraces(this.traceNextToken);
  },

  // ── Detail ────────────────────────────────────────────────────────────────
  async openTrace(id) {
    if (!id) return;
    this.traceDetailLoading = true;
    this.traceSelected = null;
    try {
      const r = await this._traceFetch("traces/detail?ids=" + encodeURIComponent(id));
      if (!r.ok) {
        this.traceSelected = { error: r.status === 429 ? "throttled" : "unavailable" };
        return;
      }
      const body = await r.json();
      const first = (body.Traces || [])[0] || null;
      this.traceSelected = first
        ? { tree: first, unprocessed: body.UnprocessedTraceIds || [] }
        : { error: "not-found" };
    } catch {
      this.traceSelected = { error: "unavailable" };
    } finally {
      this.traceDetailLoading = false;
    }
  },

  closeTrace() {
    this.traceSelected = null;
  },

  // Waterfall: flatten the parsed tree into rows with depth + relative offset.
  // start_time on segments is a unix float in seconds. Origin = min start_time.
  flattenSpans(tree) {
    if (!tree || !Array.isArray(tree.Segments)) return { rows: [], span: 0 };
    const rows = [];
    const walk = (nodes, depth) => {
      for (const n of nodes) {
        rows.push({
          id: n.id || "",
          name: n.name || "(unnamed)",
          start: Number(n.start_time || 0),
          end: Number(n.end_time || n.start_time || 0),
          error: !!n.error || !!n.fault,
          depth,
        });
        if (Array.isArray(n.subsegments)) walk(n.subsegments, depth + 1);
      }
    };
    walk(tree.Segments, 0);
    if (rows.length === 0) return { rows: [], span: 0 };
    const origin = Math.min(...rows.map((r) => r.start));
    const finish = Math.max(...rows.map((r) => r.end));
    const span = Math.max(finish - origin, 0.001); // avoid div-by-zero
    return {
      rows: rows.map((r) => ({
        ...r,
        offsetPct: ((r.start - origin) / span) * 100,
        widthPct: Math.max(((r.end - r.start) / span) * 100, 0.5),
        durationMs: ((r.end - r.start) * 1000).toFixed(1),
      })),
      span,
    };
  },

  // ── Service map ───────────────────────────────────────────────────────────
  async loadServiceMap() {
    if (!this.apiUrl) return;
    this.serviceMapLoading = true;
    try {
      const now = Math.floor(Date.now() / 1000);
      const params = new URLSearchParams({
        start: String(now - Math.max(1, this.traceFilter.minutes) * 60),
        end: String(now),
      });
      const r = await this._traceFetch("traces/map?" + params.toString());
      if (!r.ok) {
        this.serviceMap = { error: r.status === 429 ? "throttled" : "unavailable" };
        return;
      }
      this.serviceMap = await r.json();
    } catch {
      this.serviceMap = { error: "unavailable" };
    } finally {
      this.serviceMapLoading = false;
    }
  },

  // ── Log deep link (R6.6) ──────────────────────────────────────────────────
  // Placeholder — AOS Discover URL structure is delivered by #219/#207. Until
  // then this points at the ClawPool internal wiki entry that documents the
  // trace_root search recipe. Swapping to the real URL is a one-line change.
  logDeepLink(traceRoot) {
    if (!traceRoot) return "#";
    // #219/#207 will replace this with the AOS Discover URL.
    return "/console/logs?trace_root=" + encodeURIComponent(traceRoot) + "#todo-219";
  },

  // ── HTTP helper (shares apiKey with ocCore) ───────────────────────────────
  async _traceFetch(path) {
    const url = this.apiUrl.replace(/\/+$/, "") + "/" + path;
    const headers = { "x-api-key": this.apiKey };
    const token = localStorage.getItem("oc_id_token");
    if (token) headers["Authorization"] = "Bearer " + token;
    return fetch(url, { method: "GET", headers });
  },
};
