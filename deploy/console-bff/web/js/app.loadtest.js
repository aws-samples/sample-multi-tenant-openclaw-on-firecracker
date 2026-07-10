// Load Test: 并发触发 N 个 POST /tenants,真实测控制面注册 API 的 p50/p99
// 和 creating→running 可用时延。纯前端 + 现有 API,节点名前缀 lt- 便于清理。
// 不 mock:每次都是真 POST、真起 microVM、真轮询 DDB 状态。
window.ocLoadtest = {
  ltN: 30,
  ltVcpu: 1,
  ltMem: 2048,
  ltRunning: false,
  ltStatus: "",
  ltLog: "",
  ltResult: {
    done: false,
    total: 0,
    ok: 0,
    fail: 0,
    p50: 0,
    p99: 0,
    max: 0,
    running: 0,
    runningSec: 0,
  },

  _pct(sorted, p) {
    if (!sorted.length) return 0;
    const i = Math.min(sorted.length - 1, Math.floor(sorted.length * p));
    return Number(sorted[i].toFixed(3));
  },

  async runLoadTest() {
    if (this.ltRunning) return;
    if (!this.apiUrl || !this.apiKey) {
      this.ltStatus = "请先在 Settings 配置 API URL + key";
      return;
    }
    const N = Math.max(1, Math.min(500, this.ltN | 0));
    this.ltRunning = true;
    this.ltLog = "";
    this.ltResult = {
      done: false,
      total: N,
      ok: 0,
      fail: 0,
      p50: 0,
      p99: 0,
      max: 0,
      running: 0,
      runningSec: 0,
    };
    this.ltStatus = `并发 POST ${N} 个 /tenants…`;
    const base = this.apiUrl.replace(/\/+$/, "");
    const headers = {
      "x-api-key": this.apiKey,
      "Content-Type": "application/json",
    };
    const tok = localStorage.getItem("oc_id_token");
    if (tok) headers["Authorization"] = "Bearer " + tok;

    const t0 = performance.now();
    const ids = [];
    const postTimes = [];
    // 并发触发全部 POST(真并发,Promise.all)
    const results = await Promise.all(
      Array.from({ length: N }, (_, i) => {
        const s = performance.now();
        return fetch(base + "/tenants", {
          method: "POST",
          headers,
          body: JSON.stringify({
            name: "lt-" + Date.now() + "-" + i,
            vcpu: this.ltVcpu,
            mem_mb: this.ltMem,
          }),
        })
          .then(async (r) => {
            const dt = (performance.now() - s) / 1000;
            postTimes.push(dt);
            const j = await r.json().catch(() => ({}));
            return {
              ok: r.ok,
              id: j.id,
              err: j.error || (r.ok ? "" : "HTTP " + r.status),
            };
          })
          .catch((e) => {
            postTimes.push((performance.now() - s) / 1000);
            return { ok: false, err: String(e) };
          });
      }),
    );
    const postSec = ((performance.now() - t0) / 1000).toFixed(2);
    const fails = [];
    for (const r of results) {
      if (r.ok && r.id) ids.push(r.id);
      else fails.push(r.err);
    }
    const sorted = postTimes.slice().sort((a, b) => a - b);
    this.ltResult.ok = ids.length;
    this.ltResult.fail = fails.length;
    this.ltResult.p50 = this._pct(sorted, 0.5);
    this.ltResult.p99 = this._pct(sorted, 0.99);
    this.ltResult.max = Number(Math.max(...postTimes, 0).toFixed(3));
    this.ltLog =
      `POST 全返回 ${postSec}s · 成功 ${ids.length} · 失败 ${fails.length}` +
      (fails.length ? "\n失败样本: " + fails.slice(0, 5).join(" | ") : "");
    this.ltStatus = `POST 完成,轮询 ${ids.length} 个节点 creating→running…`;

    // 轮询直到全部 running(或 ~3min 上限),测端到端可用时延
    let running = 0;
    for (let poll = 0; poll < 60 && running < ids.length; poll++) {
      await new Promise((s) => setTimeout(s, 3000));
      const states = await Promise.all(
        ids.map((id) =>
          fetch(base + "/tenants/" + id, { headers })
            .then((r) => r.json())
            .then((j) => j.status)
            .catch(() => "?"),
        ),
      );
      running = states.filter((s) => s === "running").length;
      const sec = Math.round((performance.now() - t0) / 1000);
      this.ltResult.running = running;
      this.ltStatus = `t=${sec}s: running ${running}/${ids.length}`;
    }
    this.ltResult.runningSec = Math.round((performance.now() - t0) / 1000);
    this.ltResult.done = true;
    this.ltRunning = false;
    this.ltStatus = `完成:${this.ltResult.ok}/${N} 创建,${running}/${ids.length} running,用时 ${this.ltResult.runningSec}s`;
  },

  async cleanupLoadTest() {
    if (this.ltRunning) return;
    this.ltStatus = "清理 lt- 节点中…";
    const all = await this.api("GET", "tenants").catch(() => []);
    const lt = (Array.isArray(all) ? all : []).filter(
      (t) => (t.id || "").startsWith("lt-") || (t.name || "").startsWith("lt-"),
    );
    let n = 0;
    for (const t of lt) {
      try {
        await this.api(
          "DELETE",
          "tenants/" + t.id + "?keep_data=false&skip_backup=true",
        );
        n++;
      } catch (_) {}
    }
    this.ltStatus = `已清理 ${n} 个 lt- 节点`;
  },
};
