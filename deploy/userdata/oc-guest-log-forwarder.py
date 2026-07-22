#!/usr/bin/env python3
# oc-guest-log-forwarder — guest 内日志转发器(烤进镜像,以 agent 用户跑)。
#
# 把 OpenClaw 三类日志 tail 出来,guest 主动 connect vsock 发给 host 侧 reader。
# guest 零凭据:只往本地 vsock 写,凭据/Firehose 全在 host 侧(学 Lambda/FireLens)。
# 背压兜底:非阻塞 connect+send,超时/失败丢行 + 计数,绝不阻塞(日志是旁路,
# host 收不动也不能拖死 gateway,Fly.io 事故教训)。
#
# 连接方向:guest connect(HOST_CID=2, VSOCK_PORT) → Firecracker 落到 host 的
# ${VM_DIR}/vsock.sock_<port>,host reader 在那 listen(guest 只 connect 不 bind)。
# host 侧按 UDS 路径打权威 tenant_id,本 forwarder 不打 tenant 身份(不可信)。
#
# framing:每帧 = 一行 JSON + "\n"。**按编码后字节**限帧(MAX_FRAME_BYTES),控制字符
# JSON 转义会 6x 膨胀,必须量最终帧不是原始行(codex);两端字节上限一致(reader 32768)。
# 游标持久化到 data 盘(CURSOR_FILE),重启不从头重放全部审计/session 日志(codex)。

import glob
import json
import os
import socket
import sys
import time

HOST_CID = 2  # Firecracker 固定的 host CID
VSOCK_PORT = int(os.environ.get("OC_GUEST_LOG_VSOCK_PORT", "9999"))
POLL_SEC = float(os.environ.get("OC_GUEST_LOG_POLL_SEC", "2"))
SEND_TIMEOUT = float(os.environ.get("OC_GUEST_LOG_SEND_TIMEOUT", "0.5"))
MAX_FRAME_BYTES = int(
    os.environ.get("OC_GUEST_LOG_MAX_FRAME_BYTES", "30000")
)  # < FB 32k
MAX_BATCH_BYTES = int(os.environ.get("OC_GUEST_LOG_MAX_BATCH_BYTES", str(1024 * 1024)))
READ_CHUNK = 262144  # 每文件每轮最多读的字节数(防大文件一次性吃满内存)
HOME = os.environ.get("HOME", "/home/agent")
CURSOR_FILE = os.environ.get(
    "OC_GUEST_LOG_CURSOR", f"{HOME}/.openclaw/.oc-fwd-cursor.json"
)

# 三类日志源(glob 模式)。host 侧 log_source 由 category 区分。
SOURCES = [
    ("oc-runtime", "/tmp/openclaw/openclaw-*.log"),
    ("oc-audit", f"{HOME}/.openclaw/logs/*.jsonl"),
    ("oc-session", f"{HOME}/.openclaw/agents/*/sessions/*.jsonl"),
]

# per-inode 状态:{"dev:ino": {"offset": int, "skip": bool}}。skip=丢弃至下个换行
# (超长行截断后,剩余尾巴丢掉,只产生一条截断记录)。启动时从 CURSOR_FILE 载入。
_state = {}
_dropped = 0  # 累计丢行数(host 收不动),周期性上报


def _load_cursor():
    """载入持久化状态。返回 (files_cursor, dropped_total)。兼容旧格式(纯 files dict)。"""
    try:
        with open(CURSOR_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}, 0
    if isinstance(d, dict) and "files" in d:
        return d.get("files", {}), int(d.get("dropped", 0))
    return d if isinstance(d, dict) else {}, 0  # 旧格式:纯 files dict


def _save_cursor():
    """原子持久化游标 + 丢行累计到 data 盘(tmp + fsync + rename)。重启不从 offset 0
    重放全部日志;_dropped 也随之持久化,重启计数不归零(codex)。"""
    tmp = CURSOR_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"files": _state, "dropped": _dropped}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CURSOR_FILE)
    except OSError:
        pass  # 持久化失败不致命(旁路);最坏重启多重放一轮,不阻塞业务


def _encode_frame(category, path, line):
    return json.dumps(
        {"source": category, "path": path, "line": line}, ensure_ascii=False
    )


def _rec(category, path, raw_line):
    """封装成 host 待收 JSON 帧,保证【编码后】字节 ≤ MAX_FRAME_BYTES(控制字符会膨胀,
    必须量最终帧;超限就二分截断 line 直到帧装得下,避免截出坏 JSON / 超 FB 上限)。"""
    line = raw_line.decode("utf-8", "replace")
    frame = _encode_frame(category, path, line)
    if len(frame.encode("utf-8")) <= MAX_FRAME_BYTES:
        return frame
    lo, hi = 0, len(line)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if (
            len(_encode_frame(category, path, line[:mid]).encode("utf-8"))
            <= MAX_FRAME_BYTES
        ):
            lo = mid
        else:
            hi = mid - 1
    return _encode_frame(category, path, line[:lo])


def _emit(category, path, raw_line, out):
    """封装一行并计入 out;返回该帧字节数(用于 batch 字节预算,量编码后帧不是原始行)。"""
    frame = _rec(category, path, raw_line)
    out.append(frame)
    return len(frame.encode("utf-8")) + 1


def _process_chunk(data, category, path, out, out_bytes, stt):
    """处理一块二进制:按 \\n 切;超长行截断成一条 + 进 skip;半行留到下轮。改 stt 就地。
    返回消费字节数 + 累计字节。用 per-inode skip 状态,不再靠有 bug 的 full_read(codex #2)。"""
    consumed = 0
    if stt["skip"]:  # 丢弃超长行的尾巴,直到下个换行
        nl = data.find(b"\n")
        if nl < 0:
            return len(data), out_bytes  # 整块都丢,继续 skip
        consumed = nl + 1
        stt["skip"] = False
        data = data[consumed:]
    while data and out_bytes < MAX_BATCH_BYTES:
        nl = data.find(b"\n")
        if nl < 0:  # 半行
            if len(data) > MAX_FRAME_BYTES:  # 超长且未完:截一条 + 丢余下至换行
                out_bytes += _emit(category, path, data, out)
                consumed += len(data)
                stt["skip"] = True
            break  # 普通半行:留到下轮(不消费)
        line = data[:nl]
        consumed += nl + 1
        if line:
            out_bytes += _emit(category, path, line, out)
        data = data[nl + 1 :]
    return consumed, out_bytes


def _read_new_lines(path, category, out, out_bytes):
    """二进制读某文件新增整行。按 (dev,inode) 跟踪字节游标(持久化),轮转/截断不跳错位。"""
    try:
        st = os.stat(path)
    except OSError:
        return out_bytes
    key = f"{st.st_dev}:{st.st_ino}"
    stt = _state.setdefault(key, {"offset": 0, "skip": False})
    if stt["offset"] > st.st_size:  # 文件被截断 → 从头
        stt["offset"] = 0
        stt["skip"] = False
    if stt["offset"] >= st.st_size or out_bytes >= MAX_BATCH_BYTES:
        return out_bytes
    try:
        with open(path, "rb") as f:
            f.seek(stt["offset"])
            data = f.read(min(READ_CHUNK, st.st_size - stt["offset"]))
    except OSError:
        return out_bytes
    consumed, out_bytes = _process_chunk(data, category, path, out, out_bytes, stt)
    stt["offset"] += consumed
    return out_bytes


def _collect_batch():
    """扫三源新增行,组成 host 待收的 payload 列表(限总字节,防 OOM)。"""
    out, out_bytes = [], 0
    for category, pattern in SOURCES:
        for path in glob.glob(pattern):
            out_bytes = _read_new_lines(path, category, out, out_bytes)
            if out_bytes >= MAX_BATCH_BYTES:
                return out
    return out


def _send_batch(batch):
    """guest 主动 connect vsock 发一批行。非阻塞超时:失败/超时丢整批并计数,不阻塞。"""
    global _dropped
    if not batch:
        return
    # socket 创建本身可能失败(内核无 AF_VSOCK / 无 /dev/vsock)。日志是旁路,任何
    # 失败都必须 fail-safe 成丢行,绝不让 forwarder 崩溃/阻塞 gateway(不拖死业务)。
    try:
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    except (OSError, AttributeError):
        _dropped += len(batch)
        return
    s.settimeout(SEND_TIMEOUT)
    try:
        s.connect((HOST_CID, VSOCK_PORT))
    except (OSError, socket.timeout):
        _dropped += len(batch)  # host reader 没起/没接:丢行,不阻塞业务
        s.close()
        return
    sent = 0
    for line in batch:
        try:
            s.sendall((line + "\n").encode("utf-8", "replace"))
            sent += 1
        except (OSError, socket.timeout):
            break  # 写超时:剩余丢弃,下轮重连
    _dropped += len(batch) - sent
    s.close()


def _drain_once(last_report):
    """一轮:收新行、附带丢行 meta、发送、持久化游标。返回更新后的 last_report。"""
    global _dropped
    batch = _collect_batch()
    now = time.monotonic()
    if _dropped and now - last_report >= 30:
        batch.append(
            json.dumps(
                {"source": "oc-forwarder-meta", "dropped_lines": _dropped},
                ensure_ascii=False,
            )
        )
        last_report = now
    _send_batch(batch)
    _save_cursor()  # 每轮持久化,重启从上次 offset 续,不重放全部(codex #3)
    return last_report


def main():
    global _state, _dropped
    _state, _dropped = _load_cursor()  # 恢复文件游标 + 丢行累计(重启不归零)
    last_report = 0.0
    while True:
        last_report = _drain_once(last_report)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
