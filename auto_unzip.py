# -*- coding: utf-8 -*-
r"""
auto_unzip —— 自动层层解压并集中收集结果的工具

用法:
    auto_unzip [目标目录] [选项]
    打包为 exe / 配合 .bat 双击启动时, 自动以"程序所在目录"为目标执行。

行为约定:
  1. 一次性运行。对目标目录做递归扫描, 找出所有"压缩包来源"。
  2. 来源 = 一个"卷组"。多卷压缩包即使分卷被分散在不同文件夹(G102-1 放 .001、G102-2 放 .002),
     只要全树范围内各卷号唯一, 就会自动收齐、拷贝到临时目录拼接解压。
     若同一卷号在多个位置出现(无法配对) -> 整组跳过并在日志中告警, 原文件保持不动。
     单文件压缩包(.zip/.7z/.rar 无分卷)各自独立, 不同文件夹同名互不影响。
  3. 密码支持:
       - 读取程序所在目录下(或 --password-file 指定)的密码文件, 每行一个密码, 空行与 # 注释忽略;
       - 命令行可用 --password 追加(可多次);
       - 每个来源先尝试"无密码", 再按顺序逐个尝试密码;
       - 密码错误(引擎退出码 11 或输出含 password/口令等) -> 立即尝试下一个;
       - 其它错误(损坏/缺卷/非压缩包/不支持的格式) -> 不浪费尝试, 直接判失败。
  4. 每个来源在独立临时目录中解压, 然后"剥皮":
       - 只要是解压产生的、且内部含压缩包的文件夹, 就把里面的压缩包就地解压并删除该压缩包,
         这一层文件夹视为"包裹层", 解压结束后将其子项提升到上一层并删除空壳;
       - 反复直到整棵临时目录树里不再有任何可解压的压缩包;
       - 最顶层散落文件中的压缩包不再向下递归(需求: 散文件不继续)。
  5. 处理完毕后, 临时目录的最终内容整体移入:
        <目标目录>/<合集目录>/<来源名>/     (若唯一结果是单个文件夹则摊开其内容)
  6. 成功处理后, 原始压缩包及其全部分卷, 各自按其所在子目录镜像移动到:
        <目标目录>/<已处理目录>/<相对路径>/
  7. 中间层 / 临时目录自动清理; 失败不移动原文件。
  8. 全程控制台实时输出每个来源的进度与完成状态, 结束打印汇总表。
  9. 伪装扩展名(候选扩展名可经 --fake-ext 增改, 默认 .mp4,.tmp):
       - 头部即 zip/rar/7z 的文件 -> 作为压缩包处理(第一级会先改名为真实扩展名, 失败回滚);
       - "前置数据 + 追加 ZIP"的 polyglot(如 mp4 后拼接 zip) -> 解压前截取 ZIP 部分;
     该检测同时作用于第一级与解压产物(层内), 所以像解出来的 xxx.tmp 若本身是压缩包会继续解。
     仅对候选扩展名做文件头/尾部判定, 不会误伤普通视频/文档。
 10. 归类: 解压全部完成后, 合集里名称以 cos 开头(不区分大小写)的文件夹移入
     --route-human(默认 D:\JUICE\human), 以 G 开头的移入 --route-game(默认 D:\JUICE\game),
     以 dm 开头的移入 --route-animation(默认 D:\JUICE\animation; 优先级低于 G), 其余不动。
 11. 归类完成后, 自动清空"已处理"目录(删除已归档的原压缩包); 可用 --keep-processed 保留。

引擎: 自动检测 7-Zip(7z.exe) 优先; 否则用 WinRAR(Rar.exe 解 .rar, WinRAR.exe 解其余)。
"""
import argparse
import collections
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

# ---------------------------------------------------------------- 常量

DEFAULT_COLLECTION = "解压合集"
DEFAULT_PROCESSED = "已处理"
DEFAULT_LOG = "auto_unzip.log"
PASSWORDS_FILE = "passwords.txt"
WORK_PREFIX = ".auz_tmp_"

# 解压完成后, 对"合集"里的文件夹按名称前缀归类(前缀不区分大小写)
ROUTE_HUMAN_DEFAULT = r"D:\JUICE\human"      # 名称以 cos 开头
ROUTE_GAME_DEFAULT = r"D:\JUICE\game"        # 名称以 G 开头
ROUTE_ANIM_DEFAULT = r"D:\JUICE\animation"   # 名称以 dm 开头(优先级低于 G)

# 单文件压缩包扩展名(不含 .rar/.zip/.7z, 它们要单独参与分卷判定)
SINGLE_EXTS = (
    ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2",
    ".cab", ".jar", ".iso", ".lzh",
)

VOL_FAMILIES = ("rar", "zip", "7z", "generic")

ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_PART_RAR_RE = re.compile(r"\.part(\d+)\.rar$")
_RAR_CONT_RE = re.compile(r"\.r(\d+)$")
_7Z_VOL_RE = re.compile(r"\.7z\.(\d{3,})$")
_ZIP_VOL_RE = re.compile(r"\.zip\.(\d{3,})$")
_ZIP_CONT_RE = re.compile(r"\.z(\d{2,})$")
_GENERIC_VOL_RE = re.compile(r"\.(\d{3})$")

PASSWORD_ERR_HINTS = (
    "password", "口令", "密码", "incorrect", "wrong",
    "неправильный пароль",
)


def parse(filename):
    """解析压缩包文件名。

    返回 dict(family, base, bkey, pos, main, vol) 或 None(不是压缩包)。
    """
    low = filename.lower()
    # ---- rar ----
    m = _PART_RAR_RE.search(low)
    if m:
        n = int(m.group(1))
        base = filename[: m.start()]
        return {"family": "rar", "base": base, "bkey": base.lower(),
                "pos": n, "main": n == 1, "vol": True}
    m = _RAR_CONT_RE.search(low)
    if m:
        base = filename[: m.start()]
        return {"family": "rar", "base": base, "bkey": base.lower(),
                "pos": int(m.group(1)) + 1, "main": False, "vol": True}
    if low.endswith(".rar"):
        base = filename[:-4]
        return {"family": "rar", "base": base, "bkey": base.lower(),
                "pos": 0, "main": True, "vol": False}
    # ---- 7z ----
    m = _7Z_VOL_RE.search(low)
    if m:
        n = int(m.group(1))
        base = filename[: m.start()]
        return {"family": "7z", "base": base, "bkey": base.lower(),
                "pos": n, "main": n == 1, "vol": True}
    if low.endswith(".7z"):
        base = filename[:-3]
        return {"family": "7z", "base": base, "bkey": base.lower(),
                "pos": 0, "main": True, "vol": False}
    # ---- zip ----
    m = _ZIP_VOL_RE.search(low)
    if m:
        n = int(m.group(1))
        base = filename[: m.start()]
        return {"family": "zip", "base": base, "bkey": base.lower(),
                "pos": n, "main": n == 1, "vol": True}
    m = _ZIP_CONT_RE.search(low)
    if m:
        base = filename[: m.start()]
        return {"family": "zip", "base": base, "bkey": base.lower(),
                "pos": int(m.group(1)) + 1, "main": False, "vol": True}
    if low.endswith(".zip"):
        base = filename[:-4]
        return {"family": "zip", "base": base, "bkey": base.lower(),
                "pos": 0, "main": True, "vol": False}
    # ---- 通用编号分卷 (仅 .### 且三位) ----
    m = _GENERIC_VOL_RE.search(low)
    if m:
        n = int(m.group(1))
        base = filename[: m.start()]
        return {"family": "generic", "base": base, "bkey": base.lower(),
                "pos": n, "main": n == 1, "vol": True}
    # ---- 其余单文件格式 ----
    for ext in SINGLE_EXTS:
        if low.endswith(ext):
            base = filename[: -len(ext)]
            return {"family": "single", "base": base, "bkey": base.lower(),
                    "pos": 0, "main": True, "vol": False}
    return None


def sanitize_name(name):
    name = ILLEGAL_CHARS.sub("_", name).strip(" .")
    name = name[:80]
    return name or "archive"


# ---------------------------------------------------------------- 卷组构建

def _build_instances(file_paths, log, where):
    """把一批压缩包文件解析成来源实例列表。"""
    instances = []
    warns = []
    vol_map = collections.defaultdict(list)
    standalone = []

    for p in file_paths:
        info = parse(os.path.basename(p))
        if not info:
            continue
        if info["vol"]:
            vol_map[(info["family"], info["bkey"])].append((p, info))
        else:
            standalone.append((p, info))

    keep = []
    for p, info in standalone:
        if info["family"] in VOL_FAMILIES:
            key = (info["family"], info["bkey"])
            if key in vol_map:
                vol_map[key].append((p, info))
                continue
        keep.append((p, info))
    standalone = keep

    for key in sorted(vol_map, key=lambda k: k[1]):
        members = vol_map[key]
        family, _bkey = key
        by_pos = collections.defaultdict(list)
        main_files = []
        for p, info in members:
            by_pos[info["pos"]].append(p)
            if info["main"]:
                main_files.append((p, info))
        dup = [pos for pos, fs in by_pos.items() if len(fs) > 1]
        if dup:
            names = ", ".join(sorted(os.path.basename(f) for f in
                                     sum(by_pos.values(), [])))
            warns.append("[分卷卷号冲突] %s: 同一卷号出现多份, 无法配对, 整组跳过: %s"
                         % (family, names))
            continue
        if not main_files:
            names = ", ".join(sorted(os.path.basename(f) for f in
                                     sum(by_pos.values(), [])))
            warns.append("[缺少主卷] %s 组只有续卷, 无法解压, 跳过: %s"
                         % (family, names))
            continue
        main_path, main_info = main_files[0]
        instances.append({"base": main_info["base"],
                          "members": [p for p, _i in members],
                          "main": main_path})

    for p, info in standalone:
        instances.append({"base": info["base"], "members": [p], "main": p})

    instances.sort(key=lambda i: os.path.basename(i["main"]).lower())
    return instances, warns


def _archive_magic(path):
    """按文件头识别真实格式, 返回应使用的扩展名或 None。

    支持 zip / rar / 7z (足以覆盖被改名的压缩包)。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if head[:2] == b"PK" and head[2:3] in (b"\x03", b"\x05", b"\x07"):
        return ".zip"
    if head[:7] == b"Rar!\x1a\x07\x00" or head[:8] == b"Rar!\x1a\x07\x01\x00":
        return ".rar"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return ".7z"
    return None


def _detect_prefixed_zip(path):
    """检测"前置数据 + ZIP"的 polyglot 文件(如 mp4 后面追加 zip)。

    这类 ZIP 的偏移量是相对 ZIP 自身而非整个文件, 直接改名 .zip 或直接解压都会失败。
    支持 ZIP64(中央目录与 EOCD 之间有 ZIP64 EOCD 记录与定位器)。
    返回 ZIP 数据在文件中的起始偏移 L (>0) 或 None。
    """
    try:
        size = os.path.getsize(path)
        if size < 22:
            return None
        tail_len = min(size, 22 + 65535 + 4096)
        with open(path, "rb") as fh:
            fh.seek(size - tail_len)
            tail = fh.read()
            base = size - tail_len
            idx = tail.rfind(b"PK\x05\x06")
            if idx < 0:
                return None
            eocd = tail[idx:idx + 22]
            if len(eocd) < 22:
                return None
            cdsize, cdoff = struct.unpack("<II", eocd[12:20])
            eocd_abs = base + idx

            # 确定中央目录结束位置: 若紧邻 EOCD 前有 ZIP64 定位器/记录, 需扣除
            cd_end = eocd_abs
            loc_abs = eocd_abs - 20
            if loc_abs >= base and \
                    tail[loc_abs - base:loc_abs - base + 4] == b"PK\x06\x07":
                z64 = tail.rfind(b"PK\x06\x06", 0, loc_abs - base)
                if z64 >= 0:
                    cd_end = base + z64
                    # 若 EOCD 中偏移/大小为 0xFFFFFFFF, 从 ZIP64 EOCD 读取
                    if cdoff == 0xFFFFFFFF or cdsize == 0xFFFFFFFF:
                        rec = tail[z64:z64 + 56]
                        if len(rec) >= 56:
                            cdsize = struct.unpack("<Q", rec[40:48])[0]
                            cdoff = struct.unpack("<Q", rec[48:56])[0]

            cd_start = cd_end - cdsize
            if cd_start <= 0:
                return None
            fh.seek(cd_start)
            if fh.read(4) != b"PK\x01\x02":
                return None
            L = cd_start - cdoff
            if L <= 0:
                return None
            return L
    except (OSError, struct.error):
        return None


def scan_instances(root, excluded_dirs, log, where, fake_exts=()):
    """递归扫描 root, 构建来源实例列表。

    fake_exts: 允许"伪装扩展名"的候选扩展名集合(默认仅顶层使用)。
      仅当 where == '顶层' 时, 对扩展名属于 fake_exts 的文件做两类识别:
        1) 文件头即 zip/rar/7z -> 解压前改名为真实扩展名;
        2) 前置数据 + 追加 ZIP(polyglot) -> 解压前截取 ZIP 部分。
      只存在于第一级, 后续层不做此检测。
    """
    files = []
    fake_head = []
    fake_poly = []
    top_level = (where == "顶层")
    fake_set = {e.lower() for e in fake_exts}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if os.path.join(dirpath, d) not in excluded_dirs
            and not d.startswith(WORK_PREFIX)
        ]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            if parse(fn):
                files.append(full)
            elif top_level:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in fake_set:
                    continue
                real = _archive_magic(full)
                if real:
                    fake_head.append((full, real))
                    continue
                L = _detect_prefixed_zip(full)
                if L:
                    fake_poly.append((full, L))
    instances, warns = _build_instances(files, log, where)
    for p, real_ext in fake_head:
        stem = os.path.splitext(os.path.basename(p))[0]
        instances.append({"base": stem, "members": [p], "main": p,
                          "family": real_ext.lstrip("."), "fix_ext": True,
                          "rename_ext": real_ext})
        log.info("  [伪装扩展名] %s 实为 %s, 解压前将改名为 %s%s",
                 os.path.basename(p), real_ext, stem, real_ext)
    for p, L in fake_poly:
        stem = os.path.splitext(os.path.basename(p))[0]
        instances.append({"base": stem, "members": [p], "main": p,
                          "family": "zip", "prefixed_zip": L})
        log.info("  [内嵌ZIP] %s 前置 %d 字节, 解压前将截取 ZIP 部分",
                 os.path.basename(p), L)
    instances.sort(key=lambda i: os.path.basename(i["main"]).lower())
    for w in warns:
        log.warning("  %s", w)
    return instances


# ---------------------------------------------------------------- 引擎

def _probe(paths):
    for c in paths:
        if c and os.path.isfile(c):
            return c
    return None


def _registry_install_dirs(exe_name):
    """从注册表读取已安装程序的目录(含 7-Zip/WinRAR)。"""
    dirs = []
    try:
        import winreg
    except ImportError:
        return dirs
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\7-Zip", "Path"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\7-Zip", "Path"),
    ]
    for root, sub, value in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                p, _ = winreg.QueryValueEx(k, value)
                if p:
                    dirs.append(p)
        except OSError:
            pass
    # 通过 App Paths 找具体 exe
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe_name
        try:
            with winreg.OpenKey(root, sub) as k:
                p, _ = winreg.QueryValueEx(k, "")
                if p:
                    dirs.append(os.path.dirname(p))
        except OSError:
            pass
    return dirs


def _drive_roots():
    """所有已就绪的盘符根, 如 ['C:\\\\', 'D:\\\\']。"""
    roots = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":\\"
        if os.path.isdir(root):
            roots.append(root)
    return roots


def _lookup_7z():
    cands = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            cands.append(os.path.join(p, "7z.exe"))
    for d in _registry_install_dirs("7z.exe"):
        cands.append(os.path.join(d, "7z.exe"))
    rels = (r"Program Files\7-Zip\7z.exe",
            r"Program Files (x86)\7-Zip\7z.exe",
            r"7-Zip\7z.exe")
    for root in _drive_roots():
        for rel in rels:
            cands.append(os.path.join(root, rel))
    return _probe(cands)


def _lookup_rar():
    cands = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            cands.append(os.path.join(p, "Rar.exe"))
    for d in _registry_install_dirs("WinRAR.exe"):
        cands.append(os.path.join(d, "Rar.exe"))
    for root in _drive_roots():
        for rel in (r"Program Files\WinRAR\Rar.exe",
                    r"Program Files (x86)\WinRAR\Rar.exe",
                    r"WinRAR\Rar.exe"):
            cands.append(os.path.join(root, rel))
    return _probe(cands)


def _lookup_winrar():
    cands = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            cands.append(os.path.join(p, "WinRAR.exe"))
    for d in _registry_install_dirs("WinRAR.exe"):
        cands.append(os.path.join(d, "WinRAR.exe"))
    for root in _drive_roots():
        for rel in (r"Program Files\WinRAR\WinRAR.exe",
                    r"Program Files (x86)\WinRAR\WinRAR.exe",
                    r"WinRAR\WinRAR.exe"):
            cands.append(os.path.join(root, rel))
    return _probe(cands)


def find_engine():
    """返回 dict(7z, rar, winrar) 各路径或 None。"""
    return {"7z": _lookup_7z(),
            "rar": _lookup_rar(),
            "winrar": _lookup_winrar()}


def engine_pick(engine, family):
    """按格式族选择 (kind, exe); kind: '7z' | 'wr'(Rar/WinRAR 同款语法)。"""
    if engine["7z"]:
        return ("7z", engine["7z"])
    if family == "rar" and engine["rar"]:
        return ("wr", engine["rar"])
    if engine["winrar"]:
        return ("wr", engine["winrar"])
    if engine["rar"]:
        return ("wr", engine["rar"])
    return (None, None)


def engine_can(engine, family, is_vol):
    """当前引擎能否处理该格式。"""
    if engine["7z"]:
        return True
    kind, _exe = engine_pick(engine, family)
    if not kind:
        return False
    if not is_vol:
        return True
    return family in ("rar", "zip")     # WinRAR 不支持 .7z.001 / .001


# ---------------------------------------------------------------- 密码管理

def load_password_file(path):
    if not path or not os.path.isfile(path):
        return []
    passwords = []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                if line.lstrip().startswith("#"):
                    continue
                passwords.append(line)
    except OSError:
        return []
    return passwords


def resolve_passwords(args, base_dir):
    pwd_file = args.password_file or os.path.join(base_dir, PASSWORDS_FILE)
    pws = load_password_file(pwd_file)
    pws.extend(args.password or [])
    seen, out = set(), []
    for p in pws:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------- 引擎调用

def _text(out):
    if not out:
        return ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return " ".join(out.split())


def _dir_has_items(path):
    try:
        return any(os.scandir(path))
    except OSError:
        return False


def _run_engine(kind, exe, arc, dest_dir, timeout, password):
    """调用一次解压命令, 返回 (code, 输出文本)。"""
    os.makedirs(dest_dir, exist_ok=True)
    if kind == "7z":
        cmd = [exe, "x", "-y", "-bso0", "-bsp0", "-o" + dest_dir]
        # 始终带 -p: 无密码时用空密码, 避免 7z 交互式询问(否则会 Break signaled)
        cmd.append("-p" + (password if password is not None else ""))
        cmd.append(arc)
    else:
        cmd = [exe, "x", "-y", "-o+", "-idq", "-ep1"]
        # -p- 表示显式"无密码", 避免弹窗询问(加密时返回 11)
        cmd.append("-p-" if password is None else "-p" + password)
        cmd.append(arc)
        cmd.append(dest_dir)
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, _text((p.stdout or b"") + (p.stderr or b""))
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as exc:  # noqa: BLE001
        return -2, str(exc)


def _classify_result(kind, code, text, has_items):
    """判定一次尝试的结果。kind: '7z' | 'wr'。

    返回: ('ok'|'password'|'fatal', 说明)
    依据(实测):
      - 7z: 成功=0/1; 密码错误=2 且输出含 Wrong password;
            交互被打断(255/Break signaled)=需要密码 -> 也按密码类继续尝试;
            损坏/伪包=2 且无 password 字样 -> 致命。
      - wr (Rar/WinRAR): 成功=0; 部分=1(有产物算成功);
            密码错误/无密码遇加密=11; 损坏/伪包=10/13/1(空) 等非密码错误。
    """
    low = text.lower()
    pw_hint = any(h in low for h in PASSWORD_ERR_HINTS)

    if kind == "7z":
        if code in (0, 1):
            return "ok", "成功(exit=%s)" % code
        if pw_hint or code == 255 or "break signaled" in low:
            return "password", "需要密码/密码错误(exit=%s)" % code
        return "fatal", "非密码错误(exit=%s): %s" % (code, text[:150] or "无输出")

    # wr: Rar.exe / WinRAR.exe
    if code == 0:
        return "ok", "成功"
    if code == 1:
        if has_items:
            return "ok", "成功(有警告, exit=1)"
        return "fatal", "无有效输出(exit=1): %s" % (text[:150] or "无输出")
    if code == 11 or pw_hint:
        return "password", "密码错误(exit=%s)" % code
    return "fatal", "非密码错误(exit=%s): %s" % (code, text[:150] or "无输出")


def try_extract_instance(engine, inst, scratch_root, opts, log, password_list,
                         counter=None, report=None):
    """对一个卷组逐密码尝试解压。

    返回 (ok, success_dir, used_desc)
      - scratch_root: 调用方保证会被清理的目录; 尝试目录建在其下。
      - 成功: success_dir = 成功尝试的输出目录; 其余已删。
      - 失败: (False, None, 原因)。
    """
    members = inst["members"]
    main_path = inst["main"]
    info_main = parse(os.path.basename(main_path))
    family = inst.get("family") or (info_main["family"] if info_main else "zip")
    is_vol = len(members) > 1
    kind, exe = engine_pick(engine, family)
    if not kind:
        return False, None, "未找到可用的解压引擎"

    vols_dir = None
    arc = main_path
    if is_vol:
        vols_dir = os.path.join(scratch_root, "volumes")
        os.makedirs(vols_dir, exist_ok=True)
        for p in members:
            shutil.copy2(p, os.path.join(vols_dir, os.path.basename(p)))
        arc = os.path.join(vols_dir, os.path.basename(main_path))
    elif inst.get("prefixed_zip"):
        # 前置数据 + 追加 ZIP: 截取出 ZIP 部分为临时 .zip
        trimmed = os.path.join(scratch_root, "trimmed.zip")
        offset = inst["prefixed_zip"]
        try:
            total = os.path.getsize(main_path)
        except OSError:
            total = 0
        log.info("  截取内嵌 ZIP: 从偏移 %d 起, 约 %.0f MB (临时占用同量磁盘)",
                 offset, max(0, total - offset) / 1048576.0)
        with open(main_path, "rb") as fi, open(trimmed, "wb") as fo:
            fi.seek(offset)
            shutil.copyfileobj(fi, fo, 1024 * 1024)
        arc = trimmed

    try:
        candidates = [None] + password_list
        last_fatal = ""
        for idx, pwd in enumerate(candidates):
            label = "无密码" if pwd is None else "密码#%d(%s)" % (
                idx, pwd if opts.show_passwords else "****")
            if report:
                report("尝试解压: %s" % label)
            out_dir = os.path.join(scratch_root, "try_%d" % idx)
            code, out = _run_engine(kind, exe, arc, out_dir, opts.timeout,
                                    password=pwd)
            has_items = _dir_has_items(out_dir)
            result, why = _classify_result(kind, code, out, has_items)
            if result == "ok":
                log.info("  密码(%s) -> 成功", label)
                return True, out_dir, label
            if result == "password":
                log.info("  密码(%s) -> 失败(%s), 立即尝试下一个", label, why)
                shutil.rmtree(out_dir, ignore_errors=True)
                continue
            last_fatal = why
            shutil.rmtree(out_dir, ignore_errors=True)
            break
        log.warning("  来源解压失败%s: %s",
                    " (%s)" % last_fatal if last_fatal else "",
                    os.path.basename(main_path))
        return False, None, last_fatal or "所有密码均失败"
    finally:
        if vols_dir and os.path.isdir(vols_dir):
            shutil.rmtree(vols_dir, ignore_errors=True)


# ---------------------------------------------------------------- 目录工具

def unique_dir(parent, wanted, used):
    base = sanitize_name(wanted)
    name = base
    i = 2
    while name in used or os.path.exists(os.path.join(parent, name)):
        name = "%s_%d" % (base, i)
        i += 1
    used.add(name)
    p = os.path.join(parent, name)
    os.makedirs(p, exist_ok=True)
    return p


def move_unique(src, dest_dir, used_names=None):
    if used_names is None:
        used_names = set()
    base = os.path.basename(src)
    name = base
    i = 2
    while name in used_names or os.path.exists(os.path.join(dest_dir, name)):
        stem, ext = os.path.splitext(base)
        name = "%s_%d%s" % (stem, i, ext)
        i += 1
    used_names.add(name)
    dst = os.path.join(dest_dir, name)
    shutil.move(src, dst)
    return dst


def route_dest_for(name, args):
    """按名称前缀返回归类目标目录, 无匹配返回 None。

    规则(前缀不区分大小写, 优先级从上到下):
      cos* -> --route-human; g* -> --route-game; dm* -> --route-animation。
    即 G 优先于 dm(如 Gundam 归 game)。
    """
    low = name.lower()
    human = getattr(args, "route_human", "") or ""
    game = getattr(args, "route_game", "") or ""
    anim = getattr(args, "route_animation", "") or ""
    if human and low.startswith("cos"):
        return human
    if game and low.startswith("g"):
        return game
    if anim and low.startswith("dm"):
        return anim
    return None


def route_collection(collection_dir, args, log):
    """解压完成后, 把合集里按前缀匹配的文件夹移入指定目录, 其余不动。"""
    if not os.path.isdir(collection_dir):
        return 0
    moved = 0
    for name in sorted(os.listdir(collection_dir)):
        src = os.path.join(collection_dir, name)
        dest = route_dest_for(name, args)
        if not dest:
            continue
        try:
            os.makedirs(dest, exist_ok=True)
            dst = move_unique(src, dest)
            moved += 1
            log.info("  [归类] %s -> %s", name, dst)
        except OSError as exc:
            log.warning("  [归类] 移动失败 %s: %s", name, exc)
    return moved


def cleanup_processed(processed_dir, log):
    """删除"已处理"目录下的全部内容(已归档的原压缩包), 返回删除的顶层项数。"""
    if not os.path.isdir(processed_dir):
        return 0
    count = 0
    for name in os.listdir(processed_dir):
        p = os.path.join(processed_dir, name)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            count += 1
        except OSError as exc:
            log.warning("  [清理] 删除失败 %s: %s", p, exc)
    return count


def merge_children(src_dir, dst_dir):
    """把 src_dir 子项并入 dst_dir(同名目录递归合并, 同名文件覆盖)。"""
    os.makedirs(dst_dir, exist_ok=True)
    for name in list(os.listdir(src_dir)):
        s = os.path.join(src_dir, name)
        d = os.path.join(dst_dir, name)
        _move_into(s, d)


def _move_into(src, dst):
    """把 src 移成 dst; 同名: 目录则递归合并, 文件则覆盖。"""
    if os.path.isdir(src):
        if os.path.exists(dst):
            if os.path.isdir(dst):
                merge_children(src, dst)
                try:
                    os.rmdir(src)
                except OSError:
                    pass
                return
            os.remove(dst)
        shutil.move(src, dst)
    else:
        if os.path.exists(dst):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        shutil.move(src, dst)


def promote_children(wdir, parent):
    """把 wdir 的所有子项(文件/目录)提升到 parent, 随后删除 wdir。

    逐个容错: 单个失败不阻断其它子项。
    """
    for name in list(os.listdir(wdir)):
        s = os.path.join(wdir, name)
        d = os.path.join(parent, name)
        try:
            _move_into(s, d)
        except OSError:
            pass
    try:
        os.rmdir(wdir)
    except OSError:
        pass


def rmtree_empties(container):
    while True:
        removed = False
        for dirpath, _dirs, filenames in os.walk(container, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(container):
                continue
            if not filenames and not any(True for _ in os.scandir(dirpath)):
                try:
                    os.rmdir(dirpath)
                    removed = True
                except OSError:
                    pass
        if not removed:
            break


# ---------------------------------------------------------------- 剥皮

def _collect_nested_instances(container, hidden_exts, log):
    """收集 container 内的来源实例, 并额外识别"伪装扩展名"的文件。

    hidden_exts: 解压产物中允许按文件头识别的候选扩展名(默认 .tmp 等)。
      - 头部即 zip/rar/7z -> 直接作为来源(引擎能按内容识别, 无需改名);
      - 前置数据 + 追加 ZIP 的 polyglot -> 作为来源(解压时截取)。
    """
    files = []
    disguised = []
    hs = {e.lower() for e in hidden_exts}
    for dirpath, _dirs, filenames in os.walk(container):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if parse(fn):
                files.append(full)
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in hs:
                continue
            real = _archive_magic(full)
            if real:
                disguised.append((full, real, None))
                continue
            L = _detect_prefixed_zip(full)
            if L:
                disguised.append((full, None, L))
    instances, warns = _build_instances(files, log, "层内")
    for p, real, L in disguised:
        stem = os.path.splitext(os.path.basename(p))[0]
        if real:
            instances.append({"base": stem, "members": [p], "main": p,
                              "family": real.lstrip("."), "disguised": True})
            log.info("  层内发现伪装扩展名: %s 实为 %s",
                     os.path.basename(p), real)
        else:
            instances.append({"base": stem, "members": [p], "main": p,
                              "family": "zip", "prefixed_zip": L,
                              "disguised": True})
            log.info("  层内发现内嵌 ZIP: %s (偏移 %d)",
                     os.path.basename(p), L)
    instances.sort(key=lambda i: os.path.basename(i["main"]).lower())
    return instances, warns


def peel_container(container, scratch_root, engine, opts, log, state,
                   passwords, report=None):
    """把临时目录树剥到底, 直到不再有任何可解压的压缩包。

    - 组内各卷分散则拷齐后解压;
    - 解压成功输出并入主卷所在目录, 删除该卷组全部原始成员;
    - 顶层散落压缩包不再递归;
    - 结束时折叠包裹层, 删除残留空目录。
    """
    wrapped = set()
    counter = [0]

    while True:
        instances, warns = _collect_nested_instances(
            container, parse_fake_exts(opts), log)
        for w in warns:
            log.info("  层内: %s", w)

        valid = []
        for inst in instances:
            parents = {os.path.dirname(p) for p in inst["members"]}
            if not inst.get("disguised") and len(parents) == 1 \
                    and os.path.abspath(next(iter(parents))) \
                    == os.path.abspath(container):
                continue   # 顶层散落的普通压缩包 -> 不递归(伪装包除外)
            valid.append(inst)
        if not valid:
            break
        if state["extractions"] >= opts.max_extractions:
            raise RuntimeError(
                "解压次数超过上限 %d, 疑似无限套娃, 已中止该来源"
                % opts.max_extractions)

        progress = False
        for inst in valid:
            main_info = parse(os.path.basename(inst["main"]))
            family = inst.get("family") or (main_info["family"]
                                            if main_info else "zip")
            is_vol = len(inst["members"]) > 1
            if not engine_can(engine, family, is_vol):
                log.info("  跳过不支持的 %s(%s 分卷, 需 7-Zip 引擎)",
                         inst["base"], family)
                continue
            if report:
                report("剥皮解压: %s(%s)" % (
                    inst["base"], "多卷" if is_vol else "单包"))
            main_parent = os.path.dirname(inst["main"])
            state["extractions"] += 1

            sub_scratch = os.path.join(scratch_root, "peel_%d" % counter[0])
            counter[0] += 1
            os.makedirs(sub_scratch, exist_ok=True)
            ok, out_dir, _label = try_extract_instance(
                engine, inst, sub_scratch, opts, log, passwords,
                counter=state["extractions"], report=report)
            if not ok:
                log.warning("  剥皮解压失败 %s", inst["base"])
                shutil.rmtree(sub_scratch, ignore_errors=True)
                continue
            merge_children(out_dir, main_parent)
            shutil.rmtree(sub_scratch, ignore_errors=True)
            for p in inst["members"]:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            if os.path.abspath(main_parent) != os.path.abspath(container):
                wrapped.add(os.path.abspath(main_parent))
            progress = True
            log.debug("  已解压并移除卷组: %s", inst["base"])

        if not progress:
            break

    # 折叠包裹层(从深到浅): 其子项提升到父目录后删除
    for wdir in sorted(wrapped, key=lambda p: p.count(os.sep), reverse=True):
        if not os.path.isdir(wdir):
            continue
        parent = os.path.dirname(wdir)
        if not os.path.isdir(parent):
            continue
        promote_children(wdir, parent)

    rmtree_empties(container)


# ---------------------------------------------------------------- 来源处理

def _move_members_to_processed(members, root, processed_dir, log):
    for p in sorted(set(members)):
        if not os.path.exists(p):
            continue
        rd = os.path.relpath(os.path.dirname(p), root)
        target = processed_dir if rd == "." else os.path.join(processed_dir, rd)
        os.makedirs(target, exist_ok=True)
        try:
            move_unique(p, target)
            log.debug("  已移至: %s", os.path.relpath(
                os.path.join(target, os.path.basename(p)), root))
        except OSError as exc:
            log.warning("  移动来源失败 %s: %s", p, exc)


def process_instance(inst, idx, total, root, collection_dir, processed_dir,
                     engine, opts, log, used_collection, state, passwords):
    label = inst["base"]
    rel_of_main = os.path.relpath(inst["main"], root)
    rel_dir = os.path.dirname(rel_of_main)
    extra = " + %d 卷" % (len(inst["members"]) - 1) if len(
        inst["members"]) > 1 else ""
    log.info("")
    log.info("== [%d/%d] 来源: %s  [所在: %s]%s",
             idx, total, label, rel_dir or ".", extra)

    def report(msg):
        log.info("     | %s", msg)

    workdir = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=root)
    dest_dir = None
    renamed = None
    moved = False
    try:
        # 伪装扩展名: 先改名为真实扩展名 (失败会回滚)
        if inst.get("fix_ext"):
            old = inst["main"]
            d = os.path.dirname(old)
            stem = os.path.splitext(os.path.basename(old))[0]
            real_ext = inst.get("rename_ext", ".zip")
            new = os.path.join(d, stem + real_ext)
            n = 2
            while os.path.exists(new):
                new = os.path.join(d, "%s_%d%s" % (stem, n, real_ext))
                n += 1
            os.rename(old, new)
            renamed = (old, new)
            inst["main"] = new
            inst["members"] = [new if m == old else m for m in inst["members"]]
            log.info("  伪装扩展名改名: %s -> %s",
                     os.path.basename(old), os.path.basename(new))

        info = parse(os.path.basename(inst["main"]))
        family = inst.get("family") or (info["family"] if info else "zip")
        is_vol = len(inst["members"]) > 1
        if not engine_can(engine, family, is_vol):
            log.info("  跳过: %s(%s 分卷) 需 7-Zip, 当前引擎为 %s",
                     label, family,
                     "7-Zip" if engine["7z"] else "WinRAR")
            return "跳过", None, "需 7-Zip 引擎"

        report("开始解压 (候选: 无密码 + %d 个密码)" % len(passwords))
        scratch = os.path.join(workdir, "scratch_top")
        os.makedirs(scratch, exist_ok=True)
        ok, container, used_desc = try_extract_instance(
            engine, inst, scratch, opts, log, passwords,
            counter=state["extractions"], report=report)
        if not ok:
            log.info("  结果: 失败")
            return "失败", None, used_desc

        report("开始逐层剥皮")
        peel_scratch = os.path.join(workdir, "scratch_peel")
        os.makedirs(peel_scratch, exist_ok=True)
        peel_container(container, peel_scratch, engine, opts, log, state,
                       passwords, report=report)

        # 若剥完后只剩一个单文件夹, 摊开它(避免 合集/名/名 双重嵌套)
        items = sorted(os.listdir(container))
        if len(items) == 1 and os.path.isdir(os.path.join(container, items[0])):
            sub = os.path.join(container, items[0])
            sub_items = sorted(os.listdir(sub))
            if sub_items:
                container = sub
                items = sub_items
            # sub 为空 -> 保留空文件夹形态; 外层空壳由 finally 清理

        dest_dir = unique_dir(collection_dir, label, used_collection)
        if not items:
            log.info("  来源为空(无内容)")
            try:
                os.rmdir(dest_dir)
            except OSError:
                pass
            dest_dir = None
        else:
            for child in items:
                move_unique(os.path.join(container, child), dest_dir)
            log.info("  完成 -> %s", os.path.relpath(dest_dir, root))
        if dest_dir is not None:
            _move_members_to_processed(inst["members"], root, processed_dir, log)
            moved = True
        log.info("  结果: 成功")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        # 失败/未归档时把伪装扩展名改回原名
        if renamed and not moved:
            old, new = renamed
            try:
                if os.path.exists(new) and not os.path.exists(old):
                    os.rename(new, old)
                    log.info("  已回滚改名: %s -> %s",
                             os.path.basename(new), os.path.basename(old))
            except OSError:
                pass

    if dest_dir is not None:
        return "成功", dest_dir, ""
    return "成功(空)", None, ""


# ---------------------------------------------------------------- 入口

def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="auto_unzip",
        description="自动层层解压并集中收集 zip/7z/rar(含跨文件夹分卷), "
                    "支持密码逐个尝试; 双击启动时处理程序所在目录。")
    ap.add_argument("target", nargs="?", default=None,
                    help="目标目录(默认: 程序所在目录)")
    ap.add_argument("-p", "--password", action="append", default=None,
                    help="追加解压密码(可多次); 也会读取程序目录下 %s"
                         % PASSWORDS_FILE)
    ap.add_argument("--password-file", default=None,
                    help="密码文件路径(默认程序目录下 %s)" % PASSWORDS_FILE)
    ap.add_argument("--show-passwords", action="store_true",
                    help="日志显示明文密码(默认打码)")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help="合集目录名(默认 %(default)s)")
    ap.add_argument("--processed", default=DEFAULT_PROCESSED,
                    help="已处理目录名(默认 %(default)s)")
    ap.add_argument("--log-file", default=None,
                    help="日志文件路径(默认 <目标目录>/%s)" % DEFAULT_LOG)
    ap.add_argument("--max-extractions", type=int, default=300,
                    help="单个来源最多解压次数(防无限套娃, 默认 %(default)s)")
    ap.add_argument("--timeout", type=int, default=900,
                    help="单次解压超时秒数(默认 %(default)s)")
    ap.add_argument("--selftest", action="store_true",
                    help="自检解析规则与引擎, 然后退出")
    ap.add_argument("--dry-run", action="store_true",
                    help="只扫描并列出会处理的来源(不移动/不解压/不改动任何文件)")
    ap.add_argument("--fake-ext", default=".mp4,.tmp",
                    help="疑似伪装的扩展名(逗号分隔), 默认 %(default)s; "
                         "第一级与解压产物中均按文件头识别 zip/rar/7z 或内嵌 ZIP")
    ap.add_argument("--route-human", default=ROUTE_HUMAN_DEFAULT,
                    help="解压完成后, 合集里名称以 cos 开头的文件夹移到此目录"
                         "(默认 %(default)s; 设为空则禁用)")
    ap.add_argument("--route-game", default=ROUTE_GAME_DEFAULT,
                    help="解压完成后, 合集里名称以 G 开头的文件夹移到此目录"
                         "(默认 %(default)s; 设为空则禁用)")
    ap.add_argument("--route-animation", default=ROUTE_ANIM_DEFAULT,
                    help="解压完成后, 合集里名称以 dm 开头的文件夹移到此目录"
                         "(默认 %(default)s; 设为空则禁用; 优先级低于 G)")
    ap.add_argument("--keep-processed", action="store_true",
                    help="归类完成后保留'已处理'目录里的原压缩包(默认会自动清空)")
    return ap


def selftest():
    bad = 0
    cases = [
        ("a.zip", True, 0), ("a.7z", True, 0), ("a.rar", True, 0),
        ("a.part1.rar", True, 1), ("a.part2.rar", True, 2),
        ("a.r00", True, 1), ("a.7z.001", True, 1), ("a.7z.002", True, 2),
        ("a.zip.001", True, 1), ("a.001", True, 1), ("a.002", True, 2),
        ("a.txt", False, 0), ("photo.2024", False, 0), ("data.tar.gz", True, 0),
    ]
    for name, is_arc, pos in cases:
        info = parse(name)
        if (info is not None) != is_arc:
            print("FAIL parse(%r): is_archive=%r expect=%r"
                  % (name, info is not None, is_arc))
            bad += 1
        elif info is not None and info["pos"] != pos:
            print("FAIL parse(%r): pos=%r expect=%r" % (name, info["pos"], pos))
            bad += 1
    if bad:
        print("解析规则自检失败 %d 项" % bad)
        return 1

    tmp = tempfile.mkdtemp(prefix="auz_voltest_")
    try:
        def mk(folder, name):
            d = os.path.join(tmp, folder)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, name)
            with open(p, "wb") as fh:
                fh.write(b"x")
            return p

        g1 = mk("G102-1", "data.7z.001")
        g2 = mk("G102-2", "data.7z.002")
        s1 = mk("A", "one.zip")
        s2 = mk("B", "one.zip")
        dup_a = mk("C", "dup.7z.001")
        dup_b = mk("D", "dup.7z.001")
        log = logging.getLogger("selftest")
        log.addHandler(logging.NullHandler())
        insts, warns = _build_instances([g1, g2, s1, s2, dup_a, dup_b],
                                        log, "自检")
        merged = [i for i in insts if i["base"] == "data"]
        single_a = [i for i in insts if i["base"] == "one" and
                    os.path.basename(os.path.dirname(i["main"])) == "A"]
        single_b = [i for i in insts if i["base"] == "one" and
                    os.path.basename(os.path.dirname(i["main"])) == "B"]
        if not (len(merged) == 1 and len(merged[0]["members"]) == 2):
            print("FAIL 跨文件夹分卷未合并")
            bad += 1
        if not (len(single_a) == 1 and len(single_b) == 1):
            print("FAIL 同名单文件未各自独立")
            bad += 1
        if not any("dup" in w for w in warns):
            print("FAIL 同卷号冲突未告警")
            bad += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if bad:
        print("卷组构建自检失败 %d 项" % bad)
        return 1

    engine = find_engine()
    if not (engine["7z"] or engine["rar"] or engine["winrar"]):
        print("未找到解压引擎(7-Zip 或 WinRAR)。请先安装其中之一。")
        return 1
    desc = []
    for k in ("7z", "rar", "winrar"):
        if engine[k]:
            desc.append("%s=%s" % (k, engine[k]))
    print("引擎: " + "; ".join(desc))

    tmp2 = tempfile.mkdtemp(prefix="auz_selftest_")
    try:
        src = os.path.join(tmp2, "probe.zip")
        outd = os.path.join(tmp2, "out")
        os.makedirs(outd)
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("hello.txt", "ok")
        family = "zip"
        kind, exe = engine_pick(engine, family)
        if not kind:
            print("引擎解压实测: 跳过(无可用引擎)")
            return 1
        code, out = _run_engine(kind, exe, src, outd, 120, None)
        has = _dir_has_items(outd)
        result, _why = _classify_result(kind, code, out, has)
        if result == "ok" and os.path.isfile(os.path.join(outd, "hello.txt")):
            print("引擎解压实测: 通过")
            return 0
        print("引擎解压实测: 失败 (result=%s code=%s out=%s)" % (result, code, out))
        return 1
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


def _ensure_console_utf8():
    """尽力把 Windows 控制台代码页设为 UTF-8, 保证中文输出不乱码。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass


def parse_fake_exts(args):
    """把 --fake-ext 的逗号分隔值解析为扩展名集合(带点, 小写)。"""
    out = []
    for part in (getattr(args, "fake_ext", "") or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        if part not in out:
            out.append(part)
    return out


def dry_run_report(root, args, engine, passwords, base_dir):
    """只扫描并报告会处理的来源, 不改动任何文件。"""
    collection_dir = os.path.join(root, args.collection)
    processed_dir = os.path.join(root, args.processed)
    excluded = {os.path.abspath(collection_dir),
                os.path.abspath(processed_dir)}
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    log = logging.getLogger("auto_unzip")

    eng_desc = ", ".join("%s=%s" % (k, v) for k, v in engine.items() if v)
    print("=" * 64)
    print(" auto_unzip  [预览模式 dry-run] 不会修改任何文件")
    print(" 目标目录: %s" % root)
    print(" 引擎: %s" % eng_desc)
    pwd_src = args.password_file or os.path.join(base_dir, PASSWORDS_FILE)
    print(" 密码: 无密码 + %d 个(来源: %s)" % (len(passwords), pwd_src))
    print(" 合集将写入: %s" % collection_dir)
    print(" 原包将移到: %s" % processed_dir)
    print(" 伪装扩展名检测: %s (识别: 头部即压缩包 / 前置数据+内嵌ZIP)"
          % (", ".join(parse_fake_exts(args)) or "(无)"))
    print("=" * 64)

    instances = scan_instances(root, excluded, log, "顶层",
                               fake_exts=parse_fake_exts(args))

    print("")
    print("共发现 %d 个来源(卷组/单文件):" % len(instances))
    print("-" * 64)
    existing = set(os.listdir(collection_dir)) if os.path.isdir(
        collection_dir) else set()
    used = set(existing)
    for i, inst in enumerate(instances, 1):
        members = inst["members"]
        vol = len(members)
        kind = "单文件" if vol == 1 else "多卷组(%d 卷)" % vol
        name = sanitize_name(inst["base"])
        final = name
        n = 2
        while final in used:
            final = "%s_%d" % (name, n)
            n += 1
        used.add(final)
        rel_main = os.path.relpath(inst["main"], root)
        if inst.get("fix_ext"):
            tag = "  [伪装扩展名->将改名 %s]" % inst.get("rename_ext", ".zip")
        elif inst.get("prefixed_zip"):
            tag = "  [内嵌ZIP->解压前截取]"
        else:
            tag = ""
        print("[%2d] %-22s %s%s" % (i, inst["base"], kind, tag))
        print("     主卷: %s" % rel_main)
        if vol > 1:
            for p in members:
                print("       卷: %s" % os.path.relpath(p, root))
        print("     结果文件夹: %s\\%s" % (args.collection, final))
        route = route_dest_for(final, args)
        if route:
            print("     完成后归类: -> %s" % route)
        print("     处理后原包移至: %s\\%s" % (
            args.processed, os.path.dirname(rel_main)
            if os.path.dirname(rel_main) else ""))
    print("-" * 64)
    print("提示: 确认无误后去掉 --dry-run 再运行, 才会真正解压与移动。")
    return 0


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    _ensure_console_utf8()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.selftest:
        return selftest()

    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    target = args.target or base_dir
    root = os.path.abspath(target)
    if not os.path.isdir(root):
        print("目录不存在: %s" % root, file=sys.stderr)
        return 2

    engine = find_engine()
    if not (engine["7z"] or engine["rar"] or engine["winrar"]):
        print("未找到解压引擎: 请安装 7-Zip 或 WinRAR。", file=sys.stderr)
        return 2

    passwords = resolve_passwords(args, base_dir)

    if args.dry_run:
        return dry_run_report(root, args, engine, passwords, base_dir)

    log_path = args.log_file or os.path.join(root, DEFAULT_LOG)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("auto_unzip")

    collection_dir = os.path.join(root, args.collection)
    processed_dir = os.path.join(root, args.processed)
    os.makedirs(collection_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    excluded = {os.path.abspath(collection_dir),
                os.path.abspath(processed_dir),
                os.path.abspath(log_path)}

    eng_desc = ", ".join("%s=%s" % (k, v) for k, v in engine.items() if v)
    log.info("=" * 60)
    log.info(" auto_unzip")
    log.info(" 目标目录: %s", root)
    log.info(" 引擎: %s", eng_desc)
    pwd_src = args.password_file or os.path.join(base_dir, PASSWORDS_FILE)
    log.info(" 密码: 无密码 + %d 个(读取: %s%s)" % (
        len(passwords), pwd_src,
        " + 命令行 %d 个" % len(args.password) if args.password else ""))
    log.info(" 合集: %s", collection_dir)
    log.info(" 已处理: %s", processed_dir)
    log.info(" 伪装扩展名检测: %s (仅第一级, 头部即压缩包 / 前置数据+内嵌ZIP)",
             ", ".join(parse_fake_exts(args)) or "(无)")
    log.info("=" * 60)

    instances = scan_instances(root, excluded, log, "顶层",
                               fake_exts=parse_fake_exts(args))
    log.info("扫描完成: 共 %d 个来源。", len(instances))

    state = {"extractions": 0}
    used_collection = set(os.listdir(collection_dir))
    summary = []
    ok_cnt = fail_cnt = 0
    start = time.time()
    total = len(instances)

    for idx, inst in enumerate(instances, 1):
        try:
            status, dest, why = process_instance(
                inst, idx, total, root, collection_dir, processed_dir,
                engine, args, log, used_collection, state, passwords)
        except RuntimeError as exc:
            log.warning("处理异常: %s", exc)
            status, dest, why = "异常", None, str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("处理出错: %s", exc)
            status, dest, why = "异常", None, str(exc)
        if status.startswith("成功"):
            ok_cnt += 1
        else:
            fail_cnt += 1
        summary.append((inst["base"], status, dest, why))

    used_secs = time.time() - start

    # 解压完成后, 按名称前缀归类合集里的文件夹
    log.info("")
    log.info("开始归类 (cos* -> %s, G* -> %s, dm* -> %s)",
             getattr(args, "route_human", "") or "(禁用)",
             getattr(args, "route_game", "") or "(禁用)",
             getattr(args, "route_animation", "") or "(禁用)")
    moved_cnt = route_collection(collection_dir, args, log)
    log.info("归类完成: 移动 %d 个文件夹。", moved_cnt)

    # 归类完成后清空"已处理"目录里的原压缩包
    if getattr(args, "keep_processed", False):
        log.info("保留'已处理'目录(--keep-processed)。")
    else:
        del_cnt = cleanup_processed(processed_dir, log)
        log.info("已清空'已处理'目录: 删除 %d 项。", del_cnt)

    log.info("")
    log.info("=" * 60)
    log.info(" 汇总: 成功 %d / 失败或跳过 %d / 用时 %.1fs",
             ok_cnt, fail_cnt, used_secs)
    log.info("-" * 60)
    for base, status, dest, why in summary:
        rel = os.path.relpath(dest, root) if dest else ""
        tail = "  -> %s" % rel if rel else ""
        if why:
            tail += "  [%s]" % why
        mark = "OK " if status.startswith("成功") else "!! "
        log.info(" %s %-4s %-28s %s", mark, status, base, tail)
    log.info("=" * 60)
    log.info("日志文件: %s", log_path)
    return 1 if fail_cnt else 0


if __name__ == "__main__":
    sys.exit(main())
