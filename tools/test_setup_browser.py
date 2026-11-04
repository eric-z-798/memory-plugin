"""Onboarding browser QA, isolated from real installation, login and networking.

Run ``python tools/test_setup_browser.py`` with Playwright and Chromium installed.
The server stays in demo mode. Native job and network responses are browser
fixtures; server-side subprocess and probe functions are also explicitly blocked.
"""

from qa_auth import BOOTSTRAP, TOKEN, urlopen as qa_urlopen, authenticate, authenticate_async
import copy
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


PROJECT = Path(__file__).resolve().parents[1]
ARTIFACTS = PROJECT / "work" / "setup-browser-qa"
NAMES = {"claude": "Claude", "gpt": "GPT", "gemini": "Gemini"}
INSTALL_HOSTS = {"claude": "claude.ai", "gpt": "chatgpt.com", "gemini": "antigravity.google"}
LOGIN_COMMANDS = {"claude": "claude auth login", "gpt": "codex login", "gemini": "agy"}


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="data-", dir=ARTIFACTS) as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = "http://127.0.0.1:%s" % port
        sentinel = Path(directory) / "forbidden-action.txt"
        bootstrap = """
import sys
import subprocess
from pathlib import Path
import run
import secrets
secrets.token_urlsafe=lambda n: 'synthetic-browser-qa-token-only'
import aigc.setup as setup
import aigc.server as server

def blocked(*args, **kwargs):
    Path(sys.argv[sys.argv.index('--data-dir') + 1], 'forbidden-action.txt').write_text('blocked', encoding='utf-8')
    raise RuntimeError('Browser QA forbids native subprocesses and network probes')

subprocess.Popen = blocked
setup._launch_terminal = blocked
setup.find_command = lambda *args, **kwargs: None
setup.SetupManager.start = blocked
server.network_probe = blocked
run.main()
"""
        with (ARTIFACTS / "server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "-c", bootstrap, "--demo", "--no-browser", "--port", str(port),
                 "--data-dir", directory], cwd=PROJECT, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise AssertionError("Guarded demo server exited; see server.log")
                    try:
                        with qa_urlopen(base + "/api/info", timeout=1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise AssertionError("Guarded demo server did not become ready")
                run_browser(base, sentinel)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def run_browser(base, sentinel):
    checks, errors, blocked, requests, starts, cancellations, network_calls = [], [], [], [], [], [], []
    fixture = {"enabled": False, "can_launch": False, "reason": "", "overview": None,
               "jobs": {}, "installed": {"claude": False, "gpt": True, "gemini": True},
               "auth": {"claude": False, "gpt": False, "gemini": None}}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium") or None,
                                             headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1, reduced_motion="reduce")
        authenticate(page)
        page.add_init_script("""window.__copiedSetupCommands = [];
            Object.defineProperty(navigator, 'clipboard', {configurable: true,
                value: {writeText: async text => window.__copiedSetupCommands.push(text)}});""")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append("console: " + message.text)
                if message.type == "error" else None)

        def api(method, path, data=None):
            response = page.request.fetch(base + path, method=method, headers={"X-AIGC": "1"}, data=data)
            assert response.ok, (response.status, response.text())
            return response.json()

        def overview_fixture():
            result = copy.deepcopy(fixture["overview"])
            result.update(can_launch=fixture["can_launch"], reason=fixture["reason"])
            for member, info in result["members"].items():
                info["installed"] = fixture["installed"][member]
                info["active_job"] = next((copy.deepcopy(job) for job in fixture["jobs"].values()
                                           if job["member"] == member and job["status"] in ("launching", "running")), None)
            return result

        def guard_and_mock(route):
            request = route.request
            if not request.url.startswith(base + "/"):
                blocked.append(request.url)
                route.abort()
                return
            path = urlsplit(request.url).path
            body = request.post_data_json if request.post_data else None
            requests.append({"method": request.method, "path": path, "body": body})
            start = re.fullmatch(r"/api/setup/(claude|gpt|gemini)/start", path)
            network = re.fullmatch(r"/api/setup/(claude|gpt|gemini)/network", path)
            job_path = re.fullmatch(r"/api/setup/jobs/([a-f0-9]{32})(/cancel)?", path)
            if start:
                # Never forward a launch to the server, even if the UI misbehaves.
                member = start.group(1)
                starts.append((member, body, fixture["enabled"]))
                job_id = "%032x" % len(starts)
                job = {"id": job_id, "member": member, "action": body.get("action"),
                       "status": "launching", "created_at": time.time(), "updated_at": time.time(),
                       "message": "浏览器测试夹具：准备打开终端，未执行任何命令"}
                fixture["jobs"][job_id] = job
                route.fulfill(status=202, json={"job": job})
            elif network:
                network_calls.append((network.group(1), body))
                route.fulfill(json={"ok": True, "checked_at": time.time(), "proxy_mode": "custom",
                                    "endpoint": "https://" + INSTALL_HOSTS[network.group(1)] + "/",
                                    "message": "浏览器夹具：网络可达；未实际发送请求"})
            elif job_path:
                job_id = job_path.group(1)
                if job_path.group(2):
                    cancellations.append((job_id, body))
                    fixture["jobs"][job_id].update(status="cancelled", updated_at=time.time(),
                                                  message="浏览器夹具：已取消；未执行安装")
                route.fulfill(json={"job": fixture["jobs"][job_id]})
            elif fixture["enabled"] and path == "/api/setup":
                route.fulfill(json=overview_fixture())
            elif fixture["enabled"] and path == "/api/info":
                response = route.fetch()
                info = response.json()
                info["demo"] = False
                route.fulfill(response=response, json=info)
            elif fixture["enabled"] and path == "/api/members":
                response = route.fetch()
                info = response.json()
                for member in info["members"]:
                    mid = member["id"]
                    member.update(demo=False, installed=fixture["installed"][mid], auth_ok=fixture["auth"][mid],
                                  auth="夹具：已通过 CLI 核验" if fixture["auth"][mid] is True else "夹具：登录状态未确认")
                route.fulfill(response=response, json=info)
            else:
                route.continue_()

        page.route("**/*", guard_and_mock)

        def close():
            page.locator('.modal-head button[aria-label="关闭"]').click()
            expect(page.locator(".modal")).to_have_count(0)

        def open_wizard():
            page.locator("#btn-setup").click()
            expect(page.locator(".setup-member")).to_have_count(3)
            proxy_settings = page.locator("details.setup-proxy")
            if proxy_settings.get_attribute("open") is None:
                proxy_settings.locator("summary").click()
            expect(page.get_by_label("联网方式", exact=True)).to_be_visible()

        def card(member):
            return page.locator('[data-setup-member="%s"]' % member)

        def verify_rendering():
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"), "Horizontal overflow"
            assert page.locator(".modal").evaluate("node => node.getBoundingClientRect().left >= 0 && node.getBoundingClientRect().right <= window.innerWidth")
            assert page.locator(".modal-body").evaluate("""node => {
                const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const text = walker.currentNode.textContent.trim();
                    if (text === 'null' || text.includes('[object HTML')) return false;
                }
                return true;
            }"""), "Raw optional-node text rendered in wizard"

        def set_job(member, status):
            job = next(job for job in reversed(list(fixture["jobs"].values())) if job["member"] == member)
            job.update(status=status, updated_at=time.time(), message="浏览器测试夹具：" + status)
            return job

        def start_job(member, action):
            before = len(starts)
            request_offset = len(requests)
            label = NAMES[member] + (" 一键安装并登录" if action == "setup" else " 仅打开登录")
            page.get_by_role("button", name=label, exact=True).click()
            expect(card(member).locator(".setup-job-state")).to_have_text("正在打开终端")
            assert starts[before:] == [(member, {"action": action}, True)], starts[before:]
            post_paths = [request["path"] for request in requests[request_offset:] if request["method"] == "POST"]
            assert post_paths[:2] == ["/api/setup/proxy", "/api/setup/%s/start" % member], post_paths
            expect(page.get_by_role("button", name=NAMES[member] + " 一键安装并登录", exact=True)).to_be_disabled()
            return next(reversed(fixture["jobs"].values()))

        try:
            page.goto(base)
            expect(page.locator("#btn-setup")).to_be_visible()
            fixture["overview"] = api("GET", "/api/setup")
            assert fixture["overview"]["can_launch"] is False
            open_wizard()
            expect(page.get_by_label("联网方式", exact=True)).to_have_value("system")
            expect(page.get_by_label("代理地址", exact=True)).to_be_hidden()
            expect(page.locator(".setup-host-notice")).to_contain_text("演示模式不会安装软件")
            for member, name in NAMES.items():
                expect(page.get_by_role("button", name=name + " 一键安装并登录", exact=True)).to_be_disabled()
                expect(page.get_by_role("button", name=name + " 仅打开登录", exact=True)).to_be_disabled()
                expect(page.get_by_role("button", name=name + " 保存并测试网络", exact=True)).to_be_disabled()
                expect(card(member).locator(".setup-install-state")).to_have_text("需要安装官方客户端")
                expect(card(member).locator(".setup-auth")).to_contain_text("演示模式，不代表真实安装或登录")
            assert not starts and not network_calls
            checks.append("demo onboarding defaults and disabled native actions make no installation/login claim")

            page.get_by_role("button", name="保存联网设置", exact=True).click()
            expect(page.locator(".setup-proxy-notice")).to_contain_text("已保存")
            before = api("GET", "/api/settings")
            assert before["proxy"] == {"mode": "system", "url": ""}
            page.get_by_label("联网方式", exact=True).select_option("custom")
            for invalid in ("http://username:password@127.0.0.1:7890", "socks5://127.0.0.1:7890"):
                page.get_by_label("代理地址", exact=True).fill(invalid)
                offset = len(requests)
                page.get_by_role("button", name="保存联网设置", exact=True).click()
                expect(page.locator(".modal-error")).to_contain_text("不能包含用户名、密码、路径或 SOCKS")
                assert not any(request["method"] == "POST" for request in requests[offset:])
                assert api("GET", "/api/settings") == before
            page.get_by_label("代理地址", exact=True).fill("http://127.0.0.1:7890/")
            page.get_by_role("button", name="保存联网设置", exact=True).click()
            expect(page.get_by_label("代理地址", exact=True)).to_have_value("http://127.0.0.1:7890")
            assert api("GET", "/api/settings")["proxy"] == {"mode": "custom", "url": "http://127.0.0.1:7890"}
            close()
            open_wizard()
            expect(page.get_by_label("联网方式", exact=True)).to_have_value("custom")
            expect(page.get_by_label("代理地址", exact=True)).to_have_value("http://127.0.0.1:7890")
            checks.append("proxy rejects userinfo and SOCKS without writes; valid HTTP proxy persists and reloads")

            for member, name in NAMES.items():
                info = fixture["overview"]["members"][member]
                assert urlsplit(info["install_url"]).hostname == INSTALL_HOSTS[member]
                assert info["login_command"] == LOGIN_COMMANDS[member]
                card(member).locator(".setup-manual summary").click()
                for suffix, key in ((" 官方安装命令", "install_command"), (" 登录命令", "login_command")):
                    command = page.get_by_label(name + suffix, exact=True)
                    expect(command).to_have_value(info[key])
                    expect(command).to_have_attribute("readonly", "")
                    command.locator("..").get_by_role("button", name="复制命令", exact=True).click()
                    assert page.evaluate("window.__copiedSetupCommands.at(-1)") == info[key]
            assert len(page.evaluate("window.__copiedSetupCommands")) == 6
            assert not starts and not network_calls
            checks.append("all three official install/login commands copy exactly without executing")

            page.get_by_role("button", name="打开成员与连接（高级设置）", exact=True).click()
            expect(page.get_by_label("GPT 模型选择", exact=True)).to_be_visible()
            expect(page.locator(".modal-head")).to_contain_text("成员与连接")
            page.get_by_label("GPT 手动模型", exact=True).fill("qa-preserve-advanced")
            page.get_by_role("button", name="保存", exact=True).click()
            expect(page.locator(".modal")).to_have_count(0)
            settings = api("GET", "/api/settings")
            assert settings["members"]["gpt"]["model"] == "qa-preserve-advanced"
            assert settings["proxy"] == {"mode": "custom", "url": "http://127.0.0.1:7890"}
            checks.append("existing advanced settings remain usable and preserve proxy configuration")

            # From here only browser fixtures pretend a native host is available.
            fixture.update(enabled=True, can_launch=False, reason="测试夹具：无图形终端，无法打开本机终端。")
            page.reload()
            open_wizard()
            expect(page.locator(".setup-host-notice")).to_contain_text("无图形终端")
            for name in NAMES.values():
                expect(page.get_by_role("button", name=name + " 一键安装并登录", exact=True)).to_be_disabled()
                expect(page.get_by_role("button", name=name + " 仅打开登录", exact=True)).to_be_disabled()
            assert not starts
            checks.append("headless host disables launching and retains manual command fallback")
            close()
            fixture.update(can_launch=True, reason="")
            page.reload()
            open_wizard()
            expect(page.locator(".setup-host-notice")).to_contain_text("运行本工具的电脑")
            expect(page.get_by_role("button", name="Claude 仅打开登录", exact=True)).to_be_disabled()
            expect(page.get_by_role("button", name="GPT 仅打开登录", exact=True)).to_be_enabled()
            expect(card("gpt")).to_contain_text("点击后打开官方登录")
            assert not starts and not network_calls
            verify_rendering()
            expect(page.locator(".toast")).to_have_count(0, timeout=10000)
            page.locator(".modal-body").evaluate("node => node.scrollTop = 0")
            page.screenshot(path=str(ARTIFACTS / "setup-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            verify_rendering()
            page.locator(".modal-body").evaluate("node => node.scrollTop = 0")
            page.screenshot(path=str(ARTIFACTS / "setup-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})

            start_job("gpt", "login")
            set_job("gpt", "running")
            expect(card("gpt").locator(".setup-job-state")).to_have_text("流程进行中", timeout=5000)
            set_job("gpt", "completed")
            expect(card("gpt").locator(".setup-job-state")).to_have_text("流程结束", timeout=5000)
            expect(card("gpt").locator(".setup-job")).to_contain_text("流程结束不等于登录成功")
            expect(card("gpt").locator(".setup-auth")).to_have_text("夹具：登录状态未确认", timeout=5000)
            expect(card("gpt").locator(".setup-auth")).not_to_contain_text("已核验登录")
            fixture["auth"]["gpt"] = True
            page.get_by_role("button", name="重新检查安装与登录", exact=True).click()
            expect(card("gpt").locator(".setup-auth")).to_contain_text("已核验登录：夹具", timeout=5000)
            assert len(starts) == 1
            checks.append("explicit login action shows launching/running/completed; only separate verification establishes login")

            start_job("claude", "setup")
            set_job("claude", "failed")
            expect(card("claude").locator(".setup-job-state")).to_have_text("流程失败", timeout=5000)
            expect(card("claude").locator(".setup-install-state")).to_have_text("需要安装官方客户端")
            expect(card("claude").locator(".setup-auth")).to_contain_text("未检测到命令", timeout=5000)
            gemini_job = start_job("gemini", "setup")
            set_job("gemini", "running")
            expect(card("gemini").locator(".setup-job-state")).to_have_text("流程进行中", timeout=5000)
            page.get_by_role("button", name="取消 Gemini 安装登录流程", exact=True).click()
            expect(card("gemini").locator(".setup-job-state")).to_have_text("已取消")
            assert cancellations == [(gemini_job["id"], {})], cancellations
            expect(card("gemini").locator(".setup-auth")).to_contain_text("登录状态无法自动确认", timeout=5000)
            assert starts == [("gpt", {"action": "login"}, True), ("claude", {"action": "setup"}, True),
                              ("gemini", {"action": "setup"}, True)]
            checks.append("provider-specific setup failure and cancellation stay distinct from installed/authenticated state")

            page.get_by_role("button", name="Gemini 保存并测试网络", exact=True).click()
            expect(card("gemini").locator(".setup-network-result")).to_contain_text("网络检查通过")
            expect(card("gemini").locator(".setup-network-result")).to_contain_text("网络可达不代表已经登录")
            assert network_calls == [("gemini", {})]
            checks.append("network fixture requires a click and does not imply login or available model quota")

            verify_rendering()
            page.set_viewport_size({"width": 390, "height": 844})
            verify_rendering()
            card("gemini").scroll_into_view_if_needed()
            verify_rendering()
            checks.append("desktop and 390 px wizard layout have no overflow or raw node text")
            assert not errors, errors
            assert not blocked, blocked
            assert not sentinel.exists(), "The server attempted a forbidden native action"
            checks.append("no page/console errors, external requests, native installs, login processes or network probes")
            report = {"passed": True, "checks": checks, "errors": errors, "native_actions_executed": 0,
                      "job_states_are_browser_fixtures": True, "screenshots": ["setup-desktop.png", "setup-mobile.png"]}
            (ARTIFACTS / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
        except Exception:
            page.screenshot(path=str(ARTIFACTS / "failure.png"), full_page=True)
            (ARTIFACTS / "failure.html").write_text(page.content(), encoding="utf-8")
            print(json.dumps({"passed": False, "completed_checks": checks, "errors": errors,
                              "blocked_requests": blocked, "starts": starts}, ensure_ascii=False), file=sys.stderr)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
