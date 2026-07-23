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

  async loadHosts() {
    if (!this.apiUrl || !this.apiKey) return;
    this.loadingHosts = true;
    try { this.hosts = await this.api('GET', 'hosts'); this.connected = true; }
    catch { this.connected = false; }
    this.loadingHosts = false;
    try { this.rootfsVersion = (await this.api('GET', 'hosts/rootfs-version')).version; } catch {}
    try { this.agentcoreStatus = await this.api('GET', 'agentcore/status'); } catch {}
    try { this.snapshots = await this.api('GET', 'list_image_versions'); } catch {}  // #337(原#217 /snapshots)顶部下拉
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
      try { this.snapshots = await this.api('GET', 'list_image_versions'); } catch {}
    } catch (e) {
      this.toast = '✗ snapshot failed: ' + (e && e.message ? e.message : 'error');
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
  async _fetchPullProgress(host_id) {
    if (this._progressInFlight[host_id]) return this.pullProgress[host_id];
    this._progressInFlight[host_id] = true;
    try {
      const p = await this.api('GET', `hosts/${host_id}/pull-image-progress`);
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
      return line;
    } catch (e) {
      return this.pullProgress[host_id] || '';
    } finally {
      this._progressInFlight[host_id] = false;
    }
  },
  // #309 — on each poll beat, refresh progress for every host that's upgrading so
  // the card shows the live pull-image status line (no manual button needed).
  pollUpgradingProgress() {
    for (const h of this.hosts) {
      if (h.status === 'upgrading') this._fetchPullProgress(h.instance_id);
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
      this.snapshots = await this.api('GET', 'list_image_versions');  // #337(原#217 /snapshots)
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
