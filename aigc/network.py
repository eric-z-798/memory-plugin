"""Application-scoped proxy configuration and a generation-free network check.

Proxy choices only affect child-process environments and explicit HTTP openers.
They never edit system proxy settings, shell profiles or the TLS trust policy.
The probe visits fixed public homepages without following redirects or reading
response bodies. Receiving HTTP is not proof of login or model availability.
"""

import base64
import ipaddress
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_PROXY = {"mode": "system", "url": ""}
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_PROXY_KEYS = {"http_proxy", "https_proxy", "all_proxy"}
_ENDPOINTS = {
    "claude": "https://claude.ai/",
    "gpt": "https://chatgpt.com/",
    "gemini": "https://antigravity.google/",
}
_PROBE_SLOTS = threading.BoundedSemaphore(3)
_PROBE_TIMEOUT = 5.0


def _proxy_url(value):
    if not isinstance(value, str):
        raise ValueError("代理地址必须是字符串")
    if not value:
        return ""
    if len(value) > 2048 or any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError("代理地址不能包含空白或控制字符")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError("代理地址格式不正确")
    if parsed.scheme not in ("http", "https") or not host or port is None or not 1 <= port <= 65535:
        raise ValueError("代理地址需要 http:// 或 https://、主机名和明确端口，例如 http://127.0.0.1:7890")
    if (parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or "?" in value or "#" in value):
        raise ValueError("代理地址不支持用户名密码、路径、查询参数或片段")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("代理 IPv6 地址格式不正确")
        rendered_host = "[" + host.lower() + "]"
    else:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("代理主机名格式不正确")
        if (len(ascii_host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.?", ascii_host)
                or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                       for label in ascii_host.rstrip(".").split("."))):
            raise ValueError("代理主机名格式不正确")
        rendered_host = ascii_host
    return "%s://%s:%d" % (parsed.scheme, rendered_host, port)


def clean_proxy(value):
    """Validate saved settings; no proxy credentials are accepted or persisted."""
    if not isinstance(value, dict) or set(value) - {"mode", "url"}:
        raise ValueError("代理设置格式不正确")
    mode = value.get("mode", "system")
    if mode not in ("system", "direct", "custom"):
        raise ValueError("代理模式只能是 system、direct 或 custom")
    url = _proxy_url(value.get("url", ""))
    if mode == "custom" and not url:
        raise ValueError("自定义代理需要填写代理地址")
    return {"mode": mode, "url": url}


def apply_proxy(env, config):
    """Return a detached subprocess environment with OAuth loopback bypasses."""
    if not isinstance(env, dict):
        raise ValueError("进程环境格式不正确")
    proxy = clean_proxy(config)
    result = dict(env)
    bypass = []
    # Preserve both common forms: subprocesses disagree about precedence.
    bypass_keys = ["NO_PROXY", "no_proxy"] + [key for key in result
        if str(key).lower() == "no_proxy" and key not in ("NO_PROXY", "no_proxy")]
    for key in bypass_keys:
        for item in str(result.get(key) or "").split(","):
            item = item.strip()
            if item and item.casefold() not in {p.casefold() for p in bypass}:
                bypass.append(item)
    for host in _LOOPBACK:
        if host.casefold() not in {p.casefold() for p in bypass}:
            bypass.append(host)
    for key in list(result):
        if str(key).lower() == "no_proxy":
            result.pop(key, None)
        elif proxy["mode"] != "system" and str(key).lower() in _PROXY_KEYS:
            result.pop(key, None)
    if proxy["mode"] == "custom":
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            result[key] = proxy["url"]
    result["NO_PROXY"] = result["no_proxy"] = ",".join(bypass)
    return result


class _ExplicitProxyHandler(urllib.request.ProxyHandler):
    """Honor the supplied environment, including bypasses, without reading globals."""

    def __init__(self, env):
        def selected(name):
            if name in env:
                return env[name]
            return env.get(name.upper(), "")
        fallback = selected("all_proxy")
        proxies = {scheme: selected(scheme + "_proxy") or fallback for scheme in ("http", "https")}
        self._no_proxy = selected("no_proxy")
        super().__init__({key: value for key, value in proxies.items() if value})

    def proxy_open(self, req, proxy, protocol):
        # ProxyHandler's default bypass check reads os.environ even when an
        # explicit proxies mapping was passed. Keep this check call-scoped.
        if urllib.request.proxy_bypass_environment(req.host, {"no": self._no_proxy}):
            return None
        original_type = req.type
        proxy_type, username, password, hostport = urllib.request._parse_proxy(proxy)
        proxy_type = proxy_type or original_type
        if proxy_type not in ("http", "https"):
            raise urllib.error.URLError("Unsupported inherited proxy protocol")
        if username is not None and password is not None:
            encoded = (urllib.parse.unquote(username) + ":" + urllib.parse.unquote(password)).encode()
            req.add_header("Proxy-authorization", "Basic " + base64.b64encode(encoded).decode("ascii"))
        req.set_proxy(urllib.parse.unquote(hostport), proxy_type)
        if original_type == proxy_type or original_type == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def network_probe(member, settings):
    """Probe a fixed official homepage in at most five seconds for the caller.

    No authentication, response-body parsing, TLS bypass or model API is used.
    Daemon workers cover operating-system DNS calls that ignore socket timeouts;
    concurrency is capped so repeated checks cannot create unlimited workers.
    """
    if not isinstance(member, str) or member not in _ENDPOINTS:
        raise ValueError("未知成员")
    if not isinstance(settings, dict):
        raise ValueError("网络设置格式不正确")
    proxy = clean_proxy(settings.get("proxy", DEFAULT_PROXY))
    env = apply_proxy(dict(os.environ), proxy)
    result = {"ok": False, "checked_at": time.time(), "endpoint": _ENDPOINTS[member],
              "proxy_mode": proxy["mode"], "message": ""}
    slots = _PROBE_SLOTS
    if not slots.acquire(blocking=False):
        result["message"] = "已有网络检测正在等待响应，请稍后再试"
        return result
    responses = queue.Queue(maxsize=1)

    def work():
        try:
            opener = urllib.request.build_opener(_ExplicitProxyHandler(env), _NoRedirect())
            request = urllib.request.Request(_ENDPOINTS[member], method="HEAD",
                                             headers={"User-Agent": "Xingshu-network-check"})
            try:
                with opener.open(request, timeout=_PROBE_TIMEOUT) as response:
                    code = response.getcode()
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
            if code == 407:
                responses.put((False, "代理要求身份认证；自定义代理目前不支持用户名密码"))
            else:
                responses.put((True, "收到 HTTP %s 响应；网站或代理已响应，不代表登录或模型调用可用" % code))
        except Exception:
            # Raw exceptions may contain proxy credentials, local paths or
            # server diagnostics. Only return a fixed actionable description.
            responses.put((False, "网络检测未完成，请检查代理地址、代理是否运行、网络和证书设置；不会关闭 TLS 验证"))
        finally:
            slots.release()

    threading.Thread(target=work, name="aigc-network-check", daemon=True).start()
    try:
        result["ok"], result["message"] = responses.get(timeout=_PROBE_TIMEOUT)
    except queue.Empty:
        result["message"] = "网络检测超过 5 秒；请检查代理或网络后重试"
    return result
