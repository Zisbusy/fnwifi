# -*- coding: utf-8 -*-
"""
main.py —— fnwifi 后端入口

职责：
- 解析命令行参数（Unix socket / 网关前缀 / www 目录 / 数据目录）
- --cleanup 模式：仅撤销热点（供停用/卸载时调用），不启动服务器
- 正常模式：启动 Unix Socket HTTP 服务 + 后台自动恢复线程
- 信号处理：SIGTERM/SIGINT 优雅退出并清理 socket

启动方式（见 cmd/main）：
    python3 server/main.py --unix-socket <sock> --base-path /app/fnwifi \
        --www-root <app>/www --data-dir <var 目录>
"""

import argparse
import os
import signal
import sys
import threading
import time

import api
import httpd
import net
import paths
import util


def auto_restore_hotspot():
    """开机/服务启动后，若关机前热点处于开启状态，自动恢复热点。

    实现：读取 hotspot.state，若为开启则等待 NetworkManager / 无线网卡
    就绪（最多重试 AUTO_RESTORE_ATTEMPTS 次，间隔 AUTO_RESTORE_DELAY 秒），
    然后调用 perform_start() 恢复，全程不阻塞服务器启动。
    """
    try:
        data = util.load_shell_state(paths.HOTSPOT_STATE_FILE)
    except Exception:
        return
    if str(data.get("HOTSPOT_ENABLED", "0")).strip() not in {"1", "true"}:
        return
    restore_log = os.path.join(paths.DATA_DIR, "auto-restore.log")
    _log(restore_log, "auto restore triggered")
    max_attempts = int(os.environ.get("AUTO_RESTORE_ATTEMPTS", "6"))
    delay = int(os.environ.get("AUTO_RESTORE_DELAY", "10"))
    for attempt in range(1, max_attempts + 1):
        time.sleep(delay)
        try:
            out, notice = net.perform_start()
            _log(restore_log, f"auto restore OK (attempt {attempt})\n{out}\n{notice}")
            return
        except net.StartError as exc:
            _log(
                restore_log,
                f"auto restore failed (attempt {attempt}): {exc.status} {exc.message}",
            )
        except Exception as exc:
            _log(restore_log, f"auto restore error (attempt {attempt}): {exc}")


def _log(path, message):
    """追加一行日志（失败静默）。"""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="fnwifi Unix socket server")
    parser.add_argument("--unix-socket", required=True)
    parser.add_argument("--base-path", default="/app/fnwifi")
    parser.add_argument("--www-root", required=True)
    parser.add_argument("--data-dir", default=paths.DATA_DIR)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="停用/卸载时撤销热点，不启动服务器",
    )
    args = parser.parse_args()

    # 按 --data-dir 重算所有数据文件路径
    paths.configure(args.data_dir)
    paths.ensure_data_dir()

    # 停用/卸载模式：只撤销热点，不启动服务器
    if args.cleanup:
        try:
            net.perform_stop()
            print("cleanup done")
        except Exception as exc:
            print(f"cleanup error: {exc}")
        sys.exit(0)

    # 清理可能残留的旧 socket 文件
    if os.path.exists(args.unix_socket):
        os.unlink(args.unix_socket)

    server = httpd.ThreadingUnixHTTPServer(
        args.unix_socket, httpd.Handler, base_path=args.base_path, www_root=args.www_root
    )

    def shutdown(_signum, _frame):
        server.server_close()
        if os.path.exists(args.unix_socket):
            os.unlink(args.unix_socket)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # 后台自动恢复热点（不阻塞服务器启动）
    threading.Thread(target=auto_restore_hotspot, daemon=True).start()

    try:
        server.serve_forever()
    finally:
        server.server_close()
        if os.path.exists(args.unix_socket):
            os.unlink(args.unix_socket)


if __name__ == "__main__":
    main()
