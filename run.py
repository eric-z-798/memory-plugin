#!/usr/bin/env python3
"""星枢 XINGSHU —— 多模型协作与共享记忆空间。

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
import secrets
import signal
from contextlib import ExitStack
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

from aigc import APP_NAME, APP_NAME_LATIN, __version__  # noqa: E402
from aigc.engine import Engine  # noqa: E402
from aigc.members import Registry  # noqa: E402
from aigc.server import App, make_server  # noqa: E402
from aigc.security import DataDirectoryLock, PrivateStartupFiles
from aigc.store import Store  # noqa: E402

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv):
    p = argparse.ArgumentParser(description=APP_NAME + " " + APP_NAME_LATIN + "：多模型协作与共享记忆空间")
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

    directory = Path(args.data_dir) if args.data_dir else ROOT / ("demo-data" if args.demo else "data")
    try:
        with DataDirectoryLock(directory):
            serve(args, local_only, directory)
    except (OSError, RuntimeError) as exc:
        raise SystemExit("无法启动：%s" % exc) from None


def serve(args, local_only, directory):
    store = Store(directory)
    registry = Registry(demo=args.demo)
    engine = Engine(store, registry)
    app = App(store, engine, registry, password=args.password or None, local_only=local_only, demo=args.demo)
    try:
        server, port = bind(args.host, args.port, app)
    except BaseException:
        engine.shutdown()
        raise

    shown_host = "localhost" if local_only else args.host
    url = "http://%s:%d" % (shown_host if ":" not in shown_host else "[%s]" % shown_host, port)
    url += "/#token=" + app.access_token
    timer = None
    previous_term = None
    try:
        with ExitStack() as stack:
            startup = stack.enter_context(PrivateStartupFiles(store.root, url)) if os.name == 'posix' else None
            if startup is None and not sys.stdout.isatty():
                raise RuntimeError("无法验证此平台的启动文件 ACL；请在交互终端启动，不要重定向输出")
            print("%s %s %s 已启动%s" % (APP_NAME, APP_NAME_LATIN, __version__, "（演示模式）" if args.demo else ""))
            print("数据保存在：%s" % store.root)
            if sys.stdout.isatty():
                print("在浏览器打开：%s" % url)
            if startup:
                print("本人启动链接文件（仅本次进程有效）：%s" % startup.link)
            if not local_only:
                print("远程访问请将链接主机名替换为服务器地址；还需密码。HTTP 明文连接建议通过 SSH 隧道保护。")
            print("按 Ctrl+C 停止。", flush=True)
            if local_only and not args.no_browser and has_display() and startup:
                timer = threading.Timer(1.0, lambda: webbrowser.open(startup.page.as_uri()))
                timer.start()
            elif not startup:
                print("此平台请手动打开终端链接；未验证 ACL，不自动创建含令牌文件。", flush=True)
            if threading.current_thread() is threading.main_thread():
                previous_term = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                print("\n正在停止……")
            finally:
                if timer:
                    timer.cancel()
                    timer.join(timeout=2)
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        engine.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
