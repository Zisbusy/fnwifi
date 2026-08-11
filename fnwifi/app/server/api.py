# -*- coding: utf-8 -*-
"""
api.py —— HTTP 接口动作处理

定义所有前端可调用的 action（config_get/config_set/status/start/stop/...）。
统一以 JSON 响应：{"ok": true, ...} 或 {"ok": false, "error": "中文错误"}。

说明：
- 已移除国际化，所有文案直接使用中文；
- 已移除国家码自定义选项，监管域固定为 CN（见 config.DEFAULT_COUNTRY）；
- 请求上下文（handler/body/query）由 httpd.py 在分发前注入 REQUEST_CONTEXT。
"""

import json
import threading
from http import HTTPStatus

import clients
import config
import net
import util


# ---------------------------------------------------------------------------
# 请求上下文（每个请求线程独立）
# ---------------------------------------------------------------------------
REQUEST_CONTEXT = threading.local()


class ResponseDone(Exception):
    """响应已写出，用于终止当前请求处理。"""

    pass


def current_request():
    """取当前线程的请求上下文 dict。"""
    return getattr(REQUEST_CONTEXT, "value", {})


# 当前处理步骤（用于意外错误时的定位信息）
CURRENT_STEP = "init"


# ---------------------------------------------------------------------------
# 响应输出
# ---------------------------------------------------------------------------
def http_write(payload):
    """把 dict 以 JSON 写出到 HTTP 响应（请求上下文中取 handler）。"""
    request = current_request()
    handler = request.get("handler", None)
    if handler is None:
        raise RuntimeError("no active request handler")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
    raise ResponseDone()


def ok_response(payload=None):
    """成功响应。"""
    body = {"ok": True}
    if payload:
        body.update(payload)
    http_write(body)


def error_response(http_status, message):
    """失败响应（带 HTTP 状态码与中文错误文案）。"""
    http_write(
        {
            "ok": False,
            "error": util.sanitize_text(message or ""),
            "http_status": http_status,
        }
    )


def output_response(output_text, notice=None):
    """输出类响应（如 start/stop 的结果 + 可选提示）。"""
    text = util.sanitize_text(output_text or "")
    if notice:
        line = "注意：" + util.sanitize_text(notice)
        text = f"{text}\n{line}" if text else line
    ok_response({"output": text})


# ---------------------------------------------------------------------------
# 请求参数读取
# ---------------------------------------------------------------------------
def first_query_value(name):
    """取 query 参数（单个值）。"""
    request = current_request()
    query = request.get("query", {})
    values = query.get(name, [""])
    return values[0]


def first_form_value(name):
    """取表单/JSON 体参数（单个值）。"""
    request = current_request()
    body = request.get("body", {})
    values = body.get(name, [""])
    return values[0]


# ---------------------------------------------------------------------------
# 各 action 处理器
# ---------------------------------------------------------------------------
def handle_config_get():
    """读取配置：返回当前配置 + 各频段可用信道列表。"""
    global CURRENT_STEP
    CURRENT_STEP = "config_get"
    cfg = config.load_cfg()
    ok_response(
        {
            "config": {
                "iface": cfg["IFACE"],
                "uplinkIface": cfg["UPLINK_IFACE"],
                "ipCidr": cfg["IP_CIDR"],
                "allowPorts": cfg["ALLOW_PORTS"],
                "ssid": cfg["SSID"],
                "password": cfg["PASSWORD"],
                "band": cfg["BAND"],
                "channel": cfg["CHANNEL"],
                "channelWidth": cfg["CHANNEL_WIDTH"],
                "isolation": cfg["ISOLATION"],
            },
            "channelOptions": {
                "bg": net.iw_channels_for_band("bg"),
                "a": net.iw_channels_for_band("a"),
            },
        }
    )


def handle_config_set():
    """保存配置（不立即启停热点；若热点运行中由前端触发重启）。"""
    global CURRENT_STEP
    CURRENT_STEP = "config_set"
    cfg = config.load_cfg()
    cfg.update(
        {
            "IFACE": first_form_value("iface"),
            "UPLINK_IFACE": first_form_value("uplinkIface"),
            "IP_CIDR": first_form_value("ipCidr"),
            "ALLOW_PORTS": first_form_value("allowPorts"),
            "SSID": first_form_value("ssid"),
            "PASSWORD": first_form_value("password"),
            "BAND": first_form_value("band"),
            "CHANNEL": first_form_value("channel"),
            "CHANNEL_WIDTH": first_form_value("channelWidth"),
            "ISOLATION": first_form_value("isolation"),
        }
    )
    config.ensure_iface(cfg)
    cfg["IFACE"] = config.normalize_parent_wifi_iface(cfg.get("IFACE", ""))
    cfg_error = config.validate_cfg(cfg)
    if cfg_error:
        error_response("400 Bad Request", cfg_error)
    if not config.save_cfg(cfg):
        error_response("500 Internal Server Error", "保存配置失败（配置文件不可写）")
    ok_response()


def handle_status():
    """查询热点运行状态（含互联网连通性、驱动/功率信息）。"""
    global CURRENT_STEP
    CURRENT_STEP = "status"
    cfg = config.load_cfg()
    config.ensure_iface(cfg)
    nat_state = net.load_nat_state()
    parent_iface = cfg["IFACE"]
    hotspot_iface = nat_state["HOTSPOT_IFACE"] or parent_iface
    running = net.hostapd_running()
    active = cfg["SSID"] if running else ""
    state = "ap" if running else "down"
    if not running and util.command_exists("nmcli"):
        ok, stdout, _ = util.run_ok(
            ["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "dev", "status"]
        )
        if ok:
            for line in stdout.splitlines():
                if line.startswith(f"{hotspot_iface}:"):
                    parts = line.split(":")
                    state = parts[1] if len(parts) > 1 else "down"
                    active = ":".join(parts[2:]) if len(parts) > 2 else ""
                    break
    sta_ap_concurrent = net.iw_supports_sta_ap()
    parent_active_connection = ""
    if util.command_exists("nmcli"):
        ok, stdout, _ = util.run_ok(
            ["nmcli", "-g", "GENERAL.CONNECTION", "dev", "show", parent_iface]
        )
        if ok and stdout.splitlines():
            parent_active_connection = util.trim(stdout.splitlines()[0])
            if parent_active_connection == "--":
                parent_active_connection = ""
    will_disconnect_sta = (
        hotspot_iface == parent_iface
        and not sta_ap_concurrent
        and bool(parent_active_connection)
    )
    ip_addr = ""
    if util.command_exists("ip"):
        ok, stdout, _ = util.run_ok(["ip", "-4", "addr", "show", "dev", hotspot_iface])
        if ok:
            match = util.re_search(r"inet\s+([^\s]+)", stdout)
            if match:
                ip_addr = match.group(1)
    tx_power = net.wifi_txpower_dbm(hotspot_iface)
    driver = net.wifi_driver_name(hotspot_iface)
    effective_uplink = (
        nat_state["NAT_UPLINK_IFACE"]
        or cfg["UPLINK_IFACE"]
        or net.detect_route_dev("1.1.1.1")
    )
    internet_status = False
    internet_reason = "null"
    if util.command_exists("curl"):
        ok, _, _ = util.run_ok(
            [
                "curl",
                "--max-time",
                "3",
                "-I",
                "http://1.1.1.1",
                "--silent",
                "--output",
                "/dev/null",
            ]
        )
        if ok:
            internet_status = True
        else:
            internet_reason = f"检查互联网连接失败（设备：{hotspot_iface}）"
    ok_response(
        {
            "status": {
                "running": running,
                "iface": parent_iface,
                "hotspotIface": hotspot_iface,
                "state": state,
                "activeConnection": active,
                "parentActiveConnection": parent_active_connection,
                "staApConcurrent": sta_ap_concurrent,
                "willDisconnectSta": will_disconnect_sta,
                "ip": ip_addr,
                "txPowerDbm": tx_power,
                "wifiDriver": driver,
                "lowTxPower": net.wifi_txpower_is_suspiciously_low(hotspot_iface),
                "uplinkIface": cfg["UPLINK_IFACE"],
                "effectiveUplinkIface": effective_uplink,
                "internetStatus": internet_status,
                "internetReason": internet_reason,
            }
        }
    )


def handle_start():
    """开启热点。"""
    global CURRENT_STEP
    CURRENT_STEP = "start"
    try:
        out, notice = net.perform_start()
    except net.StartError as exc:
        error_response(exc.status, exc.message)
    output_response(out, notice)


def handle_stop():
    """关闭热点。"""
    global CURRENT_STEP
    CURRENT_STEP = "stop"
    output_response(net.perform_stop())


def handle_clients():
    """返回当前客户端列表。"""
    global CURRENT_STEP
    CURRENT_STEP = "clients"
    cfg = config.load_cfg()
    ok_response({"clients": clients.build_clients(cfg)})


def handle_ifaces():
    """返回可用无线网卡列表。"""
    global CURRENT_STEP
    CURRENT_STEP = "ifaces"
    ok_response({"ifaces": config.wifi_ifaces()})


def handle_uplinks():
    """返回可用上联网卡列表（过滤虚拟/容器网卡）。"""
    global CURRENT_STEP
    CURRENT_STEP = "uplinks"
    uplinks = []
    if util.command_exists("nmcli"):
        ok, stdout, _ = util.run_ok(["nmcli", "-t", "-f", "DEVICE", "dev", "status"])
        if ok:
            for device in stdout.splitlines():
                device = util.trim(device)
                if device:
                    uplinks.append(device)
    elif util.command_exists("ip"):
        ok, stdout, _ = util.run_ok(["ip", "-o", "link", "show"])
        if ok:
            for line in stdout.splitlines():
                match = util.re_match(r"^\d+:\s+([^:@\s]+)", line)
                if match:
                    uplinks.append(match.group(1))
    # 过滤回环、p2p 与常见的虚拟网卡
    uplinks = [
        device
        for device in uplinks
        if device
        and device != "lo"
        and not device.startswith("p2p")
        and not util.re_match(
            r"^(veth|docker|br-|virbr|vnet|tap|tun|wg|zt|tailscale|vboxnet|vmnet)",
            device,
        )
    ]
    ok_response({"uplinks": list(dict.fromkeys(uplinks))})


def handle_kick():
    """强制下线指定 MAC 的客户端。"""
    global CURRENT_STEP
    CURRENT_STEP = "kick"
    cfg = config.load_cfg()
    mac = util.trim(first_query_value("mac")).lower()
    if not util.re_fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac):
        error_response("400 Bad Request", f"MAC 地址不合法：{mac}")
    ok, out = clients.kick_client(cfg, mac)
    if ok:
        output_response(out)
    error_response("500 Internal Server Error", out)


def handle_stpre():
    """开启前的预检查：返回可能阻止启动的问题（abort）与警告列表（warnings）。

    注意：此处会先应用固定国家码 CN 并修复 regulatory.db 链接，
    保证首次安装（regdom=00）时检查结果与真实启动一致。
    """
    global CURRENT_STEP
    CURRENT_STEP = "stpre"
    cfg = config.load_cfg()
    cfg_error = config.validate_cfg(cfg)
    if cfg_error:
        ok_response({"abort": True, "error": cfg_error})
    warnings = []
    iface_status = config.require_wifi_iface(cfg)
    if iface_status == 1:
        ok_response(
            {
                "abort": True,
                "error": (
                    f"设备 '{cfg['IFACE']}' 不是 Wi-Fi 网卡。"
                    f"可用 Wi-Fi 网卡：{' '.join(config.wifi_ifaces())}"
                ),
            }
        )
    if iface_status == 2:
        ok_response(
            {"abort": True, "error": "未找到 Wi-Fi 网卡，请检查 'nmcli dev status'。"}
        )
    sta_prev_con = ""
    if util.command_exists("nmcli"):
        ok, stdout, _ = util.run_ok(
            ["nmcli", "-g", "GENERAL.CONNECTION", "dev", "show", cfg["IFACE"]]
        )
        if ok and stdout.splitlines():
            sta_prev_con = util.trim(stdout.splitlines()[0])
            if sta_prev_con == "--":
                sta_prev_con = ""
    # 先修复 regulatory.db 链接并应用固定国家码，再检测监管域，
    # 否则首次安装（-debian 状态）时检查阶段会误报 00。
    net.fix_regulatory_links()
    net.ensure_regdom(config.DEFAULT_COUNTRY)
    regdom = net.iw_reg_country() or "00"
    if regdom == "00":
        warnings.append("监管域为 00；5.0GHz 信道可能不可用。")
        # 附加只读诊断：内核监管库报错 + alternatives 链接指向
        for hint in net.regdom_diagnose():
            warnings.append(hint)
    if not net.iw_supports_sta_ap():
        if sta_prev_con:
            warnings.append(
                f"网卡不支持 STA+AP，已断开 '{sta_prev_con}' 在 '{cfg['IFACE']}'。"
            )
        else:
            warnings.append(
                f"网卡不支持 STA+AP；热点将使用 '{cfg['IFACE']}'（可能中断 Wi-Fi）。"
            )
    if cfg["UPLINK_IFACE"] and cfg["UPLINK_IFACE"] == cfg["IFACE"]:
        ok_response(
            {
                "abort": True,
                "error": (
                    f"共享网卡不能与热点网卡相同（{cfg['IFACE']}），"
                    "请选择其他网卡或留空自动选择。"
                ),
            }
        )
    if not net.nmcli_ap_mode_supported():
        ok_response(
            {
                "abort": True,
                "error": (
                    f"设备 '{cfg['IFACE']}' 不支持 AP/热点模式（iw list 无 '* AP'）。"
                    "请更换无线网卡。"
                ),
            }
        )
    runtime_error = net.validate_runtime_channel(cfg)
    if runtime_error:
        warnings.append(runtime_error)
    power_notice = net.wifi_low_power_notice(cfg["IFACE"])
    if power_notice:
        warnings.append(power_notice)
    if warnings:
        ok_response({"warnings": warnings})
    ok_response()


# action 路由表
ACTIONS = {
    "config_get": handle_config_get,
    "config_set": handle_config_set,
    "status": handle_status,
    "start": handle_start,
    "stop": handle_stop,
    "clients": handle_clients,
    "ifaces": handle_ifaces,
    "uplinks": handle_uplinks,
    "kick": handle_kick,
    "stpre": handle_stpre,
}
