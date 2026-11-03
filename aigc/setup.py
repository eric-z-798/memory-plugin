"""Local installation/login jobs launched in the user's native terminal.

The HTTP app never collects passwords, device codes, OAuth URLs or terminal
output. Only fixed actions and coarse progress cross this boundary.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .common import MEMBER_ORDER
from .members import CLI_INFO, find_command, member_env
from .network import clean_proxy


INSTALL_URLS = {
    "claude": {"windows": "https://claude.ai/install.ps1", "unix": "https://claude.ai/install.sh"},
    "gpt": {"windows": "https://chatgpt.com/codex/install.ps1", "unix": "https://chatgpt.com/codex/install.sh"},
    "gemini": {"windows": "https://antigravity.google/cli/install.ps1", "unix": "https://antigravity.google/cli/install.sh"},
}
LOGIN_ARGS = {"claude": ["auth", "login"], "gpt": ["login"], "gemini": []}
ACTIVE = {"launching", "running"}
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
WORKER = Path(__file__).with_name("setup_worker.py").resolve()


def _platform():
    return "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"


def _write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _terminal():
    platform = _platform()
    if platform == "windows":
        if not (shutil.which("powershell.exe") or shutil.which("pwsh.exe")):
            return None, "没有找到 PowerShell，请先在运行工具的电脑安装或启用 PowerShell。"
        return "windows", ""
    if platform == "macos":
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            return None, "当前通过 SSH 运行，请在主机终端完成安装与登录。"
        return ("macos", "") if Path("/usr/bin/open").exists() else (None, "没有找到 macOS Terminal 启动入口。")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None, "运行工具的主机没有图形桌面，请复制命令到该主机的终端执行。"
    for name in ("gnome-terminal", "konsole", "xfce4-terminal", "xterm", "x-terminal-emulator"):
        executable = shutil.which(name)
        if executable:
            return executable, ""
    return None, "没有找到支持的终端程序，请安装终端或复制命令手动执行。"


def _launch_terminal(terminal, job_dir, env):
    command = [sys.executable, str(WORKER), str(job_dir)]
    platform = _platform()
    kwargs = {"env": env, "cwd": str(job_dir), "close_fds": True}
    if platform == "windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
        return subprocess.Popen(command, **kwargs)
    if platform == "macos":
        launcher = Path(job_dir) / "launch.command"
        launcher.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(value) for value in command) + "\n", encoding="utf-8")
        launcher.chmod(0o700)
        return subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(launcher)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    name = Path(terminal).name
    args = ([terminal, "--"] + command if name == "gnome-terminal" else
            [terminal, "--execute"] + command if name == "xfce4-terminal" else
            [terminal, "-e"] + command)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, **kwargs)


class SetupManager:
    def __init__(self, store, registry, local_only=True, demo=False):
        self.store = store
        self.registry = registry
        self.local_only = bool(local_only)
        self.demo = bool(demo)
        self.root = Path(store.root).resolve() / "setup-jobs"
        self._lock = threading.RLock()

    def _capability(self):
        if self.demo:
            return None, "演示模式不会安装软件或启动真实账号登录。"
        if not self.local_only:
            return None, "安装与登录只允许在本机访问模式发起；远程浏览器请复制命令到运行工具的主机。"
        return _terminal()

    def _job_path(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("安装登录任务编号不合法")
        return self.root / job_id

    def job(self, job_id):
        with self._lock:
            state = _read_json(self._job_path(job_id) / "state.json")
            if not isinstance(state, dict) or state.get("id") != job_id:
                raise ValueError("找不到安装登录任务")
            if state.get("status") in ACTIVE and time.time() - state.get("updated_at", 0) > 90:
                state.update(status="failed", message="流程状态已失联，请检查原终端后再尝试。", updated_at=time.time())
                _write_private_json(self._job_path(job_id) / "state.json", state)
            if (self._job_path(job_id) / "cancel").exists():
                state["cancel_requested"] = True
            return state

    def _active(self):
        if not self.root.exists():
            return []
        result = []
        for directory in self.root.iterdir():
            if directory.is_dir() and JOB_ID.fullmatch(directory.name):
                try:
                    job = self.job(directory.name)
                except ValueError:
                    continue
                if job.get("status") in ACTIVE:
                    result.append(job)
        return result

    def overview(self, settings):
        terminal, reason = self._capability()
        platform = _platform()
        active = {job["member"]: job for job in self._active()}
        members = {}
        for member in MEMBER_ORDER:
            found = find_command(member, settings.get("members", {}).get(member, {}))
            url = INSTALL_URLS[member]["windows" if platform == "windows" else "unix"]
            shell = "sh" if member == "gpt" else "bash"
            install = "irm %s | iex" % url if platform == "windows" else "curl -fsSL %s | %s" % (url, shell)
            login = " ".join([CLI_INFO[member]["command"]] + LOGIN_ARGS[member])
            prerequisites = ["使用运行本工具的同一系统用户安装和登录。", "浏览器账号授权需要你本人完成。"]
            if platform != "windows" and not shutil.which(shell):
                prerequisites.append("安装脚本需要 %s，请先安装该组件。" % shell)
            members[member] = {
                "installed": bool(found), "command": found or CLI_INFO[member]["command"],
                "install_url": url, "install_command": install, "login_command": login,
                "login_note": "完成 Google 账号授权后，在 agy 输入 /exit 结束引导；不自动发送模型请求。" if member == "gemini" else "在打开的官方授权网页完成登录；不要在群聊里填写密码。",
                "prerequisites": prerequisites, "active_job": active.get(member),
            }
        return {"platform": platform, "can_launch": bool(terminal), "reason": reason, "members": members,
                "network_note": "macOS 的继承模式使用新 Terminal 的环境，可能与工具启动环境不同；建议明确选择自定义代理或直连。浏览器授权页使用浏览器自己的网络设置。" if platform == "macos" else
                                "代理只影响本工具启动的命令行和下载流程；浏览器授权页使用浏览器自己的网络设置。"}

    def start(self, member, action, settings):
        if member not in MEMBER_ORDER or action not in ("install", "login", "setup"):
            raise ValueError("不支持的成员或安装登录操作")
        terminal, reason = self._capability()
        if not terminal:
            raise ValueError(reason)
        proxy = clean_proxy(settings.get("proxy", {"mode": "system", "url": ""}))
        command = find_command(member, settings.get("members", {}).get(member, {}))
        if action == "login" and not command:
            raise ValueError("尚未找到 CLI，请先安装，或使用安装并登录。")
        with self._lock:
            active = self._active()
            if any(job.get("member") == member for job in active):
                raise ValueError("该成员已有安装登录流程，请先完成或停止原流程。")
            if len(active) >= len(MEMBER_ORDER):
                raise ValueError("已有三个准备流程，请先等待其中一个完成。")
            self.root.mkdir(parents=True, exist_ok=True)
            self.root.chmod(0o700)
            job_id = uuid.uuid4().hex
            directory = self._job_path(job_id)
            directory.mkdir(mode=0o700)
            created = time.time()
            state = {"id": job_id, "member": member, "action": action, "status": "launching",
                     "message": "正在打开系统终端，请在终端和官方网页完成操作。", "created_at": created, "updated_at": created}
            config = {"member": member, "action": action, "command": command or "", "proxy": proxy,
                      "membership_only": True}
            _write_private_json(directory / "config.json", config)
            _write_private_json(directory / "state.json", state)
            minimal = {"membership_only": True, "proxy": proxy}
            env, _ = member_env(member, minimal)
            try:
                _launch_terminal(terminal, directory, env)
            except (OSError, ValueError):
                state.update(status="failed", message="无法打开系统终端，请复制命令手动执行。", updated_at=time.time())
                _write_private_json(directory / "state.json", state)
            return state

    def cancel(self, job_id):
        with self._lock:
            state = self.job(job_id)
            if state.get("status") in ACTIVE:
                directory = self._job_path(job_id)
                with open(directory / "cancel", "w", encoding="utf-8") as output:
                    output.write("cancel\n")
                (directory / "cancel").chmod(0o600)
                # The worker owns the final state and terminates only its child.
                return dict(state, message="已请求停止；已完成的安装不会回滚。", cancel_requested=True)
            return state
