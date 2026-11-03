"""Provider pauses across real engine rounds and HTTP handlers (no real CLI)."""

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aigc.engine import Engine
from aigc.members import MemberError, QuotaUnavailable
from aigc.quota import QuotaMonitor
from aigc.store import Store
from test_engine import ScriptedRegistry, status
import test_server as server_tests


class QuotaEngineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aigc-quota-integration-")
        self.store = Store(Path(self.tmp))
        self.reg = ScriptedRegistry()
        self.engine = Engine(self.store, self.reg)
        self.gid = self.store.create_group("额度测试")["id"]
        self.engine.update_group(self.gid, {"auto_rounds": 0})

    def tearDown(self):
        self.engine.shutdown()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(r.snapshot()["running"] for r in self.engine._runners.values()):
            time.sleep(0.01)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, gid=None):
        gid = gid or self.gid
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if not self.engine.state(gid)["running"]:
                return
            time.sleep(0.01)
        self.fail("Paused-provider round did not finish")

    def send(self, text, gid=None):
        self.engine.post_user_message(gid or self.gid, text)
        self.wait(gid)

    def fail_claude(self, message="quota exhausted"):
        def fail(prompt):
            raise MemberError(message)
        self.reg.behaviors["claude"] = fail

    def test_quota_failure_pauses_provider_globally_and_other_members_continue(self):
        self.fail_claude()
        self.send("大家回答")
        self.assertEqual([m for m, p in self.reg.prompts], ["claude", "gpt", "gemini"])
        self.assertTrue(self.engine.availability.snapshot()["claude"]["paused"])
        self.assertEqual(self.engine.availability.snapshot()["claude"]["reason"], "quota")
        self.assertEqual([m["author"] for m in self.store.messages(self.gid) if m["kind"] == "chat"], ["gpt", "gemini"])
        other = self.store.create_group("另一项目")["id"]
        self.engine.update_group(other, {"auto_rounds": 0})
        start = len(self.reg.prompts)
        self.send("这个群也继续", other)
        self.assertEqual([m for m, p in self.reg.prompts[start:]], ["gpt", "gemini"])
        self.assertTrue(self.engine.state(other)["model_availability"]["claude"]["paused"])

    def test_paused_votes_do_not_loop_or_falsely_make_consensus(self):
        self.engine.update_group(self.gid, {"auto_rounds": 5})
        self.fail_claude()
        self.reg.behaviors["gpt"] = lambda p: "提出方案。" + status(
            propose_decisions=["先做一个原型"],
            propose_tasks=[{"title": "制作原型", "owner": "gpt", "deliverable": "草稿"}])
        self.reg.behaviors["gemini"] = lambda p: "同意方案与分工。" + status(votes=[
            {"id": "D-001", "vote": "agree"}, {"id": "T-001", "vote": "agree"}])
        self.send("讨论方案与分工")
        self.assertEqual(len(self.reg.prompts), 3)
        board = self.store.load_board(self.gid)
        self.assertEqual(board["decisions"][0]["status"], "discussing")
        self.assertEqual(board["tasks"][0]["status"], "proposed")
        self.assertNotIn("claude", board["decisions"][0]["votes"])
        self.assertEqual(self.store.load_group(self.gid)["members"], ["claude", "gpt", "gemini"])
        self.assertFalse(any("自动续轮" in m["text"] for m in self.store.messages(self.gid) if m["kind"] == "system"))

    def test_model_changes_do_not_bypass_a_paused_account(self):
        self.fail_claude()
        self.send("@Claude 回答")
        self.store.save_settings({"members": {"claude": {"model": "another-model"}}})
        self.engine.update_group(self.gid, {"model_overrides": {"claude": {"mode": "manual", "model": "host-selected-model"}}})
        self.send("@Claude 再回答")
        self.assertEqual(len(self.reg.prompts_of("claude")), 1)
        self.assertTrue(self.engine.availability.snapshot()["claude"]["paused"])

    def test_pause_survives_engine_restart_and_resume_does_not_call_any_model(self):
        self.fail_claude()
        self.send("@Claude 回答")
        self.engine.shutdown()
        self.store = Store(Path(self.tmp))
        self.reg = ScriptedRegistry()
        self.engine = Engine(self.store, self.reg)
        self.assertTrue(self.engine.availability.snapshot()["claude"]["paused"])
        self.send("@Claude 仍应暂停")
        self.assertEqual(self.reg.prompts, [])
        self.engine.availability.resume("claude")
        self.assertEqual(self.reg.prompts, [])
        self.send("@Claude 现在回答")
        self.assertEqual(len(self.reg.prompts_of("claude")), 1)
        self.assertFalse(self.engine.availability.snapshot()["claude"]["paused"])

    def test_midround_resume_only_makes_provider_eligible_next_round(self):
        self.engine.availability.pause("claude", "manual", "暂时暂停")
        changed = []

        def resume_during_gpt(prompt):
            if not changed:
                changed.append(True)
                self.engine.availability.resume("claude")
            return "完成。" + status()

        self.reg.behaviors["gpt"] = resume_during_gpt
        # Put GPT first so the resumed Claude would be called later without a round snapshot.
        group = self.store.load_group(self.gid)
        group["round_counter"] = 1
        self.store.save_group(group)
        self.send("这一轮继续")
        self.assertEqual([m for m, p in self.reg.prompts], ["gpt", "gemini"])
        self.send("下一轮继续")
        self.assertEqual(len(self.reg.prompts_of("claude")), 1)

    def test_auth_timeout_and_model_errors_do_not_pause_provider(self):
        for message in ("not logged in; please authenticate", "request timed out after 60 seconds", "model not found"):
            with self.subTest(message=message):
                self.fail_claude(message)
                before = len(self.reg.prompts_of("claude"))
                self.send("@Claude 回答")
                self.assertEqual(len(self.reg.prompts_of("claude")), before + 1)
                self.assertFalse(self.engine.availability.snapshot()["claude"]["paused"])

    def test_typed_rate_limit_error_retains_reason_without_keyword_matching(self):
        def fail(prompt):
            raise QuotaUnavailable("provider denied this operation", kind="rate_limit")
        self.reg.behaviors["claude"] = fail
        self.send("@Claude 回答")
        saved = self.engine.availability.snapshot()["claude"]
        self.assertTrue(saved["paused"])
        self.assertEqual(saved["reason"], "rate_limit")

    def test_successful_reply_about_quota_does_not_pause(self):
        self.reg.behaviors["claude"] = lambda p: "HTTP 429 和 quota exhausted 是两种错误提示。" + status()
        self.send("@Claude 解释额度相关提示")
        self.assertFalse(self.engine.availability.snapshot()["claude"]["paused"])

    def test_summary_quota_failure_pauses_one_provider_and_uses_available_secretary(self):
        self.store.save_settings({"recent_window": 4, "summary_batch": 2})
        self.engine.update_group(self.gid, {"secretary": "claude"})
        for i in range(6):
            self.store.append_message(self.gid, {"kind": "user", "author": "user", "text": "历史问题 %d" % i})

        def reply(prompt):
            if "书记员" in prompt:
                raise MemberError("rate limit exceeded")
            return "普通回复。" + status()

        self.reg.behaviors["claude"] = reply
        self.send("@GPT 整理一下")
        self.assertTrue(self.engine.availability.snapshot()["claude"]["paused"])
        self.assertEqual(self.engine.availability.snapshot()["claude"]["reason"], "rate_limit")
        self.assertEqual(self.store.load_board(self.gid)["summary_by"], "gpt")
        self.assertEqual(len(self.reg.prompts_of("claude")), 1)
        self.send("继续讨论")
        self.assertEqual(len(self.reg.prompts_of("claude")), 1)

    def test_parallel_stop_preserves_completed_peer_and_discards_unfinished_outputs(self):
        self.engine.update_group(self.gid, {"mode": "parallel", "parallel_review": False})
        self.reg.delays.update({"gpt": 2, "gemini": 2})
        self.reg.behaviors["claude"] = lambda p: "Claude 已完整交付。" + status()
        self.reg.behaviors["gpt"] = lambda p: "尚未完整生成的回复。" + status(
            propose_decisions=["未完成回复的决议"],
            memories=[{"content": "未完成回复的记忆", "kind": "fact"}])
        self.engine.post_user_message(self.gid, "分别回答")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            chats = [m for m in self.store.messages(self.gid) if m["kind"] == "chat"]
            if chats:
                break
            time.sleep(0.01)
        self.assertEqual([m["author"] for m in chats], ["claude"])
        completed = chats[0]
        self.assertTrue(self.engine.stop(self.gid))
        self.wait()
        self.assertEqual([m for m in self.store.messages(self.gid) if m["kind"] == "chat"], [completed])
        self.assertEqual(self.store.load_board(self.gid)["decisions"], [])
        self.assertEqual(self.store.memory.list_memories(self.store.load_group(self.gid)["project_id"]), [])
        self.assertFalse(any(v["paused"] for v in self.engine.availability.snapshot().values()))

    def test_unknown_error_and_full_percentage_observations_do_not_block_calls(self):
        self.engine.quota = QuotaMonitor(demo=False, ttl=0)
        observations = [
            {"status": "unsupported", "source": "stub", "message": "无只读接口", "windows": []},
            {"status": "error", "source": "stub", "message": "只读查询超时", "windows": []},
            {"status": "available", "source": "stub", "message": "历史快照", "windows": [
                {"used_percent": 100, "remaining_percent": 0, "window": "primary"}]},
        ]
        for observation in observations:
            with self.subTest(status=observation["status"]), patch("aigc.quota.query_quota", return_value=observation) as query:
                before = len(self.reg.prompts_of("gpt"))
                self.send("@GPT 正常回答")
                self.assertEqual(len(self.reg.prompts_of("gpt")), before + 1)
                self.assertEqual(query.call_count, 1)
                self.assertFalse(self.engine.availability.snapshot()["gpt"]["paused"])
                reply = [m for m in self.store.messages(self.gid) if m["kind"] == "chat"][-1]
                self.assertEqual(reply["model_route"]["quota_snapshot"]["status"], observation["status"])
                self.assertEqual(reply["model_route"]["quota_snapshot"]["windows"], observation["windows"])

    def test_concurrent_retry_requests_generate_only_one_replacement_reply(self):
        self.fail_claude("request timed out")
        self.send("@Claude 回答")
        self.assertIn("claude", self.engine.incomplete_members(self.gid))
        self.reg.prompts.clear()
        release = threading.Event()
        start = threading.Barrier(3)
        outcomes = []

        def reply(prompt):
            if not release.wait(3):
                raise RuntimeError("Retry reply was not released")
            return "补答完成。" + status()

        def retry():
            try:
                start.wait(timeout=3)
                self.engine.retry_member(self.gid, "claude")
                outcomes.append("accepted")
            except ValueError:
                outcomes.append("rejected")
            except Exception as exc:
                outcomes.append(repr(exc))

        original_state = self.engine.state

        def delayed_state(gid):
            result = original_state(gid)
            # Model two overlapping requests after an idle observation. An
            # atomic admission lock must also protect this check-to-submit gap.
            time.sleep(0.05)
            return result

        self.reg.behaviors["claude"] = reply
        threads = [threading.Thread(target=retry) for _ in range(2)]
        try:
            with patch.object(self.engine, "state", side_effect=delayed_state):
                for thread in threads:
                    thread.start()
                start.wait(timeout=3)
                for thread in threads:
                    thread.join(timeout=3)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertCountEqual(outcomes, ["accepted", "rejected"])
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=3)
        self.wait()
        self.assertEqual([member for member, prompt in self.reg.prompts], ["claude"])
        self.assertEqual(len([m for m in self.store.messages(self.gid) if m["kind"] == "chat"]), 1)
        self.assertNotIn("claude", self.engine.incomplete_members(self.gid))

    def test_cancel_while_reply_waits_for_group_lock_remains_retryable(self):
        ready, release = threading.Event(), threading.Event()

        def reply(prompt):
            ready.set()
            if not release.wait(3):
                raise RuntimeError("Reply was not released")
            return "取消的回复。" + status(
                propose_decisions=["不应保存的决议"],
                memories=[{"content": "不应保存的记忆", "kind": "note"}])

        self.reg.behaviors["claude"] = reply
        self.engine.post_user_message(self.gid, "@Claude 回答后准备保存")
        try:
            self.assertTrue(ready.wait(3))
            with self.store.lock(self.gid):
                release.set()
                # The reply can finish but cannot acquire the commit lock.
                time.sleep(0.05)
                self.assertTrue(self.engine.stop(self.gid))
        finally:
            release.set()
        self.wait()
        self.assertFalse(any(m["kind"] == "chat" for m in self.store.messages(self.gid)))
        self.assertEqual(self.store.load_board(self.gid)["decisions"], [])
        project_id = self.store.load_group(self.gid)["project_id"]
        self.assertEqual(self.store.memory.list_memories(project_id), [])
        self.assertIn("claude", self.engine.incomplete_members(self.gid))

    def test_member_error_after_cancellation_remains_retryable_without_pausing(self):
        ready, release = threading.Event(), threading.Event()

        def fail_after_stop(prompt):
            ready.set()
            if not release.wait(3):
                raise RuntimeError("Failed reply was not released")
            raise MemberError("quota exhausted while process was being stopped")

        self.reg.behaviors["claude"] = fail_after_stop
        self.engine.post_user_message(self.gid, "@Claude 等待取消")
        try:
            self.assertTrue(ready.wait(3))
            self.assertTrue(self.engine.stop(self.gid))
        finally:
            release.set()
        self.wait()
        self.assertFalse(any(m["kind"] == "chat" for m in self.store.messages(self.gid)))
        self.assertIn("claude", self.engine.incomplete_members(self.gid))
        self.assertFalse(self.engine.availability.snapshot()["claude"]["paused"])

    def test_cancellation_or_pause_during_readonly_query_prevents_model_call(self):
        self.engine.quota = QuotaMonitor(demo=False, ttl=0)
        for action in ("cancel", "pause"):
            with self.subTest(action=action):
                entered, release = threading.Event(), threading.Event()

                def read_quota(member, settings):
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("Read-only query was not released")
                    return {"status": "available", "source": "stub", "windows": [], "message": "只读结果"}

                with patch("aigc.quota.query_quota", side_effect=read_quota):
                    self.engine.post_user_message(self.gid, "@GPT 等待额度快照后回答")
                    try:
                        self.assertTrue(entered.wait(3))
                        if action == "cancel":
                            self.assertTrue(self.engine.stop(self.gid))
                        else:
                            self.engine.availability.pause("gpt", "manual", "查询期间暂停")
                    finally:
                        release.set()
                    self.wait()
                self.assertEqual(self.reg.prompts, [])
                self.assertFalse(any(m["kind"] == "chat" for m in self.store.messages(self.gid)))
                self.assertTrue(any(m.get("incomplete") and m.get("retry_member") == "gpt"
                                    for m in self.store.messages(self.gid)))

    def test_summary_continues_when_readonly_quota_is_unknown(self):
        self.engine.quota = QuotaMonitor(demo=False, ttl=0)
        self.store.save_settings({"recent_window": 4, "summary_batch": 2})
        self.engine.update_group(self.gid, {"secretary": "claude"})
        for i in range(6):
            self.store.append_message(self.gid, {"kind": "user", "author": "user", "text": "历史问题 %d" % i})
        observation = {"status": "error", "source": "stub", "windows": [], "message": "查询不可用"}
        with patch("aigc.quota.query_quota", return_value=observation) as query:
            self.send("@GPT 正常回答")
            self.assertEqual([call.args[0] for call in query.call_args_list], ["gpt", "claude"])
        self.assertEqual(self.store.load_board(self.gid)["summary_by"], "claude")
        self.assertEqual(self.store.load_board(self.gid)["summary_model_route"]["quota_snapshot"]["status"], "error")
        self.assertFalse(any(v["paused"] for v in self.engine.availability.snapshot().values()))


class QuotaHttpIntegrationTest(server_tests.Base):
    def setUp(self):
        super().setUp()
        self.gid = self.store.create_group("额度接口")["id"]
        self.ping_calls = []
        self.server.app.registry.ping = self.ping

    def ping(self, member, settings, cwd):
        self.ping_calls.append(member)
        return {"ok": True, "reply": "收到"}

    def wait(self):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if not self.engine.state(self.gid)["running"]:
                return
            time.sleep(0.01)
        self.fail("Retry round did not complete")

    def test_resume_then_retry_only_failed_member_using_latest_shared_context(self):
        reg = ScriptedRegistry()
        self.engine.registry = reg
        self.engine.update_group(self.gid, {"auto_rounds": 0})

        def fail(prompt):
            raise QuotaUnavailable("quota exhausted after partial response")

        reg.behaviors["claude"] = fail
        reg.behaviors["gpt"] = lambda p: "GPT 的完整成功回复。" + status(propose_decisions=["正常成员的方案"])
        reg.behaviors["gemini"] = lambda p: "Gemini 的完整成功回复。" + status(votes=[{"id": "D-001", "vote": "agree"}])
        self.engine.post_user_message(self.gid, "请三家评审方案")
        self.wait()
        before = self.store.messages(self.gid)
        successful = [m for m in before if m["kind"] == "chat"]
        self.assertEqual([m["author"] for m in successful], ["gpt", "gemini"])
        interrupted = [m for m in before if m.get("incomplete")]
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]["kind"], "system")
        self.assertEqual(interrupted[0]["retry_member"], "claude")
        self.assertEqual(interrupted[0]["task"], "reply")
        board = self.store.load_board(self.gid)
        self.assertEqual(len(board["decisions"]), 1)
        self.assertEqual(board["decisions"][0]["proposer"], "gpt")
        self.assertNotIn("claude", board["decisions"][0]["votes"])
        self.assertEqual(self.store.memory.list_memories(self.store.load_group(self.gid)["project_id"]), [])

        url = "/api/groups/%s/retry/claude" % self.gid
        code, result, _ = self.request("POST", url, {})
        self.assertEqual(code, 400, result)
        self.assertEqual(len(reg.prompts), 3)
        code, result, _ = self.request("POST", "/api/members/claude/resume", {})
        self.assertEqual(code, 200, result)
        self.assertEqual(len(reg.prompts), 3)

        reg.behaviors["claude"] = lambda p: "Claude 恢复后的完整回复。" + status()
        self.engine.update_group(self.gid, {"auto_rounds": 5})
        self.store.save_settings({"recent_window": 4, "summary_batch": 2})
        for text in ("新增限制：预算上限两万元", "新增限制：先做网页", "补充：请纳入两家已完成的意见"):
            self.store.append_message(self.gid, {"kind": "user", "author": "user", "text": text})
        code, result, _ = self.request("POST", url, {})
        self.assertEqual(code, 200, result)
        self.wait()
        self.assertEqual([m for m, p in reg.prompts[3:]], ["claude"])
        prompt = reg.prompts_of("claude")[-1]
        for expected in ("GPT 的完整成功回复。", "Gemini 的完整成功回复。", "预算上限两万元", "先做网页"):
            self.assertIn(expected, prompt)
        after = [m for m in self.store.messages(self.gid) if m["kind"] == "chat"]
        self.assertEqual(after[:2], successful)
        self.assertEqual([m["author"] for m in after], ["gpt", "gemini", "claude"])

    def test_pause_resume_visibility_ping_guard_and_no_implicit_requests(self):
        code, data, _ = self.request("POST", "/api/members/claude/pause", {})
        self.assertEqual(code, 200, data)
        self.assertTrue(data["availability"]["paused"])
        self.assertEqual(data["availability"]["reason"], "manual")
        code, members, _ = self.request("GET", "/api/members")
        self.assertEqual(code, 200, members)
        member = next(m for m in members["members"] if m["id"] == "claude")
        self.assertTrue(member["availability"]["paused"])
        code, group, _ = self.request("GET", "/api/groups/" + self.gid)
        self.assertEqual(code, 200, group)
        self.assertTrue(group["state"]["model_availability"]["claude"]["paused"])
        code, ping, _ = self.request("POST", "/api/members/claude/ping", {})
        self.assertEqual(code, 200, ping)
        self.assertFalse(ping["ok"])
        self.assertEqual(self.ping_calls, [])
        code, data, _ = self.request("POST", "/api/members/claude/resume", {})
        self.assertEqual(code, 200, data)
        self.assertFalse(data["availability"]["paused"])
        self.assertEqual(self.ping_calls, [])
        self.assertEqual(self.store.messages(self.gid), [])
        code, ping, _ = self.request("POST", "/api/members/claude/ping", {})
        self.assertEqual(code, 200, ping)
        self.assertTrue(ping["ok"])
        self.assertEqual(self.ping_calls, ["claude"])

    def test_failed_ping_with_quota_pauses_and_next_ping_is_not_executed(self):
        def failed_ping(member, settings, cwd):
            self.ping_calls.append(member)
            return {"ok": False, "error": "quota exhausted"}
        self.server.app.registry.ping = failed_ping
        _, data, _ = self.request("POST", "/api/members/gpt/ping", {})
        self.assertFalse(data["ok"])
        self.assertTrue(self.engine.availability.snapshot()["gpt"]["paused"])
        self.request("POST", "/api/members/gpt/ping", {})
        self.assertEqual(self.ping_calls, ["gpt"])

    def test_pause_resume_require_csrf_header_and_unknown_member_is_rejected(self):
        for action in ("pause", "resume"):
            self.assertEqual(self.request("POST", "/api/members/gpt/" + action, {}, headers={"X-AIGC": ""})[0], 403)
            self.assertEqual(self.request("POST", "/api/members/unknown/" + action, {})[0], 404)
        self.assertFalse(self.engine.availability.snapshot()["gpt"]["paused"])

    def test_quota_http_cache_and_state_reads_never_generate_or_implicitly_refresh(self):
        self.engine.quota = QuotaMonitor(demo=False, ttl=60)
        observation = {"status": "available", "source": "stub", "windows": [
            {"used_percent": 100, "remaining_percent": 0, "window": "primary"}], "message": "快照"}
        with patch("aigc.quota.query_quota", return_value=observation) as query:
            for path in ("/api/groups", "/api/groups/" + self.gid,
                         "/api/groups/%s/poll?after=0&rev=-1" % self.gid):
                self.assertEqual(self.request("GET", path)[0], 200)
            self.assertEqual(query.call_count, 0)
            code, data, _ = self.request("GET", "/api/members/gpt/quota")
            self.assertEqual(code, 200, data)
            self.assertFalse(data["quota"]["cached"])
            self.assertEqual(query.call_count, 1)
            _, cached, _ = self.request("GET", "/api/members/gpt/quota")
            self.assertTrue(cached["quota"]["cached"])
            _, group, _ = self.request("GET", "/api/groups/" + self.gid)
            self.assertEqual(group["state"]["model_quota"]["gpt"]["windows"][0]["used_percent"], 100)
            self.request("GET", "/api/groups/%s/poll?after=0&rev=-1" % self.gid)
            self.assertEqual(query.call_count, 1)
            self.assertFalse(self.engine.availability.snapshot()["gpt"]["paused"])

            self.engine.availability.pause("gpt", "manual", "手动暂停")
            observation["windows"][0].update(used_percent=0, remaining_percent=100)
            code, fresh, _ = self.request("GET", "/api/members/gpt/quota?refresh=1")
            self.assertEqual(code, 200, fresh)
            self.assertEqual(query.call_count, 2)
            self.assertEqual(fresh["quota"]["windows"][0]["remaining_percent"], 100)
            self.assertTrue(self.engine.availability.snapshot()["gpt"]["paused"])
        self.assertEqual(self.ping_calls, [])
        self.assertEqual(self.store.messages(self.gid), [])


if __name__ == "__main__":
    unittest.main()
