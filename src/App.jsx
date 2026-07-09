import React, { useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  Activity,
  ArrowUpRight,
  Bot,
  Braces,
  Building2,
  Cable,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  Clock3,
  CloudCog,
  Database,
  GitBranch,
  HardDrive,
  KeyRound,
  Layers3,
  Link2,
  LockKeyhole,
  Network,
  Play,
  RadioTower,
  Rocket,
  Route,
  Search,
  Server,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  Users,
  Workflow,
  Zap,
} from 'lucide-react'
import './styles.css'

const departments = [
  {
    id: 'ops',
    name: 'Operations',
    color: 'teal',
    agents: [
      { name: 'Incident Commander', harness: 'SwarmClaw Planner', status: 'active', load: 74 },
      { name: 'Runbook Analyst', harness: 'OpenCode CLI', status: 'active', load: 62 },
      { name: 'SRE Watcher', harness: 'MCP Tool Loop', status: 'idle', load: 22 },
    ],
  },
  {
    id: 'finance',
    name: 'Finance',
    color: 'amber',
    agents: [
      { name: 'Margin Sentinel', harness: 'Codex CLI', status: 'active', load: 58 },
      { name: 'Invoice Matcher', harness: 'Structured Session', status: 'active', load: 44 },
      { name: 'Spend Forecaster', harness: 'LangGraph', status: 'paused', load: 12 },
    ],
  },
  {
    id: 'sales',
    name: 'Revenue',
    color: 'violet',
    agents: [
      { name: 'Deal Desk Copilot', harness: 'Claude Code', status: 'active', load: 69 },
      { name: 'RFP Builder', harness: 'Qwen Code', status: 'active', load: 51 },
      { name: 'Pipeline Scout', harness: 'Browser MCP', status: 'idle', load: 19 },
    ],
  },
  {
    id: 'legal',
    name: 'Legal',
    color: 'rose',
    agents: [
      { name: 'Clause Reviewer', harness: 'Gemini CLI', status: 'active', load: 47 },
      { name: 'Risk Register', harness: 'SwarmClaw Memory', status: 'idle', load: 26 },
    ],
  },
  {
    id: 'people',
    name: 'People',
    color: 'green',
    agents: [
      { name: 'Onboarding Guide', harness: 'Hermes Agent', status: 'active', load: 39 },
      { name: 'Policy Interpreter', harness: 'OpenRouter', status: 'idle', load: 17 },
    ],
  },
]

const users = [
  { name: 'Maya Chen', role: 'Enterprise Admin', team: 'Operations', signal: 'online' },
  { name: 'Elliot Vance', role: 'Workflow Owner', team: 'Finance', signal: 'online' },
  { name: 'Priya Raman', role: 'Agent Trainer', team: 'Revenue', signal: 'review' },
  { name: 'Tom Walker', role: 'Client Viewer', team: 'Legal', signal: 'online' },
  { name: 'Ari Mensah', role: 'Security Lead', team: 'Platform', signal: 'offline' },
]

const workflows = [
  { name: 'Critical Incident Swarm', dept: 'Operations', state: 'running', steps: 11, eta: '04m', agents: 5 },
  { name: 'Month-End Close Reconcile', dept: 'Finance', state: 'running', steps: 18, eta: '22m', agents: 4 },
  { name: 'Enterprise RFP Draft', dept: 'Revenue', state: 'review', steps: 9, eta: 'client', agents: 3 },
  { name: 'Vendor Contract Diff', dept: 'Legal', state: 'queued', steps: 6, eta: '12m', agents: 2 },
  { name: 'New Starter Day Zero', dept: 'People', state: 'running', steps: 7, eta: '08m', agents: 2 },
]

const mcps = [
  { name: 'GitHub Enterprise', type: 'MCP', status: 'healthy', latency: '38ms', scope: 'code, issues, PRs' },
  { name: 'ServiceNow', type: 'Connector', status: 'healthy', latency: '62ms', scope: 'incidents, changes' },
  { name: 'SharePoint Vault', type: 'MCP', status: 'healthy', latency: '71ms', scope: 'policies, docs' },
  { name: 'Snowflake Finance', type: 'Gateway', status: 'review', latency: '104ms', scope: 'read-only tables' },
  { name: 'Slack Client Rooms', type: 'Connector', status: 'healthy', latency: '42ms', scope: 'messages, alerts' },
  { name: 'Browser Sandbox', type: 'Tool', status: 'healthy', latency: '55ms', scope: 'web tasks' },
]

const tenants = [
  { name: 'acme-enterprise', vm: 'fc-172.16.3.2', cpu: '8 vCPU', mem: '16 GB', disk: '42 GB', state: 'running' },
  { name: 'northstar-pilot', vm: 'fc-172.16.4.2', cpu: '4 vCPU', mem: '8 GB', disk: '18 GB', state: 'running' },
  { name: 'demo-boardroom', vm: 'fc-172.16.5.2', cpu: '6 vCPU', mem: '12 GB', disk: '27 GB', state: 'running' },
]

function StatusPill({ value }) {
  return <span className={`pill ${value}`}>{value}</span>
}

function Metric({ label, value, hint, icon: Icon }) {
  return (
    <section className="metric-panel">
      <div className="metric-icon" title={label}><Icon size={18} /></div>
      <div>
        <div className="metric-value">{value}</div>
        <div className="metric-label">{label}</div>
        {hint && <div className="metric-hint">{hint}</div>}
      </div>
    </section>
  )
}

function DepartmentPanel({ department, selected, onSelect }) {
  const active = department.agents.filter((agent) => agent.status === 'active').length
  return (
    <button className={`department-row ${selected ? 'selected' : ''}`} onClick={() => onSelect(department.id)}>
      <span className={`dept-dot ${department.color}`} />
      <span>
        <strong>{department.name}</strong>
        <small>{department.agents.length} agents · {active} active</small>
      </span>
      <ChevronDown size={16} />
    </button>
  )
}

function AgentTable({ agents }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Agent</th>
            <th>Harness</th>
            <th>Status</th>
            <th>Load</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((agent) => (
            <tr key={agent.name}>
              <td>
                <div className="agent-name">
                  <Bot size={17} />
                  <span>{agent.name}</span>
                </div>
              </td>
              <td>{agent.harness}</td>
              <td><StatusPill value={agent.status} /></td>
              <td>
                <div className="load-cell">
                  <span className="load-track"><i style={{ width: `${agent.load}%` }} /></span>
                  <b>{agent.load}%</b>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TopologyMap() {
  return (
    <section className="topology">
      <div className="topology-header">
        <div>
          <h2>Tenant Fabric</h2>
          <p>Firecracker isolation with SwarmClaw orchestration lanes.</p>
        </div>
        <StatusPill value="running" />
      </div>
      <div className="fabric">
        <div className="fabric-node control">
          <Server size={20} />
          <span>XoomAI OS</span>
        </div>
        <div className="fabric-line a" />
        <div className="fabric-line b" />
        <div className="fabric-line c" />
        {tenants.map((tenant, index) => (
          <div className={`fabric-node tenant t${index + 1}`} key={tenant.name}>
            <HardDrive size={18} />
            <span>{tenant.name}</span>
            <small>{tenant.vm}</small>
          </div>
        ))}
        <div className="fabric-node gateway">
          <Network size={20} />
          <span>MCP Gateway</span>
          <small>6 connections</small>
        </div>
      </div>
    </section>
  )
}

function WorkflowRow({ flow }) {
  return (
    <div className="workflow-row">
      <div className="workflow-main">
        <span className={`state-dot ${flow.state}`} />
        <div>
          <strong>{flow.name}</strong>
          <small>{flow.dept} · {flow.steps} steps · {flow.agents} agents</small>
        </div>
      </div>
      <div className="workflow-meta">
        <StatusPill value={flow.state} />
        <span>{flow.eta}</span>
      </div>
    </div>
  )
}

function App() {
  const [departmentId, setDepartmentId] = useState('ops')
  const [view, setView] = useState('control')

  const selectedDepartment = useMemo(
    () => departments.find((dept) => dept.id === departmentId) || departments[0],
    [departmentId],
  )

  const allAgents = departments.flatMap((dept) => dept.agents.map((agent) => ({ ...agent, dept: dept.name })))
  const activeAgents = allAgents.filter((agent) => agent.status === 'active').length

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">X</div>
          <div>
            <strong>XoomAI</strong>
            <small>Enterprise OS</small>
          </div>
        </div>
        <nav className="nav-list">
          <button className={view === 'control' ? 'active' : ''} onClick={() => setView('control')}><Activity size={18} /> Control</button>
          <button className={view === 'agents' ? 'active' : ''} onClick={() => setView('agents')}><Bot size={18} /> Agents</button>
          <button className={view === 'workflows' ? 'active' : ''} onClick={() => setView('workflows')}><Workflow size={18} /> Workflows</button>
          <button className={view === 'security' ? 'active' : ''} onClick={() => setView('security')}><ShieldCheck size={18} /> Security</button>
        </nav>
        <div className="sidebar-footer">
          <span><CircleDot size={13} /> Demo tenant fabric</span>
          <strong>Hetzner / Ubuntu</strong>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Client demo environment</p>
            <h1>XoomAI Enterprise OS Command Center</h1>
          </div>
          <div className="top-actions">
            <a className="primary-link" href="https://xoomai.enterprise-os.demo" target="_blank" rel="noreferrer">
              <Rocket size={17} />
              XoomAI Enterprise OS
              <ArrowUpRight size={15} />
            </a>
            <a className="secondary-link" href="https://swarmclaw.fake/demo/acme-enterprise" target="_blank" rel="noreferrer">
              <Sparkles size={17} />
              SwarmClaw tenant
              <ArrowUpRight size={15} />
            </a>
          </div>
        </header>

        <section className="metrics-grid">
          <Metric icon={Layers3} label="Tenants" value="3" hint="all isolated microVMs" />
          <Metric icon={Bot} label="Agents" value={allAgents.length} hint={`${activeAgents} actively executing`} />
          <Metric icon={Workflow} label="Workflows" value="5" hint="3 running · 1 review" />
          <Metric icon={Cable} label="MCP / Connections" value="6" hint="all scoped by tenant" />
        </section>

        <section className="main-grid">
          <div className="left-column">
            <TopologyMap />

            <section className="panel">
              <div className="panel-title">
                <div>
                  <h2>Department Swarms</h2>
                  <p>Each department owns its agents, tools, memory, and approval boundaries.</p>
                </div>
                <div className="search-chip"><Search size={15} /> acme-enterprise</div>
              </div>
              <div className="department-layout">
                <div className="department-list">
                  {departments.map((dept) => (
                    <DepartmentPanel
                      key={dept.id}
                      department={dept}
                      selected={dept.id === departmentId}
                      onSelect={setDepartmentId}
                    />
                  ))}
                </div>
                <div className="agent-panel">
                  <div className="agent-panel-head">
                    <div>
                      <h3>{selectedDepartment.name}</h3>
                      <p>{selectedDepartment.agents.length} agents mapped to tenant-scoped harnesses.</p>
                    </div>
                    <StatusPill value="active" />
                  </div>
                  <AgentTable agents={selectedDepartment.agents} />
                </div>
              </div>
            </section>
          </div>

          <div className="right-column">
            <section className="panel compact">
              <div className="panel-title tight">
                <h2>Tenant Access</h2>
                <StatusPill value="running" />
              </div>
              <div className="access-stack">
                <div className="access-card">
                  <Link2 size={18} />
                  <div>
                    <strong>XoomAI Enterprise OS</strong>
                    <span>https://xoomai.enterprise-os.demo</span>
                  </div>
                </div>
                <div className="access-card">
                  <Route size={18} />
                  <div>
                    <strong>SwarmClaw tenant</strong>
                    <span>https://swarmclaw.fake/demo/acme-enterprise</span>
                  </div>
                </div>
                <div className="access-card">
                  <KeyRound size={18} />
                  <div>
                    <strong>Scoped access key</strong>
                    <span>sc_demo_acme_72f4...b91</span>
                  </div>
                </div>
              </div>
            </section>

            <section className="panel compact">
              <div className="panel-title tight">
                <h2>Workflows</h2>
                <Clock3 size={18} />
              </div>
              <div className="workflow-list">
                {workflows.map((flow) => <WorkflowRow key={flow.name} flow={flow} />)}
              </div>
            </section>

            <section className="panel compact">
              <div className="panel-title tight">
                <h2>MCP & Connections</h2>
                <Braces size={18} />
              </div>
              <div className="connection-list">
                {mcps.map((mcp) => (
                  <div className="connection-row" key={mcp.name}>
                    <div>
                      <strong>{mcp.name}</strong>
                      <small>{mcp.type} · {mcp.scope}</small>
                    </div>
                    <span className={`connection-status ${mcp.status}`}>
                      {mcp.status === 'healthy' ? <CheckCircle2 size={14} /> : <RadioTower size={14} />}
                      {mcp.latency}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </section>

        <section className="bottom-grid">
          <section className="panel">
            <div className="panel-title tight">
              <h2>Users & Roles</h2>
              <Users size={18} />
            </div>
            <div className="user-grid">
              {users.map((user) => (
                <div className="user-row" key={user.name}>
                  <div className="avatar">{user.name.split(' ').map((part) => part[0]).join('')}</div>
                  <div>
                    <strong>{user.name}</strong>
                    <small>{user.role} · {user.team}</small>
                  </div>
                  <span className={`presence ${user.signal}`}>{user.signal}</span>
                </div>
              ))}
            </div>
          </section>

          <section className="panel">
            <div className="panel-title tight">
              <h2>Infrastructure</h2>
              <CloudCog size={18} />
            </div>
            <div className="infra-grid">
              <div><TerminalSquare size={18} /><strong>Ubuntu host</strong><span>hetzner-rx220-01</span></div>
              <div><LockKeyhole size={18} /><strong>Isolation</strong><span>Firecracker KVM</span></div>
              <div><Database size={18} /><strong>State</strong><span>local JSON + ext4 disks</span></div>
              <div><GitBranch size={18} /><strong>Release</strong><span>swarmclaw-hetzner-backend</span></div>
              <div><Zap size={18} /><strong>Routing</strong><span>nginx /vm/&lt;tenant&gt;</span></div>
              <div><Play size={18} /><strong>Automation</strong><span>direct lifecycle scripts</span></div>
            </div>
          </section>
        </section>
      </main>
    </div>
  )
}

createRoot(document.getElementById('root')).render(<App />)
