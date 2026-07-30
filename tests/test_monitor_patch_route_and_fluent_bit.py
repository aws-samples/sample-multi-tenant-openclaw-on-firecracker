import importlib.util
from pathlib import Path
from subprocess import CompletedProcess


ROOT = Path(__file__).resolve().parents[1]


def _load_route_ops():
    path = ROOT / "deploy" / "userdata" / "route_ops.py"
    spec = importlib.util.spec_from_file_location("monitor_patch_route_ops", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bitmap_rebuild_replaces_cross_process_state(monkeypatch):
    route_ops = _load_route_ops()
    bitmap = route_ops.PortBitmap(low=10000, high=10002)
    bitmap.mark_used(10000)
    bitmap.mark_used(10001)
    monkeypatch.setattr(route_ops, "list_dnat_rules", lambda: {10002: "10.0.0.2"})

    assert route_ops.rebuild_bitmap_from_iptables(bitmap) == 1
    assert bitmap.snapshot() == {10002}


def test_delete_route_releases_live_and_legacy_rules(monkeypatch):
    route_ops = _load_route_ops()
    calls = []

    monkeypatch.setattr(route_ops, "rebuild_bitmap_from_iptables", lambda bitmap: 1)
    monkeypatch.setattr(
        route_ops,
        "release_port_and_dnat",
        lambda bitmap, port, guest: calls.append(("live", port, guest)),
    )
    monkeypatch.setattr(
        route_ops,
        "dnat_remove_all",
        lambda port, guest: calls.append(("legacy", port, guest)),
    )

    class Writer:
        def del_route(self, tenant_id):
            calls.append(("redis", tenant_id))
            return True

    monkeypatch.setattr(route_ops, "_cli_redis_writer", lambda: Writer())
    rc = route_ops._cli_delete_route(
        "tenant-1", 10001, "10.0.0.2", route_ops.GATEWAY_GUEST_PORT
    )

    assert rc == 0
    assert ("live", 10001, "10.0.0.2") in calls
    assert ("legacy", route_ops.GATEWAY_GUEST_PORT, "10.0.0.2") in calls
    assert ("redis", "tenant-1") in calls


def test_remove_all_does_not_hide_iptables_errors(monkeypatch):
    route_ops = _load_route_ops()
    monkeypatch.setattr(
        route_ops,
        "_run_iptables",
        lambda args: CompletedProcess(args, 4, "", "xtables lock unavailable"),
    )

    try:
        route_ops.dnat_remove_all(10000, "10.0.0.2")
    except RuntimeError as exc:
        assert "DNAT check failed" in str(exc)
    else:
        raise AssertionError("iptables errors must fail delete-route")


def test_control_plane_no_longer_creates_scheme_a_dnat():
    source = (
        ROOT / "deploy" / "lambda" / "api" / "core" / "ssm_dispatch.py"
    ).read_text()
    launch_body = source.split("def _launch_vm(", 1)[1].split(
        "\ndef _ssm_send(", 1
    )[0]
    assert "iptables" not in launch_body
    assert "launch-vm.sh" in launch_body


def test_delete_route_is_a_retryable_hard_gate():
    source = (
        ROOT / "deploy" / "lambda" / "api" / "services" / "tenant_service.py"
    ).read_text()
    delete_body = source.split("def delete_tenant(", 1)[1].split(
        "\ndef _reserve_migration_slot(", 1
    )[0]
    assert "route_ops.py delete-route" in delete_body
    assert "route cleanup failed (bitmap/DNAT/Redis)" in delete_body
    assert "_mark_delete_retryable()" in delete_body
    assert "route_ops.py del-route" not in delete_body


def test_edge_fluent_bit_inputs_are_injected_and_fail_closed():
    stack = (ROOT / "deploy" / "stacks" / "ha_edge.py").read_text()
    installer = (
        ROOT / "deploy" / "edge" / "fluent-bit" / "install-fluent-bit.sh"
    ).read_text()

    assert 'FIREHOSE_DELIVERY_STREAM="claw-logs{self._gsuffix}"' in stack
    assert 'ASSETS_BUCKET="{assets_bucket.bucket_name}"' in stack
    assert 'AWS_REGION="{self.region}"' in stack
    assert "unresolved FB_* placeholder(s)" in installer
    assert "delivery_stream rendered empty" in installer
    assert "--dry-run" in installer
    assert "systemctl is-active --quiet fluent-bit" in installer
