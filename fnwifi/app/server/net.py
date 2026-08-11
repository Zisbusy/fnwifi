# -*- coding: utf-8 -*-
"""
net.py —— 网络核心逻辑

包含热点启停所需的所有底层操作：
- 监管域（regdom）设置与 regulatory.db 链接瞬态切换
- hostapd 配置生成与启停
- 自管 dnsmasq（仅 DHCP）
- iptables：NAT / 端口策略 / 网络隔离
- 接口 IP、虚拟 AP 接口（STA+AP 并发）、NetworkManager 协作
- 顶层流程：perform_start / perform_stop / restore_iface

设计原则（尽量不影响宿主环境）：
- 所有系统改动均为运行期瞬态，停止/卸载时精确还原；
- 自带静态二进制优先，回退系统命令；
- 不修改系统配置文件（除 regulatory.db 链接的瞬态切换，停止即恢复）。
"""

import os
import re
import signal
import time
from ipaddress import IPv4Interface

import config
import paths
import util


# ===========================================================================
# 监管域（regdom）
# ===========================================================================
def iw_reg_country():
    """读取当前系统监管域国家码，读不到返回空串。"""
    if not util.command_exists("iw"):
        return ""
    ok, stdout, _ = util.run_ok(["iw", "reg", "get"])
    if not ok:
        return ""
    for line in stdout.splitlines():
        match = re.match(r"^country\s+([A-Za-z0-9]{2}):", line)
        if match:
            return match.group(1)
    return ""


def iw_channels_for_band(band):
    """从 iw list 解析某频段的信道列表。

    返回形如 "36:5180:supported" / "149:5745:disabled" 的条目，
    disabled 表示当前监管域下该信道不可用。
    """
    if not util.command_exists("iw"):
        return []
    ok, stdout, _ = util.run_ok(["iw", "list"])
    if not ok:
        return []
    band_pat = "Band 1:" if band in {"bg", "2.4g", "2g"} else "Band 2:"
    in_band = False
    channels = []
    for line in stdout.splitlines():
        if re.match(rf"^\s*{re.escape(band_pat)}", line):
            in_band = True
            continue
        if in_band and re.match(r"^\s*Band", line):
            in_band = False
        if not in_band:
            continue
        match = re.match(r"^\s*\*?\s*([0-9]+) MHz \[([0-9]+)\](.*)$", line)
        if not match:
            continue
        freq, channel, tail = match.groups()
        state = "disabled" if ("disabled" in tail or "no IR" in tail) else "supported"
        channels.append(f"{channel}:{freq}:{state}")
    return channels


def iw_channel_line(channel):
    """取指定信道的原始 iw list 行（用于判断 disabled / no IR）。"""
    if not channel or not util.command_exists("iw"):
        return ""
    ok, stdout, _ = util.run_ok(["iw", "list"])
    if not ok:
        return ""
    for line in stdout.splitlines():
        if re.match(rf"^\s*\*\s+[0-9]+ MHz \[{re.escape(str(channel))}\].*$", line):
            return line.strip()
    return ""


def validate_runtime_channel(cfg):
    """检查配置信道在当前监管域下是否可用；不可用返回中文错误，可用返回 None。"""
    line = iw_channel_line(cfg.get("CHANNEL", ""))
    if not line:
        return None
    regdom = iw_reg_country() or "00"
    channel = cfg.get("CHANNEL", "")
    if "disabled" in line:
        return f"信道：{channel} 已被禁用（监管域={regdom}）"
    if "no IR" in line:
        return (
            f"信道：{channel} 标记为 'no IR'（监管域={regdom}），"
            "可能不允许开启热点。建议改用 2.4G (bg)。"
        )
    return None


def regdom_diagnose():
    """regdom=00 时的只读诊断：返回内核监管库报错 + alternatives 链接指向。

    不做任何修改，仅帮助定位 regulatory.db 问题。
    """
    hints = []
    ok, stdout, _ = util.run_ok(["dmesg"])
    if ok:
        for line in stdout.splitlines():
            low = line.lower()
            if "regulatory" in low or "cfg80211" in low:
                if any(
                    word in low
                    for word in ("malformed", "signature", "failed", "error")
                ):
                    hints.append(line.strip())
    for path in ("/etc/alternatives/regulatory.db", "/etc/alternatives/regulatory.db.p7s"):
        try:
            hints.append(f"{path} -> {os.path.realpath(path)}")
        except OSError:
            pass
    return hints


def apply_regdom(country):
    """设置监管域并验证是否生效，失败自动重试最多 3 次。

    已是目标国家时直接返回，不做 reload/set——避免重复把 country 推送给
    固件（如 brcmfmac 无 CLM blob 时，反复推送会把驱动状态机搞坏，
    后续 AP 启动全部 ENETDOWN，需重启恢复）。
    """
    country = util.trim(country).upper()
    if not country or country == "00":
        return True
    if not util.command_exists("iw"):
        return False
    if iw_reg_country() == country:
        return True
    # 仅当 regdom 不对时才重载 db 并设置(开机时可能读到 initramfs 旧副本)
    util.run_ok(["iw", "reg", "reload"])
    for _attempt in range(3):
        _ok, _, _ = util.run_ok(["iw", "reg", "set", country])
        time.sleep(1)
        if iw_reg_country() == country:
            return True
    return False


def ensure_regdom(country):
    """仅运行时设置监管域(iw reg set)，不写任何系统文件；重启后自动复原。"""
    country = util.trim(country).upper()
    if not country or country == "00":
        return True
    return apply_regdom(country)


# ---------------------------------------------------------------------------
# regulatory.db alternatives 链接瞬态切换
# ---------------------------------------------------------------------------
def readlink_safe(path):
    try:
        return os.readlink(path)
    except OSError:
        return ""


def set_symlink(target, link):
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass
    try:
        os.symlink(target, link)
    except OSError:
        pass


def fix_regulatory_links():
    """开启热点前确保 regulatory.db 链接指向内核可验签的 -upstream 变体。

    仅当当前指向 -debian（内核验签失败→regdom=00）且 -upstream 文件存在时修改；
    原指向记录到状态文件，停止/卸载时恢复。只改链接，不重载内核。
    """
    if not (os.path.isfile(paths.REGDB_UP_DB) and os.path.isfile(paths.REGDB_UP_P7S)):
        return
    cur_db = readlink_safe(paths.REGDB_ALT_DB)
    cur_p7s = readlink_safe(paths.REGDB_ALT_P7S)
    if cur_db == paths.REGDB_UP_DB and cur_p7s == paths.REGDB_UP_P7S:
        return
    if not os.path.isfile(paths.REGDB_STATE_FILE):
        util.write_shell_state(
            paths.REGDB_STATE_FILE,
            {"REGDB_DB": cur_db, "REGDB_P7S": cur_p7s},
        )
    if cur_db != paths.REGDB_UP_DB:
        set_symlink(paths.REGDB_UP_DB, paths.REGDB_ALT_DB)
    if cur_p7s != paths.REGDB_UP_P7S:
        set_symlink(paths.REGDB_UP_P7S, paths.REGDB_ALT_P7S)


def restore_regulatory_links():
    """停止/卸载时把链接恢复为开启前的指向。只改链接，不重载内核：
    内核运行态随重启自然还原，避免反复 reload 向固件推送 country 导致驱动异常。
    """
    if not os.path.isfile(paths.REGDB_STATE_FILE):
        return
    state = util.load_shell_state(paths.REGDB_STATE_FILE)
    old_db = state.get("REGDB_DB", "")
    old_p7s = state.get("REGDB_P7S", "")
    if old_db:
        set_symlink(old_db, paths.REGDB_ALT_DB)
    if old_p7s:
        set_symlink(old_p7s, paths.REGDB_ALT_P7S)
    try:
        os.remove(paths.REGDB_STATE_FILE)
    except FileNotFoundError:
        pass


# ===========================================================================
# 无线驱动信息（用于低功率告警等）
# ===========================================================================
def wifi_driver_name(device):
    """查询网卡驱动名（ethtool -i）。"""
    if not device or not util.command_exists("ethtool"):
        return ""
    ok, stdout, _ = util.run_ok(["ethtool", "-i", device])
    if not ok:
        return ""
    for line in stdout.splitlines():
        if line.startswith("driver:"):
            return util.trim(line.split(":", 1)[1])
    return ""


def wifi_txpower_dbm(device):
    """查询网卡当前发射功率(dBm)，取不到返回空串。"""
    if not device or not util.command_exists("iw"):
        return ""
    ok, stdout, _ = util.run_ok(["iw", "dev", device, "info"])
    if not ok:
        return ""
    match = re.search(r"txpower\s+([0-9.]+)\s+dBm", stdout)
    return match.group(1) if match else ""


def wifi_txpower_is_suspiciously_low(device):
    """功率是否异常低（<= 3.5 dBm）。"""
    tx_power = wifi_txpower_dbm(device)
    if not tx_power:
        return False
    try:
        return float(tx_power) <= 3.5
    except ValueError:
        return False


def wifi_low_power_notice(device):
    """mt7921e 等网卡低功率时的提示文案（中文）。"""
    driver = wifi_driver_name(device) or "unknown"
    tx_power = wifi_txpower_dbm(device) or "unknown"
    if driver == "mt7921e" and wifi_txpower_is_suspiciously_low(device):
        return (
            f"警告：驱动 '{driver}' 当前发射功率很低（{tx_power} dBm）。"
            "热点可以开启，但发现设备或覆盖范围可能较差。建议先使用 2.4GHz/20MHz；"
            "如果覆盖仍然很弱，通常指向 mt7921e 驱动/固件功率问题，而不是热点配置问题。"
        )
    return ""


# ===========================================================================
# 路由 / 上联网卡
# ===========================================================================
def detect_route_dev(target="1.1.1.1"):
    """取访问目标 IP 的出口网卡（默认路由）。"""
    if not util.command_exists("ip"):
        return ""
    ok, stdout, _ = util.run_ok(["ip", "-4", "route", "get", target])
    if not ok:
        return ""
    parts = stdout.split()
    for idx, token in enumerate(parts):
        if token == "dev" and idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


# ===========================================================================
# 状态文件（NAT / 热点开关）
# ===========================================================================
def write_nat_state(hotspot_iface, uplink_iface, parent_iface="", virtual_iface=""):
    """记录 NAT 相关网卡信息，供停止时精确清理。"""
    util.write_shell_state(
        paths.NAT_STATE_FILE,
        {
            "HOTSPOT_IFACE": hotspot_iface,
            "NAT_UPLINK_IFACE": uplink_iface,
            "HOTSPOT_PARENT_IFACE": parent_iface,
            "HOTSPOT_VIRTUAL_IFACE": virtual_iface,
        },
    )


def load_nat_state():
    data = util.load_shell_state(paths.NAT_STATE_FILE)
    return {
        "HOTSPOT_IFACE": data.get("HOTSPOT_IFACE", ""),
        "NAT_UPLINK_IFACE": data.get("NAT_UPLINK_IFACE", ""),
        "HOTSPOT_PARENT_IFACE": data.get("HOTSPOT_PARENT_IFACE", ""),
        "HOTSPOT_VIRTUAL_IFACE": data.get("HOTSPOT_VIRTUAL_IFACE", ""),
    }


def clear_nat_state():
    try:
        os.remove(paths.NAT_STATE_FILE)
    except FileNotFoundError:
        pass


def write_hotspot_state(enabled):
    """记录热点开关状态（用于开机自动恢复）。"""
    paths.ensure_data_dir()
    normalized = "1" if str(enabled).lower() in {"1", "true"} else "0"
    with open(paths.HOTSPOT_STATE_FILE, "w", encoding="utf-8") as handle:
        handle.write(f"HOTSPOT_ENABLED={normalized}\n")
        handle.write(f"ENABLED={util.shell_quote(normalized)}\n")


# ===========================================================================
# 自管 dnsmasq（仅 DHCP，不占 53 端口）
# ===========================================================================
def stop_local_dnsmasq():
    """停掉自管 dnsmasq 并清理 PID/配置文件。"""
    pid = util.read_pid_file(paths.DNSMASQ_PID_FILE)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, OSError):
                break
            time.sleep(0.1)
    for path in (paths.DNSMASQ_PID_FILE, paths.DNSMASQ_CONF_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def write_local_dnsmasq_config(hotspot_iface, cfg):
    """生成 dnsmasq 配置文件（仅 DHCP 服务）。返回局域网细节。"""
    details = config.hotspot_lan_details(config.effective_ip_cidr(cfg))
    if not details:
        raise ValueError("IP/CIDR：IPv4 CIDR 不合法（例如 192.168.80.1/24）")
    resolvers = config.system_nameservers(
        strip_private=util.is_isolation_enabled(cfg.get("ISOLATION", "1"))
    )
    lines = [
        "port=0",  # 不启用 DNS，避免与系统 dnsmasq 抢 53 端口
        "bind-interfaces",
        "except-interface=lo",
        "dhcp-authoritative",
        f"interface={hotspot_iface}",
        f"listen-address={details['gateway']}",
        f"dhcp-range={details['start']},{details['end']},{details['netmask']},1h",
        f"dhcp-option=option:router,{details['gateway']}",
        f"dhcp-option=option:dns-server,{','.join(resolvers)}",
        f"pid-file={paths.DNSMASQ_PID_FILE}",
        f"dhcp-leasefile={paths.DNSMASQ_LEASE_FILE}",
    ]
    paths.ensure_data_dir()
    with open(paths.DNSMASQ_CONF_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return details


def start_local_dnsmasq(hotspot_iface, cfg):
    """启动自管 dnsmasq，成功返回 (True, "")，失败返回 (False, 中文错误)。"""
    if not util.command_exists("dnsmasq"):
        return False, "未找到 dnsmasq 命令"
    stop_local_dnsmasq()
    try:
        write_local_dnsmasq_config(hotspot_iface, cfg)
    except ValueError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"dnsmasq 配置写入失败：{exc}"
    ok, stdout, stderr = util.run_ok(
        ["dnsmasq", "--test", f"--conf-file={paths.DNSMASQ_CONF_FILE}"]
    )
    if not ok:
        return False, f"dnsmasq 启动失败：{util.sanitize_text(stderr or stdout)}"
    ok, stdout, stderr = util.run_ok(["dnsmasq", f"--conf-file={paths.DNSMASQ_CONF_FILE}"])
    if not ok:
        return False, f"dnsmasq 启动失败：{util.sanitize_text(stderr or stdout)}"
    return True, ""


# ===========================================================================
# 端口放行策略（iptables 自定义链）
# ===========================================================================
PORTS_CHAIN = "FN_HOTSPOT_PORTS"


def load_ports_state():
    """读取端口策略状态：(iface, rules)。"""
    iface = ""
    rules = []
    if not os.path.isfile(paths.PORTS_STATE_FILE):
        return iface, rules
    try:
        with open(paths.PORTS_STATE_FILE, "r", encoding="utf-8") as handle:
            lines = [line.rstrip("\n") for line in handle]
    except OSError:
        return iface, rules
    if lines and lines[0].startswith("iface\t"):
        iface = lines[0].split("\t", 1)[1]
        payload = lines[1:]
    else:
        payload = lines
    for line in payload:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        proto, start, end = parts
        try:
            rules.append((proto, int(start), int(end)))
        except ValueError:
            continue
    return iface, rules


def write_ports_state(iface, rules):
    paths.ensure_data_dir()
    with open(paths.PORTS_STATE_FILE, "w", encoding="utf-8") as handle:
        handle.write(f"iface\t{iface}\n")
        for proto, start, end in rules:
            handle.write(f"{proto}\t{start}\t{end}\n")


def remove_allow_ports():
    """移除端口策略链及其 INPUT 挂载。"""
    if not util.command_exists("iptables"):
        return
    iface, _rules = load_ports_state()
    if iface:
        util.run_cmd(["iptables", "-D", "INPUT", "-i", iface, "-j", PORTS_CHAIN])
    util.run_cmd(["iptables", "-F", PORTS_CHAIN])
    util.run_cmd(["iptables", "-X", PORTS_CHAIN])
    try:
        os.remove(paths.PORTS_STATE_FILE)
    except FileNotFoundError:
        pass


def apply_allow_ports(hotspot_iface, spec):
    """端口策略（三态，与隔离开关无关，统一挂 INPUT）：

    - 全部放行：填 *
    - 全部拦截：留空
    - 部分端口：填端口列表（如 80,443,67-68/udp）

    规则链：ESTABLISHED/DHCP/DNS 放行 -> 白名单 ACCEPT -> 末尾 DROP（全部放行时无 DROP）。
    """
    if not hotspot_iface or not util.command_exists("iptables"):
        return
    remove_allow_ports()
    util.run_cmd(["iptables", "-N", PORTS_CHAIN])
    util.run_cmd(["iptables", "-F", PORTS_CHAIN])
    # 已建立连接/回程流量
    util.run_cmd(
        [
            "iptables",
            "-A",
            PORTS_CHAIN,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ]
    )
    # DHCP / DNS（联网必需）
    util.run_cmd(["iptables", "-A", PORTS_CHAIN, "-p", "udp", "--dport", "67", "-j", "ACCEPT"])
    util.run_cmd(["iptables", "-A", PORTS_CHAIN, "-p", "udp", "--dport", "53", "-j", "ACCEPT"])
    util.run_cmd(["iptables", "-A", PORTS_CHAIN, "-p", "tcp", "--dport", "53", "-j", "ACCEPT"])
    try:
        rules = config.allow_ports_to_rules(spec)
    except ValueError:
        rules = []
    is_all = bool(rules and rules[0][0] == "ALL")
    if is_all:
        # 全部放行：直接 ACCEPT 全部
        util.run_cmd(["iptables", "-A", PORTS_CHAIN, "-j", "ACCEPT"])
    else:
        for proto, start, end in rules:
            dport = str(start) if start == end else f"{start}:{end}"
            util.run_cmd(
                [
                    "iptables",
                    "-A",
                    PORTS_CHAIN,
                    "-p",
                    proto,
                    "--dport",
                    dport,
                    "-j",
                    "ACCEPT",
                ]
            )
        # 全部拦截(空)或部分端口：其余一律丢弃，不依赖系统默认策略
        util.run_cmd(["iptables", "-A", PORTS_CHAIN, "-j", "DROP"])
    # 挂到 INPUT 最前
    util.run_cmd(["iptables", "-I", "INPUT", "1", "-i", hotspot_iface, "-j", PORTS_CHAIN])
    write_ports_state(hotspot_iface, rules)


# ===========================================================================
# 网络隔离（客户端禁止访问主网段）
# ===========================================================================
def lan_cidrs_for_uplink(uplink):
    """获取上联网卡所在的所有主网段(排除回环)，归一化为网络地址形式。"""
    cidrs = []
    if not uplink or not util.command_exists("ip"):
        return cidrs
    ok, stdout, _ = util.run_ok(["ip", "-4", "-o", "addr", "show", "dev", uplink])
    if not ok:
        return cidrs
    for line in stdout.splitlines():
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if match:
            try:
                network = IPv4Interface(match.group(1)).network
            except Exception:
                continue
            cidr = str(network)
            if not cidr.startswith("127."):
                cidrs.append(cidr)
    return list(dict.fromkeys(cidrs))


def write_isolation_state(hotspot_iface, uplink, lan_cidrs, hotspot_cidr):
    util.write_shell_state(
        paths.ISOLATION_STATE_FILE,
        {
            "HOTSPOT_IFACE": hotspot_iface,
            "UPLINK_IFACE": uplink,
            "LAN_CIDRS": ",".join(lan_cidrs),
            "HOTSPOT_CIDR": hotspot_cidr,
        },
    )


def load_isolation_state():
    data = util.load_shell_state(paths.ISOLATION_STATE_FILE)
    return {
        "HOTSPOT_IFACE": data.get("HOTSPOT_IFACE", ""),
        "UPLINK_IFACE": data.get("UPLINK_IFACE", ""),
        "LAN_CIDRS": [
            item.strip() for item in data.get("LAN_CIDRS", "").split(",") if item.strip()
        ],
        "HOTSPOT_CIDR": data.get("HOTSPOT_CIDR", ""),
    }


def remove_isolation():
    """移除隔离规则(FORWARD 主网段 DROP)。"""
    if not util.command_exists("iptables"):
        return
    state = load_isolation_state()
    hotspot = state["HOTSPOT_IFACE"]
    for cidr in state["LAN_CIDRS"]:
        if hotspot:
            util.run_cmd(
                ["iptables", "-D", "FORWARD", "-i", hotspot, "-d", cidr, "-j", "DROP"]
            )
    try:
        os.remove(paths.ISOLATION_STATE_FILE)
    except FileNotFoundError:
        pass


def apply_isolation(hotspot_iface, cfg):
    """网络隔离：热点客户端不能访问主网段设备(FORWARD DROP)。

    注：热点访问本机端口的策略由端口链 FN_HOTSPOT_PORTS 负责，与隔离无关。
    """
    if not hotspot_iface or not util.command_exists("iptables"):
        return
    remove_isolation()
    if not util.is_isolation_enabled(cfg.get("ISOLATION", "1")):
        return
    uplink = cfg.get("UPLINK_IFACE", "") or detect_route_dev("1.1.1.1")
    lan_cidrs = lan_cidrs_for_uplink(uplink)
    hotspot_cidr = config.effective_ip_cidr(cfg)
    # FORWARD:热点 -> 主网段直接丢弃(插到最前,优先于 NAT 放行规则)
    for cidr in lan_cidrs:
        if cidr == hotspot_cidr:
            continue
        util.run_cmd(
            [
                "iptables",
                "-I",
                "FORWARD",
                "1",
                "-i",
                hotspot_iface,
                "-d",
                cidr,
                "-j",
                "DROP",
            ]
        )
    write_isolation_state(hotspot_iface, uplink, lan_cidrs, hotspot_cidr)


# ===========================================================================
# NAT / IP 转发
# ===========================================================================
def ensure_ip_forward():
    """开启内核 IPv4 转发（运行期瞬态，停止时还原原值）。"""
    if util.command_exists("sysctl"):
        util.run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    ok, stdout, _ = util.run_ok(["sysctl", "-n", "net.ipv4.ip_forward"])
    if ok and util.trim(stdout) != "1":
        print("WARN: net.ipv4.ip_forward could not be enabled")


def iptables_apply_nat(hotspot, uplink):
    """添加 NAT 与 FORWARD 放行规则（幂等：已存在则跳过）。"""
    if not hotspot or not uplink or not util.command_exists("iptables"):
        return
    checks = [
        (
            ["iptables", "-t", "nat", "-C", "POSTROUTING", "-o", uplink, "-j", "MASQUERADE"],
            ["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", uplink, "-j", "MASQUERADE"],
        ),
        (
            ["iptables", "-C", "FORWARD", "-i", hotspot, "-o", uplink, "-j", "ACCEPT"],
            ["iptables", "-A", "FORWARD", "-i", hotspot, "-o", uplink, "-j", "ACCEPT"],
        ),
        (
            ["iptables", "-C", "FORWARD", "-i", uplink, "-o", hotspot, "-j", "ACCEPT"],
            ["iptables", "-A", "FORWARD", "-i", uplink, "-o", hotspot, "-j", "ACCEPT"],
        ),
    ]
    for check_cmd, add_cmd in checks:
        ok, _, _ = util.run_ok(check_cmd)
        if not ok:
            util.run_cmd(add_cmd)


def iptables_remove_nat(hotspot, uplink):
    """移除 NAT 与 FORWARD 放行规则。"""
    if not hotspot or not uplink or not util.command_exists("iptables"):
        return
    util.run_cmd(["iptables", "-t", "nat", "-D", "POSTROUTING", "-o", uplink, "-j", "MASQUERADE"])
    util.run_cmd(["iptables", "-D", "FORWARD", "-i", hotspot, "-o", uplink, "-j", "ACCEPT"])
    util.run_cmd(["iptables", "-D", "FORWARD", "-i", uplink, "-o", hotspot, "-j", "ACCEPT"])


def apply_hotspot_nat(hotspot, uplink, parent_iface="", virtual_iface=""):
    """应用 NAT：自动探测上联网卡，写入状态并验证规则生效。"""
    if not hotspot:
        return
    if not uplink:
        uplink = detect_route_dev("1.1.1.1")
    write_nat_state(hotspot, uplink or "", parent_iface or "", virtual_iface or "")
    if not uplink:
        print("WARN: no uplink iface detected, NAT skipped")
        return
    ensure_ip_forward()
    iptables_apply_nat(hotspot, uplink)
    # 验证 NAT 规则是否真的生效(失败原因会打进应用日志)
    ok, _, err = util.run_ok(
        ["iptables", "-t", "nat", "-C", "POSTROUTING", "-o", uplink, "-j", "MASQUERADE"]
    )
    if not ok:
        print(
            f"WARN: NAT rule verification failed on {uplink}: "
            f"{util.sanitize_text(err).strip()}"
        )


def remove_hotspot_nat():
    state = load_nat_state()
    if state["HOTSPOT_IFACE"] and state["NAT_UPLINK_IFACE"]:
        iptables_remove_nat(state["HOTSPOT_IFACE"], state["NAT_UPLINK_IFACE"])
    clear_nat_state()


def save_ip_forward_state():
    """备份 ip_forward 原值，供停止时还原。"""
    old = ""
    if util.command_exists("sysctl"):
        ok, stdout, _ = util.run_ok(["sysctl", "-n", "net.ipv4.ip_forward"])
        if ok:
            old = util.trim(stdout)
    util.write_shell_state(paths.SYSCTL_STATE_FILE, {"IP_FORWARD": old})


def restore_ip_forward_state():
    """还原 ip_forward 原值并清理备份。"""
    data = util.load_shell_state(paths.SYSCTL_STATE_FILE)
    old = data.get("IP_FORWARD", "")
    if util.command_exists("sysctl") and old:
        util.run_cmd(["sysctl", "-w", f"net.ipv4.ip_forward={old}"])
    try:
        os.remove(paths.SYSCTL_STATE_FILE)
    except FileNotFoundError:
        pass


# ===========================================================================
# hostapd
# ===========================================================================
def write_hostapd_config(hotspot_iface, cfg):
    """生成 hostapd 配置(仅写入应用自己的数据目录,不碰系统)。

    country_code 固定为 CN：brcmfmac 等驱动的 wiphy 监管域跟随固件报告
    （phy 显示 country 99 时 5G 范围不含 149-165），只有 hostapd 设置
    country_code 才能把 wiphy 切到目标国家。应用启动前已通过
    `iw reg reload + iw reg set` 把 global 设为目标国家，hostapd 此处
    的 country 推送是幂等操作，不会触发固件拒绝。
    """
    band = cfg["BAND"]
    width = cfg["CHANNEL_WIDTH"]
    hw_mode = "g" if band == "bg" else "a"
    lines = [
        f"interface={hotspot_iface}",
        "driver=nl80211",
        "logger_syslog=-1",
        "logger_syslog_level=2",
        f"ssid={cfg['SSID']}",
        f"hw_mode={hw_mode}",
        f"channel={cfg['CHANNEL']}",
        f"country_code={config.DEFAULT_COUNTRY}",
        "wmm_enabled=1",
        "max_num_sta=32",
        "ignore_broadcast_ssid=0",
    ]
    password = cfg.get("PASSWORD", "")
    if password and len(password) >= 8:
        lines += [
            "wpa=2",
            "auth_algs=1",
            f"wpa_passphrase={password}",
            "wpa_key_mgmt=WPA-PSK",
            "rsn_pairwise=CCMP",
            "wpa_group_rekey=0",
        ]
    else:
        lines.append("wpa=0")
    if band == "bg":
        lines.append("ieee80211n=1")
        lines.append("ht_capab=[HT20]" if width == "20" else "ht_capab=[HT40+]")
    else:
        lines.append("ieee80211n=1")
        lines.append("ieee80211ac=1")
        if width == "80":
            lines.append("vht_capab=[VHT80]")
    paths.ensure_data_dir()
    with open(paths.HOSTAPD_CONF_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def hostapd_running():
    """hostapd 是否在运行（按 PID 探活）。"""
    pid = util.read_pid_file(paths.HOSTAPD_PID_FILE)
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_hostapd():
    """停止 hostapd 并清理 PID/日志文件。"""
    pid = util.read_pid_file(paths.HOSTAPD_PID_FILE)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, OSError):
                break
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    for path in (paths.HOSTAPD_PID_FILE, paths.HOSTAPD_LOG_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def start_hostapd(hotspot_iface, cfg):
    """启动 hostapd，成功返回 (True, "")，失败返回 (False, 中文错误)。"""
    if not util.command_exists("hostapd"):
        return False, "未找到 hostapd：请将静态编译的 hostapd 放入 bin/<arch>/ 目录"
    stop_hostapd()
    try:
        write_hostapd_config(hotspot_iface, cfg)
    except OSError as exc:
        return False, f"hostapd 配置写入失败：{exc}"
    ok, stdout, stderr = util.run_ok(
        [
            "hostapd",
            "-B",
            "-P",
            paths.HOSTAPD_PID_FILE,
            "-f",
            paths.HOSTAPD_LOG_FILE,
            paths.HOSTAPD_CONF_FILE,
        ]
    )
    if not ok:
        return False, f"hostapd 启动失败：{util.sanitize_text(stderr or stdout)}"
    time.sleep(2)
    if not hostapd_running():
        log_tail = util.trim(util.read_text(paths.HOSTAPD_LOG_FILE)).splitlines()[-5:]
        return False, "hostapd 启动后很快退出：" + " | ".join(log_tail)
    return True, ""


# ===========================================================================
# 接口 IP 配置
# ===========================================================================
def iface_has_ip(iface, cidr):
    """接口是否已配置指定 IP。"""
    if not util.command_exists("ip"):
        return False
    ok, stdout, _ = util.run_ok(["ip", "-4", "addr", "show", "dev", iface])
    if not ok:
        return False
    ip_part = cidr.split("/")[0]
    return re.search(r"inet\s+" + re.escape(ip_part) + r"\b", stdout) is not None


def iface_flags(iface):
    """读取接口 flags（如 UP,LOWER_UP）。"""
    if not util.command_exists("ip"):
        return ""
    ok, stdout, _ = util.run_ok(["ip", "-o", "link", "show", "dev", iface])
    if not ok:
        return ""
    match = re.search(r"<([^>]*)>", stdout)
    return match.group(1) if match else ""


def iface_is_up(iface):
    """IFF_UP 是否置位(hostapd 在接口 down 时 beacon 设置会返回 ENETDOWN)。"""
    return "UP" in iface_flags(iface).split(",")


def setup_iface_ip(iface, cidr):
    """配置接口 IP(运行时瞬态,停止时清理)。

    注意：NetworkManager unmanage 是异步的，可能在我们 up 之后才把接口 down 掉，
    所以这里拉起后要验证 IFF_UP，失败则重试，最多 ~10 秒。
    """
    if not util.command_exists("ip"):
        return False, "未找到 ip 命令"
    for _attempt in range(10):
        util.run_cmd(["ip", "link", "set", iface, "up"])
        if iface_is_up(iface):
            break
        time.sleep(1)
    if not iface_is_up(iface):
        return False, f"接口 {iface} 无法启用"
    if iface_has_ip(iface, cidr):
        return True, ""
    util.run_cmd(["ip", "addr", "flush", "dev", iface])
    ok, stdout, stderr = util.run_ok(["ip", "addr", "add", cidr, "dev", iface])
    if not ok:
        return False, util.sanitize_text(stderr or stdout)
    return True, ""


# ===========================================================================
# STA+AP 并发（虚拟 AP 接口）
# ===========================================================================
def iw_supports_sta_ap():
    """网卡是否支持 STA+AP 并发（iw list 的 valid interface combinations）。"""
    if not util.command_exists("iw"):
        return False
    ok, stdout, _ = util.run_ok(["iw", "list"])
    if not ok:
        return False
    in_section = False
    for line in stdout.splitlines():
        if "valid interface combinations" in line:
            in_section = True
            continue
        if in_section and line and not line.startswith((" ", "\t")):
            in_section = False
        if (
            in_section
            and line.lstrip().startswith("*")
            and "managed" in line
            and re.search(r"(^|\s)AP(\s|$)", line)
        ):
            return True
    return False


def mk_ap_iface_name(base):
    """为物理网卡生成虚拟 AP 接口名（如 wlan0 -> wlan0ap，最长 15 字符）。"""
    base = util.trim(base)
    suffix = "ap"
    if len(base + suffix) <= 15:
        return base + suffix
    prefix_len = max(1, 15 - len(suffix))
    return base[:prefix_len] + suffix


def ensure_virtual_ap_iface(parent, ap_iface):
    """创建虚拟 AP 接口（已存在则直接返回 True）。"""
    if not parent or not ap_iface or not util.command_exists("iw"):
        return False
    ok, _, _ = util.run_ok(["iw", "dev", ap_iface, "info"])
    if ok:
        return True
    ok, _, _ = util.run_ok(
        ["iw", "dev", parent, "interface", "add", ap_iface, "type", "__ap"]
    )
    if not ok:
        return False
    if util.command_exists("ip"):
        util.run_cmd(["ip", "link", "set", ap_iface, "up"])
    if util.command_exists("nmcli"):
        # hostapd 直接管理该接口，NM 不参与
        util.run_cmd(["nmcli", "dev", "set", ap_iface, "managed", "no"])
    return True


def delete_virtual_ap_iface(iface):
    """删除虚拟 AP 接口。"""
    if not iface or not util.command_exists("iw"):
        return
    ok, _, _ = util.run_ok(["iw", "dev", iface, "info"])
    if not ok:
        return
    if util.command_exists("nmcli"):
        util.run_cmd(["nmcli", "dev", "set", iface, "managed", "no"])
    if util.command_exists("ip"):
        util.run_cmd(["ip", "link", "set", iface, "down"])
    util.run_cmd(["iw", "dev", iface, "del"])


# ===========================================================================
# NetworkManager 协作
# ===========================================================================
def nmcli_connection_down(connection_id):
    if connection_id:
        util.run_cmd(["nmcli", "con", "down", "id", connection_id])


def nmcli_connection_delete(connection_id):
    if connection_id:
        util.run_cmd(["nmcli", "con", "delete", connection_id])


def nmcli_device_disconnect(device):
    if device:
        util.run_cmd(["nmcli", "device", "disconnect", device])


def restore_previous_connection(sta_prev_con):
    """恢复热点开启前网卡连接的 NM 连接。"""
    if sta_prev_con:
        util.run_cmd(["nmcli", "con", "up", "id", sta_prev_con])


def nmcli_ap_mode_supported():
    """驱动是否支持 AP 模式（iw list 有 '* AP'）。"""
    if not util.command_exists("iw"):
        return True
    ok, stdout, _ = util.run_ok(["iw", "list"])
    return bool(ok and re.search(r"^\s*\*\s+AP\b", stdout, flags=re.MULTILINE))


# ===========================================================================
# 顶层流程：撤销 / 启动 / 停止
# ===========================================================================
class StartError(Exception):
    """启动流程错误，携带 HTTP 状态码与中文错误信息。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def restore_iface(hotspot_iface, sta_prev_con, virtual_iface):
    """撤销热点：停 hostapd/dnsmasq、清理 iptables 规则、还原接口与 NM 状态。"""
    remove_hotspot_nat()
    remove_allow_ports()
    remove_isolation()
    stop_local_dnsmasq()
    stop_hostapd()
    restore_ip_forward_state()
    restore_regulatory_links()
    if virtual_iface and virtual_iface != hotspot_iface:
        delete_virtual_ap_iface(virtual_iface)
    if util.command_exists("ip") and hotspot_iface:
        util.run_cmd(["ip", "link", "set", hotspot_iface, "down"])
    if util.command_exists("nmcli") and hotspot_iface:
        util.run_cmd(["nmcli", "dev", "set", hotspot_iface, "managed", "yes"])
        restore_previous_connection(sta_prev_con)


def perform_start():
    """完整启动热点流程，返回 (输出文案, 提示文案)；出错抛 StartError。"""
    cfg = config.load_cfg()
    cfg_error = config.validate_cfg(cfg)
    if cfg_error:
        raise StartError("400 Bad Request", cfg_error)
    # 确保 regulatory.db 链接可被内核验签（瞬态：停止/卸载时恢复）
    fix_regulatory_links()
    # 固定国家码 CN：仅运行时设置监管域，不写任何系统文件
    ensure_regdom(config.DEFAULT_COUNTRY)
    runtime_error = validate_runtime_channel(cfg)
    if runtime_error:
        raise StartError("400 Bad Request", runtime_error)
    remove_allow_ports()
    iface_status = config.require_wifi_iface(cfg)
    if iface_status == 2:
        raise StartError(
            "400 Bad Request",
            "未找到 Wi-Fi 网卡，请检查 'iw dev' / 'nmcli dev status'。",
        )
    if iface_status == 1:
        raise StartError(
            "400 Bad Request",
            f"设备 '{cfg['IFACE']}' 不是 Wi-Fi 网卡。可用 Wi-Fi 网卡：{' '.join(config.wifi_ifaces())}",
        )
    if not nmcli_ap_mode_supported():
        raise StartError(
            "400 Bad Request",
            f"设备 '{cfg['IFACE']}' 不支持 AP/热点模式（iw list 无 '* AP'）。请更换无线网卡。",
        )
    parent_iface = cfg["IFACE"]
    hotspot_iface = cfg["IFACE"]
    virtual_iface = ""
    sta_prev_con = ""
    if util.command_exists("nmcli"):
        ok, stdout, _ = util.run_ok(
            ["nmcli", "-g", "GENERAL.CONNECTION", "dev", "show", cfg["IFACE"]]
        )
        if ok and stdout.splitlines():
            sta_prev_con = util.trim(stdout.splitlines()[0])
            if sta_prev_con == "--":
                sta_prev_con = ""
    # STA+AP 并发：网卡正在连 WiFi 时，建虚拟 AP 接口，避免断开原连接
    if sta_prev_con and iw_supports_sta_ap():
        virtual_iface = mk_ap_iface_name(cfg["IFACE"])
        if ensure_virtual_ap_iface(cfg["IFACE"], virtual_iface):
            hotspot_iface = virtual_iface
        else:
            virtual_iface = ""
    # 共享网卡不能与热点网卡相同（STA+AP 并发时允许与物理网卡相同）
    if cfg["UPLINK_IFACE"] and cfg["UPLINK_IFACE"] == hotspot_iface:
        raise StartError(
            "400 Bad Request",
            f"共享网卡不能与热点网卡相同（{hotspot_iface}），请选择其他网卡或留空自动选择。",
        )
    if (
        cfg["UPLINK_IFACE"]
        and cfg["UPLINK_IFACE"] == cfg["IFACE"]
        and hotspot_iface == cfg["IFACE"]
    ):
        raise StartError(
            "400 Bad Request",
            f"共享网卡不能与热点网卡相同（{cfg['IFACE']}）且不支持 STA+AP 并发。",
        )
    ip_cidr = config.effective_ip_cidr(cfg)
    # 让 NetworkManager 让出接口(运行时状态,重启自动复原;无 NM 时跳过)
    if util.command_exists("nmcli"):
        nmcli_device_disconnect(hotspot_iface)
        util.run_cmd(["nmcli", "dev", "set", hotspot_iface, "managed", "no"])
        # NM 释放设备是异步的，可能把我们后续的 ip link up 又覆盖成 down
        # (hostapd 会报 ENETDOWN)。等它释放完再拉接口。
        time.sleep(2)
    stop_local_dnsmasq()
    stop_hostapd()
    # 接口 IP（瞬态）
    ok, ip_error = setup_iface_ip(hotspot_iface, ip_cidr)
    if not ok:
        restore_iface(hotspot_iface, sta_prev_con, virtual_iface)
        raise StartError("500 Internal Server Error", ip_error)
    # hostapd 建 AP
    ok, hostapd_error = start_hostapd(hotspot_iface, cfg)
    if not ok:
        restore_iface(hotspot_iface, sta_prev_con, virtual_iface)
        raise StartError("500 Internal Server Error", hostapd_error)
    # DHCP(自带 dnsmasq,port=0 只做 DHCP,不占 53 端口,与系统 dnsmasq 无冲突)
    ok, dnsmasq_error = start_local_dnsmasq(hotspot_iface, cfg)
    if not ok:
        restore_iface(hotspot_iface, sta_prev_con, virtual_iface)
        raise StartError("500 Internal Server Error", dnsmasq_error)
    # NAT / 端口策略 / 隔离(瞬态 iptables 规则,停止时精确清理)
    save_ip_forward_state()
    apply_hotspot_nat(hotspot_iface, cfg["UPLINK_IFACE"], parent_iface, virtual_iface)
    apply_allow_ports(hotspot_iface, cfg["ALLOW_PORTS"])
    apply_isolation(hotspot_iface, cfg)
    write_hotspot_state(True)
    return (
        f"hostapd 已在 {hotspot_iface} 启动（SSID {cfg['SSID']}）",
        wifi_low_power_notice(hotspot_iface),
    )


def perform_stop(clear_state=True):
    """完整停止热点流程，返回输出文案。"""
    cfg = config.load_cfg()
    config.ensure_iface(cfg)
    nat_state = load_nat_state()
    hotspot_iface = nat_state["HOTSPOT_IFACE"] or cfg["IFACE"]
    virtual_iface = nat_state["HOTSPOT_VIRTUAL_IFACE"]
    sta_prev_con = ""
    if util.command_exists("nmcli"):
        ok, stdout, _ = util.run_ok(
            ["nmcli", "-g", "GENERAL.CONNECTION", "dev", "show", hotspot_iface]
        )
        if ok and stdout.splitlines():
            sta_prev_con = util.trim(stdout.splitlines()[0])
            if sta_prev_con == "--":
                sta_prev_con = ""
    restore_iface(hotspot_iface, sta_prev_con, virtual_iface)
    if clear_state:
        write_hotspot_state(False)
    return "热点已关闭"
