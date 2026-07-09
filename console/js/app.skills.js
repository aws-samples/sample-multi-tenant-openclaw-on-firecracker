// Skills (1.4.1 #63): list, markdown render, toggle/preview, save, delete, upload.
window.ocSkills = {
  skills: [],
  // 1.4.1 (#63) — Console skills CRUD state
  expandedSkill: null,         // skill name whose preview/edit panel is open
  skillEditMode: false,        // true = textarea editor visible, false = preview only
  skillContent: '',            // current SKILL.md content in the editor
  skillSaving: false,          // disables save button during PUT
  showSkillUpload: false,      // upload modal visibility
  skillUploadForm: { name: '', content: '# New Skill\n\nDescribe what this skill does.\n' },

  // ===== 1.4.1 (#63) — Skill CRUD =====
  async loadSkills() {
    try {
      const sk = await this.api('GET', 'skills');
      this.skills = sk.skills || [];
    } catch {}
  },
  renderMd(text) {
    // Use Marked if loaded, otherwise just escape and show as <pre>
    if (window.marked) return window.marked.parse(text || '');
    const esc = (text || '').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
    return `<pre style="white-space:pre-wrap">${esc}</pre>`;
  },
  async toggleSkill(name) {
    if (this.expandedSkill === name) {
      this.expandedSkill = null;
      this.skillEditMode = false;
      this.skillContent = '';
      return;
    }
    this.expandedSkill = name;
    this.skillEditMode = false;
    try {
      const r = await this.api('GET', 'skills/' + encodeURIComponent(name));
      this.skillContent = r.content || '';
    } catch (e) {
      this.toast = '✗ Load failed: ' + (e.message || e);
      setTimeout(() => this.toast = '', 4000);
      this.expandedSkill = null;
    }
  },
  async saveSkill(name) {
    if (this.skillSaving) return;
    // Quick client-side sanity — server enforces this too but fail fast in UI.
    const hasH1 = this.skillContent.split('\n').some(ln => /^\s*#\s+\S/.test(ln));
    if (!hasH1) {
      this.toast = '✗ SKILL.md must have a top-level "# Title" line';
      setTimeout(() => this.toast = '', 4000);
      return;
    }
    this.skillSaving = true;
    try {
      const r = await this.api('PUT', 'skills/' + encodeURIComponent(name), { content: this.skillContent });
      if (r.error) throw new Error(r.error);
      this.toast = '✓ Saved ' + name;
      this.skillEditMode = false;
      await this.loadSkills();
    } catch (e) {
      this.toast = '✗ Save failed: ' + (e.message || e);
    }
    this.skillSaving = false;
    setTimeout(() => this.toast = '', 3000);
  },
  async deleteSkill(name) {
    if (!confirm(`Delete skill "${name}"?\n\nThis removes the entire s3://${this.systemInfo?.assets_bucket || '<bucket>'}/skills/${name}/ prefix and is not reversible.`)) return;
    try {
      const r = await this.api('DELETE', 'skills/' + encodeURIComponent(name));
      if (r.error) throw new Error(r.error);
      this.toast = '✓ Deleted ' + name;
      if (this.expandedSkill === name) this.expandedSkill = null;
      await this.loadSkills();
    } catch (e) {
      this.toast = '✗ Delete failed: ' + (e.message || e);
    }
    setTimeout(() => this.toast = '', 3000);
  },
  async uploadSkill() {
    const name = (this.skillUploadForm.name || '').trim();
    if (!/^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/.test(name)) {
      this.toast = '✗ Invalid skill name (lowercase letters, digits, hyphens; 1-64 chars)';
      setTimeout(() => this.toast = '', 4000);
      return;
    }
    try {
      const r = await this.api('PUT', 'skills/' + encodeURIComponent(name), { content: this.skillUploadForm.content });
      if (r.error) throw new Error(r.error);
      this.toast = '✓ Uploaded ' + name;
      this.showSkillUpload = false;
      this.skillUploadForm = { name: '', content: '# New Skill\n\nDescribe what this skill does.\n' };
      await this.loadSkills();
    } catch (e) {
      this.toast = '✗ Upload failed: ' + (e.message || e);
    }
    setTimeout(() => this.toast = '', 3000);
  },
};
