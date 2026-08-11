# -*- coding: utf-8 -*-
"""
fnwifi 后端 Python 包

模块划分（便于维护）：
- paths.py   路径与常量
- util.py    通用工具（命令执行/状态文件/校验）
- config.py  配置管理（默认值/读写/校验/网卡识别）
- net.py     网络核心（regdom/hostapd/dnsmasq/iptables/NAT/启停流程）
- clients.py 终端列表与下线
- api.py     HTTP action 处理
- httpd.py   Unix Socket HTTP 服务
- main.py    入口（参数解析/清理/自动恢复）
"""
