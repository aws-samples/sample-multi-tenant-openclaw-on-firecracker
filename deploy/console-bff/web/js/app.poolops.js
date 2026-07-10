// Pool Ops: fleet-wide scheduling/lifecycle actions in one place.
// Every action hits a REAL control-plane endpoint (no mocks):
//   - batch lifecycle  → POST /batch/tenants {action, filter|ids}  (start/stop/delete/backup)
//   - rolling rebake    → POST /hosts/refresh-rootfs               (pull latest rootfs to hosts)
//   - capacity reconcile→ POST /tenants/{id} reads + health sweep   (surface stuck/creating)
// Merged into the Alpine root via mergeModules() (see app.js). Uses this.api()
// from app.core.js (carries Cognito Bearer + x-api-key + RBAC role).
window.ocPoolOps = {
  poBusy: false,
  poStatus: "",
  poAction: "stop", // stop | start | delete | backup
  poFilterTag: "", // optional "key:value" — empty = all non-deleted
  poResult: null, // {requested, accepted, job_id, mode}
  poHostsBusy: false,
  poHostsStatus: "",

  // ---- Batch lifecycle across the fleet (by tag or all) ----
  async poRunBatch() {
    if (this.poBusy) return;
    const action = this.poAction;
    if (!["stop", "start", "delete", "backup"].includes(action)) {
      this.poStatus = "无效动作";
      return;
    }
    const tag = (this.poFilterTag || "").trim();
    const human = {
      stop: "停止",
      start: "启动",
      delete: "删除",
      backup: "备份",
    }[action];
    const scope = tag ? `tag=${tag}` : "全部租户";
    if (
      !confirm(
        `确认对 [${scope}] 批量${human}?\n` +
          (action === "delete"
            ? "删除不可逆(会先备份,除非租户无数据)。"
            : "此操作影响多个 openclaw 节点。"),
      )
    )
      return;
    this.poBusy = true;
    this.poStatus = `批量${human}中 (${scope})…`;
    this.poResult = null;
    try {
      // Backend takes exactly one of {ids, filter}. With a tag → filter.
      // Without a tag → resolve the full id list first (RBAC-scoped: admin sees
      // all, non-admin only their own), then pass ids. async:true routes large
      // batches to the self-invoked worker (202 + job_id) past the 30s API-GW cap.
      const body = { action, async: true };
      if (tag) {
        body.filter = { tag };
      } else {
        const all = await this.api("GET", "tenants?limit=1000").catch(() => []);
        const items = Array.isArray(all) ? all : all.tenants || [];
        body.ids = items
          .filter((t) => t.status && t.status !== "deleted")
          .map((t) => t.id);
        if (body.ids.length === 0) {
          this.poStatus = "没有可操作的租户";
          this.poBusy = false;
          return;
        }
      }
      const r = await this.api("POST", "batch/tenants", body);
      // async job path returns {job_id, accepted, ...}; sync returns {results:[...]}
      const accepted =
        r.accepted != null
          ? r.accepted
          : Array.isArray(r.results)
            ? r.results.length
            : "?";
      this.poResult = {
        action: human,
        scope,
        accepted,
        job_id: r.job_id || null,
        mode: r.job_id ? "async(轮询 job)" : "sync",
      };
      this.poStatus = `已提交批量${human}: 受理 ${accepted} 个${r.job_id ? " · job=" + r.job_id : ""}`;
      // refresh tenant list if the merged module exposes it
      if (typeof this.loadTenants === "function")
        setTimeout(() => this.loadTenants(), 1500);
    } catch (e) {
      this.poStatus = `批量${human}失败: ${e.message || e}`;
    } finally {
      this.poBusy = false;
    }
  },

  // ---- Poll an async batch job ----
  async poPollJob() {
    const id = this.poResult && this.poResult.job_id;
    if (!id) {
      this.poStatus = "无 job_id 可轮询(同步批量已完成)";
      return;
    }
    try {
      const j = await this.api("GET", "batch/jobs/" + id);
      this.poStatus = `job ${id}: ${j.status || "?"} — 完成 ${j.done || 0}/${j.total || "?"}${j.failed ? " 失败 " + j.failed : ""}`;
    } catch (e) {
      this.poStatus = `轮询失败: ${e.message || e}`;
    }
  },

  // ---- Rolling rebake: pull latest golden rootfs/data to all hosts ----
  async poRefreshRootfs() {
    if (this.poHostsBusy) return;
    if (
      !confirm(
        "确认拉取最新黄金镜像(rootfs+data template)到所有 host?\n" +
          "新建/重建的节点将继承最新镜像(已运行节点不受影响,需滚动重建才换镜像)。",
      )
    )
      return;
    this.poHostsBusy = true;
    this.poHostsStatus = "下发拉取最新镜像到所有 host…";
    try {
      const r = await this.api("POST", "hosts/refresh-rootfs", {});
      this.poHostsStatus = `已下发: ${r.message || JSON.stringify(r).slice(0, 160)}`;
    } catch (e) {
      this.poHostsStatus = `失败: ${e.message || e}`;
    } finally {
      this.poHostsBusy = false;
    }
  },

  // ---- Capacity reconcile snapshot: surface ledger vs reality ----
  // Reads hosts + tenants and shows stuck-creating count (the health_check
  // reaper releases their slots automatically; this just surfaces the state).
  async poReconcileView() {
    this.poHostsStatus = "对账中(读 hosts + 统计 creating)…";
    try {
      const hosts = await this.api("GET", "hosts").catch(() => []);
      const all = await this.api("GET", "tenants?limit=1000").catch(() => []);
      const items = Array.isArray(all) ? all : all.tenants || [];
      const creating = items.filter((t) => t.status === "creating").length;
      const running = items.filter((t) => t.status === "running").length;
      const hostList = Array.isArray(hosts) ? hosts : hosts.hosts || [];
      const ledgerVm = hostList.reduce(
        (s, h) => s + (parseInt(h.vm_count) || 0),
        0,
      );
      this.poHostsStatus =
        `账面 vm_count=${ledgerVm} · running=${running} · creating=${creating}` +
        (creating > 0
          ? ` (creating 超 15min 的由 health_check reaper 自动回收容量)`
          : " (无僵尸)");
    } catch (e) {
      this.poHostsStatus = `对账失败: ${e.message || e}`;
    }
  },
};
