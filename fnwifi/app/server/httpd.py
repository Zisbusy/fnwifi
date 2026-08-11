# -*- coding: utf-8 -*-
"""
httpd.py —— Unix Socket HTTP 服务

- ThreadingUnixHTTPServer：监听应用专属 Unix socket（由 fnOS 统一网关转发）
- Handler：路由 /api/* 到 api.py 的 action 处理，其余路径提供 www 静态文件
- 安全：静态文件做了路径穿越防护，只允许访问 www_root 内的文件
"""

import json
import mimetypes
import socketserver
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlsplit

import api


# ---------------------------------------------------------------------------
# 路径规范化
# ---------------------------------------------------------------------------
def normalize_base_path(path):
    """把网关前缀规范化为 /xxx 形式（去掉尾部斜杠）。"""
    if not path:
        return "/"
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.rstrip("/") or "/"


def strip_base_path(path, base_path):
    """去掉请求路径中的网关前缀，得到应用内路径。"""
    if base_path != "/" and path.startswith(base_path):
        return path[len(base_path):] or "/"
    return path or "/"


# ---------------------------------------------------------------------------
# 请求体与 action 解析
# ---------------------------------------------------------------------------
def parse_request_body(handler):
    """解析 POST 请求体：支持 application/json 与表单两种格式。"""
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    content_type = handler.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = json.loads(text or "{}")
        return {
            key: ["" if value is None else str(value)] for key, value in payload.items()
        }
    return parse_qs(text, keep_blank_values=True)


def merge_query_action(path, query):
    """解析 query 参数；支持把 /api/<action> 路径形式转换为 action 参数。"""
    parsed = parse_qs(query or "", keep_blank_values=True)
    if "action" not in parsed:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "api":
            parsed["action"] = [parts[1]]
    return parsed


def dispatch_api(handler, api_path, query):
    """分发 API 请求到对应的 action 处理器。"""
    # 把请求上下文注入线程局部变量，供 api 模块读取
    api.REQUEST_CONTEXT.value = {
        "handler": handler,
        "body": parse_request_body(handler),
        "query": merge_query_action(api_path, query),
    }
    try:
        action = api.first_query_value("action")
        if action.endswith(".cgi"):
            action = action[:-4]
        if not action:
            api.error_response("400 Bad Request", "缺少 action 参数")
        handler_fn = api.ACTIONS.get(action)
        if not handler_fn:
            api.error_response("404 Not Found", f"未知操作：{action}")
        else:
            handler_fn()
    except api.ResponseDone:
        return
    except Exception as exc:
        try:
            api.error_response(
                "500 Internal Server Error",
                f"意外错误（步骤={api.CURRENT_STEP}）：{exc}",
            )
        except api.ResponseDone:
            return
    finally:
        # 清理线程局部上下文，避免线程复用导致串数据
        if hasattr(api.REQUEST_CONTEXT, "value"):
            del api.REQUEST_CONTEXT.value


# ---------------------------------------------------------------------------
# Unix Socket HTTP 服务
# ---------------------------------------------------------------------------
class ThreadingUnixHTTPServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer,  # pyright: ignore[reportAttributeAccessIssue]
):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path, handler_cls, *, base_path, www_root):
        self.server_name = "fnwifi"
        self.server_port = 0
        self.base_path = normalize_base_path(base_path)
        self.www_root = Path(www_root)
        super().__init__(socket_path, handler_cls)  # pyright: ignore[reportCallIssue]


class Handler(BaseHTTPRequestHandler):
    server: ThreadingUnixHTTPServer  # type: ignore[reportIncompatibleVariableOverride]
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def log_message(self, format, *args):
        import sys

        sys.stdout.write(
            "%s - - [%s] %s\n"
            % (self.client_address, self.log_date_time_string(), format % args)
        )
        sys.stdout.flush()

    def route(self):
        parsed = urlsplit(self.path)
        # 网关前缀本身 -> 301 跳转到带尾斜杠的入口（便于相对路径资源加载）
        if parsed.path == self.server.base_path:
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header(
                "Location",
                self.server.base_path
                + "/"
                + (("?" + parsed.query) if parsed.query else ""),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = strip_base_path(parsed.path, self.server.base_path)
        if path.startswith("/api"):
            dispatch_api(self, path, parsed.query)
            return
        self.serve_static(path)

    def serve_static(self, path):
        """提供 www 静态文件（含路径穿越防护）。"""
        rel_path = unquote(path or "/")
        if rel_path in ("", "/"):
            rel_path = "/index.html"
        target = (self.server.www_root / rel_path.lstrip("/")).resolve()
        root = self.server.www_root.resolve()
        if root != target and root not in target.parents:
            self.send_error(HTTPStatus.BAD_REQUEST, "Bad request")
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = (
            mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        )
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type = f"{content_type}; charset=utf-8"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Cache-Control",
            "no-store" if target.name == "index.html" else "public, max-age=60",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
