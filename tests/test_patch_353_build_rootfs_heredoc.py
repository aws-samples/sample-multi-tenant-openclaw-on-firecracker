import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "patch" / "353-secret-ttl-plus-post315-rollup"
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


def test_artifact_and_manifest_hash_follow_canonical_source():
    source, artifact = BUILD_ROOTFS_FILES
    manifest = json.loads((KIT / "manifest.json").read_text())
    expected = manifest["paths"]["build-rootfs.sh"]["patch_sha256"]

    assert source.read_bytes() == artifact.read_bytes()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
