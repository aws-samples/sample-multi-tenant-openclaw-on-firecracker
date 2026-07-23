# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deploy/lambda/agentcore_tools/handler.py."""

import importlib.util
import json
import sys

import pytest

spec = importlib.util.spec_from_file_location("ac_tools", "deploy/lambda/agentcore_tools/handler.py")
ac = importlib.util.module_from_spec(spec)
sys.modules["ac_tools"] = ac
spec.loader.exec_module(ac)


class TestHello:
    @pytest.mark.unit
    def test_default_name(self):
        result = ac.lambda_handler({"toolName": "hello", "arguments": {}}, None)
        assert "Hello, World!" in result["message"]
        assert "AgentCore Gateway" in result["message"]

    @pytest.mark.unit
    def test_custom_name(self):
        result = ac.lambda_handler({"toolName": "hello", "arguments": {"name": "Alice"}}, None)
        assert "Hello, Alice!" in result["message"]

    @pytest.mark.unit
    def test_alternate_event_format(self):
        """Support both toolName/arguments and name/input formats."""
        result = ac.lambda_handler({"name": "hello", "input": {"name": "Bob"}}, None)
        assert "Hello, Bob!" in result["message"]


class TestSystemInfo:
    @pytest.mark.unit
    def test_returns_runtime_info(self):
        result = ac.lambda_handler({"toolName": "system_info", "arguments": {}}, None)
        assert result["runtime"] == "AWS Lambda"
        assert "python" in result

    @pytest.mark.unit
    def test_has_region(self):
        result = ac.lambda_handler({"toolName": "system_info", "arguments": {}}, None)
        assert "region" in result


class TestTimestamp:
    @pytest.mark.unit
    def test_iso_format(self):
        result = ac.lambda_handler({"toolName": "timestamp", "arguments": {"format": "iso"}}, None)
        assert "T" in result["timestamp"]  # ISO 8601 contains T

    @pytest.mark.unit
    def test_unix_format(self):
        result = ac.lambda_handler({"toolName": "timestamp", "arguments": {"format": "unix"}}, None)
        assert isinstance(result["timestamp"], int)
        assert result["timestamp"] > 1700000000  # After 2023

    @pytest.mark.unit
    def test_default_is_iso(self):
        result = ac.lambda_handler({"toolName": "timestamp", "arguments": {}}, None)
        assert "T" in result["timestamp"]


class TestUnknownTool:
    @pytest.mark.unit
    def test_unknown_tool_returns_error(self):
        result = ac.lambda_handler({"toolName": "nonexistent", "arguments": {}}, None)
        assert "error" in result
        assert "available" in result
        assert "hello" in result["available"]

    @pytest.mark.unit
    @pytest.mark.regression
    def test_empty_tool_name(self):
        result = ac.lambda_handler({"toolName": "", "arguments": {}}, None)
        assert "error" in result
