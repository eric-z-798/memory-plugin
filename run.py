#!/usr/bin/env python3
"""AI 群聊 —— 让 Claude、GPT、Gemini 在同一个群里讨论和协作。

常用启动方式：
    python3 run.py                      本机使用，自动打开浏览器
    python3 run.py --demo               演示模式：不调用真实命令行，不消耗额度
    python3 run.py --host 0.0.0.0 --password 你的密码
                                        在云服务器上运行，用浏览器远程访问（必须设密码）
"""

import sys

if sys.version_info < (3, 8):
    sys.stderr.write("需要 Python 3.8 或更新版本，当前是 %s\n" % sys.version.split()[0])
    sys.exit(1)

import argparse
import os
import socket
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from aigc import __version__  # noqa: E402
from aigc.engine import Engine  # noqa: E402
from aigc.members import Registry  # noqa: E402
from aigc.server import App, make_server  # noqa: E402
from aigc.store import Store  # noqa: E402

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv):
    p = argparse.ArgumentParser(description="AI 群聊：让 Claude、GPT、Gemini 在同一个群里讨论和协作")
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址。默认 127.0.0.1（只允许本机访问）；云服务器上要远程访问时用 0.0.0.0，并且必须设置 --password")
    p.add_argument("--port", type=int, default=8765, help="端口，默认 8765；被占用时自动往后找")
    p.add_argument("--password", default=os.environ.get("AIGC_PASSWORD", ""),
                   help="访问密码（也可以用环境变量 AIGC_PASSWORD 设置）。浏览器弹出登录框时，用户名随意，密码填这个")
    p.add_argument("--data-dir", default=None, help="数据保存位置；正式模式默认 data，演示模式默认 demo-data")
    p.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    p.add_argument("--demo", action="store_true", help="演示模式：用模拟成员回复，不调用真实命令行，不消耗额度")
    return p.parse_args(argv)


def has_display() -> bool:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def bind(host, port, app):
    last_error = None
    for candidate in range(port, port + 20):
        try:
            return make_server(host, candidate, app), candidate
        except OSError as exc:
            last_error = exc
    raise SystemExit("端口 %d-%d 都被占用了，请用 --port 换一个端口（%s）" % (port, port + 19, last_error))


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    local_only = args.host in LOOPBACK
    if not local_only and not args.password:
        raise SystemExit(
            "监听 %s 会让其他电脑也能访问，必须设置访问密码，例如：\n"
            "    python3 run.py --host %s --password 你的密码\n"
            "更安全的做法是保持默认的 127.0.0.1，用 SSH 隧道访问，见 README。" % (args.host, args.host))

    store = Store(Path(args.data_dir) if args.data_dir else ROOT / ("demo-data" if args.demo else "data"))
    registry = Registry(demo=args.demo)
    engine = Engine(store, registry)
    app = App(store, engine, registry, password=args.password or None, local_only=local_only, demo=args.demo)
    server, port = bind(args.host, args.port, app)

    shown_host = "localhost" if local_only else args.host
    url = "http://%s:%d" % (shown_host if ":" not in shown_host else "[%s]" % shown_host, port)
    print("AI 群聊 %s 已启动%s" % (__version__, "（演示模式：不调用真实命令行，不消耗额度）" if args.demo else ""))
    print("数据保存在：%s" % store.root)
    if local_only:
        print("在浏览器打开：%s" % url)
    else:
        print("在浏览器打开：http://<这台服务器的IP>:%d （用户名随意，密码是你设置的那个）" % port)
        print("提醒：这是 HTTP 明文连接。公网上更推荐 SSH 隧道或在前面加 HTTPS，见 README。")
    print("按 Ctrl+C 停止。")

    if local_only and not args.no_browser and has_display():
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        engine.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
