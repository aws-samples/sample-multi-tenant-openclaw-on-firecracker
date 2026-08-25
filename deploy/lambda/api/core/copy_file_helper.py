# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#335 — copy-file-from-s3 的 host 侧写入器:逐组件 O_NOFOLLOW + 目录 fd,关掉 TOCTOU。

**这个文件不在 Lambda 里执行**,它只被 Lambda【读源码】:
host_service.copy_file_from_s3() 把本文件原样内联进 SSM 脚本的 quoted heredoc,
真正跑它的是 host 上的 python3(以 root)。之所以放在 Lambda 包内而不是
deploy/userdata/:API Lambda 的资产边界是 `Code.from_asset("deploy/lambda/api")`
(deploy/stacks/lambdas.py:379),包外的文件一个字节都不进部署包 —— 放 userdata/
运行时读不到。每次调用都下发【本次部署包里的字节】,host 磁盘上不留副本,
所以 helper 自身也没有被 host 上的人替换的窗口。

## 修的是什么

字符串**、不是文件句柄;随后的 `mkdir -p` / `aws s3 cp` / `chown` / `mv` 每一步都
拿这个字符串**重新解析一遍**,check 与 use 之间没有任何东西把二者钉在同一个 inode 上。
host 上有 ubuntu 级代码执行的攻击者只要在两次解析之间把某一级目录换成软链,root
身份的写入就落到白名单外(#335;最宽的窗口跨越 `aws s3 cp` 的整个 S3 网络往返)。

## 怎么修

整条路径**只走一次**,而且走的不是路径、是目录 fd:

* 逐段 `os.open(comp, O_RDONLY|O_DIRECTORY|O_NOFOLLOW, dir_fd=上一级)` —— 任一段是
  软链立刻 fail-loud,不存在"解析成什么"这回事。errno 实测是 `ENOTDIR`(不是 `ELOOP`):
  `O_DIRECTORY` 的类型检查先于 `O_NOFOLLOW` 的软链检查,单独用 `O_NOFOLLOW` 才是 `ELOOP`
  —— 在 Linux 6.17(真机)与 macOS 上都一样,所以报错信息把 errno 名原样带出来,不写死;
* 之后所有操作(建临时文件 / fchown / fchmod / rename / unlink)**全部相对最后那个
  目录 fd**,结构性地没有第二次路径解析可以被换掉;
* `O_CREAT|O_EXCL` 让预埋的同名文件/软链直接 `EEXIST`(不复用、不跟随);
* 同目录 `rename(dst_dir_fd=)` 保住 #334 的原子写 —— rename(2) 不跟随软链。

**不用 `openat2(RESOLVE_NO_SYMLINKS)`**(issue 里列的首选):Python 无 stdlib 绑定,
要 ctypes 手搓 `syscall(437)` + `struct open_how`,把实现绑死在 Linux ≥ 5.6 与具体
arch 上;而它多给的 `RESOLVE_BENEATH` 防的是 `..`,那个已在 API 层
(host_service._validate_copy_target)拒掉,逐段 split 之后 `..` 根本不进 syscall。

## 参数(环境变量,不进 argv —— host 上 `ps` 看不到 s3_uri/目标)

`OC_COPY_ROOT` 白名单根(绝对路径,信任锚点) · `OC_COPY_REL` 根下相对路径 ·
`OC_COPY_S3_URI` · `OC_COPY_REGION` · `OC_COPY_OWNER` 落地属主 `user:group`。
失败一律 fail-loud:stderr 带 `[copy-file] ` 前缀 + 退出码 1,由 SSM 带回
host_service 的 502 `COPY_FAILED`(对外返回契约不变)。
"""

import errno
import grp
import os
import pwd
import secrets
import stat
import subprocess
import sys

FILE_MODE = 0o755
#: 缺失的中间层级按这个模式建(与旧 shell `mkdir -p` 的 root umask 022 结果一致)。
DIR_MODE = 0o755

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

#: 测试注入点,签名 `(depth, dir_fd)`,每打开一层目录后触发(depth=0 是白名单根)。
#: 为什么需要它:窗口宽度取决于 S3 网络往返与 host 调度,进程外抢不稳定 —— 抢不到只
#: 证明"这次没抢到"。对抗测试用这个钩子在【已持有 fd 之后】做原子替换,断言写入仍落在
#: helper 源码每次由 Lambda 现下发、host 上不落盘,攻击者也没有路径能设它。
_RACE_HOOK = None


def _ename(exc):
    """OSError → errno 名(ELOOP / ENOTDIR / EEXIST …),日志里比裸数字可读。"""
    return errno.errorcode.get(exc.errno, str(exc.errno))


def _fail(msg):
    """fail-loud:stderr 带 `[copy-file] ` 前缀 + 退出 1(SSM 据此判 Failed → 502)。"""
    raise SystemExit("[copy-file] " + msg)


def _hook(depth, dir_fd):
    if _RACE_HOOK is not None:
        _RACE_HOOK(depth, dir_fd)


def _env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        _fail("refused: %s is required" % name)
    return value


def _owner_ids(owner):
    """`user:group` → (uid, gid)。在 **host 上**解析,不在 Lambda 里猜数字 id。"""
    if ":" not in owner:
        _fail("refused: OC_COPY_OWNER must be 'user:group', got %r" % owner)
    user, group = owner.split(":", 1)
    try:
        return pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid
    except KeyError as exc:
        _fail("cannot resolve owner %r on this host: %s" % (owner, exc))


def _split_rel(root, rel):
    """rel 拆成路径段,并在 helper 自己的入口再校验一遍。

    API 层 `_validate_copy_target` 已拒 `..`/尾斜杠/裸根;这里重校验不是冗余而是边界
    纪律 —— helper 是独立程序,被单独调用时也必须自己守住入口。
    """
    if rel.startswith("/"):
        _fail("refused: OC_COPY_REL must be relative, got %r" % rel)
    comps = [c for c in rel.split("/") if c]  # POSIX:`a//b` 与 `a/b` 同义
    if not comps:
        _fail("refused: empty OC_COPY_REL under %s" % root)
    if ".." in comps:
        _fail("refused: '..' in OC_COPY_REL: %r" % rel)
    return comps


def _open_root(root):
    """打开白名单根 —— **唯一允许跟随软链的一步**,这是信任锚点。

    为什么可以:两处根的父目录(`/home`、`/opt`)是 root:root 0755,非 root 改不了
    `/home/ubuntu` / `/opt/openclaw` 这个**名字**(rename 要父目录的写权限)。锚点
    之下每一段都不再跟随软链。
    """
    if not root.startswith("/"):
        _fail("refused: OC_COPY_ROOT must be absolute, got %r" % root)
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        _fail("cannot open allowed root %s: %s" % (root, _ename(exc)))


def _descend(dir_fd, comp, where):
    """打开(必要时先建)`dir_fd` 下的 `comp` 目录,返回新的目录 fd。

    `O_NOFOLLOW`:comp 是软链 → 立刻 fail-loud,绝不"解析后再判断";comp 是普通
    文件 → ENOTDIR,同样 fail-loud。所以白名单根之下不存在任何软链逃逸路径。
    """
    def _open():
        return os.open(comp, _DIR_FLAGS, dir_fd=dir_fd)

    def _refuse(exc):
        _fail(
            "refused: path component %r under %s is a symlink or not a directory "
            "(errno=%s)" % (comp, where, _ename(exc))
        )

    try:
        return _open()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _refuse(exc)
    try:
        os.mkdir(comp, DIR_MODE, dir_fd=dir_fd)
    except FileExistsError:
        pass  # 并发的另一次 copy 刚建好 —— 与 `mkdir -p` 同语义,继续按 fd 打开
    except OSError as exc:
        _fail("cannot create directory %r under %s: %s" % (comp, where, _ename(exc)))
    try:
        return _open()
    except OSError as exc:
        _refuse(exc)


def _refuse_dir_or_symlink_target(dir_fd, base):
    """保住 #334 的对外契约:目标已存在为**目录**或**软链** → 502 带明确原因。

    这**不是**安全检查(所以它被竞态换掉也无所谓):安全性来自下面的
    `O_CREAT|O_EXCL` + 同目录 `rename(2)` —— rename 不跟随软链,写入永远落在这个
    dir_fd 里,与这里看到什么无关。它只负责把"你在覆盖一个目录/软链"按老契约报出来,
    而不是默默替换掉对方。
    """
    try:
        st = os.stat(base, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        _fail("cannot stat target %r: %s" % (base, _ename(exc)))
    if stat.S_ISDIR(st.st_mode):
        _fail("target is an existing directory: %s" % base)
    if stat.S_ISLNK(st.st_mode):
        _fail("target is a symlink (refused): %s" % base)


def _download(fd, s3_uri, region):
    """`aws s3 cp <uri> -`:对象内容直接写进**我们持有的 fd**。

    不落 awscli 自己的临时文件、不给出第二条可被替换的路径。失败把 awscli 的
    stderr 原文带出去(502 的 `error` 就是它)。
    """
    cmd = ["aws", "s3", "cp", s3_uri, "-", "--region", region, "--no-progress"]
    proc = subprocess.run(cmd, stdout=fd, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().replace("\n", " ")
        _fail("aws s3 cp failed (rc=%d): %s" % (proc.returncode, detail[-300:]))


def _write(dir_fd, base, s3_uri, region, uid, gid):
    """在 `dir_fd` 里下载到临时文件 → fchown/fchmod → 同目录 rename 到 base。

    全部相对 dir_fd,没有一次路径解析。临时名带 pid + 随机后缀:并发不撞名;
    `O_EXCL` 让预埋的同名文件/软链直接 EEXIST。失败清理临时文件、**保留旧文件**。
    """
    tmp = ".copy-file.%s.%d.%s.tmp" % (base, os.getpid(), secrets.token_hex(8))
    try:
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        _fail("cannot create temp file %r: %s" % (tmp, _ename(exc)))
    renamed = False
    try:
        _download(fd, s3_uri, region)
        os.fchown(fd, uid, gid)  # #334 属主纠正:作用在 fd 上,不是路径
        os.fchmod(fd, FILE_MODE)
        os.close(fd)
        fd = -1
        os.rename(tmp, base, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        renamed = True
    finally:
        if fd >= 0:
            os.close(fd)
        if not renamed:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass  # 清理是 best-effort:真实失败原因正在传播,别把它盖掉


def copy_file(root, rel, s3_uri, region, uid, gid):
    """把 `s3_uri` 的对象写到 `<root>/<rel>`,全程目录 fd 相对,零路径重走。

    失败 raise SystemExit(fail-loud)。中间层级缺失则逐段创建(等价旧 `mkdir -p`,
    但每段建完立刻用 O_NOFOLLOW 打开确认它就是我们建的那个目录)。
    """
    comps = _split_rel(root, rel)
    base = comps[-1]
    fds = [_open_root(root)]
    where = root
    try:
        _hook(0, fds[-1])
        for depth, comp in enumerate(comps[:-1], start=1):
            fds.append(_descend(fds[-1], comp, where))
            where = "%s/%s" % (where, comp)
            _hook(depth, fds[-1])
        _refuse_dir_or_symlink_target(fds[-1], base)
        _write(fds[-1], base, s3_uri, region, uid, gid)
    finally:
        for fd in fds:
            os.close(fd)


def main():
    root = _env("OC_COPY_ROOT").rstrip("/")
    rel = _env("OC_COPY_REL")
    s3_uri = _env("OC_COPY_S3_URI")
    region = _env("OC_COPY_REGION")
    owner = _env("OC_COPY_OWNER")
    uid, gid = _owner_ids(owner)
    copy_file(root, rel, s3_uri, region, uid, gid)
    print(
        "[copy-file] %s -> %s/%s OK (dir_fd + O_NOFOLLOW, atomic rename, "
        "chown %s, chmod %04o)" % (s3_uri, root, rel, owner, FILE_MODE)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
