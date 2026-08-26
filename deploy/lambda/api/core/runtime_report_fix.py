"""修复 awslambdaric 4.0.2 导入期错误上报通道的编码问题。

根因链:
1. bootstrap.py:252 的 build_fault_result() 收集异常消息与带源码行的 stackTrace。
2. lambda_runtime_marshaller.py:24 用 ensure_ascii=False 生成含原始中文的 JSON。
3. lambda_runtime_client.py:82-89 的 call_rapid() 把 str body 交给 http.client,
   后者按 latin-1 编码,中文会触发二次 UnicodeEncodeError。
4. lambda_runtime_client.py:96 的 post_init_error() 正是经 call_rapid 上报导入期异常。

这是上报通道编码修复,不改业务异常文案。补丁必须 fail-open:任何安装异常都只退化成
awslambdaric 原有行为,不能反过来阻止 Lambda 启动。

两条载重假设,都从 4.0.2 源码确认过,改动前必须重新核对:

- **只影响 init/restore 错误上报,碰不到业务响应。** `to_json` 这个名字在
  lambda_runtime_client 里只有一处使用(:88,call_rapid 内),而 call_rapid 只服务
  post_init_error(:96)、restore_next(:118)、report_restore_error(:124)。正常调用的
  返回值走的是 LambdaMarshaller.marshal_response + C 扩展 post_invocation_result(:173),
  跟本补丁无关。所以替换它不会改任何 API 响应的序列化。
- **我们被 import 时 client 模块一定已在 sys.modules 里。** bootstrap.py:14 在模块作用域
  `from .lambda_runtime_client import LambdaRuntimeClient`,而它 import 我们的 handler 是
  在之后的 bootstrap.py:509 `_get_handler()` → importlib.import_module。所以这里只查
  sys.modules、不主动 import 是安全的(主动 import 会拖一个带 C 扩展的重依赖)。

另:ensure_ascii=False 那条分支由 AWS_EXECUTION_ENV 门控(marshaller :18-26,只在
AWS_Lambda_python3.12/3.13/3.14/3.15 生效)。本仓两个 api Lambda 都是 PYTHON_3_12
(deploy/stacks/lambdas.py:567 / :907),正好落在里面 —— 这解释了缺陷为什么现在才显形,
也说明换到门控外的 runtime 时本补丁会退化成无害的空操作。
"""

import json
import sys


def apply() -> str:
    """把已加载 runtime client 的 to_json 替换为 ASCII-safe 实现。"""
    try:
        client = sys.modules.get("awslambdaric.lambda_runtime_client")
        if client is None:
            return "skipped:client-module-missing"

        original_to_json = getattr(client, "to_json", None)
        if not callable(original_to_json):
            return "skipped:to-json-not-callable"
        if getattr(original_to_json, "_OPENCLAW_ASCII_SAFE", False):
            return "already-patched"

        def _ascii_safe_to_json(obj):
            try:
                return json.dumps(obj, ensure_ascii=True, default=str)
            except Exception:
                # 新实现遇到意外对象时回到原函数,维持 awslambdaric 原有容错边界。
                result = original_to_json(obj)
                if not isinstance(result, str):
                    return result
                try:
                    result.encode("latin-1")
                except UnicodeEncodeError:
                    return result.encode("unicode_escape").decode("ascii")
                return result

        setattr(_ascii_safe_to_json, "_OPENCLAW_ASCII_SAFE", True)
        setattr(_ascii_safe_to_json, "_OPENCLAW_ORIGINAL", original_to_json)
        setattr(client, "to_json", _ascii_safe_to_json)
        return "patched"
    except Exception as exc:
        # fail-open 是本补丁的安全边界:任何形状漂移或安装异常都不能阻止 Lambda 启动。
        return f"skipped:unexpected-{type(exc).__name__}"
