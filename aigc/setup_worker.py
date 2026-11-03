#!/usr/bin/env python3
"""Native-terminal installation and account login worker.

Only coarse state is persisted. CLI output and interactive authorization stay in
the terminal; no model request is sent by this worker.
"""

from __future__ import annotations

import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.members import CLI_INFO, find_command, member_env
from aigc.network import apply_proxy, clean_proxy, _ExplicitProxyHandler
from aigc.setup import INSTALL_URLS, JOB_ID, LOGIN_ARGS, _platform, _read_json, _write_private_json


MAX_INSTALLER_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT = 45


class SetupStopped(Exception):
    pass


class SetupFailure(Exception):
    pass


class HTTPSRedirectsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme.lower() != "https":
            raise SetupFailure("官方下载发生了非 HTTPS 跳转，已停止。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _cancelled(directory):
    if (directory / "cancel").exists():
        raise SetupStopped()


def _state(directory, status, message):
    current = _read_json(directory / "state.json")
    if not isinstance(current, dict):
        raise SetupFailure("无法读取安装登录任务状态。")
    current.update(status=status, message=message, updated_at=time.time())
    _write_private_json(directory / "state.json", current)


def _stop_child(proc):
    """Keep the native terminal's controlling TTY; stop only this child tree."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            proc.kill()
    else:
        descendants = []
        try:
            listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                                     text=True, timeout=2, check=True).stdout
            pairs = [tuple(map(int, line.split())) for line in listing.splitlines() if len(line.split()) == 2]
            parents = {proc.pid}
            while True:
                children = {pid for pid, parent in pairs if parent in parents and pid not in parents}
                if not children:
                    break
                for pid in children:
                    identity = _process_identity(pid)
                    if identity is not None:
                        descendants.append((pid, identity))
                parents.update(children)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        for pid, identity in reversed(descendants):
            _signal_same_process(pid, identity, signal.SIGTERM)
        proc.terminate()
        # A child may ignore TERM and outlive the CLI parent. Keep its identity
        # across the grace interval and force-stop only the original process.
        if descendants:
            time.sleep(.2)
        for pid, identity in reversed(descendants):
            _signal_same_process(pid, identity, signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def _process_identity(pid):
    """PID plus creation identity, so a later reused PID is never signalled."""
    if sys.platform.startswith("linux"):
        try:
            fields = Path("/proc/%d/stat" % pid).read_text().rpartition(")")[2].split()
            return ("proc", fields[19])  # /proc stat field 22: starttime ticks
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                                text=True, timeout=2, check=True).stdout.strip()
        return ("ps", result) if result else None
    except (OSError, subprocess.SubprocessError):
        return None


def _signal_same_process(pid, identity, sig):
    if _process_identity(pid) != identity:
        return
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _run_child(command, directory, env, timeout, message):
    _cancelled(directory)
    _state(directory, "running", message)
    # Do not create a separate POSIX session: interactive agy needs the native
    # terminal's controlling TTY. No stdout/stderr pipes or logs are created.
    proc = subprocess.Popen(command, cwd=str(directory), env=env)
    deadline = time.monotonic() + timeout
    heartbeat = time.monotonic()
    try:
        while proc.poll() is None:
            _cancelled(directory)
            if time.monotonic() >= deadline:
                raise SetupFailure("流程等待超时，已停止本次操作；可检查终端信息后重试。")
            if time.monotonic() - heartbeat >= 3:
                _state(directory, "running", message)
                heartbeat = time.monotonic()
            time.sleep(.2)
        _cancelled(directory)
        if proc.returncode:
            raise SetupFailure("官方命令未成功完成（退出码 %s），请查看终端中的原始提示。" % proc.returncode)
    except BaseException:
        _stop_child(proc)
        raise


def _download_installer(member, directory, env):
    platform = _platform()
    url = INSTALL_URLS[member]["windows" if platform == "windows" else "unix"]
    target = directory / ("official-install.ps1" if platform == "windows" else "official-install.sh")
    opener = urllib.request.build_opener(_ExplicitProxyHandler(env), HTTPSRedirectsOnly())
    _state(directory, "running", "正在从官方 HTTPS 地址下载安装脚本。")
    print("官方下载地址：" + url, flush=True)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    responses = queue.Queue(maxsize=1)
    stopped = threading.Event()

    def download():
        try:
            chunks, count = [], 0
            with opener.open(url, timeout=15) as response:
                if urlsplit(response.geturl()).scheme.lower() != "https":
                    raise SetupFailure("官方下载连接不是 HTTPS，已停止。")
                while not stopped.is_set():
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_INSTALLER_BYTES:
                        raise SetupFailure("官方安装脚本超出允许大小，已停止。")
                    chunks.append(chunk)
            if not stopped.is_set():
                responses.put((True, b"".join(chunks)))
        except SetupFailure as exc:
            if not stopped.is_set():
                responses.put((False, str(exc)))
        except Exception:
            if not stopped.is_set():
                responses.put((False, "下载官方安装脚本失败，请检查网络、代理或稍后重试。"))

    # DNS lookups may ignore socket timeouts. A daemon only downloads bytes;
    # it cannot write files, launch a script or alter state after cancellation.
    threading.Thread(target=download, daemon=True).start()
    heartbeat = time.monotonic()
    try:
        while True:
            _cancelled(directory)
            if time.monotonic() >= deadline:
                raise SetupFailure("下载安装脚本超时，请检查网络或代理。")
            if time.monotonic() - heartbeat >= 3:
                _state(directory, "running", "正在从官方 HTTPS 地址下载安装脚本。")
                heartbeat = time.monotonic()
            try:
                ok, payload = responses.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
                break
            except queue.Empty:
                continue
        _cancelled(directory)
        if not ok:
            raise SetupFailure(payload)
        if not payload:
            raise SetupFailure("官方下载返回空脚本，已停止。")
        with open(target, "wb") as output:
            target.chmod(0o600)
            output.write(payload)
    finally:
        stopped.set()
    return target


def _installer_command(member, installer, directory, proxy_mode):
    if _platform() == "windows":
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if not shell:
            raise SetupFailure("没有找到 PowerShell，请先安装或启用它。")
        # Windows PowerShell 5.1 does not use HTTP_PROXY for its web cmdlets.
        # Apply explicit proxy defaults in this same interpreter without writing
        # the proxy URL (or inherited credentials) into the launcher script.
        wrapper = directory / "install-wrapper.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "if ($env:HTTPS_PROXY) {\n"
            "  $PSDefaultParameterValues['Invoke-WebRequest:Proxy'] = $env:HTTPS_PROXY\n"
            "  $PSDefaultParameterValues['Invoke-RestMethod:Proxy'] = $env:HTTPS_PROXY\n"
            "}\n" +
            ("[System.Net.WebRequest]::DefaultWebProxy = $null\n"
             "if ($PSVersionTable.PSVersion.Major -ge 6) {\n"
             "  $PSDefaultParameterValues['Invoke-WebRequest:NoProxy'] = $true\n"
             "  $PSDefaultParameterValues['Invoke-RestMethod:NoProxy'] = $true\n}\n" if proxy_mode == "direct" else "") +
            "& $args[0]\nif (-not $?) { exit 1 }\n", encoding="utf-8")
        wrapper.chmod(0o600)
        return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper), str(installer)]
    shell = shutil.which("sh" if member == "gpt" else "bash")
    if not shell:
        raise SetupFailure("没有找到安装脚本需要的 shell，请按引导补齐系统组件。")
    return [shell, str(installer)]


def perform(directory):
    directory = Path(directory).resolve()
    if not JOB_ID.fullmatch(directory.name):
        raise SetupFailure("安装登录任务编号不合法。")
    config = _read_json(directory / "config.json")
    if not isinstance(config, dict) or config.get("member") not in INSTALL_URLS or config.get("action") not in ("install", "login", "setup"):
        raise SetupFailure("安装登录任务配置无效。")
    member, action = config["member"], config["action"]
    proxy = clean_proxy(config.get("proxy", {"mode": "system", "url": ""}))
    minimal = {"membership_only": True, "proxy": proxy}
    env, _ = member_env(member, minimal)
    env = apply_proxy(env, proxy)
    conf = {"command": config.get("command") or ""}
    command = find_command(member, conf)
    _cancelled(directory)
    print("AI 群聊 · %s 安装与登录\n浏览器授权由你本人完成。本窗口不会向群聊发送模型请求。" % CLI_INFO[member]["cli"], flush=True)
    if action == "install" or (action == "setup" and not command):
        installer = _download_installer(member, directory, env)
        try:
            _run_child(_installer_command(member, installer, directory, proxy["mode"]), directory, env, 900,
                       "官方安装程序正在终端运行，请按提示完成；不会自动请求管理员权限。")
        finally:
            try:
                installer.unlink()
            except FileNotFoundError:
                pass
        command = find_command(member, {})
        if not command:
            raise SetupFailure("安装流程已结束，但尚未找到 CLI。请刷新检测或填写官方命令路径。")
    if action in ("login", "setup"):
        if not command:
            raise SetupFailure("没有找到 CLI，请先安装后再登录。")
        # A native installer may have updated Windows PATH in the registry.
        env, _ = member_env(member, minimal)
        env = apply_proxy(env, proxy)
        if member == "gemini":
            print("请在 agy 完成 Google 账号登录，之后输入 /exit 退出。本流程不会自动发送问题。", flush=True)
        _run_child([command] + LOGIN_ARGS[member], directory, env, 1200,
                   "请在官方终端/浏览器中完成账号授权；完成后退出 CLI，再回到网页刷新检测。")
    _state(directory, "completed", "流程已结束。请返回网页刷新检测；此状态不代表已确认账号登录成功。")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    directory = Path(args[0]).resolve()
    if not JOB_ID.fullmatch(directory.name):
        return 2
    result = 0
    try:
        perform(directory)
    except (SetupStopped, KeyboardInterrupt):
        _state(directory, "cancelled", "本次流程已停止；已完成的安装不会回滚。")
        result = 1
    except (SetupFailure, ValueError) as exc:
        _state(directory, "failed", str(exc))
        print(str(exc), file=sys.stderr)
        result = 1
    except Exception:
        _state(directory, "failed", "流程未能完成，请查看终端或刷新检测；未保存账号授权输出。")
        print("流程未能完成，请检查终端和网络设置。", file=sys.stderr)
        result = 1
    print("请返回 AI 群聊页面刷新安装和登录状态。", flush=True)
    if sys.stdin.isatty():
        try:
            input("按 Enter 关闭此窗口。")
        except (EOFError, KeyboardInterrupt):
            pass
    return result


if __name__ == "__main__":
    raise SystemExit(main())
