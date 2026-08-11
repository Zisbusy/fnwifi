# -*- coding: utf-8 -*-
"""
clients.py —— 终端（客户端）列表

- 解析 iw station dump（信号/时长/流量）
- 通过邻居表 + dnsmasq 租约 + 反向解析，把 MAC 对应到 IP 和主机名
- 提供客户端列表组装与下线（kick）接口
"""

import glob
import re

import config
import paths
import util


# ---------------------------------------------------------------------------
# iw station dump 解析
# ---------------------------------------------------------------------------
def parse_station_dump(text):
    """解析 `iw dev <iface> station dump` 输出为客户端 dict 列表。"""
    stations = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Station "):
            if current:
                stations.append(current)
            current = {
                "mac": line.split()[1].lower(),
                "signalDbm": None,
                "connectedSeconds": None,
                "rxBytes": None,
                "txBytes": None,
            }
        elif current is not None and line.startswith("signal:"):
            match = re.search(r"signal:\s*(-?\d+)", line)
            if match:
                current["signalDbm"] = int(match.group(1))
        elif current is not None and line.startswith("connected time:"):
            match = re.search(r"connected time:\s*(\d+)", line)
            if match:
                current["connectedSeconds"] = int(match.group(1))
        elif current is not None and line.startswith("rx bytes:"):
            match = re.search(r"rx bytes:\s*(\d+)", line)
            if match:
                current["rxBytes"] = int(match.group(1))
        elif current is not None and line.startswith("tx bytes:"):
            match = re.search(r"tx bytes:\s*(\d+)", line)
            if match:
                current["txBytes"] = int(match.group(1))
    if current:
        stations.append(current)
    return stations


# ---------------------------------------------------------------------------
# IP <-> MAC 关联（邻居表 + 租约）
# ---------------------------------------------------------------------------
def ipv4_in_cidr(ip_addr, cidr):
    """IP 是否属于该 CIDR。"""
    try:
        from ipaddress import IPv4Interface

        network = IPv4Interface(cidr).network
        host = IPv4Interface(f"{ip_addr}/32").ip
        return host in network
    except Exception:
        return False


def filter_ip_for_hotspot(ip_addr, cidr):
    """只保留属于热点网段的 IP（避免混入主网络的其他条目）。"""
    return ip_addr if ip_addr and cidr and ipv4_in_cidr(ip_addr, cidr) else ""


def parse_neighbors(hotspot_dev, cidr):
    """从邻居表取热点网卡下所有 (mac -> ip)。"""
    if not util.command_exists("ip"):
        return {}
    ok, stdout, _ = util.run_ok(["ip", "neigh", "show", "dev", hotspot_dev])
    if not ok:
        return {}
    neighbors = {}
    for line in stdout.splitlines():
        parts = line.split()
        if "lladdr" not in parts:
            continue
        index = parts.index("lladdr")
        if index + 1 >= len(parts):
            continue
        ip_addr = filter_ip_for_hotspot(parts[0], cidr)
        mac = parts[index + 1].lower()
        if ip_addr:
            neighbors[mac] = ip_addr
    return neighbors


def parse_lease_hosts(cidr):
    """从各 dnsmasq 租约文件收集 (mac -> hostname) / (ip -> hostname) / (mac -> ip)。"""
    hosts_by_mac = {}
    hosts_by_ip = {}
    ip_by_mac = {}
    patterns = [
        "/var/lib/NetworkManager/dnsmasq-*.leases",
        "/var/lib/misc/dnsmasq.leases",
        "/tmp/dnsmasq.leases",
        paths.DNSMASQ_LEASE_FILE,
    ]
    for pattern in patterns:
        if "*" in pattern:
            paths_list = sorted(glob.glob(pattern))
        else:
            paths_list = [pattern]
        for lease_path in paths_list:
            for line in util.read_text(lease_path).splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                mac = parts[1].lower()
                ip_addr = filter_ip_for_hotspot(parts[2], cidr)
                host = parts[3]
                if host not in {"", "*", "-"}:
                    hosts_by_mac[mac] = host
                if ip_addr:
                    ip_by_mac[mac] = ip_addr
                    if host not in {"", "*", "-"}:
                        hosts_by_ip[ip_addr] = host
    return hosts_by_mac, hosts_by_ip, ip_by_mac


def resolve_hostname(ip_addr):
    """反向解析主机名（getent hosts）。"""
    if not ip_addr or not util.command_exists("getent"):
        return ""
    ok, stdout, _ = util.run_ok(["getent", "hosts", ip_addr])
    if not ok:
        return ""
    parts = stdout.split()
    return parts[1] if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# 客户端列表组装
# ---------------------------------------------------------------------------
def build_clients(cfg):
    """组装当前连接的客户端列表（供前端展示与网速计算）。"""
    config.ensure_iface(cfg)
    nat_state = load_nat_state()
    hotspot_dev = nat_state["HOTSPOT_IFACE"] or cfg["IFACE"]

    # hostapd 未运行（接口不是 AP 模式）时直接返回空
    if util.command_exists("iw"):
        ok, stdout, _ = util.run_ok(["iw", "dev", hotspot_dev, "info"])
        if ok and "type AP" not in stdout:
            return []

    stations = []
    if util.command_exists("iw"):
        ok, stdout, _ = util.run_ok(["iw", "dev", hotspot_dev, "station", "dump"])
        if ok:
            stations = parse_station_dump(stdout)

    ip_cidr = config.effective_ip_cidr(cfg)
    neighbors = parse_neighbors(hotspot_dev, ip_cidr)
    hosts_by_mac, hosts_by_ip, ip_by_mac = parse_lease_hosts(ip_cidr)

    clients = []
    seen = set()

    def emit_client(
        mac, ip_addr, signal=None, connected=None, rx_bytes=None, tx_bytes=None
    ):
        """去重后追加一个客户端（附带可解析的主机名）。"""
        mac = (mac or "").lower()
        if not mac or mac in seen:
            return
        seen.add(mac)
        hostname = hosts_by_mac.get(mac, "")
        if not hostname and ip_addr:
            hostname = hosts_by_ip.get(ip_addr, "") or resolve_hostname(ip_addr)
        item = {"mac": mac}
        if hostname:
            item["hostname"] = hostname
        if ip_addr:
            item["ip"] = ip_addr
        if signal is not None:
            item["signalDbm"] = signal
        if connected is not None:
            item["connectedSeconds"] = connected
        if rx_bytes is not None:
            item["rxBytes"] = rx_bytes
        if tx_bytes is not None:
            item["txBytes"] = tx_bytes
        clients.append(item)

    for station in stations:
        ip_addr = ip_by_mac.get(station["mac"], "") or neighbors.get(station["mac"], "")
        emit_client(
            station["mac"],
            ip_addr,
            station.get("signalDbm"),
            station.get("connectedSeconds"),
            station.get("rxBytes"),
            station.get("txBytes"),
        )
    # station dump 为空（部分驱动不支持）时，退化为只列邻居表
    if not stations:
        for mac, ip_addr in neighbors.items():
            emit_client(mac, ip_addr)
    return clients


def load_nat_state():
    """读取 NAT 状态（热点网卡等），供 build_clients 使用。"""
    data = util.load_shell_state(paths.NAT_STATE_FILE)
    return {
        "HOTSPOT_IFACE": data.get("HOTSPOT_IFACE", ""),
        "NAT_UPLINK_IFACE": data.get("NAT_UPLINK_IFACE", ""),
        "HOTSPOT_PARENT_IFACE": data.get("HOTSPOT_PARENT_IFACE", ""),
        "HOTSPOT_VIRTUAL_IFACE": data.get("HOTSPOT_VIRTUAL_IFACE", ""),
    }


# ---------------------------------------------------------------------------
# 下线（kick）
# ---------------------------------------------------------------------------
def kick_client(cfg, mac):
    """强制下线一个客户端。返回 (是否成功, 输出文案)。"""
    config.ensure_iface(cfg)
    nat_state = load_nat_state()
    hotspot_dev = nat_state["HOTSPOT_IFACE"] or cfg["IFACE"]
    if not hotspot_dev:
        return False, "未检测到 Wi-Fi 网卡"
    if not util.command_exists("iw"):
        return False, "未找到 iw 命令"
    ok, stdout, stderr = util.run_ok(["iw", "dev", hotspot_dev, "station", "del", mac])
    out = stdout or stderr
    if ok:
        # 同时清理邻居表条目，防止残留 ARP 记录
        if util.command_exists("ip"):
            ok_neigh, neigh_stdout, _ = util.run_ok(
                ["ip", "neigh", "show", "dev", hotspot_dev]
            )
            if ok_neigh:
                for line in neigh_stdout.splitlines():
                    parts = line.split()
                    if "lladdr" in parts:
                        index = parts.index("lladdr")
                        if index + 1 < len(parts) and parts[index + 1].lower() == mac:
                            util.run_cmd(
                                ["ip", "neigh", "del", parts[0], "dev", hotspot_dev]
                            )
                            break
        return True, out
    return False, f"下线失败：{out}"
