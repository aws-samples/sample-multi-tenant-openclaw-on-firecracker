// Skill groups (1.4.0 #62 / 1.4.1 #63): list, create, add/remove member skills.
window.ocGroups = {
  // 1.4.0 (#62) / 1.4.1 (#63) — Groups CRUD state
  groups: [],
  showGroupModal: false,
  groupForm: { name: '', description: '', skills_text: '' },
  expandedGroup: null,         // group name whose skills picker is open
  addSkillToGroupName: '',     // skill name being typed into the picker

  // ===== 1.4.1 (#63) — Groups CRUD (uses v1.4.0 endpoints) =====
  async loadGroups() {
    try {
      const r = await this.api('GET', 'groups');
      this.groups = r.groups || [];
    } catch {}
  },
  async createGroup() {
    const name = (this.groupForm.name || '').trim();
    if (!/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(name)) {
      this.toast = '✗ Invalid group name (same rules as tenant name)';
      setTimeout(() => this.toast = '', 4000);
      return;
    }
    const skills = (this.groupForm.skills_text || '')
      .split(',').map(s => s.trim()).filter(Boolean);
    try {
      const r = await this.api('POST', 'groups', {
        name, description: this.groupForm.description || '', skills,
      });
      if (r.error) throw new Error(r.error);
      this.toast = '✓ Created group ' + name;
      this.showGroupModal = false;
      this.groupForm = { name: '', description: '', skills_text: '' };
      await this.loadGroups();
    } catch (e) {
      this.toast = '✗ Create failed: ' + (e.message || e);
    }
    setTimeout(() => this.toast = '', 3000);
  },
  async addSkillToGroup(groupName) {
    const sk = (this.addSkillToGroupName || '').trim();
    if (!sk) return;
    try {
      const r = await this.api('POST', `groups/${encodeURIComponent(groupName)}/skills`, { skill: sk });
      if (r.error) throw new Error(r.error);
      this.addSkillToGroupName = '';
      await this.loadGroups();
    } catch (e) {
      this.toast = '✗ Add failed: ' + (e.message || e);
      setTimeout(() => this.toast = '', 3000);
    }
  },
  async removeSkillFromGroup(groupName, skill) {
    if (!confirm(`Remove "${skill}" from group "${groupName}"?`)) return;
    try {
      await this.api('DELETE', `groups/${encodeURIComponent(groupName)}/skills/${encodeURIComponent(skill)}`);
      await this.loadGroups();
    } catch (e) {
      this.toast = '✗ Remove failed: ' + (e.message || e);
      setTimeout(() => this.toast = '', 3000);
    }
  },
};
