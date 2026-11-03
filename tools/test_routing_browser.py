"""Browser acceptance of model routing using an isolated, offline demo server.

Run with ``python tools/test_routing_browser.py``. Requires Playwright and a
Chromium browser. No real member process is invoked and no personal data is used.
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


PROJECT = Path(__file__).resolve().parents[1]
ARTIFACTS = PROJECT / "work" / "routing-browser-qa"
NAMES = {"claude": "Claude", "gpt": "GPT", "gemini": "Gemini"}
TIERS = {"fast": "快速", "balanced": "均衡", "deep": "深入"}
PROFILES = {member: {tier: "qa-%s-%s" % (member, tier) for tier in TIERS}
            for member in NAMES}


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="data-", dir=ARTIFACTS) as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = "http://127.0.0.1:%s" % port
        log_path = ARTIFACTS / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            # Leave enough time for the browser to observe and cancel a reply.
            # This changes only the isolated demo adapter's delay, not production.
            bootstrap = ("from functools import partial; import run; "
                         "run.Registry = partial(run.Registry, demo_delay=3.0); run.main()")
            server = subprocess.Popen(
                [sys.executable, "-c", bootstrap, "--demo", "--no-browser", "--port", str(port),
                 "--data-dir", directory], cwd=PROJECT, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise AssertionError("Demo server exited; see %s" % log_path)
                    try:
                        with urllib.request.urlopen(base + "/api/info", timeout=1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise AssertionError("Demo server did not become ready")
                run_browser(base, Path(directory))
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def run_browser(base, data_dir):
    checks, errors, requests = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("chromium") or None,
            headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append("console: " + message.text)
                if message.type == "error" else None)
        page.on("request", lambda request: requests.append((request.method, request.url)))

        def api(method, path, data=None):
            response = page.request.fetch(base + path, method=method,
                                          headers={"X-AIGC": "1"}, data=data)
            assert response.ok, (response.status, response.text())
            return response.json()

        def close():
            page.locator('.modal-head button[aria-label="关闭"]').click()
            expect(page.locator(".modal")).to_have_count(0)

        def new_group(name):
            page.locator("#btn-new").click()
            page.get_by_label("群名", exact=True).fill(name)
            page.get_by_role("button", name="创建", exact=True).click()
            expect(page.locator(".modal")).to_have_count(0)
            expect(page.locator("#group-title")).to_have_text(name)
            gid = page.url.split("#", 1)[1]
            api("PATCH", "/api/groups/" + gid, {"auto_rounds": 0})
            return gid

        def saved_snapshot(gid):
            paths = [data_dir / "settings.json"]
            paths.extend(data_dir / "groups" / gid / name for name in
                         ("group.json", "board.json", "messages.jsonl"))
            return {str(path): path.read_bytes() if path.exists() else None for path in paths}

        def preview(text):
            page.get_by_label("模型预览问题", exact=True).fill(text)
            with page.expect_response(lambda response: response.url.endswith("/model-preview")) as got:
                page.get_by_role("button", name="预览已保存配置", exact=True).click()
            assert got.value.ok, got.value.text()
            expect(page.locator(".model-preview-card")).to_have_count(3)
            return got.value.json()

        def send(text, expected_count):
            before = api("GET", "/api/groups/" + current[0])["messages"]
            last_seq = max((message["seq"] for message in before), default=0)
            page.locator("#input").fill(text)
            with page.expect_response(lambda response: response.url.endswith("/messages")
                                      and response.request.method == "POST"):
                page.locator("#btn-send").click()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = api("GET", "/api/groups/" + current[0])
                new = [message for message in result["messages"] if message["seq"] > last_seq]
                if not result["state"]["running"]:
                    break
                page.wait_for_timeout(100)
            else:
                raise AssertionError("Demo round did not complete")
            chats = [message for message in new if message["kind"] == "chat"]
            assert len(chats) == expected_count, [(m["author"], m["text"]) for m in new]
            for message in chats:
                expect(page.locator('.msg.ai[data-seq="%s"]' % message["seq"])).to_be_visible(timeout=10000)
            expect(page.locator("#btn-stop")).to_be_hidden(timeout=10000)
            return chats

        def no_overflow():
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"), \
                "Page has horizontal overflow at 390 px"
            assert page.locator(".modal").evaluate(
                "node => node.getBoundingClientRect().left >= 0 && "
                "node.getBoundingClientRect().right <= window.innerWidth"), "Modal exceeds viewport"

        def no_null_text():
            assert page.locator(".modal-body").evaluate("""node => {
                const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const text = walker.currentNode.textContent.trim();
                    if (text === 'null' || text.includes('[object HTML')) return false;
                }
                return true;
            }"""), "An optional element or node array was rendered as raw text"

        try:
            page.goto(base)
            expect(page.locator("#btn-new")).to_be_visible()
            gid = new_group("模型验收 · 主群")
            current = [gid]

            # Empty Gemini profiles must stop the whole form before any write.
            before = saved_snapshot(gid)
            page.locator("#btn-settings").click()
            expect(page.get_by_label("Gemini 模型选择", exact=True)).to_be_visible()
            for member, name in NAMES.items():
                page.get_by_label(name + " 模型选择", exact=True).select_option("auto")
                if member != "gemini":
                    for tier, label in TIERS.items():
                        page.get_by_label(name + " · " + label, exact=True).fill(PROFILES[member][tier])
            start = len(requests)
            page.get_by_role("button", name="保存", exact=True).click()
            expect(page.locator(".modal-error")).to_contain_text("Gemini 自动模式需要填写")
            assert not any(method == "PUT" for method, _ in requests[start:])
            assert before == saved_snapshot(gid), "Invalid form mutated persisted settings"
            checks.append("empty Gemini auto profiles reject the complete save")

            for tier, label in TIERS.items():
                page.get_by_label("Gemini · " + label, exact=True).fill(PROFILES["gemini"][tier])
            page.get_by_role("button", name="保存", exact=True).click()
            expect(page.locator(".modal")).to_have_count(0)
            settings = api("GET", "/api/settings")
            for member in NAMES:
                assert settings["members"][member]["routing"] == {"mode": "auto", "profiles": PROFILES[member]}
            page.locator("#btn-settings").click()
            for member, name in NAMES.items():
                expect(page.get_by_label(name + " 模型选择", exact=True)).to_have_value("auto")
                for tier, label in TIERS.items():
                    expect(page.get_by_label(name + " · " + label, exact=True)).to_have_value(PROFILES[member][tier])
            close()
            checks.append("three provider auto profiles persist and reload")

            page.locator("#btn-models").click()
            no_null_text()
            for member, name in NAMES.items():
                expect(page.locator('[data-quota-member="%s"]' % member)).to_contain_text("演示模式，不查询真实账号额度")
                expect(page.get_by_role("button", name=name + " 刷新官方额度", exact=True)).to_be_disabled()
            page.get_by_label("Claude 模型策略", exact=True).select_option("manual")
            page.get_by_label("Claude 指定模型", exact=True).fill("qa-host-claude")
            page.get_by_role("button", name="保存本群设置", exact=True).click()
            expect(page.locator(".modal")).to_have_count(0)
            assert api("GET", "/api/groups/" + gid)["group"]["model_overrides"] == {
                "claude": {"mode": "manual", "model": "qa-host-claude"}}
            page.locator("#btn-models").click()
            expect(page.get_by_label("Claude 指定模型", exact=True)).to_have_value("qa-host-claude")
            page.get_by_label("Claude 指定模型", exact=True).fill("qa-unsaved-must-not-route")
            before = saved_snapshot(gid)
            start = len(requests)
            data = preview("请给出定理证明")
            assert data["estimate"] is True and not data["errors"], data
            assert data["routes"]["claude"]["requested_model"] == "qa-host-claude"
            assert data["routes"]["claude"]["source"] == "group_override"
            for member in ("gpt", "gemini"):
                assert data["routes"][member]["requested_model"] == PROFILES[member]["deep"]
            expect(page.locator(".model-preview-card.m-claude")).to_contain_text("qa-host-claude")
            expect(page.locator(".model-preview-card.m-claude")).not_to_contain_text("qa-unsaved")
            assert saved_snapshot(gid) == before, "Preview mutated stored data"
            assert all(method == "GET" or url.endswith("/model-preview")
                       for method, url in requests[start:]), requests[start:]
            assert not api("GET", "/api/groups/" + gid)["messages"]
            checks.extend(["saved group manual override takes priority", "preview ignores unsaved edits and has no stored side effects"])
            expect(page.locator(".toast")).to_have_count(0, timeout=10000)
            page.locator(".model-preview-result").scroll_into_view_if_needed()
            page.screenshot(path=str(ARTIFACTS / "model-routing-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            no_overflow()
            page.locator(".model-preview-result").scroll_into_view_if_needed()
            page.screenshot(path=str(ARTIFACTS / "model-routing-mobile.png"), full_page=True)
            close()
            page.set_viewport_size({"width": 1440, "height": 1000})

            chats = send("请给出定理证明", 3)
            assert {message["author"] for message in chats} == set(NAMES)
            for message in chats:
                route = message["model_route"]
                expected = "qa-host-claude" if message["author"] == "claude" else PROFILES[message["author"]]["deep"]
                assert route["requested_model"] == expected, route
                assert route["simulation"] is True and not route.get("reported_model"), route
                node = page.locator('.msg.ai[data-seq="%s"] .model-route' % message["seq"])
                expect(node).to_contain_text(expected)
                node.locator("summary").click()
                expect(node).to_contain_text("演示模拟回复，未调用真实模型")
                expect(node).to_contain_text("CLI 未报告")
            checks.append("outgoing demo replies display persisted route and honest simulation metadata")

            # Pauses are account-level, retain group membership, and do not call models on resume.
            page.locator("#btn-models").click()
            page.get_by_role("button", name="暂停 GPT 调用", exact=True).click()
            expect(page.get_by_role("button", name="恢复 GPT 调用", exact=True)).to_be_visible()
            expect(page.locator('[data-availability-member="gpt"]')).to_contain_text("手动暂停")
            expect(page.locator(".group-model-fields")).to_contain_text("所有群聊")
            close()
            expect(page.locator("#seats .m-gpt")).to_contain_text("暂停")
            expect(page.locator("#seats .m-gpt")).to_have_attribute("aria-pressed", "true")
            second_gid = new_group("模型验收 · 全局暂停")
            current[0] = second_gid
            assert api("GET", "/api/groups/" + second_gid)["state"]["model_availability"]["gpt"]["paused"]
            assert api("GET", "/api/groups/" + gid)["state"]["model_availability"]["gpt"]["paused"]
            chats = send("你好，请分别给一个建议", 2)
            assert {message["author"] for message in chats} == {"claude", "gemini"}
            group = api("GET", "/api/groups/" + second_gid)
            assert "gpt" in group["group"]["members"]
            page.locator("#btn-models").click()
            expect(page.get_by_role("button", name="恢复 GPT 调用", exact=True)).to_be_visible()
            count = len(group["messages"])
            page.get_by_role("button", name="恢复 GPT 调用", exact=True).click()
            expect(page.get_by_role("button", name="暂停 GPT 调用", exact=True)).to_be_visible()
            for check_gid in (gid, second_gid):
                state = api("GET", "/api/groups/" + check_gid)["state"]
                assert not state["model_availability"]["gpt"]["paused"] and not state["running"]
            assert len(api("GET", "/api/groups/" + second_gid)["messages"]) == count
            close()
            chats = send("@GPT 你好，请给一个建议", 1)
            assert chats[0]["author"] == "gpt"
            checks.append("global pause skips member across groups; resume waits for a new message")

            # Interrupt one actual demo response to exercise the failure/retry UI.
            page.locator("#input").fill("@GPT 请继续检查这个方案")
            page.locator("#btn-send").click()
            expect(page.locator("#typing .m-gpt")).to_be_visible(timeout=10000)
            page.locator("#btn-stop").click()
            retry = page.locator('[data-retry-member="gpt"]').last
            expect(retry).to_be_visible(timeout=10000)
            expect(page.locator(".incomplete-reply").last).to_contain_text("未完成")
            page.locator("#btn-models").click()
            page.get_by_role("button", name="暂停 GPT 调用", exact=True).click()
            expect(page.get_by_role("button", name="恢复 GPT 调用", exact=True)).to_be_visible()
            close()
            expect(retry).to_be_disabled()
            page.locator("#btn-models").click()
            page.get_by_role("button", name="恢复 GPT 调用", exact=True).click()
            expect(page.get_by_role("button", name="暂停 GPT 调用", exact=True)).to_be_visible()
            close()
            expect(retry).to_be_enabled()
            before_retry = api("GET", "/api/groups/" + second_gid)["messages"]
            with page.expect_response(lambda response: response.url.endswith("/retry/gpt")) as retried:
                retry.click()
            assert retried.value.ok, retried.value.text()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = api("GET", "/api/groups/" + second_gid)
                if not result["state"]["running"]:
                    break
                page.wait_for_timeout(100)
            else:
                raise AssertionError("Retry demo round did not complete")
            new_chats = [message for message in result["messages"][len(before_retry):] if message["kind"] == "chat"]
            assert len(new_chats) == 1 and new_chats[0]["author"] == "gpt", new_chats
            assert result["messages"][:len(before_retry)] == before_retry
            assert not result["state"]["incomplete_members"]
            expect(retry).to_be_hidden(timeout=10000)
            expect(page.locator(".incomplete-reply").last).to_contain_text("此成员当前没有待补答任务")
            checks.append("interrupted reply can retry only its member after explicit resume")

            page.locator("#btn-settings").click()
            expect(page.get_by_label("Gemini 模型选择", exact=True)).to_be_visible()
            no_null_text()
            page.set_viewport_size({"width": 390, "height": 844})
            no_overflow()
            close()
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
            checks.append("390 px group-model, provider-settings and chat layouts do not overflow")

            # Only the browser receives fake live quota responses; the backend
            # stays in demo mode. This validates presentation, not CLI access.
            page.set_viewport_size({"width": 1440, "height": 1000})

            def live_info(route):
                response = route.fetch()
                info = response.json()
                info["demo"] = False
                route.fulfill(response=response, json=info)

            def quota_fixture(route):
                member = route.request.url.split("/members/", 1)[1].split("/", 1)[0]
                quota = {"source": "Browser QA fixture", "checked_at": time.time() + 60, "windows": []}
                if member == "gpt":
                    quota.update(status="available", ordinary_usage_allowed=True, windows=[{
                        "label": "测试五小时窗口", "window_minutes": 300,
                        "used_percent": 25, "remaining_percent": 75, "resets_at": 2000000000}])
                elif member == "claude":
                    quota.update(status="unsupported", message="测试：官方接口不可读")
                else:
                    quota.update(status="error", message="测试：额度查询暂时失败")
                route.fulfill(json={"quota": quota})

            page.route("**/api/info", live_info)
            page.route("**/api/members/*/quota**", quota_fixture)
            page.reload()
            expect(page.locator("#group-title")).to_have_text("模型验收 · 全局暂停")
            page.locator("#btn-models").click()
            for member, name in NAMES.items():
                page.get_by_role("button", name=name + " 刷新官方额度", exact=True).click()
                expect(page.locator('[data-quota-member="%s"]' % member)).to_contain_text("Browser QA fixture")
            gpt_quota = page.locator('[data-quota-member="gpt"]')
            expect(gpt_quota).to_contain_text("已用 25%")
            expect(gpt_quota).to_contain_text("剩余 75%")
            expect(gpt_quota).to_contain_text("重置时间")
            expect(gpt_quota.locator("progress")).to_have_attribute("value", "25")
            expect(page.locator('[data-quota-member="claude"]')).to_contain_text("剩余额度未知")
            expect(page.locator('[data-quota-member="gemini"]')).to_contain_text("查询失败")
            no_null_text()
            page.set_viewport_size({"width": 390, "height": 844})
            no_overflow()
            checks.append("quota fixtures show percentages/source/reset and explicit unsupported/error states")
            assert not errors, errors
            checks.append("no browser console or page errors")
            report = {"passed": True, "checks": checks, "errors": errors,
                      "screenshots": ["model-routing-desktop.png", "model-routing-mobile.png"]}
            (ARTIFACTS / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
        except Exception:
            page.screenshot(path=str(ARTIFACTS / "failure.png"), full_page=True)
            (ARTIFACTS / "failure.html").write_text(page.content(), encoding="utf-8")
            print(json.dumps({"passed": False, "completed_checks": checks, "errors": errors}, ensure_ascii=False), file=sys.stderr)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
