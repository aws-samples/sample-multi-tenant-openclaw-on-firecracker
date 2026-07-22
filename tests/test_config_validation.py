# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for _normalize_config in deploy/stack.py — the balloon/overcommit guard."""

import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_normalize():
    """Extract _normalize_config from stack.py without importing the whole CDK stack."""
    src = Path(__file__).parent.parent / "deploy" / "stack.py"
    text = src.read_text()
    m = re.search(r"(def _normalize_config.*?)(\n\n_normalize_config)", text, re.S)
    assert m, "_normalize_config not found in stack.py"
    ns = {}
    exec(m.group(1), ns)  # noqa: S102 — trusted local source
    return ns["_normalize_config"]


normalize = _load_normalize()


def test_clamps_overcommit_when_balloon_off():
    cfg = {"host": {"mem_overcommit_ratio": 1.5}, "balloon": {"enabled": False}}
    normalize(cfg)
    assert cfg["host"]["mem_overcommit_ratio"] == 1.0


def test_keeps_overcommit_when_balloon_on():
    cfg = {"host": {"mem_overcommit_ratio": 1.5}, "balloon": {"enabled": True}}
    normalize(cfg)
    assert cfg["host"]["mem_overcommit_ratio"] == 1.5


def test_no_change_when_ratio_is_one():
    cfg = {"host": {"mem_overcommit_ratio": 1.0}, "balloon": {"enabled": False}}
    normalize(cfg)
    assert cfg["host"]["mem_overcommit_ratio"] == 1.0


def test_defaults_are_safe_when_keys_missing():
    cfg = {}
    normalize(cfg)  # must not raise; missing keys mean ratio 1.0 / balloon off
    assert cfg.get("host", {}).get("mem_overcommit_ratio", 1.0) == 1.0


def test_warns_when_clamping(capsys):
    cfg = {"host": {"mem_overcommit_ratio": 2.0}, "balloon": {"enabled": False}}
    normalize(cfg)
    out = capsys.readouterr().out
    assert "balloon.enabled=true" in out and "ignored" in out


# ── Issue #77: overcommit-ratio safety ceiling (guards against the 8.0 drift) ──

def test_clamps_cpu_ratio_above_ceiling():
    cfg = {"host": {"cpu_overcommit_ratio": 8.0}, "balloon": {"enabled": True}}
    normalize(cfg)
    assert cfg["host"]["cpu_overcommit_ratio"] == 4.0


def test_keeps_cpu_ratio_within_ceiling():
    cfg = {"host": {"cpu_overcommit_ratio": 2.0}, "balloon": {"enabled": True}}
    normalize(cfg)
    assert cfg["host"]["cpu_overcommit_ratio"] == 2.0


def test_clamps_mem_ratio_above_ceiling_when_balloon_on():
    # balloon on so it isn't forced to 1.0 first; 6.0 > 4.0 ceiling → 4.0
    cfg = {"host": {"mem_overcommit_ratio": 6.0}, "balloon": {"enabled": True}}
    normalize(cfg)
    assert cfg["host"]["mem_overcommit_ratio"] == 4.0


def test_warns_when_clamping_cpu_ceiling(capsys):
    cfg = {"host": {"cpu_overcommit_ratio": 8.0}, "balloon": {"enabled": True}}
    normalize(cfg)
    out = capsys.readouterr().out
    assert "cpu_overcommit_ratio" in out and "ceiling" in out
