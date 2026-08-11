# -*- coding: utf-8 -*-
"""
util.py —— 通用工具函数

- 命令执行（优先应用自带静态二进制，回退系统 PATH）
- shell key=value 状态文件读写
- 文本清洗、合法性校验等小工具
"""

import os
import re
import shlex
import shutil
import subprocess

import paths


# ---------------------------------------------------------------------------
# 命令查找与执行
# ---------------------------------------------------------------------------
def find_bin(name):
    """优先使用应用自带(静态编译)的二进制,找不到再回退到系统 PATH。

    自带二进制位于 <应用目录>/bin/<arch>/ 下；若平台没有自带包，
    则用系统命令（需系统已安装）兜底。
    """
    bundled = os.path.join(paths.BIN_DIR, name)
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    if name == "iptables":
        # 现代内核(6.x)优先 nft 后端;legacy 后端在部分内核上会静默失败
        nft = shutil.which("iptables-nft")
        if nft:
            return nft
    return shutil.which(name)


def command_exists(name):
    """判断命令是否可用。"""
    return find_bin(name) is not None


def run_cmd(args, timeout=None, input_text=None):
    """执行命令，返回 (returncode, stdout, stderr)，任何异常都不抛出。"""
    try:
        if args:
            resolved = find_bin(args[0]) or args[0]
            args = [resolved] + list(args[1:])
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found"
    except Exception as exc:
        return 1, "", str(exc)


def run_ok(args, timeout=None, input_text=None):
    """执行命令并返回是否成功。"""
    rc, stdout, stderr = run_cmd(args, timeout=timeout, input_text=input_text)
    return rc == 0, stdout, stderr


# ---------------------------------------------------------------------------
# shell 状态文件（key=value）读写
# ---------------------------------------------------------------------------
def trim(value):
    return (value or "").strip()


def shell_quote(value):
    return shlex.quote(str(value or ""))


def decode_shell_value(raw):
    """把 shell 里读取到的值还原为字符串（去掉引号）。"""
    raw = raw.strip()
    if raw == "":
        return ""
    try:
        parts = shlex.split(raw, posix=True)
    except ValueError:
        return raw.strip("\"'")
    return parts[0] if parts else ""


def load_shell_state(path):
    """读取 key=value 状态文件，返回 dict；文件不存在/损坏时返回空 dict。"""
    data = {}
    if not os.path.isfile(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw_value = line.split("=", 1)
                data[key.strip()] = decode_shell_value(raw_value)
    except OSError:
        return {}
    return data


def write_shell_state(path, mapping):
    """把 dict 写成 key=value 状态文件（值会做 shell 转义，可安全回读）。"""
    paths.ensure_data_dir()
    with open(path, "w", encoding="utf-8") as handle:
        for key, value in mapping.items():
            handle.write(f"{key}={shell_quote(value)}\n")


# ---------------------------------------------------------------------------
# 文本与校验工具
# ---------------------------------------------------------------------------
def sanitize_text(text):
    """去掉终端控制字符与 \r，适合写日志/回显给前端。"""
    text = text or ""
    text = re.sub(r"\x1B\[[0-9;]*[A-Za-z]", "", text)
    return text.replace("\r", "")


def read_text(path):
    """读文件文本，失败返回空串（不抛异常）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def read_pid_file(path):
    """读 PID 文件，非法内容返回 0。"""
    raw = trim(read_text(path))
    return int(raw) if raw.isdigit() else 0


def is_iface_name(value):
    """网卡名校验：允许字母数字及 . _ : -，最长 64。"""
    return bool(re.fullmatch(r"[a-zA-Z0-9_.:-]{1,64}", value or ""))


def re_match(pattern, text, flags=0):
    """re.match 包装（从字符串开头匹配）。"""
    return re.match(pattern, text or "", flags)


def re_search(pattern, text, flags=0):
    """re.search 包装（全文查找）。"""
    return re.search(pattern, text or "", flags)


def re_fullmatch(pattern, text, flags=0):
    """re.fullmatch 包装（整串完全匹配）。"""
    return re.fullmatch(pattern, text or "", flags)


def is_ipv4_cidr(value):
    """IPv4 CIDR 校验，如 192.168.12.1/24。"""
    from ipaddress import IPv4Interface

    try:
        IPv4Interface(value)
        return True
    except Exception:
        return False


def normalize_isolation(value):
    """把隔离开关的多种写法归一化为 '1'/'0'。"""
    return (
        "1"
        if str(value or "").strip().lower()
        in {"1", "on", "true", "yes", "y", "enabled"}
        else "0"
    )


def is_isolation_enabled(value):
    """隔离开关是否开启。"""
    return normalize_isolation(value) == "1"
