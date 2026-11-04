"""调用三家官方命令行。

只用各家官方命令行工具和你自己的会员登录，不用 API Key：
- Claude：Claude Code（claude -p）
- GPT：Codex CLI（codex exec），用 ChatGPT 账号登录
- Gemini：Antigravity CLI（agy stream-json），用 Google 账号登录

提示词一律走标准输入，避免 Windows 命令行的长度和转义问题。
"""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .common import MEMBER_ORDER, name_of
from .network import apply_proxy

IS_WINDOWS = os.name == "nt"

CLI_INFO = {
    "claude": {
        "cli": "Claude Code",
        "command": "claude",
        "install_windows": ["在 PowerShell 里运行：irm https://claude.ai/install.ps1 | iex",
                            "或者：winget install Anthropic.ClaudeCode"],
        "install_unix": ["curl -fsSL https://claude.ai/install.sh | bash"],
        "login": "在终端运行 claude auth login，按提示用 Claude 账号完成浏览器授权",
    },
    "gpt": {
        "cli": "Codex CLI",
        "command": "codex",
        "install_windows": ['在 PowerShell 里运行：powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"',
                            "或者：npm install -g @openai/codex"],
        "install_unix": ["curl -fsSL https://chatgpt.com/codex/install.sh | sh",
                         "或者：npm install -g @openai/codex"],
        "login": "在终端运行 codex，选择 Sign in with ChatGPT（也可以运行 codex login）",
    },
    "gemini": {
        "cli": "Antigravity CLI",
        "command": "agy",
        "install_windows": ["在 PowerShell 里运行：irm https://antigravity.google/cli/install.ps1 | iex"],
        "install_unix": ["curl -fsSL https://antigravity.google/cli/install.sh | bash"],
        "login": "在终端运行一次 agy，用个人 Google 账号登录；请使用支持 stream-json 输入输出的当前版本",
    },
}


def install_lines(member: str) -> List[str]:
    return CLI_INFO[member]["install_windows" if IS_WINDOWS else "install_unix"]


# 去掉已知 API/第三方路由环境变量；存储的登录方式另行检查。
API_KEY_VARS = {
    "claude": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"],
    "gpt": ["OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTIGRAVITY_API_KEY",
               "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS"],
}


class MemberError(Exception):
    """成员这次没能给出回复（未安装、未登录、额度用完、超时……）。"""


class QuotaUnavailable(MemberError):
    """A failed CLI operation explicitly reported exhausted quota or throttling."""

    def __init__(self, message: str, kind: str = "quota"):
        if kind not in ("quota", "rate_limit"):
            raise ValueError("unknown quota failure kind")
        super().__init__(message)
        self.kind = kind


class Cancelled(Exception):
    """群主点了停止。"""


# ---------------------------------------------------------------- 进程管理

def kill_tree(proc: subprocess.Popen) -> None:
    """结束进程以及它启动的所有子进程。"""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        if IS_WINDOWS:
            if proc.poll() is None:
                proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
        if self.cancelled:
            kill_tree(proc)

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            threading.Thread(target=kill_tree, args=(proc,), daemon=True).start()

    def sleep(self, seconds: float) -> None:
        if self._event.wait(seconds):
            raise Cancelled()


def run_process(cmd: List[str], input_text: Optional[str], cwd: Optional[str], timeout: float,
                cancel: Optional[CancelToken] = None, env: Optional[dict] = None) -> Tuple[int, str, str]:
    """运行命令并收集输出；支持超时和中途停止。返回（退出码、标准输出、标准错误）。"""
    kwargs: Dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    if cancel and cancel.cancelled:
        raise Cancelled()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, **kwargs)
    except FileNotFoundError:
        raise MemberError("找不到命令：%s" % cmd[0])
    except OSError as exc:
        raise MemberError("无法启动 %s：%s" % (cmd[0], exc))
    if cancel:
        cancel.attach(proc)

    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []

    def pump(stream, sink):
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    break
                sink.append(chunk)
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=pump, args=(proc.stdout, out_chunks), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err_chunks), daemon=True)]
    for t in readers:
        t.start()

    if input_text is not None:
        data = input_text.encode("utf-8")

        def feed():
            try:
                proc.stdin.write(data)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

        threading.Thread(target=feed, daemon=True).start()

    deadline = time.monotonic() + timeout
    reason = None
    try:
        while True:
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel and cancel.cancelled:
                reason = "cancelled"
                break
            if time.monotonic() > deadline:
                reason = "timeout"
                break
        if reason is None and cancel and cancel.cancelled:
            reason = "cancelled"  # 进程刚好被停止操作结束
        if reason:
            kill_tree(proc)
        for t in readers:
            t.join(timeout=5)
        if any(t.is_alive() for t in readers):
            # 主进程已经退出，但它留下的子进程还占着输出管道，一并清理
            kill_tree(proc)
            for t in readers:
                t.join(timeout=2)
    finally:
        if cancel:
            cancel.detach(proc)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

    out = b"".join(out_chunks).decode("utf-8", errors="replace")
    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    if reason == "cancelled":
        raise Cancelled()
    if reason == "timeout":
        raise MemberError("超过 %d 秒还没完成，已停止。可以在「成员与连接」里调大超时" % timeout)
    return proc.returncode, out, err


# ---------------------------------------------------------------- 小工具

def _registry_path_dirs() -> List[str]:
    """Windows：直接读注册表里最新的 PATH。

    刚装好的命令行会写进注册表，但本程序启动时拿到的 PATH 可能是旧的；
    读注册表就不用为了找到新装的命令行而重启本程序。"""
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []
    dirs: List[str] = []
    keys = ((winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"))
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        for part in str(value).split(";"):
            part = os.path.expandvars(part.strip().strip('"'))
            if part and part not in dirs:
                dirs.append(part)
    return dirs


def _extra_bin_dirs() -> List[Path]:
    home = Path.home()
    dirs = [home / ".local" / "bin", home / ".claude" / "local", home / ".npm-global" / "bin",
            home / "bin", home / ".antigravity" / "bin", Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
    if IS_WINDOWS:
        for var, sub in (("APPDATA", "npm"), ("LOCALAPPDATA", "Antigravity"), ("LOCALAPPDATA", "Programs")):
            base = os.environ.get(var)
            if base:
                dirs.append(Path(base) / sub)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.extend([Path(local) / "agy" / "bin", Path(local) / "Programs" / "OpenAI" / "Codex" / "bin"])
    return dirs


def _search_path() -> str:
    """当前 PATH + 注册表里的 PATH + 常见安装位置，去重后的搜索路径。"""
    parts: List[str] = []
    for part in (os.environ.get("PATH", "").split(os.pathsep) + _registry_path_dirs()
                 + [str(d) for d in _extra_bin_dirs()]):
        if part and part not in parts:
            parts.append(part)
    return os.pathsep.join(parts)


def find_command(member: str, conf: dict) -> Optional[str]:
    custom = (conf.get("command") or "").strip().strip('"')
    if custom:
        if os.path.isfile(custom):
            return custom
        return shutil.which(custom) or shutil.which(custom, path=_search_path())
    name = CLI_INFO[member]["command"]
    if IS_WINDOWS and member == "gemini":
        # 优先用命令行版本的 agy，避免和 Antigravity 桌面应用带的同名启动器混淆
        base = os.environ.get("LOCALAPPDATA")
        current = Path(base) / "agy" / "bin" / "agy.exe" if base else None
        if current and current.is_file():
            return str(current)
        cli = Path(base) / "Antigravity" / "agy.exe" if base else None
        if cli and cli.is_file():
            return str(cli)
    return shutil.which(name) or shutil.which(name, path=_search_path())


def split_args(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=not IS_WINDOWS)
    except ValueError as exc:
        raise MemberError("附加参数格式有误：%s" % exc)


def tuning_args(member: str, text: str) -> List[str]:
    """只接受调优参数，避免覆盖输入协议、账号路由、会话和工具限制。"""
    args = split_args(text)
    allowed = {"claude": {"--effort", "--max-turns"}, "gemini": {"--effort"}}
    result = []
    i = 0
    while i < len(args):
        flag = args[i]
        if member == "gpt" and flag in ("-c", "--config") and i + 1 < len(args):
            key, sep, value = args[i + 1].partition("=")
            if sep and value and key in {"model_reasoning_effort", "model_reasoning_summary", "model_verbosity"}:
                result.extend([flag, args[i + 1]])
                i += 2
                continue
        if flag in allowed.get(member, set()) and i + 1 < len(args) and not args[i + 1].startswith("-"):
            result.extend([flag, args[i + 1]])
            i += 2
            continue
        raise MemberError("附加参数不支持 %s。此处仅允许 effort、max-turns（Claude）或 Codex 的推理/详细度配置；模型请填写模型栏" % flag)
    return result


def account_status(member: str, cmd: str, env: dict, cwd=None, cancel=None) -> Tuple[bool, str]:
    """只调用 CLI 的状态命令，不读取凭据文件、不发起模型请求。"""
    args = ["auth", "status"] if member == "claude" else ["login", "status"]
    code, out, err = run_process([cmd] + args, None, cwd, 20, cancel, env)
    if member == "claude":
        data = parse_json_output(out)
        if not isinstance(data, dict) or code != 0 or not data.get("loggedIn", data.get("logged_in", False)):
            return False, "无法确认 Claude 已登录，请在终端运行 claude auth status，并通过 claude 登录会员账号"
        method = str(data.get("authMethod") or data.get("auth_method") or "").lower()
        if method not in ("claude.ai", "oauth_token"):
            return False, "Claude 当前不是可确认的会员账号登录（%s），已停止调用；请在 claude 中切换登录方式" % (method or "未知")
        return True, "Claude 会员账号"
    if code == 0 and "chatgpt" in (out + "\n" + err).lower():
        return True, "ChatGPT 账号"
    return False, "无法确认 Codex 使用 ChatGPT 账号登录，已停止调用；请运行 codex login 选择 Sign in with ChatGPT"


def check_agy_account_settings() -> None:
    """只检查公开设置中的路由字段；不访问 token、keyring 或凭据文件。"""
    path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    if not path.exists():
        return
    try:
        with path.open(encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        raise MemberError("无法读取 Antigravity 的账号路由设置；请在 agy 中检查设置后重试")
    if not isinstance(settings, dict):
        raise MemberError("Antigravity 的 settings.json 格式不正确")
    if settings.get("modelProvider") or settings.get("gcp"):
        raise MemberError("Antigravity 配置了 API 或 Google Cloud 路由，已停止调用；请在 agy 中切换个人 Google 账号并清除对应路由配置")


def member_env(member: str, settings: dict) -> Tuple[dict, List[str]]:
    env = apply_proxy(dict(os.environ), settings.get("proxy", {"mode": "system", "url": ""}))
    removed: List[str] = []
    if settings.get("membership_only", True):
        for key in API_KEY_VARS.get(member, []):
            if env.pop(key, None) is not None:
                removed.append(key)
    if member == "claude":
        # 如果本工具是从 Claude Code 的终端里启动的，去掉这个标记，否则 claude 会拒绝嵌套运行
        env.pop("CLAUDECODE", None)
    if IS_WINDOWS:
        # 用最新的 PATH，这样刚装好的命令行（以及它依赖的 node 等）也能找到
        env["PATH"] = _search_path()
    env["NO_COLOR"] = "1"
    return env, removed


def parse_json_output(text: str):
    """从命令行输出里找出 JSON 对象（可能混有其他提示文字）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _tail(text: str, limit: int = 500) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "").strip()
    return text if len(text) <= limit else "……" + text[-limit:]


def classify_cli_failure(message) -> str:
    """Classify an already-failed CLI operation; never call on successful answers.

    Return ``quota``, ``rate_limit`` or an empty string. Ordinary quota notices,
    authentication errors and timeouts do not establish an exhausted allowance.
    Accept MemberError as a convenience for the engine's exception handler.
    """
    if isinstance(message, QuotaUnavailable):
        return message.kind
    if isinstance(message, MemberError):
        message = str(message)
    if not isinstance(message, str):
        return ""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", message).casefold().replace("’", "'")
    quota_patterns = (
        r"\binsufficient_quota\b",
        r"\bquota[\s_-]+(?:(?:is|was|has been)\s+)?(?:exhausted|exceeded|depleted)\b",
        r"\bexceeded\s+(?:(?:your|the|current|available)\s+)*quota\b",
        r"\busage[\s_-]+limit[\s_-]+(?:(?:is|was|has been)\s+)?(?:reached|exceeded)\b",
        r"\byou(?:'ve| have)\s+hit\s+your\s+(?:(?:usage|message|session|weekly|daily|monthly|opus|sonnet)\s+)?limit\b",
        r"\bno\s+remaining\s*quota\b",
        r"\bno\s+quota\s+remaining\b",
        r"额度(?:已(?:经)?)?(?:用尽|用完|耗尽|不足)",
        r"超(?:过|出)(?:了)?(?:可用|使用)?额度",
    )
    # Prefer explicit exhausted allowance over a generic accompanying HTTP 429.
    if any(re.search(pattern, text) for pattern in quota_patterns):
        return "quota"
    rate_patterns = (
        r"\brate_limit_error\b",
        r"\brate[\s_-]+limit[\s_-]+(?:(?:is|was|has been)\s+)?(?:reached|exceeded)\b",
        r"\btoo\s+many\s+requests\b",
        r"\b(?:http(?:/\d(?:\.\d)?)?(?:\s+error)?|status(?:[\s_-]*code)?|response\s+code|error\s+code)[\s\"':=()-]{0,8}429\b",
        r"请求(?:过于频繁|过多|被限流)",
        r"(?:触发|已被|遭到)限流",
    )
    if any(re.search(pattern, text) for pattern in rate_patterns):
        return "rate_limit"
    return ""


def explain(text: str, member: str) -> str:
    """把常见的报错翻译成能照着做的提示。"""
    raw = _tail(text)
    low = raw.lower()
    kind = classify_cli_failure(text)
    if kind == "quota":
        return "额度已用尽，请在官方客户端确认恢复时间。原始信息：" + raw
    if kind == "rate_limit":
        return "请求被限流，请稍后恢复调用。原始信息：" + raw
    if any(k in low for k in ("not logged in", "please log in", "login", "log in", "unauthorized", "401",
                              "authenticat", "credential", "expired")):
        return "没有登录或登录已过期：%s。原始信息：%s" % (CLI_INFO[member]["login"], raw)
    return raw or "没有任何输出"


def _cli_error(text: str, member: str, prefix: str = "") -> MemberError:
    """Use only after an explicit failed CLI result, not while parsing replies."""
    kind = classify_cli_failure(text)
    message = prefix + explain(text, member)
    return QuotaUnavailable(message, kind) if kind else MemberError(message)


def _reported_model(value) -> Optional[str]:
    """Keep bounded model labels, never objects, control characters or markup."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+\[\] ()-]{0,199}", value):
        return None
    return value


# ---------------------------------------------------------------- 三家的调用方式

class CliMember:
    def __init__(self, member: str, settings: dict):
        self.member = member
        self.settings = settings
        self.conf = dict(settings["members"][member])
        # CLI reports are evidence about usage/configuration, not proof of the
        # backend model which produced the final answer. No report stays empty.
        self.last_metadata: dict = {}

    def _command(self) -> str:
        cmd = find_command(self.member, self.conf)
        if not cmd:
            info = CLI_INFO[self.member]
            raise MemberError("没有找到 %s（命令 %s）。安装方法：%s；装好后%s" % (
                info["cli"], info["command"], install_lines(self.member)[0], info["login"]))
        return cmd

    def ask(self, prompt: str, cwd: Path, cancel: Optional[CancelToken] = None) -> str:
        self.last_metadata = {}
        env, _ = member_env(self.member, self.settings)
        timeout = int(self.conf.get("timeout") or 600)
        cmd = self._command()
        tuning_args(self.member, self.conf.get("extra_args", ""))
        if self.settings.get("membership_only", True):
            if self.member in ("claude", "gpt"):
                ok, reason = account_status(self.member, cmd, env, str(cwd), cancel)
                if not ok:
                    raise MemberError(reason)
            else:
                check_agy_account_settings()
        return getattr(self, "_ask_" + self.member)(cmd, prompt, str(cwd), timeout, cancel, env)

    # Claude Code：claude -p，提示词走标准输入，输出 JSON
    def _ask_claude(self, cmd, prompt, cwd, timeout, cancel, env) -> str:
        args = [cmd, "-p", "--output-format", "json", "--permission-mode", "dontAsk",
                "--no-session-persistence", "--strict-mcp-config"]
        if self.conf.get("web"):
            args += ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch"]
        else:
            args += ["--tools", ""]
        if self.conf.get("model"):
            args += ["--model", self.conf["model"]]
        args += tuning_args("claude", self.conf.get("extra_args", ""))
        code, out, err = run_process(args, prompt, cwd, timeout, cancel, env)
        data = parse_json_output(out)
        if isinstance(data, dict) and ("result" in data or "is_error" in data):
            result = data.get("result")
            result = result if isinstance(result, str) else ""
            if code != 0 or data.get("is_error") or data.get("subtype") not in (None, "success"):
                raise _cli_error(result or str(data.get("subtype") or "") or err, "claude")
            if not result.strip():
                raise MemberError("回复为空")
            usage = data.get("modelUsage")
            if isinstance(usage, dict):
                models = list(dict.fromkeys(model for model in
                              (_reported_model(key) for key in usage) if model))
                if models:
                    # modelUsage can include subagents; never pick one as the
                    # model that necessarily produced this final answer.
                    key = "reported_model" if len(models) == 1 else "reported_models"
                    self.last_metadata = {key: models[0] if len(models) == 1 else models,
                                          "report_kind": "usage"}
            return result
        if code != 0:
            raise _cli_error(err or out, "claude")
        raise MemberError("没能解析 Claude Code 的输出：" + _tail(out or err))

    # Codex CLI：codex exec，提示词走标准输入，最终回复写到文件
    def _ask_gpt(self, cmd, prompt, cwd, timeout, cancel, env) -> str:
        out_name = "aigc-gpt-reply-%s.txt" % uuid.uuid4().hex[:8]
        out_path = Path(cwd) / out_name
        args = [cmd, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
                "--ephemeral", "-o", out_name]
        if self.conf.get("web"):
            args += ["-c", "web_search=live"]
        if self.conf.get("model"):
            args += ["-m", self.conf["model"]]
        args += tuning_args("gpt", self.conf.get("extra_args", ""))
        if self.settings.get("membership_only", True):
            args += ["-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"']
        args += ["-c", "features.shell_tool=false"]
        args.append("-")
        try:
            code, out, err = run_process(args, prompt, cwd, timeout, cancel, env)
            text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
        finally:
            try:
                out_path.unlink()
            except OSError:
                pass
        if code != 0:
            raise _cli_error(err or out or "调用失败（退出码 %s）" % code, "gpt")
        if text.strip():
            return text
        if code == 0 and out.strip():  # 个别版本只把最终回复打印到标准输出
            return out
        raise MemberError(explain(err or out, "gpt") if (err or out) else "没有给出回复（退出码 %s）" % code)

    # Antigravity CLI：一个 user 事件经 stdin 传入，关闭 stdin 后等待最终 result。
    def _ask_gemini(self, cmd, prompt, cwd, timeout, cancel, env) -> str:
        tmp = tempfile.mkdtemp(prefix="aigc-gemini-")
        try:
            args = [cmd, "--input-format", "stream-json", "--output-format", "stream-json",
                    "--print-timeout", "%ds" % timeout]
            if self.conf.get("model"):
                args += ["--model", self.conf["model"]]
            args += tuning_args("gemini", self.conf.get("extra_args", ""))
            payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
            code, out, err = run_process(args, payload, tmp, timeout + 30, cancel, env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        results = []
        configured_models = set()
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "result" and isinstance(event.get("result"), dict):
                results.append(event["result"])
            if isinstance(event, dict) and event.get("event") == "init" and isinstance(event.get("init"), dict):
                model = _reported_model(event["init"].get("model"))
                if model:
                    configured_models.add(model)
        if len(results) == 1:
            data = results[0]
            response = data.get("response")
            status = str(data.get("status") or "").upper()
            if code != 0 or status != "SUCCESS":
                reason = str(data.get("error") or err or "未完成")
                raise _cli_error(reason, "gemini", "Gemini 调用未成功（%s），部分回复未写入看板或记忆。" % (status or "未知状态"))
            if isinstance(response, str) and response.strip():
                if len(configured_models) == 1:
                    # init.model echoes CLI configuration. It is not a
                    # server-side attestation of the model used for inference.
                    self.last_metadata = {"reported_model": next(iter(configured_models)),
                                          "report_kind": "configuration"}
                return response
            raise MemberError("Gemini 回复为空或格式不正确")
        if code != 0:
            raise _cli_error(err or out, "gemini")
        raise MemberError("没有收到唯一的 agy 完成事件，请升级 Antigravity CLI（需要 stream-json 输入输出支持）。" + _tail(err))


# ---------------------------------------------------------------- 演示成员（不调用任何命令行）

_DEMO_VOICE = {
    "claude": ("我是 Claude。", "我建议先把目标、边界和成功标准说清楚，再决定做法，避免一开始就陷进细节。"),
    "gpt": ("我是 GPT。", "我补充执行层面的看法：先拆出最小可行的第一步，定好负责人和时间点，边做边校准。"),
    "gemini": ("我是 Gemini。", "我从信息和资料的角度补充：先收集现状和可参考的案例，用事实来支撑后面的决定。"),
}


class DemoMember:
    """演示用的假成员：不消耗任何额度，用来熟悉界面和流程。"""

    def __init__(self, member: str, delay: float = 1.2):
        self.member = member
        self.delay = delay
        self.last_metadata: dict = {"simulation": True}

    def ask(self, prompt: str, cwd: Path, cancel: Optional[CancelToken] = None) -> str:
        self.last_metadata = {"simulation": True}
        if self.delay > 0:
            wait = self.delay * random.uniform(0.6, 1.4)
            if cancel:
                cancel.sleep(wait)
            else:
                time.sleep(wait)
        if "这是连通测试" in prompt:
            return "收到"
        name = name_of(self.member)
        if "书记员" in prompt:
            return "（演示摘要，由 %s 整理）大家围绕群目标交换了看法，已形成的决议和分工见看板；尚待解决的问题会在后续讨论。" % name
        return self._reply(prompt)

    def _reply(self, prompt: str) -> str:
        intro, view = _DEMO_VOICE[self.member]
        name = name_of(self.member)
        topic = ""
        for m in re.finditer(r"#\d+ 群主（[^）]*）：\n(.*?)(?=\n\n#\d+ |\n\n## |\Z)", prompt, re.S):
            encoded = m.group(1).strip()
            try:
                topic = json.loads(encoded)
            except ValueError:
                topic = encoded  # Older prompt fixtures remain readable.
        topic = re.sub(r"\s+", " ", topic)[:40] or "这个话题"
        board = prompt.split("## 共享看板", 1)[-1].split("## 前情摘要", 1)[0]

        votes, updates, delivered = [], [], []
        working = "开工" in topic
        for m in re.finditer(r"- ([DT]-\d{3})【(?:讨论中|提议中)】.*?\n  表态：([^\n]*)", board):
            if "%s 未表态" % name in m.group(2):
                votes.append({"id": m.group(1), "vote": "agree"})
        for m in re.finditer(r"- (T-\d{3})【(待办|进行中)】负责人：%s｜([^｜\n]*)" % re.escape(name), board):
            if working or m.group(2) == "进行中":
                delivered.append("**%s 交付（演示）**：%s\n1. 现状要点一\n2. 现状要点二\n3. 建议的下一步" % (m.group(1), m.group(3)))
                updates.append({"id": m.group(1), "status": "done", "note": "（演示）已完成，交付内容见正文"})
            else:
                updates.append({"id": m.group(1), "status": "doing", "note": "（演示）已开始"})

        decisions, tasks = [], []
        if "### 决议\n（还没有）" in board and "背靠背发言已经结束" not in prompt:
            decisions.append("（演示决议）围绕「%s」，先明确目标和范围，再分工推进" % topic)
            others = [m for m in MEMBER_ORDER if m != self.member]
            tasks.append({"title": "（演示任务）收集「%s」的现状和参考资料" % topic,
                          "owner": others[0], "deliverable": "一页要点"})

        lines = ["（演示回复）" + intro + "关于「%s」：%s" % (topic, view)]
        if "## 共享长期记忆（本轮快照）" in prompt:
            memory_text = prompt.split("## 共享长期记忆（本轮快照）", 1)[1].split("## 共享看板", 1)[0]
            for line in memory_text.splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(item, dict) and item.get("status") == "confirmed"
                        and isinstance(item.get("id"), str) and item["id"].startswith("mem-")
                        and isinstance(item.get("content"), str) and item["content"].strip()):
                    lines.append("（演示：已收到本轮共享记忆 %s，版本 %s）%s" % (
                        item["id"], item.get("version", "?"), item["content"][:160]))
                    break
        lines += delivered
        if votes:
            lines.append("看板上的 %s 我都同意。" % "、".join(v["id"] for v in votes))
        if decisions:
            lines.append("我提议先确定一条决议，并提一个分工，请大家表态。")
        status = {
            "summary": "%s 发表了看法%s" % (name, "并表态" if votes else ""),
            "votes": votes, "propose_decisions": decisions, "propose_tasks": tasks,
            "task_updates": updates, "withdraw": [], "need_user": "", "memories": [],
        }
        if self.member == "claude" and decisions:
            status["memories"] = [{"content": "（演示候选）先明确项目范围，再安排任务", "kind": "note"}]
            lines.append("我同时提交了一条演示记忆候选，可在记忆库中查看并确认。")
        return "\n\n".join(lines) + "\n\n<<<STATUS\n" + json.dumps(status, ensure_ascii=False) + "\nSTATUS>>>"


# ---------------------------------------------------------------- 检测与调度

class Registry:
    """根据设置给出每位成员的调用方式，并负责检测安装和登录状态。"""

    def __init__(self, demo: bool = False, demo_delay: float = 1.2):
        self.demo = demo
        self.demo_delay = demo_delay
        self._cache: Dict[str, Tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()

    def adapter(self, member: str, settings: dict):
        if self.demo:
            return DemoMember(member, self.demo_delay)
        return CliMember(member, settings)

    # ----- 检测
    def detect(self, settings: dict, refresh: bool = False) -> Dict[str, dict]:
        results: Dict[str, dict] = {}

        def work(member):
            results[member] = self._detect_one(member, settings, refresh)

        threads = [threading.Thread(target=work, args=(m,), daemon=True) for m in MEMBER_ORDER]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
        for m in MEMBER_ORDER:
            results.setdefault(m, {"id": m, "name": name_of(m), "installed": False, "auth": "检测超时", "auth_ok": None})
        return results

    def _detect_one(self, member: str, settings: dict, refresh: bool) -> dict:
        conf = settings["members"][member]
        key = "%s|%s|%s|%s" % (member, conf.get("command", ""), settings.get("membership_only", True),
                                 json.dumps(settings.get("proxy", {}), sort_keys=True))
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit and not refresh and time.time() - hit[0] < 60:
            return hit[1]

        info = CLI_INFO[member]
        result = {
            "id": member, "name": name_of(member), "cli": info["cli"], "command": info["command"],
            "install": install_lines(member), "login": info["login"], "demo": self.demo,
            "path": None, "installed": False, "version": "", "auth": "", "auth_ok": None, "warnings": [],
        }
        if self.demo:
            result.update(installed=True, version="演示模式", auth="演示模式：不调用真实命令行，不消耗额度", auth_ok=True)
            return result

        env, removed = member_env(member, settings)
        if removed:
            result["warnings"].append("检测到 API/第三方路由环境变量 %s，调用时已忽略；登录方式仍需核验" % "、".join(removed))
        path = find_command(member, conf)
        if not path:
            result["auth"] = "未安装"
            with self._cache_lock:
                self._cache[key] = (time.time(), result)
            return result
        result.update(path=path, installed=True)

        def quick(args):
            try:
                return run_process([path] + args, None, None, 20, None, env)
            except (MemberError, Cancelled) as exc:
                return -1, "", str(exc)

        code, out, err = quick(["--version"])
        result["version"] = _tail((out or err).strip().splitlines()[0] if (out or err).strip() else "", 80)

        if member == "claude":
            code, out, err = quick(["auth", "status"])
            data = parse_json_output(out)
            if code == 0 and isinstance(data, dict) and data.get("loggedIn", data.get("logged_in", False)):
                method = str((data or {}).get("authMethod") or (data or {}).get("auth_method") or "")
                account = ""
                for k in ("email", "emailAddress", "account"):
                    if isinstance((data or {}).get(k), str):
                        account = data[k]
                        break
                if method.lower() not in ("claude.ai", "oauth_token"):
                    result.update(auth="登录方式不是可确认的会员账号（API Key/第三方/未知）", auth_ok=False)
                else:
                    result.update(auth="已登录" + ("（%s）" % account if account else "（会员账号）"), auth_ok=True)
            else:
                result.update(auth="未登录：" + info["login"], auth_ok=False)
        elif member == "gpt":
            code, out, err = quick(["login", "status"])
            text = _tail((out + "\n" + err).strip(), 120)
            if code == 0:
                if "chatgpt" not in text.lower():
                    result.update(auth="登录方式不是可确认的 ChatGPT 账号（API Key/未知）", auth_ok=False)
                else:
                    result.update(auth="已登录" + ("（ChatGPT 账号）" if "chatgpt" in text.lower() else "：" + text), auth_ok=True)
            else:
                result.update(auth="未登录：" + info["login"], auth_ok=False)
        else:
            result.update(auth="已安装；请先在 agy 确认个人账号，再测试连通。连通不代表已核验计费方式", auth_ok=None)
            if settings.get("membership_only", True):
                try:
                    check_agy_account_settings()
                except MemberError as exc:
                    result.update(auth=str(exc), auth_ok=False)
            if any(part.startswith(".") for part in Path(tempfile.gettempdir()).parts[1:]):
                result["warnings"].append("临时目录路径里有以点开头的文件夹，agy 可能拒绝工作；请把 TMPDIR（Windows 为 TEMP）设为普通路径")

        with self._cache_lock:
            self._cache[key] = (time.time(), result)
        return result

    def ping(self, member: str, settings: dict, cwd: Path) -> dict:
        from .prompts import PING_PROMPT
        start = time.time()
        try:
            reply = self.adapter(member, settings).ask(PING_PROMPT, cwd, CancelToken())
            return {"ok": True, "reply": _tail(reply, 300), "elapsed": round(time.time() - start, 1)}
        except (MemberError, Cancelled) as exc:
            return {"ok": False, "error": str(exc), "elapsed": round(time.time() - start, 1)}
