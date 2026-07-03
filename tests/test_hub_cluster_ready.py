# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Go-live B1: WS 中枢上 EKS 多副本的代码前提守卫。

不真起集群,只守住"多 Pod 安全不变量"在代码/清单里成立:
- 集群模式下缺共享 token key 必须 fail-closed(否则跨 Pod token 验不过、每次换 Pod 掉线)
- SIGTERM 优雅 drain 存在(Pod 滚动不硬断聊天)
- Dockerfile 以非 root 运行
- k8s 清单用 ALB(用户拍板)+ HPA/PDB/多副本齐全
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HUB = ROOT / "deploy" / "hub" / "server.mjs"
DOCKERFILE = ROOT / "deploy" / "hub" / "Dockerfile"
MANIFEST = ROOT / "deploy" / "eks" / "claw-hub.yaml"


@pytest.mark.unit
class TestHubClusterReady:
    def test_clustered_requires_shared_token_key(self):
        src = HUB.read_text()
        assert "HUB_CLUSTERED" in src, "missing CLAW_HUB_CLUSTERED gate"
        # fail-closed: clustered + no shared key → process.exit(1)
        assert "process.exit(1)" in src, (
            "clustered hub must fail closed when CLAW_HUB_TOKEN_KEY is unset "
            "(a per-Pod random key breaks cross-Pod token verification)"
        )

    def test_graceful_sigterm_drain_present(self):
        src = HUB.read_text()
        assert 'process.on("SIGTERM"' in src, "missing SIGTERM graceful drain"
        assert "httpServer.close" in src, "drain must stop accepting new conns"

    def test_dockerfile_runs_non_root(self):
        df = DOCKERFILE.read_text()
        assert "USER node" in df, "hub container must run as non-root"
        assert "EXPOSE 8790" in df

    def test_manifest_uses_alb_and_ha_primitives(self):
        yaml = pytest.importorskip("yaml")
        docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
        kinds = {d.get("kind") for d in docs}
        for need in (
            "Deployment",
            "Ingress",
            "HorizontalPodAutoscaler",
            "PodDisruptionBudget",
        ):
            assert need in kinds, f"manifest missing {need}"
        ingress = next(d for d in docs if d.get("kind") == "Ingress")
        assert (
            ingress["metadata"]["annotations"]["kubernetes.io/ingress.class"] == "alb"
        ), "user-decided: ALB Ingress, not NLB"
        dep = next(d for d in docs if d.get("kind") == "Deployment")
        assert dep["spec"]["replicas"] >= 3, "≥3 replicas for no single point"
        # clustered env on the Pod
        envs = {
            e["name"]: e
            for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert envs.get("CLAW_HUB_CLUSTERED", {}).get("value") == "true"
        assert (
            "CLAW_HUB_TOKEN_KEY" in envs and "valueFrom" in envs["CLAW_HUB_TOKEN_KEY"]
        ), "shared token key must come from a Secret, not inline"
        assert envs.get("CLAW_HUB_SHARED_TENANT_ACCESS", {}).get("value") == "false", (
            "go-live A2: shared tenant access stays off in production"
        )
