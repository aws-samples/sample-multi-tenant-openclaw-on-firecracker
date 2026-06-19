// Hosts: load, AZ grouping, capacity bars, rootfs refresh, host pickers.
window.ocHosts = {
  hosts: [], rootfsVersion: '',
  loadingHosts: false,
  agentcoreStatus: { enabled: false }, agentcoreRegion: 'ap-northeast-1',

  async loadHosts() {
    if (!this.apiUrl || !this.apiKey) return;
    this.loadingHosts = true;
    try { this.hosts = await this.api('GET', 'hosts'); this.connected = true; }
    catch { this.connected = false; }
    this.loadingHosts = false;
    try { this.rootfsVersion = (await this.api('GET', 'hosts/rootfs-version')).version; } catch {}
    try { this.agentcoreStatus = await this.api('GET', 'agentcore/status'); } catch {}
    // Load skills + groups (v1.4.0/1.4.1)
    this.loadSkills();
    this.loadGroups();
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
