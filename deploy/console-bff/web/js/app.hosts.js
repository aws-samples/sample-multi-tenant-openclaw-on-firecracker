// Hosts: load, AZ grouping, capacity bars, rootfs refresh, host pickers.
window.ocHosts = {
  hosts: [], rootfsVersion: '',
  loadingHosts: false,
  agentcoreStatus: { enabled: false }, agentcoreRegion: 'ap-northeast-1',
  // #217 V2 — 顶部版本选择:下拉列所有快照,选一个可拉到全部 host(Pull all)。
  snapshots: [], selectedSnapshot: '',
  // #217 — 每台 host 各自选的快照(instance_id→snapshot_time);未选默认最新(hostSnapFor)。
  hostSnapChoice: {},
  // #309 — copy-file modal (replaces native prompt so the EC2 target path is clear).
  // copy-file updates host scripts, so the target must be under /opt/openclaw/ (.py
  // host-agent service) or /home/ubuntu/ (.sh + lib/*) — the two roots init-host.sh
  // installs deployment/scripts/ to. Image disks (/data/firecracker-assets/) are off-limits.
  copyModal: { show: false, host_id: '', s3_uri: 's3://', target: '/opt/openclaw/', busy: false, error: '' },
  // #309 — latest pull-image progress line per host, auto-polled while upgrading.
  pullProgress: {}, _progressInFlight: {},
  // #394 — per-host canary snapshot choice (instance_id→snapshot_time), independent of
  // the live-pull choice (hostSnapChoice). Empty → default latest (canarySnapFor).
  canarySnapChoice: {},
  // #394 — in-flight canary pull job per host (instance_id→job_id). Canary pull doesn't
  // set host.status=upgrading, so this is how the poll loop knows to show its progress.
  canaryJob: {},

  async loadHosts() {
    if (!this.apiUrl || !this.apiKey) return;
    this.loadingHosts = true;
    try { this.hosts = await this.api('GET', 'hosts'); this.connected = true; }
    catch { this.connected = false; }
    this.loadingHosts = false;
    try { this.rootfsVersion = (await this.api('GET', 'hosts/rootfs-version')).version; } catch {}
    try { this.agentcoreStatus = await this.api('GET', 'agentcore/status'); } catch {}
    try { this.snapshots = await this.api('GET', 'list_image_versions?show_deleted=true'); } catch {}  // #337(原#217 /snapshots)顶部下拉
    // Load skills + groups (v1.4.0/1.4.1)
    this.loadSkills();
    this.loadGroups();
  },
  // #217 V2 — 选中的快照拉到【全部 host】(fleet-wide)。逐台串行调 pull-image?snapshot_time=,
  // 一台失败不中断其余(汇总成败)。#249 — 跳过正在 upgrading 的 host(后端 status CAS 也会
  // 拒它 409,前端先跳过=不产生无谓 fail、汇总里单列 skipped 有解释)。
  async pullSnapshotToAll() {
    const st = this.selectedSnapshot;
    if (!st) { alert('Choose a snapshot version first (top dropdown).'); return; }
    const targets = this.hosts.filter(h => h.status !== 'upgrading');
    const skipped = this.hosts.length - targets.length;
    if (!targets.length) { alert('All hosts are upgrading; nothing to pull.'); return; }
    const skipNote = skipped ? ` (skip ${skipped} upgrading)` : '';
    if (!confirm(`Pull snapshot ${st} → ${targets.length} host(s)${skipNote}?\n(installs to live per host; each host takes no new tenants while upgrading)`)) return;
    let ok = 0, fail = 0;
    for (const h of targets) {
      this.toast = `pull ${st} → ${h.instance_id}…`;
      try { await this.api('POST', `hosts/${h.instance_id}/pull-image?snapshot_time=${encodeURIComponent(st)}`); ok++; }
      catch { fail++; }
    }
    this.toast = `✓ pull started: ${ok} ok${fail ? ', ' + fail + ' failed' : ''}${skipped ? ', ' + skipped + ' skipped (upgrading)' : ''}`;
    setTimeout(() => { this.toast = ''; this.loadHosts(); }, 4000);
  },
  // #376 — 打一版镜像快照(POST /create-image-snapshot):扫 deployment/ 全量对象写快照表。
  // 零输入:bucket 由后端 Lambda 从 ASSETS_BUCKET env 自读,label 留空后端自动取
  // deployment/rootfs/manifest.json 的 version。点一下即打,打完刷新 snapshots 下拉,新快照
  // 置顶(供随后 Pull 选)。operator+ 才可见(按钮在 canWrite 块里)。
  async takeSnapshot() {
    if (!confirm('Take a version snapshot of the assets bucket now?\n(label auto-filled from the current rootfs version)')) return;
    this.toast = 'taking snapshot…';
    try {
      const r = await this.api('POST', 'create-image-snapshot', {});
      this.toast = `✓ snapshot ${r.snapshot_time}${r.label ? ' (' + r.label + ')' : ''} · ${r.file_count} files`;
      try { this.snapshots = await this.api('GET', 'list_image_versions?show_deleted=true'); } catch {}
    } catch (e) {
      this.toast = '✗ snapshot failed: ' + (e && e.message ? e.message : 'error');
    }
    setTimeout(() => { this.toast = ''; }, 4000);
  },
  // #394 — 可拉取的快照(过滤软删):Image Snapshot 面板用 this.snapshots 看全量(含
  // deleted,带标记,这样被 host 槽位引用却被误删的版本仍显示徽标);pull 下拉只列可拉的,
  // 绝不让运维选一个已软删版本去 pull(后端也会拒)。
  get pullableSnapshots() {
    return (this.snapshots || []).filter(s => s.status !== 'deleted');
  },
  // #394 — 某快照被哪些 host 槽位引用(live/canary/previous_live),从已加载的 this.hosts
  // 本地推导(不额外打 API)。用于表格"使用中"列 + 禁用 Delete(引用中删会 409)。
  // 返回人读串(如 "i-abc:live")或空串(未被引用)。
  snapshotUsage(snapshot_time) {
    const roleShort = { live: 'live', canary: 'canary', previous_live: 'prev' };
    const hits = [];
    for (const h of (this.hosts || [])) {
      const sl = h.image_slots || {};
      for (const key of ['live', 'canary', 'previous_live']) {
        // 紧凑串:host id 末 5 位 + 角色短名(完整 id 太长,pill 里放不下也没必要)。
        if (sl[key] === snapshot_time) hits.push(`…${h.instance_id.slice(-5)} ${roleShort[key]}`);
      }
    }
    return hits.join(', ');
  },
  // 明确的布尔:是否被 host 槽位引用(供 :disabled 用,别直接把字符串塞给 :disabled)。
  snapshotInUse(snapshot_time) { return this.snapshotUsage(snapshot_time).length > 0; },
  // #394 — 灰度流水线当前处在第几步(1=待装 canary,2=canary 已装待验证/提升,3=已 promote 有 previous_live)。
  // 用于流水线 UI 高亮"你在哪一步"。有 canary → step2;否则有 previous_live(promote 过)→ step3;都没 → step1。
  canaryStage(h) {
    if (this.hostCanary(h)) return 2;
    if (this.hostPrevLive(h)) return 3;
    return 1;
  },
  // #394 — 软删一条快照记录(POST /delete-image-snapshot,与 create 对称)。后端引用保护:
  // 仍被 host 槽位/租户固定引用 → 409 IMAGE_VERSION_IN_USE(前端已按 host 引用禁用按钮,
  // 但租户引用只有后端知道,故仍可能 409 → 如实提示)。软删只标记不物删,可审计。
  async deleteSnapshot(snapshot_time) {
    if (!confirm(`Soft-delete image snapshot ${snapshot_time}?\n(marks the record deleted + drops it from the pullable list; refused if any host slot or tenant still pins it; does NOT remove S3 image files)`)) return;
    this.toast = `deleting ${snapshot_time}…`;
    try {
      const r = await this.api('POST', 'delete-image-snapshot', { snapshot_time });
      // api() 遇非 2xx 不 throw、直接返回 body → 按 code 判引用中拒删。
      if (r && r.code === 'IMAGE_VERSION_IN_USE') {
        this.toast = `✗ still in use: ${r.error || ''}`;
      } else if (r && r.code) {
        this.toast = `✗ delete failed [${r.code}]: ${r.error || ''}`;
      } else {
        this.toast = `✓ snapshot ${snapshot_time} deleted (soft)`;
        try { this.snapshots = await this.api('GET', 'list_image_versions?show_deleted=true'); } catch {}
      }
    } catch (e) {
      this.toast = '✗ delete failed: ' + (e && e.message ? e.message : 'error');
    }
    setTimeout(() => { this.toast = ''; }, 4000);
  },
  // #217 — 每台 host 当前选中的快照:选过用选的,没选默认最新(snapshots[0])。
  hostSnapFor(host_id) {
    return this.hostSnapChoice[host_id] || (this.snapshots[0] || {}).snapshot_time || '';
  },
  // #217 — 单台 Pull:用【这台自己】下拉选中的快照拉到它(各台独立,不弹窗)。
  // 装完 host 走 active→upgrading→active,轮询自动可见,不必手刷。
  async pullSnapshotToHost(host_id) {
    // #249 — 兜底:host 正 upgrading 就不发(按钮已 disable,防程序化/竞态点到)。
    const h = this.hosts.find(x => x.instance_id === host_id);
    if (h && h.status === 'upgrading') { alert('Host is upgrading (pull in progress).'); return; }
    const st = this.hostSnapFor(host_id);
    if (!st) { alert('No snapshot available to pull (use "Take snapshot" above first).'); return; }
    if (!confirm(`Pull snapshot ${st} → ${host_id}?\n(installs to live; this host takes no new tenants while upgrading)`)) return;
    this.toast = `pull-image ${st}…`;
    try {
      const r = await this.api('POST', `hosts/${host_id}/pull-image?snapshot_time=${encodeURIComponent(st)}`);
      this.toast = `✓ pull started (cmd ${r.command_id ? r.command_id.slice(0, 8) : '?'})`;
    } catch (e) {
      this.toast = '✗ pull-image failed: ' + (e && e.message ? e.message : 'error');
    }
    setTimeout(() => { this.toast = ''; this.loadHosts(); }, 3000);
  },
  // ─────────────────────────────────────────────────────────────────────────
  // #394 — per-VM canary image controls. All hit REAL control-plane endpoints:
  //   pull canary   → POST /hosts/{id}/pull-image?slot=canary   (installs to the
  //                   host's canary slot; does NOT flip host.status → live tenants
  //                   keep running on this host)
  //   promote       → POST /hosts/{id}/promote-canary            (admin, CAS)
  //   rollback      → 用 pull-image 选老版到 live(无独立 rollback;本地已装则秒级翻指针)
  //   reclaim       → POST /hosts/{id}/reclaim-images            (admin, prune 无引用版本)
  // Slot state comes from h.image_slots (the control-plane mirror surfaced by
  // GET /hosts). Promote/reclaim are admin-only (isAdmin gate).
  // ─────────────────────────────────────────────────────────────────────────
  // canary version this host will pull: explicit choice, else latest snapshot.
  canarySnapFor(host_id) {
    return this.canarySnapChoice[host_id] || (this.snapshots[0] || {}).snapshot_time || '';
  },
  // host's current slots (control-plane mirror). Absent on legacy flat-layout hosts.
  hostSlots(h) { return (h && h.image_slots) || {}; },
  hostCanary(h) { return this.hostSlots(h).canary || ''; },
  hostLiveSlot(h) { return this.hostSlots(h).live || ''; },
  hostPrevLive(h) { return this.hostSlots(h).previous_live || ''; },
  hostSlotGen(h) { const g = this.hostSlots(h).generation; return g == null ? '' : g; },
  // Pull the chosen snapshot into THIS host's canary slot (no host.status change).
  async pullCanaryToHost(host_id) {
    const st = this.canarySnapFor(host_id);
    if (!st) { alert('No snapshot available (use "Take snapshot" first).'); return; }
    if (!confirm(`Install snapshot ${st} into ${host_id}'s CANARY slot?\n(live tenants on this host are unaffected; validate with a canary tenant, then promote)`)) return;
    this.toast = `pull canary ${st}…`;
    try {
      const r = await this.api('POST', `hosts/${host_id}/pull-image?snapshot_time=${encodeURIComponent(st)}&slot=canary`);
      this.toast = `✓ canary pull started (job ${r.job_id ? r.job_id.slice(0, 10) : '?'})`;
      // #394 — canary pull 不置 host.status=upgrading,所以既有 "upgrading 才轮询/显示进度"
      // 那套看不到它。这里记下 canary job_id,poll 循环据此按 job_id 查进度并显示,终态后清除。
      if (r && r.job_id) this.canaryJob[host_id] = r.job_id;
    } catch (e) {
      this.toast = '✗ canary pull failed: ' + (e && e.message ? e.message : 'error');
    }
    setTimeout(() => { this.toast = ''; this.loadHosts(); }, 3000);
  },
  // #394 — 一键"在这台 host 上建 canary 验证租户":打开创建弹窗并预填 image_channel=canary
  // + preferred_host_id=本 host(canary 槽只在这台,必须 pin)。省得运维手动开弹窗再逐项设。
  createCanaryTenant(host_id) {
    const h = this.hosts.find(x => x.instance_id === host_id);
    if (!this.hostCanary(h)) { alert('This host has no canary staged — pull a canary first.'); return; }
    this.restoreSource = null;
    // 复用既有 create 表单结构;只预置灰度相关字段,其余留默认由运维填。
    this.form = {
      name: '', vcpu: null, memory_mb: null, config_template: '',
      preferred_host_id: host_id, group: '', tags_text: '', image_channel: 'canary',
    };
    this.showModal = true;
  },
  // #394 —— 真机实读:GET /hosts/{id}/image-slots 读 host 盘上真实 slots.json + versions/,
  // 覆盖显示用的 image_slots(DDB 镜像可能滞后)。debug 灰度时点一下看盘上真相。
  hostDiskSlots: {},  // instance_id → {slots, installed_versions, flat_layout, mirror}
  async refreshHostDiskSlots(host_id) {
    this.toast = `reading ${host_id} disk image state…`;
    try {
      const r = await this.api('GET', `hosts/${host_id}/image-slots`);
      if (r && (r.code || r.error)) {
        this.toast = `✗ read failed${r.code ? ' [' + r.code + ']' : ''}: ${r.error || ''}`;
      } else {
        this.hostDiskSlots[host_id] = r;
        // 把 host 卡片上显示的 image_slots 覆盖成盘上真值(消除"看着漂移"的困惑)。
        const h = this.hosts.find(x => x.instance_id === host_id);
        if (h && r.slots) h.image_slots = r.slots;
        this.toast = `✓ disk: live=${(r.slots && r.slots.live) || '—'} · canary=${(r.slots && r.slots.canary) || '—'} · gen ${r.slots ? r.slots.generation : '?'} · ${r.installed_versions.length} version(s)`;
      }
    } catch (e) {
      this.toast = '✗ read failed: ' + (e && e.message ? e.message : 'error');
    }
    setTimeout(() => { this.toast = ''; }, 5000);
  },
  // #394 Step5 —— 这台 host 上仍活着的 canary 验证租户(image_channel=canary,非 deleted)。
  // 用于流水线第 5 步"清理测试租户":读已加载的 this.tenants 本地过滤,不额外打 API。
  canaryTenantsOnHost(host_id) {
    return (this.tenants || []).filter(
      t => t.host_id === host_id && t.image_channel === 'canary'
        && t.status && t.status !== 'deleted'
    );
  },
  // Step5 —— 删除这台 host 上的 canary 验证租户(灰度收尾:验证完/放弃后清掉测试 VM)。
  // 逐个走既有 DELETE /tenants/{id};一个失败不挡其余,汇总提示。
  async deleteCanaryTenants(host_id) {
    const list = this.canaryTenantsOnHost(host_id);
    if (!list.length) { alert('No canary validation tenants on this host to clean up.'); return; }
    if (!confirm(`Delete ${list.length} canary validation tenant(s) on ${host_id}?\n(` + list.map(t => t.id).join(', ') + ')\nThis removes the test VMs you created in step 2.')) return;
    let ok = 0, fail = 0;
    for (const t of list) {
      this.toast = `deleting canary tenant ${t.id}…`;
      try { await this.api('DELETE', 'tenants/' + t.id); ok++; }
      catch { fail++; }
    }
    this.toast = `✓ canary tenants deleted: ${ok}${fail ? ', ' + fail + ' failed' : ''}`;
    setTimeout(() => { this.toast = ''; this.loadTenants(); this.loadHosts(); }, 3000);
  },
  // Promote this host's canary slot to live (admin, CAS on the mirrored snapshot+generation).
  async promoteCanary(host_id) {
    const h = this.hosts.find(x => x.instance_id === host_id);
    const canary = this.hostCanary(h);
    if (!canary) { alert('No canary staged on this host to promote.'); return; }
    if (!confirm(`Promote canary ${canary} → LIVE on ${host_id}?\n(new live tenants boot this version; running VMs keep their image until rebuilt. Irreversible pointer change.)`)) return;
    this.toast = `promote ${canary}…`;
    try {
      const gen = this.hostSlotGen(h);
      const body = { expected_canary_snapshot_time: canary };
      if (gen !== '') body.expected_canary_generation = gen;
      const r = await this.api('POST', `hosts/${host_id}/promote-canary`, body);
      // api() 遇非 2xx 不 throw、直接返回 body → 先按 code/error 判失败,别把错误体当成功
      // 读出 live_snapshot_time=undefined("promoted → live undefined")。
      if (r && (r.code || r.error)) {
        this.toast = `✗ promote failed${r.code ? ' [' + r.code + ']' : ''}: ${r.error || ''}`;
      } else if (r && r.already_promoted) {
        this.toast = `✓ already live: ${r.live_snapshot_time}`;
      } else {
        this.toast = `✓ promoted → live ${r.live_snapshot_time}`;
      }
    } catch (e) {
      this.toast = '✗ promote failed: ' + (e && e.message ? e.message : 'error');
    }
    // 刷新:先拉 hosts(DDB 镜像),若该 host 已展开盘上真值则也重读磁盘(promote 后指针变了)。
    await this.refreshHostAfterSlotOp(host_id);
  },
  // 槽位操作后刷新该 host 的显示:重载 hosts(DDB 镜像已被 host 回写),并且——只有当用户
  // 之前点过 ↻ disk 展开了盘上真值时——重读磁盘真值,让 live/canary/prev 立刻反映新状态,
  // 不用手动再点一次 ↻ disk。
  async refreshHostAfterSlotOp(host_id) {
    await this.loadHosts();
    if (this.hostDiskSlots[host_id]) { try { await this.refreshHostDiskSlots(host_id); } catch {} }
  },
  // #394 —— 无独立 rollback:回滚 = 用上方 pull 下拉选老版到 live(pull-image;本地版本目录
  // 已完整则后端快路径秒级翻指针,不重新下载)。previous_live 仅作展示,不再有 swap 动作。
  // Step4 —— 回收这台 host 上无人引用的版本目录(手动 prune,admin)。保留
  // live/canary/previous_live + 租户固定引用的版本,其余 versions/<snap>/ 删掉释放盘。
  async reclaimImages(host_id) {
    if (!confirm(`Reclaim unreferenced image versions on ${host_id}?\n(deletes versions/ dirs NOT referenced by live/canary/previous_live or any running tenant. Frees disk; irreversible.)`)) return;
    this.toast = `reclaiming old versions on ${host_id}…`;
    try {
      const r = await this.api('POST', `hosts/${host_id}/reclaim-images`, {});
      if (r && (r.code || r.error)) {
        this.toast = `✗ reclaim failed${r.code ? ' [' + r.code + ']' : ''}: ${r.error || ''}`;
      } else {
        this.toast = r.reclaimed_count
          ? `✓ reclaimed ${r.reclaimed_count} version(s): ${r.reclaimed_versions.join(', ')}`
          : `✓ nothing to reclaim (kept ${r.kept_versions.length})`;
      }
    } catch (e) {
      this.toast = '✗ reclaim failed: ' + (e && e.message ? e.message : 'error');
    }
    setTimeout(() => { this.toast = ''; this.loadHosts(); }, 4000);
  },
  // #309 — copy a single file from S3 to this host (EC2). Opens an in-app modal
  // (not a native prompt) so the EC2 target path is a clear, editable field.
  // Target must be under /data/firecracker-assets/ (server enforces the allowlist).
  // #309 — host-script target roots (mirror of backend _COPY_FILE_ALLOWED_ROOTS).
  copyAllowedRoots: ['/opt/openclaw/', '/home/ubuntu/'],
  openCopyFileModal(host_id) {
    this.copyModal = {
      show: true, host_id, s3_uri: 's3://',
      target: '/opt/openclaw/', busy: false, error: '',
    };
  },
  closeCopyFileModal() { this.copyModal.show = false; },
  async submitCopyFile() {
    const m = this.copyModal;
    const s3_uri = (m.s3_uri || '').trim();
    const target = (m.target || '').trim();
    if (!s3_uri.startsWith('s3://') || s3_uri.length <= 5) {
      m.error = 'S3 URI must be s3://<bucket>/<key>'; return;
    }
    if (!this.copyAllowedRoots.some(r => target.startsWith(r))) {
      m.error = 'Target must be under ' + this.copyAllowedRoots.join(' or '); return;
    }
    // #334 — 必须是完整文件路径(含文件名),拒目录/尾斜杠/裸根:否则 aws s3 cp 到目录后
    // chown 改的是目录、真文件仍 root:root(与后端 _validate_copy_target 一致)。
    if (target.endsWith('/') || this.copyAllowedRoots.some(r => target.replace(/\/+$/, '') === r.replace(/\/+$/, ''))) {
      m.error = 'Target must include a filename (not a directory). e.g. /opt/openclaw/host_agent.py'; return;
    }
    m.error = ''; m.busy = true;
    try {
      // #336 — copy-file 返回合并契约:靠【body 的 ProcessingJobStatus】判成败,不靠 HTTP 码
      // (api() 遇非 2xx 不 throw、直接返回 body)。成功 Completed;失败带 code + error。
      const r = await this.api('POST', `hosts/${m.host_id}/copy-file-from-s3`, { target, s3_uri });
      if (r && r.ProcessingJobStatus === 'Completed') {
        this.toast = `✓ copy-file done: ${target}`;
        m.show = false;
      } else {
        // Failed(COPY_FAILED / COPY_DISPATCH_FAILED)或校验错(VALIDATION):显 code + 真实原因。
        const reason = (r && (r.error || r.code)) || 'unknown error';
        m.error = `copy-file failed${r && r.code ? ' [' + r.code + ']' : ''}: ${reason}`;
      }
    } catch (e) {
      m.error = 'copy-file failed: ' + (e && e.message ? e.message : 'error');
    } finally {
      m.busy = false;
    }
    setTimeout(() => { this.toast = ''; }, 4000);
  },
  // #333 — GET /hosts/{id}/pull-image-progress 返回 SageMaker ProcessingJob 风格 JSON:
  //   ProcessingJobStatus: Completed|Failed|InProgress
  //   last_status: 进度文件末行原文(带时间戳+做了什么)
  //   Failed 时: ErrorCode + FailureReason
  // 组装成一行 inline 展示:进行中显进度原文;成功/失败显状态(+失败原因)。
  // (旧字段 p.progress 已移除,#333 改契约——这里同步消费新字段。)
  // Guards against overlapping calls per host so the 5s poll can't stack requests.
  async _fetchPullProgress(host_id, job_id) {
    if (this._progressInFlight[host_id]) return this.pullProgress[host_id];
    this._progressInFlight[host_id] = true;
    try {
      // #394 — 带 job_id 时精确查该 job(canary pull 走这条:host.status 不是 upgrading,
      // 只能靠 job_id 定位);不带时查该 host 最近一次(live pull 兼容路径)。
      const qs = job_id ? `?job_id=${encodeURIComponent(job_id)}` : '';
      const p = await this.api('GET', `hosts/${host_id}/pull-image-progress${qs}`);
      let line;
      if (p.ProcessingJobStatus === 'Failed') {
        line = `Failed: ${p.ErrorCode || ''} ${p.FailureReason || p.last_pull_error || ''}`.trim();
      } else if (p.ProcessingJobStatus === 'Completed') {
        line = 'Completed';
      } else {
        // InProgress: 显进度文件末行原文(哪个文件在下载/解压)
        line = p.last_status || p.last_pull_error || 'In progress…';
      }
      this.pullProgress[host_id] = line;
      // #394 — canary job 到终态就清除跟踪,进度行随之隐藏(下次 loadHosts 拿到新 slots)。
      if (job_id && (p.ProcessingJobStatus === 'Completed' || p.ProcessingJobStatus === 'Failed')) {
        delete this.canaryJob[host_id];
      }
      return line;
    } catch (e) {
      return this.pullProgress[host_id] || '';
    } finally {
      this._progressInFlight[host_id] = false;
    }
  },
  // #309/#394 — on each poll beat, refresh progress for every host that's either doing a
  // LIVE pull (status=upgrading) or a CANARY pull (tracked canaryJob), so the card shows
  // the pull-image status line for both (no manual button needed).
  pollUpgradingProgress() {
    for (const h of this.hosts) {
      if (h.status === 'upgrading') this._fetchPullProgress(h.instance_id);
      else if (this.canaryJob[h.instance_id]) this._fetchPullProgress(h.instance_id, this.canaryJob[h.instance_id]);
    }
  },
  // #217 — quiet poll: refresh the host list (status/snapshot) WITHOUT flipping
  // loadingHosts, so the manual-refresh spinner doesn't blink every beat. A host's
  // active→upgrading→active cycle after a Pull then updates on its own.
  async pollHosts() {
    if (!this.apiUrl || !this.apiKey) return;
    try {
      this.hosts = await this.api('GET', 'hosts');
      this.connected = true;
      this.snapshots = await this.api('GET', 'list_image_versions?show_deleted=true');  // #337(原#217 /snapshots)
    } catch { this.connected = false; }
  },
  // v1.2.8 — host AZ lookup for the new "AZ" column in the tenants table.
  hostAz(host_id) {
    if (!host_id) return '';
    const h = this.hosts.find(h => h.instance_id === host_id);
    return h ? (h.az || '') : '';
  },
  async refreshRootfs() {
    if (!confirm('Pull latest rootfs to all hosts?')) return;
    this.toast = 'refresh-rootfs…';
    await this.api('POST', 'hosts/refresh-rootfs');
    this.toast = '✓ refresh-rootfs sent';
    setTimeout(() => this.toast = '', 3000);
  },
  // #217 — host.snapshot_time → "snapshot_time (rootfs版)"。rootfs 版从 snapshots 列表
  // 按 time 反查 label(打快照时自动填的 manifest version);查不到就只显示 time。
  snapshotLabel(t) {
    if (!t) return '';
    const s = (this.snapshots || []).find(x => x.snapshot_time === t);
    return s && s.label ? `${t} (${s.label})` : t;
  },
  // #217 — 顶部展示【快照表里最新的一条】(snapshots 已按 snapshot_time 倒序,第一个即最新)。
  // snapshot_time 本身就是快照事件时刻,显示系统里最新可用的版本,不依赖 host 是否已装。
  // 各 host 当前装的版本另在各自卡片的 snapshot 行显示。
  get latestSnapshot() {
    const s = (this.snapshots || [])[0];
    return s ? s.snapshot_time + (s.label ? ' (' + s.label + ')' : '') : '';
  },
  cpuPct(h) {
    const ratio = h.cpu_overcommit_ratio || 1;
    const allocatable = h.total_vcpu * ratio;
    return allocatable ? Math.min(100, Math.round(h.used_vcpu / allocatable * 100)) : 0;
  },
  cpuBarClass(h) {
    return h.used_vcpu > h.total_vcpu ? 'bar-cpu-over' : 'bar-cpu';
  },
  memPct(h) {
    const ratio = h.mem_overcommit_ratio || 1;
    const allocatable = h.total_mem_mb * ratio;
    return allocatable ? Math.min(100, Math.round(h.used_mem_mb / allocatable * 100)) : 0;
  },
  memBarClass(h) {
    return h.used_mem_mb > h.total_mem_mb ? 'bar-mem-over' : 'bar-mem';
  },
  // 1.2.9 — group hosts by AZ when multi_az is enabled, otherwise return
  // a single un-titled group so the existing flat layout still works.
  // Sorted alphabetically by AZ for stable rendering.
  get groupedHosts() {
    const multiAz = !!(this.systemInfo?.multi_az?.enabled);
    if (!multiAz) return [{ az: null, hosts: this.hosts }];
    const groups = new Map();
    for (const h of this.hosts) {
      const az = h.az || '(no az)';
      if (!groups.has(az)) groups.set(az, []);
      groups.get(az).push(h);
    }
    return [...groups.keys()].sort().map(az => ({ az, hosts: groups.get(az) }));
  },
  // 1.2.9 — Fleet by AZ summary for Settings: counts/aggregates per AZ.
  get fleetByAz() {
    const groups = new Map();
    for (const h of this.hosts) {
      const az = h.az || '(no az)';
      if (!groups.has(az)) {
        groups.set(az, { az, hosts: 0, vms: 0, vcpu_used: 0, vcpu_total: 0 });
      }
      const g = groups.get(az);
      g.hosts++;
      g.vms += this._n(h.vm_count);
      g.vcpu_used += this._n(h.used_vcpu);
      g.vcpu_total += this._n(h.total_vcpu);
    }
    return [...groups.values()].sort((a, b) => a.az.localeCompare(b.az));
  },
  // 1.2.9 — for the create-tenant modal's preferred-host picker. Lists
  // active hosts with their az + remaining capacity. Sorted by az.
  availableHostsForCreate() {
    return this.hosts
      .filter(h => h.status === 'active' || h.status === 'idle')
      .sort((a, b) => (a.az || '').localeCompare(b.az || ''));
  },
};
