// Backups: list, restore flow, grouping by tenant.
window.ocBackups = {
  backups: [], backupFilter: 'all', loadingBackups: false,
  expandedGroups: new Set(),   // tenant_ids whose history rows are expanded
  restoreSource: null,   // set when modal opened from Backups tab; null = normal create

  async loadBackups() {
    if (!this.apiUrl || !this.apiKey) return;
    this.loadingBackups = true;
    try { this.backups = await this.api('GET', 'backups'); this.connected = true; }
    catch { this.connected = false; this.backups = []; }
    this.loadingBackups = false;
  },
  openRestore(backup) {
    this.restoreSource = backup;
    this.form = {
      name: (backup.tenant_name || backup.tenant_id) + '-restored',
      vcpu: null, memory_mb: null, config_template: '', preferred_host_id: '', group: '', tags_text: '',
    };
    this.showModal = true;
  },
  get filteredBackups() {
    if (this.backupFilter === 'orphan') return this.backups.filter(b => !b.tenant_exists);
    return this.backups;
  },
  get backupGroups() {
    // Group by tenant_id, sort groups so most recently backed-up tenant is at top.
    const groups = new Map();
    for (const b of this.filteredBackups) {
      if (!groups.has(b.tenant_id)) {
        groups.set(b.tenant_id, {
          tenant_id: b.tenant_id,
          tenant_name: b.tenant_name,
          tenant_exists: b.tenant_exists,
          backups: [],
        });
      }
      groups.get(b.tenant_id).backups.push(b);
    }
    return [...groups.values()].sort((a, b) =>
      a.backups[0].last_modified.localeCompare(b.backups[0].last_modified)
    );
  },
  toggleGroup(tenantId) {
    // Reassign to a new Set so Alpine's reactivity picks up the change.
    const s = new Set(this.expandedGroups);
    if (s.has(tenantId)) s.delete(tenantId); else s.add(tenantId);
    this.expandedGroups = s;
  },
};
