// Monitoring / system info: AgentCore tools, /system/info, AMP query URL, S3 deep links.
window.ocMonitoring = {
  // v1.2.8 — surfaces for features that already had an API but no UI.
  agentcoreTools: [],   // populated when Application tab is opened (and AgentCore enabled)
  systemInfo: null,     // populated when Monitoring/Settings tab is opened

  // v1.2.8 — Application tab: list MCP tools registered on AgentCore Gateway.
  async loadAgentCoreTools() {
    if (!this.apiUrl || !this.apiKey) return;
    try {
      const r = await this.api('GET', 'agentcore/tools');
      this.agentcoreTools = (r && r.tools) || [];
    } catch {
      this.agentcoreTools = [];
    }
  },
  // v1.2.8/9 — Monitoring + Settings tabs: feature flags + grafana url
  // + a fresh hosts snapshot so the Fleet-by-AZ table has up-to-date data.
  async loadSystemInfo() {
    if (!this.apiUrl || !this.apiKey) return;
    try {
      this.systemInfo = await this.api('GET', 'system/info');
    } catch {
      this.systemInfo = null;
    }
    // Refresh hosts so groupedHosts / fleetByAz reflect any new hosts that
    // came up since the user last opened the Tenants tab.
    this.loadHosts();
  },
  // Grafana's Prometheus data source wants the AMP workspace root — that's the
  // remote_write URL with "/api/v1/remote_write" stripped.
  ampQueryUrl() {
    const rw = this.systemInfo?.metrics?.amp_remote_write_url || '';
    return rw.replace(/\/api\/v1\/remote_write$/, '') || '-';
  },
  // v1.2.8 — link to the S3 console at this skill's prefix so operators
  // can edit/upload the SKILL.md without leaving the browser.
  skillS3Url(skill) {
    // Use the deployed bucket name + region from the .env.deploy-injected globals.
    // Falls back to the AWS console list page when we can't compute the deep link.
    const region = (this.systemInfo?.region) || (window.OC_REGION || 'us-east-1');
    const bucket = (this.systemInfo?.assets_bucket) || window.OC_ASSETS_BUCKET || '';
    if (bucket && skill?.id) {
      return `https://${region}.console.aws.amazon.com/s3/buckets/${bucket}?prefix=skills/${skill.id}/&region=${region}`;
    }
    return 'https://s3.console.aws.amazon.com/s3/home';
  },
};
