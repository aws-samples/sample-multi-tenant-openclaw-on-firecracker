#!/usr/bin/env python3
# oc-guest-log-reader — host 侧 guest 日志收集器(独立 systemd service,以 root 跑)。
#
# 学 Lambda MicroVMs / FireLens:凭据/Firehose 全在 host 侧,guest 零凭据。本 reader
# 只把 guest 从 vsock 写来的字节落成 host 侧的 per-VM 文件,交给现成的 Fluent Bit
# tail 管道打 tenant_id → Firehose(不自己碰 Firehose,顺现有架构,见 ADR §4.1)。
#
# 连接方向:guest 主动 connect(HOST_CID=2, port) → Firecracker 落到
# ${VM_DIR}/<tid>/vsock.sock_<port>(AF_UNIX)。reader 在那 listen(host 侧,
# 每 VM 一个 listener 线程,keyed off VM_DIR glob)。guest 只 connect 不 bind。
#
# tenant_id 防伪(no-cross-tenant 命门):tenant_id 由 reader 从 UDS 所在 VM 目录名
# 推(host 权威,不信 guest 正文),host 重建 envelope,guest 每行当不透明 payload。
# 输出文件路径 <tid>/oc-guest.log 决定 Fluent Bit 侧 lua 再打一次 tenant_id(双保险)。

import glob
import json
import os
import re
import socket
import sys
import threading
import time

VM_DIR = "/data/firecracker-vms"
VSOCK_PORT = int(os.environ.get("OC_GUEST_LOG_VSOCK_PORT", "9999"))
SCAN_SEC = float(os.environ.get("OC_GUEST_LOG_SCAN_SEC", "10"))
OUT_NAME = "oc-guest.log"
OUT_NAME_SOCK = "vsock.sock"  # Firecracker uds_path basename(见 launch-vm PUT /vsock)
MAX_OUT_BYTES = int(os.environ.get("OC_GUEST_LOG_MAX_OUT_BYTES", str(50 * 1024 * 1024)))
# 单帧字节上限:必须 > forwarder 封装后的帧字节数。forwarder 的 line≤16384 字节,封装
# 成 {"source","path","line"} JSON 后约 16.5KB → reader 帧上限设 32768(2x 余量)才不会
# 把合法帧截成坏 JSON(#4)。仍 < fluent-bit 32k buffer(reader 输出 envelope 的 line
# 就是 guest 的 line≤16384,封装后 < 32k)。超限帧丢弃到下个换行,只产生一条截断记录。
MAX_FRAME = int(os.environ.get("OC_GUEST_LOG_MAX_FRAME", "32768"))
ACCEPT_TIMEOUT = 5.0
RECV_TIMEOUT = 5.0

# guest 发的 source 类别白名单:非白名单一律归 "oc-unknown",不信 guest 任意值。
_SOURCE_ALLOW = {"oc-runtime", "oc-audit", "oc-session", "oc-forwarder-meta"}

# tenant_id 安全字符集(同 extract_tenant_id.lua:16 的不变量:非法路径塌成 "-",不猜)
_TID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_listeners = {}  # tid -> Listener(每 VM 一个,keyed off VM 目录)
_lock = threading.Lock()


def _valid_tid(tid):
    return bool(tid) and _TID_RE.match(tid) is not None


class Listener(threading.Thread):
    """一个 VM 一个:listen 该 VM 的 vsock UDS,收行落 <tid>/oc-guest.log。"""

    def __init__(self, tid):
        super().__init__(daemon=True)
        self.tid = tid
        self.vm_dir = os.path.join(VM_DIR, tid)
        self.uds = os.path.join(self.vm_dir, f"{OUT_NAME_SOCK}_{VSOCK_PORT}")
        self.out_path = os.path.join(self.vm_dir, OUT_NAME)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _bind(self):
        """预 listen host 侧 AF_UNIX（Firecracker guest-connect 落点）。旧 socket 先清。"""
        try:
            os.unlink(self.uds)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.uds)
        s.listen(4)
        s.settimeout(ACCEPT_TIMEOUT)
        return s

    def _envelope(self, frame):
        """一条完整帧(guest 的 {source,path,line} JSON)→ host 权威顶层 envelope。
        tenant_id 由目录名(权威)打;source 过白名单;顶层出 log_source/line 供 FB
        JSON parser 直接取(不再把 source 埋在字符串 payload 里)。解析失败当原文行。"""
        line = frame.decode("utf-8", "replace")
        source, msg = "oc-unknown", line
        try:
            g = json.loads(line)
            if isinstance(g, dict):
                cand = g.get("source")
                source = (
                    cand
                    if (isinstance(cand, str) and cand in _SOURCE_ALLOW)
                    else "oc-unknown"
                )
                raw = g.get("line", g.get("dropped_lines", line))
                # guest 完全控制 line 值,可能是 dict/list/int → 强制字符串化,否则
                # 下游 _bound 的 .encode()/切片会崩,单条不可信帧能打死整个 listener(DoS)。
                msg = (
                    raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                )
        except (ValueError, TypeError):
            pass
        if not isinstance(msg, str):
            msg = str(msg)
        return self._bound(source, msg)

    def _bound(self, source, msg):
        """保证【最终编码后】envelope 字节 ≤ MAX_FRAME。控制字符 json.dumps 会 6x 膨胀
        (32KB 控制字符 → ~197KB),不限的话 Fluent Bit 会静默跳过整行(codex 阻断项)。
        超限就二分截断 msg,并标 truncated + original_bytes,不无痕丢。"""

        def enc(m, trunc):
            rec = {"tenant_id": self.tid, "log_source": source, "line": m}
            if trunc:
                rec["truncated"] = True
                rec["original_bytes"] = len(msg.encode("utf-8"))
            return json.dumps(rec, ensure_ascii=False)

        frame = enc(msg, False)
        if len(frame.encode("utf-8")) <= MAX_FRAME:
            return frame
        lo, hi = 0, len(msg)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(enc(msg[:mid], True).encode("utf-8")) <= MAX_FRAME:
                lo = mid
            else:
                hi = mid - 1
        return enc(msg[:lo], True)

    def _write_frames(self, frames):
        """按帧落盘(每帧已是完整一行)。超上限截空防撑满 host 盘,但【留痕】不静默丢:
        截空后先写一条 truncation-marker 记录(codex:不无痕丢)。正常情况 FB 边 tail
        边推(tail-ocguest.db 记位),文件不该到上限;到了说明 FB 没跟上。"""
        if not frames:
            return
        marker = None
        if (
            os.path.exists(self.out_path)
            and os.path.getsize(self.out_path) > MAX_OUT_BYTES
        ):
            dropped = os.path.getsize(self.out_path)
            # O_NOFOLLOW:root reader 不跟随 guest 可能布下的 symlink 写到别处(codex);
            # 截空也走 openat 语义,mode 0600。
            fd = os.open(
                self.out_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            os.close(fd)
            marker = json.dumps(
                {
                    "tenant_id": self.tid,
                    "log_source": "oc-reader-meta",
                    "line": "oc-guest.log exceeded %d bytes; truncated (Fluent Bit lag), dropped ~%d bytes"
                    % (MAX_OUT_BYTES, dropped),
                },
                ensure_ascii=False,
            )
        # O_NOFOLLOW + 0600:root reader 追加写不跟随 symlink,固定 owner-only 权限(codex)。
        _fd = os.open(
            self.out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(
            _fd, 0o600
        )  # 0o600 只约束【新建】文件;已有 0644 老文件要 fchmod 收紧(codex)
        with os.fdopen(_fd, "a", encoding="utf-8") as f:
            if marker:
                f.write(marker + "\n")
            for frame in frames:
                # per-frame 兜底:单条不可信帧再怎么畸形也只坏这一条,不中断整批/杀线程。
                try:
                    f.write(self._envelope(frame) + "\n")
                except Exception:  # noqa: BLE001 — 旁路日志,单帧 fail-safe 优先于 fail-loud
                    f.write(
                        json.dumps(
                            {
                                "tenant_id": self.tid,
                                "log_source": "oc-reader-meta",
                                "line": "dropped one malformed frame",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

    def _serve_one(self, srv):
        """accept 一条 guest 连接,按 \\n 切完整帧落盘。半帧留在 buf,不落断行(#3)。
        连接生命周期显式管超时(Kata #445);单帧超上限截断防内存膨胀。"""
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            return
        conn.settimeout(RECV_TIMEOUT)
        buf = b""
        skipping = False  # 超长帧:丢弃到下个换行,只产生一条截断记录(不随分包拆多条,#4)
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    break
                except OSError:
                    break  # connection reset/其他 socket 错:只结束【本连接】,不杀 listener(codex)
                if not chunk:
                    break
                buf += chunk
                if skipping:  # 正在丢弃超长帧的尾巴:找到换行才恢复正常切帧
                    _, sep, tail = buf.partition(b"\n")
                    if not sep:
                        buf = b""  # 还没换行,继续丢
                        continue
                    buf, skipping = tail, False
                # 按 \n 切:最后一段是未完成的半帧,留到下轮(不落断行,#3)。
                head, sep, tail = buf.rpartition(b"\n")
                if sep:
                    frames = [fr[:MAX_FRAME] for fr in head.split(b"\n") if fr]
                    self._write_frames(frames)
                    buf = tail
                if len(buf) > MAX_FRAME:  # 半帧超上限:落一条截断记录,余下丢到换行
                    self._write_frames([buf[:MAX_FRAME]])
                    buf, skipping = b"", True
        finally:
            conn.close()

    def run(self):
        try:
            srv = self._bind()
        except OSError as e:
            print(f"[reader] {self.tid} bind failed: {e}", file=sys.stderr)
            return  # 线程退出;reconcile 下轮 is_alive()=False 会重建(#6)
        try:
            while not self._stop_event.is_set():
                self._serve_one(srv)
        finally:
            srv.close()
            try:
                os.unlink(self.uds)
            except FileNotFoundError:
                pass


def _live_tids():
    """按 VM_DIR glob 枚举活 VM(含 vm.json、无 .stopped),同 host-agent 的判活口径。"""
    tids = set()
    for entry in glob.glob(os.path.join(VM_DIR, "*")):
        tid = os.path.basename(entry)
        if not _valid_tid(tid):
            continue
        if not os.path.isfile(os.path.join(entry, "vm.json")):
            continue
        if os.path.exists(os.path.join(entry, ".stopped")):
            continue
        tids.add(tid)
    return tids


def _reconcile():
    """新增/线程已死的 VM 起 listener,消失的 VM 停。死线程必重建(#6:bind/写盘异常
    终止线程但登记还在 → 不查 is_alive 就永久丢日志)。stop 后 join 再让位新 socket。"""
    live = _live_tids()
    with _lock:
        for tid in live:
            cur = _listeners.get(tid)
            if cur is not None and cur.is_alive():
                continue
            if cur is not None:  # 线程已死:join 回收(其 _bind 已 unlink 旧 socket)
                cur.stop()
                cur.join(timeout=1)
            lst = Listener(tid)
            lst.start()
            _listeners[tid] = lst
        for tid in set(_listeners) - live:
            dead = _listeners.pop(tid)
            dead.stop()
            dead.join(timeout=1)


def main():
    while True:
        _reconcile()
        time.sleep(SCAN_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
