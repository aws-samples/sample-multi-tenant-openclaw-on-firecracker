import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "patch" / "353-secret-ttl-plus-post315-rollup"
CURRENT_KIT = ROOT / "patch" / "monitor-patch"

BUILD_ROOTFS_FILES = (
    ROOT / "build-rootfs.sh",
    KIT / "launch-template" / "build-rootfs.sh.patched",
)
FLUENT_BIT_FILES = (
    ROOT / "deploy" / "edge" / "fluent-bit" / "host" / "fluent-bit.conf",
    KIT / "host-scripts" / "fluent-bit" / "fluent-bit.conf",
)


def _forwarder_unit(path: Path) -> str:
    text = path.read_text()
    marker = (
        "cat > /usr/lib/systemd/user/openclaw-log-forwarder.service "
        "<< 'FWDSVC'\n"
    )
    return text.split(marker, 1)[1].split("\nFWDSVC", 1)[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_guest_forwarder_avoids_user_manager_capability_setup():
    for path in BUILD_ROOTFS_FILES:
        unit = _forwarder_unit(path)
        assert "NoNewPrivileges=true" in unit
        assert "CapabilityBoundingSet=" not in unit


def test_host_journal_filters_use_or_semantics():
    for path in FLUENT_BIT_FILES:
        text = path.read_text()
        assert "Systemd_Filter      _SYSTEMD_UNIT=host-agent.service" in text
        assert "Systemd_Filter      SYSLOG_IDENTIFIER=claw-launch" in text
        assert "Systemd_Filter_Type Or" in text


def test_historical_artifacts_and_current_sources_match_their_manifests():
    historical = json.loads((KIT / "manifest.json").read_text())
    current = json.loads((CURRENT_KIT / "manifest.json").read_text())
    pairs = (
        (
            BUILD_ROOTFS_FILES,
            "build-rootfs.sh",
        ),
        (
            FLUENT_BIT_FILES,
            "deploy/edge/fluent-bit/host/fluent-bit.conf",
        ),
    )

    for (source, artifact), manifest_path in pairs:
        assert _sha256(artifact) == historical["paths"][manifest_path]["patch_sha256"]
        assert _sha256(source) == current["paths"][manifest_path]["patch_sha256"]
