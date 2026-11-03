"""Model routing through the real engine; all model calls are local doubles."""

import copy
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.common import atomic_write_json
from aigc.engine import Engine
from aigc.members import MemberError
from aigc.store import Store
from test_engine import status


MEMBERS = ("claude", "gpt", "gemini")


def automatic(prefix="old"):
    return {"members": {member: {
        "model": member + "-legacy-fixed",
        "routing": {"mode": "auto", "profiles": {
            tier: "%s-%s-%s" % (prefix, member, tier)
            for tier in ("fast", "balanced", "deep")}},
    } for member in MEMBERS}}


class RecordingAdapter:
    def __init__(self, registry, member, settings):
        self.registry = registry
        self.member = member
        self.settings = copy.deepcopy(settings)
        self.last_metadata = {}

    def ask(self, prompt, cwd, cancel=None):
        call = {"member": self.member, "settings": copy.deepcopy(self.settings),
                "prompt": prompt, "summary": "书记员" in prompt,
                "review": "背靠背发言已经结束" in prompt}
        call["model"] = call["settings"]["members"][self.member]["model"]
        with self.registry.lock:
            self.registry.calls.append(call)
        if self.registry.hook:
            self.registry.hook(call)
        if self.member in self.registry.errors:
            raise MemberError(self.registry.errors[self.member])
        self.last_metadata = {"reported_model": "stub-report-" + (call["model"] or "default")}
        if call["summary"]:
            return "摘要由 %s 完成。" % self.member
        return "由 %s 完成回复。" % self.member + status()


class RecordingRegistry:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.hook = None
        self.lock = threading.Lock()

    def adapter(self, member, settings):
        return RecordingAdapter(self, member, settings)


class RoutingEngineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aigc-routing-")
        self.store = Store(Path(self.tmp))
        self.reg = RecordingRegistry()
        self.engine = Engine(self.store, self.reg)
        self.gid = self.store.create_group("路由测试")["id"]
        self.engine.update_group(self.gid, {"auto_rounds": 0})

    def tearDown(self):
        self.engine.shutdown()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(r.snapshot()["running"] for r in self.engine._runners.values()):
            time.sleep(0.01)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if not self.engine.state(self.gid)["running"]:
                return
            time.sleep(0.01)
        self.fail("Routing test round did not finish")

    def send(self, text):
        self.engine.post_user_message(self.gid, text)
        self.wait()

    def chats(self):
        return [m for m in self.store.messages(self.gid) if m["kind"] == "chat"]

    def test_legacy_fixed_models_and_cli_defaults_are_preserved(self):
        legacy = {"membership_only": True, "recent_window": 8, "members": {
            "claude": {"model": "legacy-claude", "timeout": 75, "command": "/old/claude"},
            "gpt": {"model": ""}, "gemini": {"model": "legacy-gemini"}}}
        atomic_write_json(self.store.root / "settings.json", legacy)
        before = (self.store.root / "settings.json").read_bytes()
        self.send("大家好")
        self.assertEqual(len(self.reg.calls), 3)
        self.assertEqual({c["member"]: c["model"] for c in self.reg.calls},
                         {"claude": "legacy-claude", "gpt": "", "gemini": "legacy-gemini"})
        for message in self.chats():
            route = message["model_route"]
            self.assertEqual(route["mode"], "manual")
            self.assertEqual(route["source"], "member_setting")
            self.assertEqual(route["requested_model"], legacy["members"][message["author"]]["model"])
        self.assertEqual((self.store.root / "settings.json").read_bytes(), before)
        self.assertEqual(self.reg.calls[0]["settings"]["members"]["claude"]["command"], "/old/claude")

    def test_auto_profiles_are_really_sent_and_match_persisted_metadata(self):
        self.store.save_settings(automatic())
        before = self.store.load_settings()
        for query, tier in (("你好", "fast"), ("分析系统架构", "balanced"), ("请给出定理证明", "deep")):
            start = len(self.reg.calls)
            self.send(query)
            calls = self.reg.calls[start:]
            self.assertEqual(len(calls), 3)
            for call in calls:
                expected = "old-%s-%s" % (call["member"], tier)
                self.assertEqual(call["model"], expected)
                message = next(m for m in reversed(self.chats()) if m["author"] == call["member"])
                route = message["model_route"]
                self.assertEqual(route["requested_model"], expected)
                self.assertEqual(route["tier"], tier)
                self.assertEqual(route["reported_model"], "stub-report-" + expected)
                self.assertEqual(route["complexity"]["context_chars"], len(call["prompt"]))
        self.assertEqual(self.store.load_settings(), before)

    def test_host_override_wins_over_global_mode_and_empty_means_cli_default(self):
        self.store.save_settings(automatic())
        self.store.save_settings({"members": {"gpt": {"routing": dict(automatic()["members"]["gpt"]["routing"], mode="manual")}}})
        self.engine.update_group(self.gid, {"model_overrides": {
            "claude": {"mode": "manual", "model": "host-claude"},
            "gpt": {"mode": "auto"},
            "gemini": {"mode": "manual", "model": ""}}})
        self.send("请给出定理证明")
        self.assertEqual({c["member"]: c["model"] for c in self.reg.calls},
                         {"claude": "host-claude", "gpt": "old-gpt-deep", "gemini": ""})
        self.assertTrue(all(m["model_route"]["source"] == "group_override" for m in self.chats()))

    def test_midround_global_and_host_changes_only_affect_next_round(self):
        self.store.save_settings(automatic())
        changed = []

        def mutate(call):
            if not changed:
                changed.append(True)
                self.store.save_settings(automatic("new"))
                self.engine.update_group(self.gid, {"model_overrides": {"gpt": {"mode": "manual", "model": "host-next-round"}}})

        self.reg.hook = mutate
        self.send("你好")
        self.assertEqual({c["member"]: c["model"] for c in self.reg.calls},
                         {m: "old-%s-fast" % m for m in MEMBERS})
        self.send("下一轮继续")
        self.assertEqual({c["member"]: c["model"] for c in self.reg.calls[3:]},
                         {"claude": "new-claude-fast", "gpt": "host-next-round", "gemini": "new-gemini-fast"})

    def test_parallel_review_uses_frozen_profiles_and_accurate_per_call_metadata(self):
        self.store.save_settings(automatic())
        self.engine.update_group(self.gid, {"mode": "parallel", "parallel_review": True})
        changed = threading.Event()
        hook_lock = threading.Lock()

        def mutate(call):
            with hook_lock:
                if not changed.is_set():
                    self.store.save_settings(automatic("new"))
                    self.engine.update_group(self.gid, {"model_overrides": {"gemini": {"mode": "manual", "model": "later-gemini"}}})
                    changed.set()

        self.reg.hook = mutate
        self.send("独立给出看法")
        self.assertEqual(len(self.reg.calls), 6)
        self.assertEqual(len(self.chats()), 6)
        for member in MEMBERS:
            calls = [c for c in self.reg.calls if c["member"] == member]
            messages = [m for m in self.chats() if m["author"] == member]
            self.assertEqual([c["model"] for c in calls], ["old-%s-fast" % member, "old-%s-balanced" % member])
            self.assertEqual([m["model_route"]["requested_model"] for m in messages], [c["model"] for c in calls])
            self.assertEqual([m["task"] for m in messages], ["parallel", "review"])

    def test_bad_stored_gemini_auto_config_never_reaches_adapter(self):
        settings = self.store.load_settings()
        settings["members"]["gemini"]["routing"] = {"mode": "auto", "profiles": {"fast": "", "balanced": "", "deep": ""}}
        atomic_write_json(self.store.root / "settings.json", settings)
        self.send("@Gemini 你好")
        self.assertEqual(self.reg.calls, [])
        self.assertEqual(self.chats(), [])
        self.assertTrue(any("三档模型" in m["text"] for m in self.store.messages(self.gid) if m["kind"] == "system"))

    def test_explicit_unavailable_model_reports_attempt_without_fallback(self):
        self.store.save_settings(automatic())
        self.engine.update_group(self.gid, {"model_overrides": {"claude": {"mode": "manual", "model": "account-unavailable-model"}}})
        self.reg.errors["claude"] = "model not found for this account"
        self.send("@Claude 你好")
        self.assertEqual(len(self.reg.calls), 1)
        self.assertEqual(self.reg.calls[0]["model"], "account-unavailable-model")
        self.assertEqual(self.chats(), [])
        errors = [m for m in self.store.messages(self.gid) if m["kind"] == "system" and "model not found" in m["text"]]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["model_route"]["requested_model"], "account-unavailable-model")
        self.assertEqual(errors[0]["model_route"]["source"], "group_override")

    def test_summary_respects_auto_and_host_manual_model_and_records_it(self):
        self.store.save_settings(dict(automatic(), recent_window=4, summary_batch=2))
        self.engine.update_group(self.gid, {"secretary": "claude"})
        self.send("第一轮短问")
        self.send("第二轮短问")
        summary_calls = [c for c in self.reg.calls if c["summary"]]
        self.assertEqual(len(summary_calls), 1)
        self.assertEqual(summary_calls[0]["model"], "old-claude-fast")
        board = self.store.load_board(self.gid)
        self.assertEqual(board["summary_model_route"]["requested_model"], "old-claude-fast")
        self.engine.update_group(self.gid, {"model_overrides": {"claude": {"mode": "manual", "model": "host-summary"}}})
        self.send("第三轮短问")
        last_summary = [c for c in self.reg.calls if c["summary"]][-1]
        self.assertEqual(last_summary["model"], "host-summary")
        board = self.store.load_board(self.gid)
        self.assertEqual(board["summary_model_route"]["requested_model"], "host-summary")
        system = [m for m in self.store.messages(self.gid) if "整理了前情摘要" in m.get("text", "")][-1]
        self.assertEqual(system["model_route"], board["summary_model_route"])

    def test_summary_keeps_round_host_choice_when_changed_during_reply(self):
        self.store.save_settings(dict(automatic(), recent_window=4, summary_batch=2))
        self.engine.update_group(self.gid, {"secretary": "claude"})
        self.send("第一轮")
        self.engine.update_group(self.gid, {"model_overrides": {"claude": {"mode": "manual", "model": "round-frozen"}}})
        changed = []

        def mutate(call):
            if not changed:
                changed.append(True)
                self.engine.update_group(self.gid, {"model_overrides": {"claude": {"mode": "manual", "model": "future-choice"}}})

        self.reg.hook = mutate
        self.send("第二轮")
        self.assertEqual([c for c in self.reg.calls if c["summary"]][-1]["model"], "round-frozen")


if __name__ == "__main__":
    unittest.main()
