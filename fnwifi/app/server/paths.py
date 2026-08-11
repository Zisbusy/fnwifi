# -*- coding: utf-8 -*-
"""
paths.py —— 路径与常量管理

集中管理应用运行时依赖的所有路径（数据文件、状态文件、PID 文件等）。
后端拆分后所有模块统一从这里取路径，避免散落的魔法字符串。

关键点：
- 应用数据目录(DATA_DIR)由 main.py 启动时通过 --data-dir 注入；
- configure() 会在启动时根据 DATA_DIR 重算所有派生路径；
- 其他模块请使用 `import paths` 后以 paths.XXX 方式引用，
  保证 configure() 之后读取到的是最新值。
"""

import os
import platform as _sys_platform

# ---------------------------------------------------------------------------
# 应用根目录与自带二进制目录
# ---------------------------------------------------------------------------
# 应用目录结构：<app>/server/main.py，所以 APP_ROOT 为 server 的上一级
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 架构 → 自带二进制子目录名 映射（自包含架构，优先用自带静态二进制）
_ARCH = _sys_platform.machine().lower()
_ARCH_MAP = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "armv7l": "armv7l",
    "armv6l": "armv6l",
}
BIN_DIR = os.environ.get(
    "BIN_DIR", os.path.join(APP_ROOT, "bin", _ARCH_MAP.get(_ARCH, _ARCH))
)

# ---------------------------------------------------------------------------
# 数据目录与状态文件（默认值，启动时由 configure() 重算）
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

# 用户配置（SSID/密码/频段等，key=value 格式，方便 shell 读取）
CFG_FILE = os.path.join(DATA_DIR, "hotspot.env")

# NAT 状态（记录热点网卡/上联网卡，用于停止时精确清理）
NAT_STATE_FILE = os.path.join(DATA_DIR, "nat.env")

# 端口放行策略状态
PORTS_STATE_FILE = os.path.join(DATA_DIR, "ports.state")

# 热点开关状态（用于开机自动恢复）
HOTSPOT_STATE_FILE = os.path.join(DATA_DIR, "hotspot.state")

# 自管 dnsmasq（仅做 DHCP，port=0 不占 53 端口，与系统 dnsmasq 无冲突）
DNSMASQ_CONF_FILE = os.path.join(DATA_DIR, "hotspot-dnsmasq.conf")
DNSMASQ_PID_FILE = os.path.join(DATA_DIR, "hotspot-dnsmasq.pid")
DNSMASQ_LEASE_FILE = os.path.join(DATA_DIR, "hotspot-dnsmasq.leases")

# 网络隔离（客户端禁止访问主网段）状态
ISOLATION_STATE_FILE = os.path.join(DATA_DIR, "isolation.state")

# hostapd 配置/PID/日志
HOSTAPD_CONF_FILE = os.path.join(DATA_DIR, "hostapd.conf")
HOSTAPD_PID_FILE = os.path.join(DATA_DIR, "hostapd.pid")
HOSTAPD_LOG_FILE = os.path.join(DATA_DIR, "hostapd.log")

# 系统参数备份（net.ipv4.ip_forward 原值，停止时还原）
SYSCTL_STATE_FILE = os.path.join(DATA_DIR, "sysctl.state")

# regulatory.db alternatives 链接原指向备份（停止/卸载时还原）
REGDB_STATE_FILE = os.path.join(DATA_DIR, "regdb.state")

# ---------------------------------------------------------------------------
# 宿主系统 regulatory.db 相关路径（瞬态管理，仅在开热点时切换）
# ---------------------------------------------------------------------------
# 背景：部分 Debian 系系统的 regulatory.db 指向 -debian 变体，
# 内核验签失败时监管域回落到 00（5GHz 不可用）。
# 方案：开热点前把 alternatives 链接临时指向可验签的 -upstream 变体，
# 停止/卸载时恢复原指向。只改链接、不重载内核，对宿主影响最小。
REGDB_ALT_DB = "/etc/alternatives/regulatory.db"
REGDB_ALT_P7S = "/etc/alternatives/regulatory.db.p7s"
REGDB_UP_DB = "/usr/lib/firmware/regulatory.db-upstream"
REGDB_UP_P7S = "/usr/lib/firmware/regulatory.db.p7s-upstream"


def configure(data_dir):
    """启动时根据 --data-dir 重算所有数据文件路径。"""
    global DATA_DIR, CFG_FILE, NAT_STATE_FILE, PORTS_STATE_FILE
    global HOTSPOT_STATE_FILE, DNSMASQ_CONF_FILE, DNSMASQ_PID_FILE
    global DNSMASQ_LEASE_FILE, ISOLATION_STATE_FILE, HOSTAPD_CONF_FILE
    global HOSTAPD_PID_FILE, HOSTAPD_LOG_FILE, SYSCTL_STATE_FILE, REGDB_STATE_FILE

    DATA_DIR = data_dir
    CFG_FILE = os.path.join(DATA_DIR, "hotspot.env")
    NAT_STATE_FILE = os.path.join(DATA_DIR, "nat.env")
    PORTS_STATE_FILE = os.path.join(DATA_DIR, "ports.state")
    HOTSPOT_STATE_FILE = os.path.join(DATA_DIR, "hotspot.state")
    DNSMASQ_CONF_FILE = os.path.join(DATA_DIR, "hotspot-dnsmasq.conf")
    DNSMASQ_PID_FILE = os.path.join(DATA_DIR, "hotspot-dnsmasq.pid")
    DNSMASQ_LEASE_FILE = os.path.join(DATA_DIR, "hotspot-dnsmasq.leases")
    ISOLATION_STATE_FILE = os.path.join(DATA_DIR, "isolation.state")
    HOSTAPD_CONF_FILE = os.path.join(DATA_DIR, "hostapd.conf")
    HOSTAPD_PID_FILE = os.path.join(DATA_DIR, "hostapd.pid")
    HOSTAPD_LOG_FILE = os.path.join(DATA_DIR, "hostapd.log")
    SYSCTL_STATE_FILE = os.path.join(DATA_DIR, "sysctl.state")
    REGDB_STATE_FILE = os.path.join(DATA_DIR, "regdb.state")


def ensure_data_dir():
    """确保数据目录存在。"""
    os.makedirs(DATA_DIR, exist_ok=True)
