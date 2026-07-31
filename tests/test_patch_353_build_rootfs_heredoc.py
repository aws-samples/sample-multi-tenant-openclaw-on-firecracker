import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "patch" / "353-secret-ttl-plus-post315-rollup"
CURRENT_KIT = ROOT / "patch" / "monitor-patch"
BUILD_ROOTFS_FILES = (
    ROOT / "build-rootfs.sh",
    KIT / "launch-template" / "build-rootfs.sh.patched",
)


def test_gateway_unit_uses_literal_heredocs_with_explicit_execstart():
    for path in BUILD_ROOTFS_FILES:
        text = path.read_text()
        assert "openclaw-gateway.service << GWSVC" not in text
        assert "openclaw-gateway.service << 'GWSVC_HEAD'" in text
        assert "openclaw-gateway.service << 'GWSVC_BODY'" in text
        assert (
            "printf 'ExecStart=%s %s gateway --port 18789\\n' "
            '"$NODE_BIN" "$OC_DIST"'
        ) in text
        assert "unprivileged `agent` user" in text


def test_build_scripts_remain_valid_bash():
    for path in BUILD_ROOTFS_FILES:
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_historical_artifact_and_current_source_follow_their_manifests():
    source, artifact = BUILD_ROOTFS_FILES
    historical = json.loads((KIT / "manifest.json").read_text())
    current = json.loads((CURRENT_KIT / "manifest.json").read_text())

    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == historical["paths"][
        "build-rootfs.sh"
    ]["patch_sha256"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == current["paths"][
        "build-rootfs.sh"
    ]["patch_sha256"]
