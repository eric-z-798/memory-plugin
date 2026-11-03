"""三家命令行的调用方式：用 tests/fake_bin 里的假命令行检查参数、输出解析、超时和停止。"""

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aigc.members import CancelToken, Cancelled, CliMember, MemberError, Registry  # noqa: E402
from aigc.store import DEFAULT_SETTINGS  # noqa: E402

FAKE = ROOT / "tests" / "fake_bin"


@unittest.skipIf(os.name == "nt", "假命令行是 Python 脚本，Windows 上不能直接执行")
class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aigc-cli-"))
        self.log = self.tmp / "calls.jsonl"
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.env_backup = dict(os.environ)
        os.environ["FAKE_LOG"] = str(self.log)
        os.environ.pop("FAKE_MODE", None)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        for m, cmd in (("claude", "claude"), ("gpt", "codex"), ("gemini", "agy")):
            self.settings["members"][m]["command"] = str(FAKE / cmd)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def ask(self, member, prompt="你好", cancel=None):
        return CliMember(member, self.settings).ask(prompt, self.ws, cancel)

    # ----- Claude
    def test_claude_ok_and_membership_only(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-be-used"
        os.environ["CLAUDECODE"] = "1"
        reply = self.ask("claude", "测试提示词")
        self.assertIn("Claude 收到", reply)
        call = self.calls()[-1]
        self.assertFalse(call["api_key"], "ANTHROPIC_API_KEY 应该被去掉")
        self.assertFalse(call["claudecode"])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.ws))
        args = call["args"]
        self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")

    def test_claude_without_web_has_no_tools(self):
        self.settings["members"]["claude"]["web"] = False
        self.settings["members"]["claude"]["model"] = "sonnet"
        self.ask("claude")
        args = self.calls()[-1]["args"]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", args)
        self.assertEqual(args[args.index("--model") + 1], "sonnet")

    def test_api_key_kept_when_membership_only_off(self):
        self.settings["membership_only"] = False
        os.environ["ANTHROPIC_API_KEY"] = "sk-x"
        self.ask("claude")
        self.assertTrue(self.calls()[-1]["api_key"])

    def test_claude_usage_limit_is_explained(self):
        os.environ["FAKE_MODE"] = "error"
        with self.assertRaises(MemberError) as cm:
            self.ask("claude")
        self.assertIn("额度", str(cm.exception))

    def test_claude_garbage_output(self):
        os.environ["FAKE_MODE"] = "garbage"
        with self.assertRaises(MemberError) as cm:
            self.ask("claude")
        self.assertIn("something went wrong", str(cm.exception))

    # ----- GPT
    def test_gpt_reads_output_file_and_cleans_up(self):
        os.environ["OPENAI_API_KEY"] = "sk-x"
        reply = self.ask("gpt", "一二三")
        self.assertEqual(reply, "GPT 收到，提示词共 3 字。")
        self.assertFalse(self.calls()[-1]["api_key"])
        self.assertEqual(list(self.ws.iterdir()), [], "临时输出文件应该删掉")

    def test_gpt_web_and_model_flags(self):
        self.settings["members"]["gpt"].update(web=True, model="gpt-5", extra_args="-c model_reasoning_effort=low")
        self.ask("gpt")
        args = self.calls()[-1]["args"]
        self.assertIn("web_search=live", args)
        self.assertEqual(args[args.index("-m") + 1], "gpt-5")
        self.assertIn("model_reasoning_effort=low", args)
        self.assertEqual(args[-1], "-")

    def test_gpt_error(self):
        os.environ["FAKE_MODE"] = "error"
        with self.assertRaises(MemberError) as cm:
            self.ask("gpt")
        self.assertIn("usage limit", str(cm.exception))

    # ----- Gemini
    def test_gemini_ok_with_noise_line(self):
        os.environ["GEMINI_API_KEY"] = "x"
        os.environ["ANTIGRAVITY_API_KEY"] = "y"
        self.assertEqual(self.ask("gemini", "短提示词"), "Gemini 收到")
        call = self.calls()[-1]
        self.assertFalse(call["api_key"])
        self.assertNotIn("--add-dir", call["args"])

    def test_gemini_long_prompt_goes_through_stdin(self):
        prompt = "长" * 25000
        reply = self.ask("gemini", prompt)
        self.assertIn("标准输入 25000 字", reply)
        self.assertEqual(self.calls()[-1]["prompt_len"], 25000)
        self.assertNotIn(prompt, self.calls()[-1]["args"])
        self.assertFalse(Path(self.calls()[-1]["cwd"]).exists(), "临时工作目录应已清理")

    def test_gemini_partial_status_is_not_success(self):
        os.environ["FAKE_MODE"] = "partial"
        with self.assertRaises(MemberError) as cm:
            self.ask("gemini")
        self.assertIn("TIMEOUT", str(cm.exception))

    def test_failed_process_cannot_publish_partial_answer(self):
        os.environ["FAKE_MODE"] = "nonzero_with_reply"
        for member in ("claude", "gpt", "gemini"):
            with self.subTest(member=member), self.assertRaises(MemberError):
                self.ask(member)

    def test_gemini_multiple_result_events_are_rejected(self):
        os.environ["FAKE_MODE"] = "duplicate_result"
        with self.assertRaises(MemberError):
            self.ask("gemini")

    def test_stored_api_login_blocks_model_call(self):
        os.environ["FAKE_AUTH"] = "api_key"
        for member in ("claude", "gpt"):
            with self.subTest(member=member), self.assertRaises(MemberError):
                self.ask(member)
        self.assertTrue(all(c["args"] in (["auth", "status"], ["login", "status"]) for c in self.calls()))

    def test_unknown_or_logged_out_claude_auth_blocks_model_call(self):
        for method in ("logged_out", "third_party", "none", ""):
            os.environ["FAKE_AUTH"] = method
            with self.subTest(method=method), self.assertRaises(MemberError):
                self.ask("claude")

    def test_protocol_and_auth_overrides_in_extra_args_are_rejected(self):
        cases = {"claude": "--tools default", "gpt": '-c forced_login_method=api',
                 "gemini": "--dangerously-skip-permissions"}
        for member, extra in cases.items():
            self.settings["members"][member]["extra_args"] = extra
            with self.subTest(member=member), self.assertRaises(MemberError):
                self.ask(member)
        self.assertEqual(self.calls(), [], "非法参数应在任何 CLI 进程启动之前被拒绝")

    def test_gemini_garbage(self):
        os.environ["FAKE_MODE"] = "garbage"
        with self.assertRaises(MemberError):
            self.ask("gemini")

    # ----- 进程管理
    def test_timeout_kills_process(self):
        os.environ["FAKE_MODE"] = "sleep"
        os.environ["FAKE_PIDFILE"] = str(self.tmp / "pid")
        self.settings["members"]["claude"]["timeout"] = 1
        started = time.time()
        with self.assertRaises(MemberError) as cm:
            self.ask("claude")
        self.assertLess(time.time() - started, 8)
        self.assertIn("超过", str(cm.exception))
        self.assert_dead(int((self.tmp / "pid").read_text()))

    def test_cancel_kills_process(self):
        os.environ["FAKE_MODE"] = "sleep"
        os.environ["FAKE_PIDFILE"] = str(self.tmp / "pid")
        token = CancelToken()
        result = {}

        def run():
            try:
                self.ask("claude", cancel=token)
            except Cancelled:
                result["cancelled"] = True

        t = threading.Thread(target=run)
        t.start()
        for _ in range(100):
            if (self.tmp / "pid").exists():
                break
            time.sleep(0.05)
        token.cancel()
        t.join(timeout=8)
        self.assertFalse(t.is_alive())
        self.assertTrue(result.get("cancelled"))
        self.assert_dead(int((self.tmp / "pid").read_text()))

    def assert_dead(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("进程 %d 没有被结束" % pid)

    # ----- 检测
    def test_detect(self):
        found = Registry().detect(self.settings, refresh=True)
        self.assertTrue(found["claude"]["installed"])
        self.assertTrue(found["claude"]["auth_ok"])
        self.assertIn("me@example.com", found["claude"]["auth"])
        self.assertEqual(found["gpt"]["auth_ok"], True)
        self.assertIn("ChatGPT", found["gpt"]["auth"])
        self.assertIsNone(found["gemini"]["auth_ok"])
        self.assertEqual(found["gemini"]["version"], "1.0.16")

    def test_detect_api_key_login_is_flagged(self):
        os.environ["FAKE_AUTH"] = "api_key"
        found = Registry().detect(self.settings, refresh=True)
        self.assertFalse(found["claude"]["auth_ok"])
        self.assertIn("API Key", found["claude"]["auth"])

    def test_missing_command(self):
        self.settings["members"]["gpt"]["command"] = str(self.tmp / "nope")
        with self.assertRaises(MemberError) as cm:
            self.ask("gpt")
        self.assertIn("chatgpt.com/codex/install", str(cm.exception))
        found = Registry().detect(self.settings, refresh=True)
        self.assertFalse(found["gpt"]["installed"])

    def test_ping_with_demo(self):
        r = Registry(demo=True, demo_delay=0).ping("gemini", self.settings, self.ws)
        self.assertEqual(r["reply"], "收到")


class WindowsPathTest(unittest.TestCase):
    """在任何系统上模拟 Windows，检查"刚装好的命令行也能找到"的逻辑。"""

    def fake_winreg(self, user_path, system_path):
        import types
        fake = types.ModuleType("winreg")
        fake.HKEY_CURRENT_USER, fake.HKEY_LOCAL_MACHINE = 1, 2

        class Key:
            def __init__(self, root):
                self.root = root

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        fake.OpenKey = lambda root, sub: Key(root)
        fake.QueryValueEx = lambda key, name: ({1: user_path, 2: system_path}[key.root], 2)
        return fake

    def test_registry_path_is_read_and_expanded(self):
        import ntpath
        from unittest import mock
        from aigc import members
        fake = self.fake_winreg(r"C:\Users\me\AppData\Local\Antigravity;%AIGC_T%\bin;", r"C:\Windows\system32")
        with mock.patch.dict(os.environ, {"AIGC_T": r"C:\X"}), \
                mock.patch.dict(sys.modules, {"winreg": fake}), \
                mock.patch.object(members, "IS_WINDOWS", True), \
                mock.patch.object(members.os.path, "expandvars", ntpath.expandvars):
            dirs = members._registry_path_dirs()
            env, _ = members.member_env("gpt", {"membership_only": True})
        self.assertEqual(dirs, [r"C:\Users\me\AppData\Local\Antigravity", r"C:\X\bin", r"C:\Windows\system32"])
        self.assertIn(r"C:\X\bin", env["PATH"])

    def test_cli_agy_is_preferred_on_windows(self):
        from unittest import mock
        from aigc import members
        tmp = Path(tempfile.mkdtemp(prefix="aigc-win-"))
        try:
            (tmp / "Antigravity").mkdir()
            (tmp / "Antigravity" / "agy.exe").write_bytes(b"")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp)}), \
                    mock.patch.object(members, "IS_WINDOWS", True), \
                    mock.patch.object(members, "_registry_path_dirs", lambda: []):
                found = members.find_command("gemini", {"command": ""})
            self.assertEqual(found, str(tmp / "Antigravity" / "agy.exe"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_not_windows_reads_nothing(self):
        from aigc import members
        if os.name != "nt":
            self.assertEqual(members._registry_path_dirs(), [])


class AccountRouteTest(unittest.TestCase):
    def test_agy_saved_api_route_is_rejected_without_reading_credentials(self):
        from unittest import mock
        from aigc.members import check_agy_account_settings
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / ".gemini" / "antigravity-cli"
            folder.mkdir(parents=True)
            for route in ({"modelProvider": "gemini"}, {"gcp": {"projectId": "billing-project"}}):
                (folder / "settings.json").write_text(json.dumps(route), encoding="utf-8")
                with mock.patch("aigc.members.Path.home", return_value=root), self.assertRaises(MemberError):
                    check_agy_account_settings()
            (folder / "settings.json").write_text('{"model": "default"}', encoding="utf-8")
            with mock.patch("aigc.members.Path.home", return_value=root):
                check_agy_account_settings()

    def test_third_party_env_routes_removed(self):
        from unittest import mock
        from aigc.members import member_env
        routes = {"ANTHROPIC_AUTH_TOKEN": "test", "CLAUDE_CODE_USE_VERTEX": "1", "AGY_ADC_AUTH": "true"}
        with mock.patch.dict(os.environ, routes):
            claude, _ = member_env("claude", {"membership_only": True})
            agy, _ = member_env("gemini", {"membership_only": True})
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", claude)
        self.assertNotIn("CLAUDE_CODE_USE_VERTEX", claude)
        self.assertNotIn("AGY_ADC_AUTH", agy)


class DemoMemoryTest(unittest.TestCase):
    def test_demo_quotes_only_supplied_confirmed_memory(self):
        from aigc.members import DemoMember
        record = {"id": "mem-test", "version": 2, "status": "confirmed", "content": "预算为3万"}
        prompt = "## 共享长期记忆（本轮快照）\n%s\n## 共享看板\n" % json.dumps(record, ensure_ascii=False)
        reply = DemoMember("gpt", 0)._reply(prompt)
        self.assertIn("预算为3万", reply)
        self.assertIn("版本 2", reply)
        record["status"] = "pending"
        reply = DemoMember("gpt", 0)._reply("## 共享长期记忆（本轮快照）\n%s\n## 共享看板\n" % json.dumps(record))
        self.assertNotIn("预算为3万", reply)


class ModelMetadataTest(unittest.TestCase):
    """Fixture-level CLI envelopes distinguish configured models from usage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aigc-model-report-")
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings["membership_only"] = False

    def adapter(self, member):
        return CliMember(member, self.settings)

    def ask_with_output(self, adapter, output, code=0):
        from unittest import mock
        with mock.patch.object(adapter, "_command", return_value="fixture-cli"), \
                mock.patch("aigc.members.run_process", return_value=(code, output, "")):
            return adapter.ask("fixture prompt", self.cwd)

    def claude_output(self, usage=None):
        data = {"result": "fixture reply", "is_error": False, "subtype": "success"}
        if usage is not None:
            data["modelUsage"] = usage
        return json.dumps(data)

    def agy_output(self, model=None, status="SUCCESS"):
        init = {"event": "init", "init": {"model": model}}
        result = {"event": "result", "result": {"status": status, "response": "fixture reply"}}
        return json.dumps(init) + "\n" + json.dumps(result)

    def test_claude_reports_multiple_usage_models_without_selecting_final_model(self):
        adapter = self.adapter("claude")
        self.ask_with_output(adapter, self.claude_output({"claude-opus-4-6": {}, "claude-haiku-4-5": {}}))
        self.assertEqual(adapter.last_metadata, {
            "reported_models": ["claude-opus-4-6", "claude-haiku-4-5"], "report_kind": "usage"})
        self.assertNotIn("reported_model", adapter.last_metadata)

    def test_claude_single_usage_model_and_missing_report(self):
        adapter = self.adapter("claude")
        self.ask_with_output(adapter, self.claude_output({"claude-opus-4-6[1m]": {}}))
        self.assertEqual(adapter.last_metadata, {"reported_model": "claude-opus-4-6[1m]", "report_kind": "usage"})
        self.ask_with_output(adapter, self.claude_output())
        self.assertEqual(adapter.last_metadata, {}, "前一次的报告不能泄漏到下一次调用")

    def test_agy_init_model_is_configuration_only(self):
        adapter = self.adapter("gemini")
        self.ask_with_output(adapter, self.agy_output("gemini-3.8-flash-medium"))
        self.assertEqual(adapter.last_metadata, {
            "reported_model": "gemini-3.8-flash-medium", "report_kind": "configuration"})

    def test_agy_no_init_model_does_not_invent_report(self):
        self.settings["members"]["gemini"]["model"] = "requested-model"
        adapter = self.adapter("gemini")
        self.ask_with_output(adapter, self.agy_output())
        self.assertEqual(adapter.last_metadata, {})

    def test_codex_configured_model_is_not_an_observed_report(self):
        self.settings["members"]["gpt"]["model"] = "requested-model"
        adapter = self.adapter("gpt")
        self.ask_with_output(adapter, "fixture reply")
        self.assertEqual(adapter.last_metadata, {})

    def test_bad_model_metadata_is_ignored(self):
        for value in ({"name": "injected"}, ["a"], 42, "", "bad\nmodel", "<script>", "x" * 201):
            adapter = self.adapter("gemini")
            with self.subTest(value=value):
                self.ask_with_output(adapter, self.agy_output(value))
                self.assertEqual(adapter.last_metadata, {})
        adapter = self.adapter("claude")
        self.ask_with_output(adapter, self.claude_output({"bad\nmodel": {}, "<script>": {}}))
        self.assertEqual(adapter.last_metadata, {})

    def test_failure_clears_previous_report(self):
        adapter = self.adapter("claude")
        self.ask_with_output(adapter, self.claude_output({"claude-sonnet": {}}))
        with self.assertRaises(MemberError):
            self.ask_with_output(adapter, self.claude_output({"claude-opus": {}}), code=1)
        self.assertEqual(adapter.last_metadata, {})
        adapter = self.adapter("gemini")
        with self.assertRaises(MemberError):
            self.ask_with_output(adapter, self.agy_output("configured-model", status="TIMEOUT"))
        self.assertEqual(adapter.last_metadata, {})

    def test_demo_identifies_simulation_without_fabricated_model(self):
        from aigc.members import DemoMember
        adapter = DemoMember("claude", delay=0)
        self.assertEqual(adapter.last_metadata, {"simulation": True})
        adapter.last_metadata["reported_model"] = "stale"
        adapter.ask("这是连通测试", self.cwd)
        self.assertEqual(adapter.last_metadata, {"simulation": True})


class FailureClassificationTest(unittest.TestCase):
    def test_explicit_quota_failures(self):
        from aigc.members import classify_cli_failure
        messages = [
            "quota exhausted", "Quota exceeded for this account", "quota has been exhausted",
            "You exceeded your current quota, please check your plan", "insufficient_quota",
            "Claude AI usage limit reached|1760000000", "usage limit exceeded",
            "You've hit your limit. Try again later", "You've hit your usage limit",
            "You’ve hit your weekly limit", "no remainingquota", "no remaining quota",
            "no quota remaining", "额度已用尽", "额度用完", "已经超过额度",
            'Command failed (exit 1): {"error":{"code":"insufficient_quota"}}',
            "HTTP 429: quota exceeded",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(classify_cli_failure(message), "quota")

    def test_explicit_rate_limits(self):
        from aigc.members import classify_cli_failure
        messages = [
            "rate_limit_error", "rate limit exceeded", "Rate limit reached",
            "RATE_LIMIT_EXCEEDED", "too many requests", "HTTP 429", "HTTP/1.1 429",
            "HTTP error 429", "status429", "status code: 429", 'status_code=429',
            '{"status":429,"message":"retry later"}', "Command failed: HTTP 429",
            "请求过于频繁", "请求被限流",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(classify_cli_failure(message), "rate_limit")

    def test_non_limit_failures_and_monthly_notices_are_not_classified(self):
        from aigc.members import classify_cli_failure, explain
        messages = [
            "quota", "limit", "rate limit", "usage limit", "quota settings not found",
            "Monthly quota notice: 300 requests remaining", "Your monthly quota resets tomorrow",
            "Configuration error: rate limit must be a positive integer", "model-429 is unknown",
            "port 429 unavailable", "请求超时", "超过 30 秒还没完成，已停止。",
            "Authentication required", "401 Unauthorized", "login expired", "API Key not configured",
            "File size limit reached", "Context window limit reached", "每月额度为1000次",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(classify_cli_failure(message), "")
                self.assertEqual(classify_cli_failure(explain(message, "gpt")), "",
                                 "错误翻译不能制造额度耗尽前缀")
        self.assertEqual(classify_cli_failure(None), "")
        self.assertEqual(classify_cli_failure({"quota": "exhausted"}), "")

    def test_quota_exception_preserves_reason_kind(self):
        from aigc.members import classify_cli_failure, QuotaUnavailable
        error = QuotaUnavailable("provider throttled the request", kind="rate_limit")
        self.assertIsInstance(error, MemberError)
        self.assertEqual(classify_cli_failure(error), "rate_limit")
        self.assertEqual(classify_cli_failure(MemberError("insufficient_quota")), "quota")

    def test_only_failed_cli_envelopes_raise_quota_exception(self):
        from unittest import mock
        from aigc.members import QuotaUnavailable, classify_cli_failure
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["membership_only"] = False
        answer = "Example: quota exhausted; HTTP 429 means throttling."
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            for member in ("claude", "gpt", "gemini"):
                adapter = CliMember(member, settings)
                if member == "claude":
                    output = json.dumps({"result": answer, "is_error": False, "subtype": "success"})
                elif member == "gemini":
                    output = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": answer}})
                else:
                    output = answer
                with self.subTest(member=member), \
                        mock.patch.object(adapter, "_command", return_value="fixture-cli"), \
                        mock.patch("aigc.members.run_process", return_value=(0, output, "")):
                    self.assertEqual(adapter.ask("question", cwd), answer)
                with self.subTest(member=member), \
                        mock.patch.object(adapter, "_command", return_value="fixture-cli"), \
                        mock.patch("aigc.members.run_process", return_value=(1, "", "HTTP 429: quota exceeded")):
                    with self.assertRaises(QuotaUnavailable) as caught:
                        adapter.ask("question", cwd)
                    self.assertEqual(classify_cli_failure(caught.exception), "quota")
                with self.subTest(member=member), \
                        mock.patch.object(adapter, "_command", return_value="fixture-cli"), \
                        mock.patch("aigc.members.run_process", side_effect=MemberError("超过 30 秒还没完成，已停止")):
                    with self.assertRaises(MemberError) as caught:
                        adapter.ask("question", cwd)
                    self.assertNotIsInstance(caught.exception, QuotaUnavailable)
                    self.assertEqual(classify_cli_failure(caught.exception), "")


if __name__ == "__main__":
    unittest.main()
