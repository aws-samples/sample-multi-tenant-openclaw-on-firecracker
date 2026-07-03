# claw-hub container (go-live B1: WS 中枢从 metal 单进程 → EKS Fargate 多副本).
# Multi-stage: install prod deps, then a slim runtime running as non-root.
# Build (from deploy/hub/):
#   docker build -t claw-hub:latest .
# The hub is stateless across Pods ONLY when CLAW_HUB_CLUSTERED=true +
# a shared CLAW_HUB_TOKEN_KEY (Secrets Manager) + ElastiCache for the cross-Pod
# routing table are wired (see develop-dev/EKS-dataplane-design.md). This image
# just packages server.mjs; the clustering env is injected by the k8s Deployment.

FROM public.ecr.aws/docker/library/node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
# no package-lock in repo; install prod deps deterministically by package.json
RUN npm install --omit=dev --no-audit --no-fund

FROM public.ecr.aws/docker/library/node:20-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
# run as the built-in unprivileged 'node' user (uid 1000), never root
COPY --from=deps /app/node_modules ./node_modules
COPY package.json server.mjs cluster-routing.mjs ./
USER node
EXPOSE 8790
# SIGTERM is handled in server.mjs for graceful drain on Pod rolling.
# tini-less: node is PID 1; the explicit SIGTERM/SIGINT handlers in server.mjs
# perform the drain, so we don't need an init shim for signal forwarding.
CMD ["node", "server.mjs"]
