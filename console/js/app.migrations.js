// Migration: open modal, list valid targets, POST migrate.
window.ocMigrations = {
  migrateSource: null,  // tenant being migrated; modal opens when truthy
  migrateTarget: '',    // selected target host_id

  // v1.2.8 — Migrate flow. API: POST /tenants/{id}/migrate {target_host_id}.
  openMigrate(t) {
    this.migrateSource = t;
    this.migrateTarget = '';
  },
  availableMigrationTargets() {
    // Active hosts that aren't the source's current host.
    if (!this.migrateSource) return [];
    return this.hosts.filter(h =>
      h.status === 'active' && h.instance_id !== this.migrateSource.host_id
    );
  },
  async migrateTenant() {
    if (!this.migrateSource || !this.migrateTarget) return;
    const id = this.migrateSource.id;
    const r = await this.api('POST', 'tenants/' + id + '/migrate',
      { target_host_id: this.migrateTarget });
    if (r && r.error) {
      this.toast = '✗ ' + r.error;
      setTimeout(() => this.toast = '', 4000);
      return;
    }
    this.toast = '✓ migrating ' + id;
    setTimeout(() => this.toast = '', 3000);
    this.migrateSource = null;
    this.migrateTarget = '';
    this.loadTenants();
  },
};
