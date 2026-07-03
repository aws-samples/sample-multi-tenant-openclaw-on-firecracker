# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""回归测试 — launch-vm.sh 对 openclaw.json 的安全加固注入。

守护 CHANGELOG 记录的安全不变量,防 bb 分支那批"为让小程序连上"的弱化补丁
(`chatCompletions.enabled=true` + `controlUi.allowedOrigins=["*"]` +
`dangerouslyDisableDeviceAuth`)复活。这些 jq 片段是部署代码的一部分,改 launch-vm.sh
时一旦回退就直接削弱每个 microVM 的对外攻击面,故用回归测试钉死。

被测对象: deploy/userdata/launch-vm.sh (jq 注入段,纯文本断言 + 默认路径 jq 实跑)。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_LAUNCH_VM = Path(__file__).resolve().parent.parent / "deploy/userdata/launch-vm.sh"


@pytest.fixture(scope="module")
def script_text():
    return _LAUNCH_VM.read_text()


class TestHardeningInvariants:
    """launch-vm.sh 的 jq 注入必须保留这些安全不变量。"""

    @pytest.mark.unit
    def test_default_path_deletes_chatcompletions(self, script_text):
        # 默认(per-tenant flag off)必须 del chatCompletions —— OpenClaw 安全默认
        assert "del(.gateway.http.endpoints.chatCompletions)" in script_text

    @pytest.mark.unit
    def test_deletes_dangerously_disable_device_auth(self, script_text):
        # 必须删除 dangerouslyDisableDeviceAuth(绕过 control UI 设备认证的弱化开关)
        assert "del(.gateway.controlUi.dangerouslyDisableDeviceAuth)" in script_text

    @pytest.mark.unit
    def test_allowed_origins_scoped_not_wildcard(self, script_text):
        # allowedOrigins 必须收窄到单个 CloudFront origin($origin),绝不是 ["*"]
        # (jq 程序嵌在 bash 双引号里,$ 被转义为 \$,故用正则兼容 \$origin / $origin)
        assert re.search(
            r"\.gateway\.controlUi\.allowedOrigins\s*=\s*\[\\?\$origin\]", script_text
        ), "allowedOrigins 必须收窄到 [$origin] 单一 CloudFront 源"
        # 反向:不得出现把 allowedOrigins 设成通配 "*" 的弱化补丁。
        # 只看非注释行 —— 脚本注释里有一行描述旧 fork 的弱化行为(讲"我们不这么做"),
        # 那是文档不是代码,不能误伤。
        code_lines = [
            ln for ln in script_text.splitlines() if not ln.lstrip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        assert not re.search(r'allowedOrigins\s*=\s*\[\s*"\*"\s*\]', code_only), (
            'launch-vm.sh 代码里出现 allowedOrigins=["*"] 通配弱化补丁(注释除外)'
        )

    @pytest.mark.unit
    def test_chatcompletions_enable_is_per_tenant_gated(self, script_text):
        # 启用 chatCompletions 只能走 per-tenant flag 分支(CHAT_EP_ENABLED),
        # 不能是无条件全局 enabled:true
        assert "CHAT_EP_ENABLED" in script_text
        enable_frag = ".gateway.http.endpoints.chatCompletions.enabled = true"
        assert enable_frag in script_text
        # 该片段必须落在 case 分支(per-tenant),其前是 1|true|... 的 case 标签
        idx = script_text.index(enable_frag)
        preceding = script_text[max(0, idx - 200) : idx]
        assert "1|true" in preceding or "CHAT_EP_ENABLED" in preceding


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
class TestDefaultJqProducesSecureConfig:
    """默认路径的 jq 片段实跑一遍,确认产出的 openclaw.json 是安全态。"""

    @pytest.mark.unit
    def test_default_jq_removes_chatcompletions_and_dangerous_flag(self, tmp_path):
        # 构造一个"被弱化过"的 openclaw.json,跑默认加固 jq,断言被收紧
        weak = {
            "gateway": {
                "auth": {"token": "old"},
                "http": {"endpoints": {"chatCompletions": {"enabled": True}}},
                "controlUi": {
                    "allowedOrigins": ["*"],
                    "dangerouslyDisableDeviceAuth": True,
                },
            },
            "channels": {},
        }
        src = tmp_path / "openclaw.json"
        src.write_text(json.dumps(weak))
        # 复刻 launch-vm.sh 默认路径的 jq(CHAT_EP off → del chatCompletions)
        jq_prog = (
            '.gateway.auth.token = "newtok" | '
            '.gateway.controlUi.allowedOrigins = ["https://d123.cloudfront.net"] | '
            "del(.gateway.controlUi.dangerouslyDisableDeviceAuth) | "
            "del(.gateway.http.endpoints.chatCompletions)"
        )
        out = subprocess.run(
            ["jq", jq_prog, str(src)], capture_output=True, text=True, check=True
        )
        cfg = json.loads(out.stdout)
        gw = cfg["gateway"]
        # chatCompletions 端点被删
        assert "chatCompletions" not in gw.get("http", {}).get("endpoints", {})
        # dangerouslyDisableDeviceAuth 被删
        assert "dangerouslyDisableDeviceAuth" not in gw["controlUi"]
        # allowedOrigins 收窄,不再是通配
        assert gw["controlUi"]["allowedOrigins"] == ["https://d123.cloudfront.net"]
        assert gw["controlUi"]["allowedOrigins"] != ["*"]
