// Config templates: load, save (JSON-validated), delete.
window.ocTemplates = {
  templates: [],
  editTpl: null,

  async loadTemplates() {
    try { const r = await this.api('GET', 'templates'); this.templates = r.templates || []; } catch {}
  },
  async saveTemplate() {
    if (!this.editTpl?.name) return;
    try {
      const content = JSON.parse(this.editTpl.content);
      await this.api('PUT', 'templates/' + this.editTpl.name, content);
      this.editTpl = null; this.loadTemplates();
    } catch (e) { alert('Invalid JSON: ' + e.message); }
  },
  async deleteTemplate(name) {
    if (!confirm('Delete template "' + name + '"?')) return;
    await this.api('DELETE', 'templates/' + name);
    this.loadTemplates();
  },
};
