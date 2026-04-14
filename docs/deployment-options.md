# Deployment Options Comparison

Five deployment approaches for running OpenClaw agents in isolated environments, from self-managed EC2 + Firecracker to fully managed AgentCore Runtime.

## Overview

| Option | Isolation | Orchestration | Complexity | Cost | Best For |
|--------|----------|---------------|------------|------|----------|
| **A. EC2 + Firecracker (current)** | microVM | ASG + Lambda | Medium | Low | Production, proven |
| **B. EKS + Kata Containers** | microVM | K8s | High | Medium | K8s-native teams |
| **C. EKS + Privileged Pod** | microVM | K8s | Medium | Medium | Quick migration |
| **D. EKS + gVisor** | Kernel sandbox | K8s | Low | Low | Lightweight isolation |
| **E. AgentCore Runtime** | microVM | Fully managed | Lowest | Pay-per-use | Pure Serverless |

---

## Option A: EC2 + Firecracker (Current) ✅ Production-verified

```
User → API Gateway → Lambda → SSM → EC2 Host → Firecracker → microVM (OpenClaw)
                                      ↑ ASG auto-scaling
CloudFront → ALB → Nginx:80 → VM Gateway:18789
```

### Architecture
- EC2 instances (c8i/m8i/r8i) with nested virtualization
- Multiple Firecracker microVMs per host
- Lambda + SSM for VM lifecycle management
- ALB path-based routing for Dashboard access
- CloudFront for HTTPS without custom domain

### Pros
- ✅ Production-verified (v0.9.3)
- ✅ microVM-level isolation (independent kernel)
- ✅ One-click deploy (CDK)
- ✅ AgentCore integration
- ✅ Low cost (high density + Spot support)
- ✅ OverlayFS shared rootfs (fast VM creation ~20s)
- ✅ Host-agent local health monitoring

### Cons
- ❌ Not K8s-native (SSM management, not kubectl)
- ❌ Intel instances only
- ❌ Custom orchestration logic (Lambda + DynamoDB)

### Best For
- Small to medium scale (1–100 tenants)
- microVM-level isolation required
- Team not mandating K8s

---

## Option B: EKS + Kata Containers + Firecracker

```
User → K8s API → EKS → Worker Node (nested virt) → Kata Runtime → Firecracker → Pod = microVM
                        ↑ Karpenter/CA auto-scaling
ALB Ingress Controller → Pod:18789
```

### Architecture
- EKS cluster with c8i/m8i/r8i Worker Nodes (nested virtualization)
- Kata Containers as CRI runtime, backed by Firecracker
- Each Pod = one Firecracker microVM = one OpenClaw instance
- K8s Ingress replaces ALB path-based routing

### Migration Effort

| Current Component | Migrates To | Effort |
|-------------------|-------------|--------|
| ASG | EKS Node Group (c8i) | Medium |
| Lambda + SSM | K8s API + kubectl | Large |
| DynamoDB (tenants/hosts) | K8s CRD or keep DynamoDB | Medium |
| ALB + Nginx | ALB Ingress Controller | Medium |
| Health Check Lambda | K8s liveness/readiness probe | Small |
| Scaler Lambda | Karpenter / HPA | Medium |
| Backup Lambda | K8s CronJob | Small |
| init-host.sh | DaemonSet + init container | Medium |
| launch-vm.sh | Pod spec (Kata runtime class) | Large |

### Pros
- ✅ K8s-native (kubectl, helm, ArgoCD)
- ✅ microVM-level isolation (Kata + Firecracker)
- ✅ Standard K8s ecosystem (Prometheus, Grafana, Istio)
- ✅ Rolling updates, blue-green deployments

### Cons
- ❌ Kata Containers setup is complex
- ❌ EKS control plane cost (~$73/month)
- ❌ Worker Nodes must support nested virtualization
- ❌ Significant migration effort (~2–3 weeks)
- ❌ Kata + Firecracker on EKS is not an officially supported path

### Best For
- Teams with existing K8s operations expertise
- Need to co-locate with existing K8s workloads
- Strong dependency on K8s ecosystem

### Key Configuration

```yaml
# RuntimeClass for Kata + Firecracker
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata-fc
handler: kata-fc

---
# OpenClaw Pod
apiVersion: v1
kind: Pod
metadata:
  name: openclaw-tenant-alice
spec:
  runtimeClassName: kata-fc
  containers:
  - name: openclaw
    image: openclaw-rootfs:v1.1
    ports:
    - containerPort: 18789
    resources:
      limits:
        cpu: "1"
        memory: 2Gi
    volumeMounts:
    - name: data
      mountPath: /home/agent
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: openclaw-alice-data
```

---

## Option C: EKS + Privileged Pod + Firecracker

```
User → K8s API → EKS → Worker Node → Privileged Pod → /dev/kvm → Firecracker → microVM
```

### Architecture
- EKS Worker Nodes with nested virtualization
- Each Pod runs privileged, mounting /dev/kvm
- Firecracker runs inside the Pod, launching microVMs
- Similar to current approach but with K8s replacing ASG + Lambda

### Pros
- ✅ Less migration effort than Option B
- ✅ Retains Firecracker microVM isolation
- ✅ K8s management

### Cons
- ❌ Privileged Pods are a security risk
- ❌ Nested Firecracker inside Pod is hard to debug
- ❌ Not K8s best practice
- ❌ Still requires nested virtualization instances

### Best For
- Quick PoC to validate EKS feasibility
- **Not recommended for production**

---

## Option D: EKS + gVisor (Reduced Isolation)

```
User → K8s API → EKS → Worker Node → gVisor Runtime → Container (OpenClaw)
```

### Architecture
- gVisor (runsc) replaces Firecracker
- gVisor intercepts syscalls in userspace — no KVM needed
- Isolation lower than microVM but higher than standard containers
- Works on any EC2 instance type

### Pros
- ✅ No nested virtualization required (any instance type)
- ✅ K8s-native RuntimeClass
- ✅ Minimal migration effort
- ✅ GKE native support, configurable on EKS

### Cons
- ❌ Isolation weaker than microVM (shared kernel)
- ❌ Syscall compatibility issues (some syscalls unsupported)
- ❌ Performance overhead (syscall interception)

### Best For
- Lower isolation requirements (internal use)
- No instance type restrictions needed
- Fastest path to K8s

---

## Option E: AgentCore Runtime (Fully Managed)

```
User → AgentCore API → AgentCore Runtime → microVM (Agent)
                        ↑ Fully managed, zero ops
```

### Architecture
- Deploy agents directly on AgentCore Runtime
- Each agent session runs in an isolated microVM
- Supports any framework (OpenClaw/Strands/LangGraph)
- Gateway + Memory + Code Interpreter + Browser fully managed

### Pros
- ✅ Zero ops (no EC2, no EKS)
- ✅ Auto-scaling (0 to hundreds of sessions)
- ✅ Native MCP support (stateful sessions)
- ✅ Built-in Memory/Gateway/Identity

### Cons
- ❌ Cannot customize rootfs (no pre-installed toolchain)
- ❌ Max session duration 8 hours (not for long-running agents)
- ❌ No persistent data volume
- ❌ Pay-per-use pricing, may cost more at scale than EC2
- ❌ No direct Dashboard access

### Best For
- Pure Serverless scenarios
- Short-lived tasks (conversations, code execution)
- No need for persistent agent instances

---

## Comparison Matrix

| Dimension | A. EC2+FC | B. EKS+Kata | C. EKS+Priv | D. EKS+gVisor | E. AgentCore |
|-----------|-----------|-------------|-------------|----------------|-------------|
| Isolation | ⭐⭐⭐ microVM | ⭐⭐⭐ microVM | ⭐⭐⭐ microVM | ⭐⭐ Kernel sandbox | ⭐⭐⭐ microVM |
| K8s Native | ❌ | ✅ | ⚠️ | ✅ | ❌ |
| Ops Complexity | Medium | High | Medium | Low | Lowest |
| Migration Effort | 0 (current) | 2–3 weeks | 1–2 weeks | 1 week | 1–2 weeks |
| Instance Constraint | Intel only | Intel only | Intel only | None | None (managed) |
| Persistent Data | ✅ | ✅ PVC | ✅ PVC | ✅ PVC | ❌ |
| Long-running | ✅ | ✅ | ✅ | ✅ | ❌ 8h limit |
| Cost | Low | Medium | Medium | Low | Pay-per-use |
| AgentCore | ✅ Integrated | ✅ Possible | ✅ Possible | ✅ Possible | ✅ Native |
| Production Ready | ✅ | ⚠️ Needs validation | ❌ | ⚠️ | ✅ |

---

## Recommended Path

```
Current: Option A (EC2 + Firecracker) — proven, production-ready
  │
  ├── Customer requires K8s → Option B (EKS + Kata) — 2–3 weeks migration
  │
  ├── Quick K8s validation → Option C (Privileged Pod) — PoC only
  │
  ├── Lower isolation OK → Option D (gVisor) — fastest to K8s
  │
  └── Pure Serverless → Option E (AgentCore Runtime) — zero ops
```

### Recommendations

1. **Short-term**: Continue with Option A — stable and proven
2. **Mid-term**: If customers require K8s, pursue Option B (Kata + Firecracker) with 2–3 weeks investment
3. **Long-term**: Monitor AgentCore Runtime evolution — if it adds custom rootfs + persistent volumes, consider Option E

### The Key Question for EKS Migration

> Does the customer want "running on EKS" or "managed by K8s"?

If the latter, consider a **hybrid approach**: EKS manages the control plane (API/Lambda containerized), while EC2 Node Groups (with nested virtualization) run Firecracker. This provides K8s management experience while preserving microVM isolation with minimal migration effort.
