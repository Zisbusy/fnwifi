# -*- coding: utf-8 -*-
"""
config.py —— 配置管理

- 默认值：SSID=Hotspot，密码=12345678，频段 2.4G，信道 6，带宽 20MHz，隔离开启
- 国家码：固定为 CN（不再提供自定义选项，简化逻辑；监管域设置见 net.py）
- 提供配置的读取/保存/校验，以及无线网卡识别相关函数
"""

import os
import re
from ipaddress import IPv4Interface

import paths
import util


# ---------------------------------------------------------------------------
# 默认配置与固定国家码
# ---------------------------------------------------------------------------
# 固定国家码：不再允许用户自定义，监管域始终尝试设为 CN。
DEFAULT_COUNTRY = "CN"

DEFAULTS = {
    "IFACE": "",             # 热点网卡（空=自动选择第一张无线网卡）
    "UPLINK_IFACE": "",      # 共享上网的网卡（空=自动取默认路由）
    "IP_CIDR": "192.168.80.1/24",
    "ALLOW_PORTS": "*",      # 本机端口策略：* 全部放行 / 空 全部拦截 / 列表 部分放行
    "SSID": "Hotspot",
    "PASSWORD": "12345678",
    "BAND": "bg",            # bg=2.4G, a=5G
    "CHANNEL": "6",
    "CHANNEL_WIDTH": "20",
    "ISOLATION": "1",        # 网络隔离开关
}

# 可持久化的配置键（不含国家码：国家码固定为 CN，不写入配置）
CONFIG_KEYS = [
    "IFACE",
    "UPLINK_IFACE",
    "IP_CIDR",
    "ALLOW_PORTS",
    "SSID",
    "PASSWORD",
    "BAND",
    "CHANNEL",
    "CHANNEL_WIDTH",
    "ISOLATION",
]


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def load_cfg():
    """读取配置：磁盘上的值覆盖默认值，并做归一化。"""
    cfg = dict(DEFAULTS)
    stored = util.load_shell_state(paths.CFG_FILE)
    for key in CONFIG_KEYS:
        if key in stored:
            cfg[key] = stored[key]
    cfg["IFACE"] = normalize_parent_wifi_iface(cfg["IFACE"])
    cfg["ISOLATION"] = util.normalize_isolation(cfg.get("ISOLATION", "1"))
    return cfg


def save_cfg(cfg):
    """保存配置到数据目录；失败返回 False。"""
    mapping = {
        "IFACE": normalize_parent_wifi_iface(cfg.get("IFACE", "")),
        "UPLINK_IFACE": cfg.get("UPLINK_IFACE", ""),
        "IP_CIDR": cfg.get("IP_CIDR", ""),
        "ALLOW_PORTS": cfg.get("ALLOW_PORTS", ""),
        "SSID": cfg.get("SSID", ""),
        "PASSWORD": cfg.get("PASSWORD", ""),
        "BAND": cfg.get("BAND", ""),
        "CHANNEL": cfg.get("CHANNEL", ""),
        "CHANNEL_WIDTH": cfg.get("CHANNEL_WIDTH", ""),
        "ISOLATION": util.normalize_isolation(cfg.get("ISOLATION", "1")),
    }
    try:
        util.write_shell_state(paths.CFG_FILE, mapping)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 无线网卡识别
# ---------------------------------------------------------------------------
def normalize_parent_wifi_iface(iface):
    """去掉 STA+AP 虚拟接口的 'ap' 后缀，还原为物理网卡名。

    例如 wlan0ap -> wlan0（若 wlan0 真实存在）。
    """
    iface = util.trim(iface)
    if not iface or not util.command_exists("iw"):
        return iface
    current = iface
    while current.endswith("ap") and len(current) > 2:
        candidate = current[:-2]
        ok, _, _ = util.run_ok(["iw", "dev", candidate, "info"])
        if not ok:
            break
        current = candidate
    return current


def wifi_ifaces():
    """列出系统所有无线网卡（优先 nmcli，回退 iw）。"""
    values = []
    if util.command_exists("nmcli"):
        ok, stdout, _ = util.run_ok(["nmcli", "-t", "-f", "DEVICE,TYPE", "dev", "status"])
        if ok:
            for line in stdout.splitlines():
                if not line:
                    continue
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                dev, dev_type = parts
                if dev_type == "wifi-p2p":
                    continue
                if dev_type == "wifi" or "wireless" in dev_type:
                    values.append(normalize_parent_wifi_iface(dev))
            return list(dict.fromkeys([value for value in values if value]))
    if util.command_exists("iw"):
        ok, stdout, _ = util.run_ok(["iw", "dev"])
        if ok:
            for line in stdout.splitlines():
                match = re.match(r"\s*Interface\s+(\S+)", line)
                if match:
                    dev = match.group(1)
                    if not dev.startswith("p2p-") and not dev.startswith("p2p-dev-"):
                        values.append(normalize_parent_wifi_iface(dev))
    return list(dict.fromkeys([value for value in values if value]))


def iface_is_wifi(device):
    """判断网卡是否为无线网卡。"""
    if not device:
        return False
    if not util.command_exists("nmcli"):
        return True
    ok, stdout, _ = util.run_ok(["nmcli", "-t", "-f", "DEVICE,TYPE", "dev", "status"])
    if not ok:
        return False
    for line in stdout.splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2 or parts[0] != device:
            continue
        if parts[1] == "wifi-p2p":
            return False
        return parts[1] == "wifi" or "wireless" in parts[1]
    return False


def ensure_iface(cfg):
    """确保配置里有热点网卡：为空时自动选第一张无线网卡。"""
    iface = normalize_parent_wifi_iface(cfg.get("IFACE", ""))
    if not iface:
        candidates = [dev for dev in wifi_ifaces() if not dev.startswith("p2p")]
        if not candidates:
            candidates = wifi_ifaces()
        iface = candidates[0] if candidates else ""
    cfg["IFACE"] = normalize_parent_wifi_iface(iface)
    return cfg["IFACE"]


def require_wifi_iface(cfg):
    """检查热点网卡是否可用：
    返回 0=正常, 1=不是无线网卡, 2=没有任何无线网卡。
    """
    iface = ensure_iface(cfg)
    if not iface:
        return 2
    return 0 if iface_is_wifi(iface) else 1


# ---------------------------------------------------------------------------
# 端口策略解析
# ---------------------------------------------------------------------------
def allow_ports_to_rules(spec):
    """把端口策略字符串解析为规则列表。

    支持格式：
      "*"                  -> 全部放行 [("ALL", 0, 65535)]
      "80,443"             -> tcp 单端口
      "67-68/udp"          -> udp 端口范围
    空字符串 -> 空列表（全部拦截）
    """
    spec = util.trim(spec)
    rules = []
    if not spec:
        return rules
    if spec == "*":
        return [("ALL", 0, 65535)]
    for token in spec.split(","):
        token = util.trim(token)
        if not token:
            continue
        proto = "tcp"
        port_part = token
        if "/" in token:
            port_part, proto = token.rsplit("/", 1)
            proto = util.trim(proto).lower()
        if proto not in {"tcp", "udp"}:
            raise ValueError(
                f"本机端口：协议必须为 tcp 或 udp（token: {token}）"
            )
        port_part = util.trim(port_part)
        if not port_part:
            raise ValueError(f"本机端口：缺少端口（token: {token}）")
        if "-" in port_part:
            start_s, end_s = [util.trim(part) for part in port_part.split("-", 1)]
        else:
            start_s = end_s = port_part
        if not start_s.isdigit() or not end_s.isdigit():
            raise ValueError(f"本机端口：端口必须是数字（token: {token}）")
        start = int(start_s)
        end = int(end_s)
        if start < 1 or end < 1 or start > 65535 or end > 65535:
            raise ValueError(f"本机端口：端口范围必须为 1-65535（token: {token}）")
        if start > end:
            raise ValueError(f"本机端口：端口范围无效（起始 > 结束）（token: {token}）")
        rules.append((proto, start, end))
    return rules


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------
def validate_cfg(cfg):
    """校验配置，返回错误文案（中文）；合法时返回 None。"""
    uplink = cfg.get("UPLINK_IFACE", "")
    ip_cidr = cfg.get("IP_CIDR", "")
    allow_ports = cfg.get("ALLOW_PORTS", "")
    ssid = cfg.get("SSID", "")
    password = cfg.get("PASSWORD", "")
    band = cfg.get("BAND", "")
    channel = str(cfg.get("CHANNEL", ""))
    channel_width = str(cfg.get("CHANNEL_WIDTH", ""))

    if uplink and not util.is_iface_name(uplink):
        return "共享网卡：网卡名不合法"
    if ip_cidr and not util.is_ipv4_cidr(ip_cidr):
        return "IP/CIDR：IPv4 CIDR 不合法（例如 192.168.80.1/24）"
    if allow_ports:
        try:
            allow_ports_to_rules(allow_ports)
        except ValueError as exc:
            return str(exc)
    if not ssid:
        return "SSID：必填"
    if "\n" in ssid or "#" in ssid:
        return "SSID：不能包含 '#' 或换行符"
    if len(password) < 8:
        return "密码：长度必须 >= 8"
    if "\n" in password or "#" in password:
        return "密码：不能包含 '#' 或换行符"
    if band not in {"bg", "a"}:
        return "频段：必须为 bg (2.4G) 或 a (5G)"
    if not channel.isdigit():
        return "信道：必须是数字"
    channel_num = int(channel)
    if band == "bg" and not (1 <= channel_num <= 14):
        return "信道：2.4G (bg) 请使用 1-14"
    if band == "a" and channel_num < 34:
        return "信道：5G (a) 请使用 5GHz 信道（例如 36/40/44/48/149...）"
    if channel_width not in {"20", "40", "80", "160"}:
        return "带宽：必须为 20、40、80、160 之一"
    if band == "bg" and channel_width not in {"20", "40"}:
        return "带宽：2.4G (bg) 仅允许 20 或 40 MHz"
    if util.normalize_isolation(cfg.get("ISOLATION", "1")) not in {"0", "1"}:
        return "网络隔离：必须为开启或关闭"
    return None


# ---------------------------------------------------------------------------
# 网络参数辅助
# ---------------------------------------------------------------------------
def effective_ip_cidr(cfg):
    """取生效的 IP/CIDR（为空时回退默认值）。"""
    return util.trim(cfg.get("IP_CIDR", "")) or DEFAULTS["IP_CIDR"]


def hotspot_lan_details(cidr):
    """根据 CIDR 计算热点局域网细节：网关/掩码/DHCP 地址池起止。

    返回 None 表示 CIDR 非法或无可用地址池。
    """
    try:
        iface = IPv4Interface(cidr)
    except Exception:
        return None
    network = iface.network
    gateway = int(iface.ip)
    first = int(network.network_address) + 1
    last = int(network.broadcast_address) - 1
    if last < first:
        return None
    start = max(first, int(network.network_address) + 10)
    if start == gateway:
        start += 1
    if start > last:
        start = first
        if start == gateway:
            start += 1
    end = last
    if end == gateway:
        end -= 1
    if start > end:
        return None
    return {
        "cidr": str(iface),
        "gateway": str(iface.ip),
        "netmask": str(network.netmask),
        "start": str(type(iface.ip)(start)),
        "end": str(type(iface.ip)(end)),
    }


# 公共 DNS 兜底（阿里/腾讯）
PUBLIC_DNS = ["223.5.5.5", "119.29.29.29"]


def system_nameservers(strip_private=False):
    """读取系统 DNS 列表。

    strip_private=True（隔离模式）时剔除回环/私网/链路本地地址——
    这些地址热点客户端要么不可达(127.x)，要么被隔离规则拦截(内网 DNS)，
    会导致客户端 DNS 超时、表现为“已连接但无网络”。空则回退公共 DNS。
    """
    from ipaddress import ip_address

    values = []
    for line in util.read_text("/etc/resolv.conf").splitlines():
        match = re.match(r"^nameserver\s+(\S+)", line.strip())
        if match:
            candidate = match.group(1)
            try:
                addr = ip_address(candidate)
                if addr.version != 4:
                    continue
                if strip_private and (
                    addr.is_loopback or addr.is_private or addr.is_link_local
                ):
                    continue
                values.append(candidate)
            except ValueError:
                continue
    values = list(dict.fromkeys(values))
    return values or list(PUBLIC_DNS)
