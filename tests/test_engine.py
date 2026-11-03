"""群聊引擎：发言顺序、背靠背、@、停止、出错、自动续轮、前情摘要。"""

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.engine import Engine, parse_mentions  # noqa: E402
from aigc.members import Cancelled, MemberError  # noqa: E402
from aigc.store import Store  # noqa: E402


def status(**kw):
    base = {"summary": "s", "votes": [], "propose_decisions": [], "propose_tasks": [],
            "task_updates": [], "withdraw": [], "need_user": ""}
    base.update(kw)
    return "\n\n<<<STATUS\n" + json.dumps(base, ensure_ascii=False) + "\nSTATUS>>>"


class ScriptedAdapter:
    def __init__(self, registry, member):
        self.registry = registry
        self.member = member

    def ask(self, prompt, cwd, cancel=None):
        reg = self.registry
        with reg.lock:
            reg.prompts.append((self.member, prompt))
        delay = reg.delays.get(self.member, reg.delay)
        if delay:
            if cancel:
                cancel.sleep(delay)
            else:
                time.sleep(delay)
        behave = reg.behaviors.get(self.member)
        if behave:
            return behave(prompt)
        if "书记员" in prompt:
            return "摘要：大家讨论了测试。"
        return "我是 %s。" % self.member + status()


class ScriptedRegistry:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.delays = {}
        self.behaviors = {}
        self.prompts = []
        self.lock = threading.Lock()

    def adapter(self, member, settings):
        return ScriptedAdapter(self, member)

    def prompts_of(self, member):
        return [p for m, p in self.prompts if m == member]


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aigc-test-")
        self.store = Store(Path(self.tmp))
        self.reg = ScriptedRegistry()
        self.engine = Engine(self.store, self.reg)
        self.gid = self.store.create_group("测试群", "测试目标")["id"]

    def tearDown(self):
        self.engine.shutdown()
        time.sleep(0.05)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, timeout=10):
        deadline = time.time() + timeout
        time.sleep(0.05)
        while time.time() < deadline:
            if not self.engine.state(self.gid)["running"]:
                return
            time.sleep(0.02)
        self.fail("引擎没有在 %s 秒内停下：%s" % (timeout, self.engine.state(self.gid)))

    def chats(self):
        return [m for m in self.store.messages(self.gid) if m["kind"] == "chat"]

    def systems(self):
        return [m["text"] for m in self.store.messages(self.gid) if m["kind"] == "system"]

    # ------------------------------------------------------------
    def test_sequential_round_and_rotation(self):
        self.engine.post_user_message(self.gid, "大家好")
        self.wait()
        self.assertEqual([m["author"] for m in self.chats()], ["claude", "gpt", "gemini"])
        # 后发言的人能看到前面的发言
        self.assertIn("我是 claude。", self.reg.prompts_of("gpt")[0])
        self.assertIn("我是 gpt。", self.reg.prompts_of("gemini")[0])
        self.engine.post_user_message(self.gid, "第二轮")
        self.wait()
        self.assertEqual([m["author"] for m in self.chats()][3:], ["gpt", "gemini", "claude"])

    def test_prompt_contains_shared_memory(self):
        self.engine.post_user_message(self.gid, "请讨论 X 方案")
        self.wait()
        prompt = self.reg.prompts_of("claude")[0]
        for part in ("## 共享看板", "测试目标", "## 前情摘要", "请讨论 X 方案", "<<<STATUS", "你是 Claude"):
            self.assertIn(part, prompt)

    def test_mentions(self):
        self.assertEqual(parse_mentions("@gemini 你怎么看"), ["gemini"])
        self.assertEqual(parse_mentions("＠ChatGPT 和 @Claude"), ["gpt", "claude"])
        self.assertIsNone(parse_mentions("@全体 说说"))
        self.assertIsNone(parse_mentions("没有点名"))
        self.engine.post_user_message(self.gid, "@Gemini 你先说")
        self.wait()
        self.assertEqual([m["author"] for m in self.chats()], ["gemini"])
        self.assertEqual(self.chats()[0]["task"], "mention")

    def test_mention_of_absent_member(self):
        self.engine.update_group(self.gid, {"members": ["claude", "gpt"]})
        self.engine.post_user_message(self.gid, "@gemini 在吗")
        self.wait()
        self.assertEqual(self.chats(), [])
        self.assertTrue(any("Gemini 不在群里" in s for s in self.systems()))

    def test_parallel_with_review(self):
        self.engine.update_group(self.gid, {"mode": "parallel"})
        self.reg.delay = 0.05
        self.engine.post_user_message(self.gid, "各自独立回答")
        self.wait()
        chats = self.chats()
        self.assertEqual(sorted(m["task"] for m in chats[:3]), ["parallel"] * 3)
        self.assertEqual(sorted(m["task"] for m in chats[3:]), ["review"] * 3)
        # 背靠背：作答时看不到同一轮别人的回答
        for member in ("claude", "gpt", "gemini"):
            first = self.reg.prompts_of(member)[0]
            self.assertNotIn("我是 ", first.split("## 最近的聊天记录")[1].split("## 现在轮到你发言")[0])
        # 互评时能看到三家的回答
        review = self.reg.prompts_of("gpt")[1]
        for member in ("claude", "gpt", "gemini"):
            self.assertIn("我是 %s。" % member, review)

    def test_member_error_does_not_break_round(self):
        def boom(prompt):
            raise MemberError("额度用完或被限流了")
        self.reg.behaviors["gpt"] = boom
        self.engine.post_user_message(self.gid, "大家好")
        self.wait()
        self.assertEqual([m["author"] for m in self.chats()], ["claude", "gemini"])
        self.assertTrue(any("GPT 这次没能回复" in s for s in self.systems()))

    def test_stop_cancels_running_round(self):
        self.reg.delay = 3
        self.engine.post_user_message(self.gid, "慢慢说")
        time.sleep(0.3)
        self.assertTrue(self.engine.state(self.gid)["running"])
        started = time.time()
        self.assertTrue(self.engine.stop(self.gid))
        self.wait(timeout=3)
        self.assertLess(time.time() - started, 2)
        self.assertEqual(self.chats(), [])
        self.assertIn("群主停止了本轮发言。", self.systems())

    def test_message_during_round_does_not_cause_duplicate_replies(self):
        self.reg.delay = 0.3
        self.engine.post_user_message(self.gid, "第一条")
        time.sleep(0.1)  # Claude 正在想
        self.engine.post_user_message(self.gid, "补充一条")
        self.wait()
        authors = [m["author"] for m in self.chats()]
        # 第一轮三家都说；第二条消息发出时只有 Claude 没看到，所以只补 Claude 一次
        self.assertEqual(authors, ["claude", "gpt", "gemini", "claude"])
        self.assertIn("补充一条", self.reg.prompts_of("gpt")[0])

    def test_auto_round_collects_missing_votes(self):
        def propose(prompt):
            if "D-001" in prompt:
                return "同意。" + status()
            return "我提议。" + status(propose_decisions=["采用方案 B"])
        self.reg.behaviors["gemini"] = propose  # 第一轮最后发言的人提议

        def agree(prompt):
            if "D-001【讨论中】" in prompt:
                return "同意。" + status(votes=[{"id": "D-001", "vote": "agree"}])
            return "好的。" + status()
        self.reg.behaviors["claude"] = agree
        self.reg.behaviors["gpt"] = agree
        self.engine.post_user_message(self.gid, "定个方案")
        self.wait()
        board = self.store.load_board(self.gid)
        self.assertEqual(board["decisions"][0]["status"], "effective")
        tasks = [m["task"] for m in self.chats()]
        self.assertEqual(tasks, ["reply", "reply", "reply", "vote", "vote"])
        self.assertTrue(any("自动续轮（1/2）" in s for s in self.systems()))

    def test_auto_rounds_can_be_disabled(self):
        self.reg.behaviors["gemini"] = lambda p: "我提议。" + status(propose_decisions=["采用方案 B"])
        self.engine.update_group(self.gid, {"auto_rounds": 0})
        self.engine.post_user_message(self.gid, "定个方案")
        self.wait()
        self.assertEqual(len(self.chats()), 3)
        self.assertEqual(self.store.load_board(self.gid)["decisions"][0]["status"], "discussing")

    def test_secretary_summarizes_old_messages(self):
        self.store.save_settings({"recent_window": 4, "summary_batch": 2})
        for i in range(3):
            self.engine.post_user_message(self.gid, "第 %d 轮" % i)
            self.wait()
        board = self.store.load_board(self.gid)
        self.assertEqual(board["summary"], "摘要：大家讨论了测试。")
        self.assertGreater(board["summary_upto"], 0)
        self.assertTrue(any("整理了前情摘要" in s for s in self.systems()))
        # 之后的提示词带上摘要，并且不再原样附上已摘要的消息
        self.engine.post_user_message(self.gid, "继续")
        self.wait()
        last_prompt = [p for _, p in self.reg.prompts if "## 最近的聊天记录" in p][-1]
        self.assertIn("摘要：大家讨论了测试。", last_prompt)
        recent = last_prompt.split("## 最近的聊天记录")[1]
        self.assertNotIn("#1 群主", recent)

    def test_comment_from_board_triggers_responses(self):
        self.reg.behaviors["claude"] = lambda p: "提议。" + status(propose_decisions=["方案 A"])
        self.engine.update_group(self.gid, {"members": ["claude"]})
        self.engine.post_user_message(self.gid, "定方案")
        self.wait()
        self.assertEqual(self.store.load_board(self.gid)["decisions"][0]["status"], "effective")
        self.reg.behaviors["claude"] = lambda p: "收到意见，同意保留。" + status(votes=[{"id": "D-001", "vote": "agree"}])
        result = self.engine.board_action(self.gid, "comment", {"id": "D-001", "text": "再想想成本"}, respond=True)
        self.assertIn("退回讨论", result)
        self.wait()
        self.assertEqual(self.store.load_board(self.gid)["decisions"][0]["status"], "effective")
        self.assertIn("再想想成本", self.reg.prompts_of("claude")[-1])

    def test_members_change_rechecks_consensus(self):
        self.reg.behaviors["claude"] = lambda p: "提议。" + status(propose_decisions=["方案 A"])
        self.reg.behaviors["gpt"] = lambda p: "同意。" + status(votes=[{"id": "D-001", "vote": "agree"}])
        self.reg.behaviors["gemini"] = lambda p: "我先不表态。" + status()
        self.engine.update_group(self.gid, {"auto_rounds": 0})
        self.engine.post_user_message(self.gid, "定方案")
        self.wait()
        self.assertEqual(self.store.load_board(self.gid)["decisions"][0]["status"], "discussing")
        self.engine.update_group(self.gid, {"members": ["claude", "gpt"]})
        self.assertEqual(self.store.load_board(self.gid)["decisions"][0]["status"], "effective")

    def test_missing_status_is_logged(self):
        self.reg.behaviors["claude"] = lambda p: "我忘了附状态更新。"
        self.engine.update_group(self.gid, {"members": ["claude"]})
        self.engine.post_user_message(self.gid, "hi")
        self.wait()
        self.assertEqual(self.chats()[0]["status_note"], "没有附状态更新，看板未变化")

    def test_deleting_group_mid_round_does_not_recreate_it(self):
        self.reg.delay = 0.4
        self.engine.post_user_message(self.gid, "hi")
        time.sleep(0.1)
        self.store.archive_group(self.gid)
        time.sleep(1.5)
        self.assertFalse(self.engine.state(self.gid)["running"])
        self.assertFalse((Path(self.tmp) / "groups" / self.gid).exists())
        self.assertTrue((Path(self.tmp) / "archive" / self.gid / "group.json").exists())

    def test_group_settings_are_sanitized(self):
        g = self.engine.update_group(self.gid, {"members": ["GPT", "bogus"], "mode": "chaos", "auto_rounds": 99,
                                                "secretary": "nobody", "name": " "})
        self.assertEqual(g["members"], ["gpt"])
        self.assertEqual(g["mode"], "sequential")
        self.assertEqual(g["auto_rounds"], 5)
        self.assertEqual(g["secretary"], "rotate")
        self.assertEqual(g["name"], "测试群")


if __name__ == "__main__":
    unittest.main()
