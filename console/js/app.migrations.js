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

  // T2-8 — in-flight migrations panel. Tenants stuck `migrating` expose their
  // phase + elapsed so an operator can see progress and cancel a wedged one.
  get inflightMigrations() {
    return (this.tenants || []).filter(t => t.status === 'migrating').map(t => ({
      id: t.id,
      phase: t.migration_phase || '?',
      mode: t.migration_mode || 'live',
      source: t.migration_source || t.host_id || '?',
      target: t.migration_target || '?',
      elapsed: t.migration_started_at
        ? Math.round((Date.now() - new Date(t.migration_started_at).getTime()) / 1000) + 's'
        : '?',
    }));
  },
  async cancelMigration(id) {
    const r = await this.api('POST', 'tenants/' + id + '/cancel-migration');
    this.toast = (r && r.error) ? '✗ ' + r.error : '✓ cancelled ' + id;
    setTimeout(() => this.toast = '', 3000);
    this.loadTenants();
  },

  // T2-8 — drain a host: migrate every tenant off, THEN it's safe to terminate
  // (unlike plain decommission, which hard-deletes the host's tenants).
  async drainHost(instanceId) {
    if (!confirm('Drain ' + instanceId + '? Every tenant on it will be migrated off.')) return;
    const r = await this.api('POST', 'hosts/' + instanceId + '/drain');
    this.toast = (r && r.error) ? '✗ ' + r.error
      : '✓ draining ' + instanceId + ' (' + ((r.migrations_started || []).length) + ' migrating)';
    setTimeout(() => this.toast = '', 4000);
    this.loadHosts();
    this.loadTenants();
  },

  // T2-8 — manual AZ failover (admin). Kicks the health-check evacuation now
  // rather than waiting for the ~10-min auto-detect.
  async triggerFailover(az) {
    if (!confirm('Trigger failover for ' + az + '? Running tenants there will be evacuated.')) return;
    const r = await this.api('POST', 'failover/' + encodeURIComponent(az));
    this.toast = (r && r.error) ? '✗ ' + r.error : '✓ failover requested for ' + az;
    setTimeout(() => this.toast = '', 4000);
  },
};
