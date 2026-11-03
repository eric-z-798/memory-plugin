"""Shared memory through the actual group engine and HTTP boundary.

The adapters here are scripted local doubles; no account or model is called.
"""

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.engine import Engine
from aigc.store import Store
from test_engine import ScriptedRegistry, status
import test_server as server_tests


class MemoryEngineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aigc-memory-integration-")
        self.store = Store(Path(self.tmp))
        self.reg = ScriptedRegistry()
        self.engine = Engine(self.store, self.reg)
        self.group = self.store.create_group("银杏项目")
        self.gid = self.group["id"]
        self.engine.update_group(self.gid, {"auto_rounds": 0})
        self.pid = self.store.load_group(self.gid).get("project_id", self.gid)

    def tearDown(self):
        self.engine.shutdown()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not any(r.snapshot()["running"] for r in self.engine._runners.values()):
                break
            time.sleep(0.01)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, gid=None):
        gid = gid or self.gid
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self.engine.state(gid)["running"]:
                return
            time.sleep(0.01)
        self.fail("Group did not finish its scripted round")

    def test_confirmed_memory_reaches_all_members_and_survives_restart(self):
        self.store.memory.add(self.pid, "银杏项目预算为两万元", kind="decision")
        self.engine.post_user_message(self.gid, "根据项目预算安排工作")
        self.wait()
        for member in ("claude", "gpt", "gemini"):
            self.assertIn("银杏项目预算为两万元", self.reg.prompts_of(member)[0])

        restarted = Store(Path(self.tmp))
        context = restarted.memory.context(self.pid, "预算")
        self.assertIn("银杏项目预算为两万元", context["text"])

    def test_same_project_shares_memory_and_other_project_stays_isolated(self):
        self.store.memory.add(self.pid, "银杏项目专用预算两万元", kind="decision")
        linked = self.store.create_group("另一个讨论窗口")
        self.engine.update_group(linked["id"], {"project_id": self.pid, "auto_rounds": 0})
        separate = self.store.create_group("独立项目")
        self.engine.update_group(separate["id"], {"auto_rounds": 0})
        self.engine.post_user_message(linked["id"], "@Claude 项目预算是什么")
        self.wait(linked["id"])
        self.assertIn("银杏项目专用预算两万元", self.reg.prompts_of("claude")[-1])
        self.engine.post_user_message(separate["id"], "@Claude 项目预算是什么")
        self.wait(separate["id"])
        self.assertNotIn("银杏项目专用预算两万元", self.reg.prompts_of("claude")[-1])

    def test_current_round_memory_is_frozen_until_next_round(self):
        item = self.store.memory.add(self.pid, "预算版本甲：两万元", kind="decision")
        changed = []

        def change_during_reply(prompt):
            if not changed:
                self.store.memory.update(item["id"], {"content": "预算版本乙：三万元"}, item["version"])
                changed.append(True)
            return "完成检查。" + status()

        self.reg.behaviors["claude"] = change_during_reply
        self.engine.post_user_message(self.gid, "讨论预算")
        self.wait()
        for member in ("claude", "gpt", "gemini"):
            self.assertIn("预算版本甲：两万元", self.reg.prompts_of(member)[0])
            self.assertNotIn("预算版本乙：三万元", self.reg.prompts_of(member)[0])
        self.engine.post_user_message(self.gid, "重新读取预算")
        self.wait()
        for member in ("claude", "gpt", "gemini"):
            self.assertIn("预算版本乙：三万元", self.reg.prompts_of(member)[-1])
            self.assertNotIn("预算版本甲：两万元", self.reg.prompts_of(member)[-1])

    def test_parallel_board_is_frozen_even_when_one_member_finishes_early(self):
        self.engine.update_group(self.gid, {"mode": "parallel", "parallel_review": False})
        marker = "只在首个成员回答后出现的秘密提议"
        self.reg.behaviors["claude"] = lambda p: "我有一个提议。" + status(propose_decisions=[marker])
        runner = self.engine.runner(self.gid)
        original = runner._speak
        first_finished = threading.Event()

        def ordered(member, task, cancel, upto_seq=None, **kwargs):
            if member != "claude":
                if not first_finished.wait(3):
                    raise RuntimeError("First member did not finish")
                return original(member, task, cancel, upto_seq=upto_seq, **kwargs)
            try:
                return original(member, task, cancel, upto_seq=upto_seq, **kwargs)
            finally:
                first_finished.set()

        runner._speak = ordered
        self.engine.post_user_message(self.gid, "独立给出建议")
        self.wait()
        self.assertEqual(len([m for m in self.store.messages(self.gid) if m["kind"] == "chat"]), 3)
        for member in ("gpt", "gemini"):
            self.assertNotIn(marker, self.reg.prompts_of(member)[0])
        self.assertEqual(self.store.load_board(self.gid)["decisions"][0]["text"], marker)

    def test_model_candidates_cannot_spoof_confirmation_source_or_global_scope(self):
        self.reg.behaviors["claude"] = lambda p: "提出一条待核实事实。" + status(memories=[{
            "content": "候选预算为七万元", "kind": "fact", "status": "confirmed",
            "project_id": "*", "source": {"author": "user", "seq": 99999},
        }])
        self.engine.post_user_message(self.gid, "@Claude 研究预算")
        self.wait()
        items = self.store.memory.list_memories(self.pid)
        matches = [m for m in items if m["content"] == "候选预算为七万元"]
        self.assertEqual(len(matches), 1)
        candidate = matches[0]
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(candidate["project_id"], self.pid)
        self.assertEqual(candidate["source"]["author"], "claude")
        self.assertEqual(candidate["source"]["group_id"], self.gid)
        actual_reply = next(m for m in self.store.messages(self.gid) if m.get("author") == "claude")
        self.assertEqual(candidate["source"]["seq"], actual_reply["seq"])
        self.assertEqual(actual_reply["memory_proposals"], [{"content": "候选预算为七万元", "kind": "fact"}])
        reopened = Store(Path(self.tmp))
        persisted_reply = next(m for m in reopened.messages(self.gid) if m["seq"] == actual_reply["seq"])
        self.assertEqual(persisted_reply["memory_proposals"], actual_reply["memory_proposals"])
        self.assertNotIn("候选预算为七万元", self.store.memory.context(self.pid, "预算")["text"])

    def test_cancelled_reply_creates_no_candidate_memories(self):
        self.reg.delay = 2
        self.reg.behaviors["claude"] = lambda p: "候选。" + status(memories=[{"content": "不应保存", "kind": "note"}])
        self.engine.post_user_message(self.gid, "@Claude 慢慢回答")
        self.assertTrue(self.engine.stop(self.gid))
        self.wait()
        self.assertEqual(self.store.memory.list_memories(self.pid), [])

    def test_cancel_while_reply_waits_for_group_lock_does_not_commit(self):
        ready, release = threading.Event(), threading.Event()

        def reply(prompt):
            ready.set()
            if not release.wait(3):
                raise RuntimeError("Reply was not released")
            return "不应提交这个回复。" + status(
                propose_decisions=["不应生效的决议"],
                memories=[{"content": "不应保存的候选", "kind": "note"}])

        self.reg.behaviors["claude"] = reply
        self.engine.post_user_message(self.gid, "@Claude 回答后准备保存")
        self.assertTrue(ready.wait(3))
        with self.store.lock(self.gid):
            release.set()
            # Give the reply thread time to reach the already-held group lock.
            time.sleep(0.05)
            self.assertTrue(self.engine.stop(self.gid))
        self.wait()
        self.assertFalse(any(m.get("kind") == "chat" for m in self.store.messages(self.gid)))
        self.assertEqual(self.store.load_board(self.gid)["decisions"], [])
        self.assertEqual(self.store.memory.list_memories(self.pid), [])

    def test_memory_deleted_from_storage_is_not_injected_in_new_round(self):
        item = self.store.memory.add(self.pid, "银杏项目待删除约束", kind="decision")
        self.engine.post_user_message(self.gid, "@Claude 项目约束")
        self.wait()
        self.assertIn("银杏项目待删除约束", self.reg.prompts_of("claude")[-1])
        self.store.memory.delete(item["id"], item["version"])
        self.engine.post_user_message(self.gid, "@Claude 重新读取项目约束")
        self.wait()
        self.assertNotIn("银杏项目待删除约束", self.reg.prompts_of("claude")[-1])

    def test_nonempty_group_cannot_be_moved_to_another_project(self):
        other = self.store.memory.create_project("不相关项目")
        self.store.append_message(self.gid, {"kind": "user", "author": "user", "text": "项目内部内容"})
        with self.assertRaisesRegex(ValueError, "已有聊天记录|正在发言"):
            self.engine.update_group(self.gid, {"project_id": other["id"]})
        self.assertEqual(self.store.load_group(self.gid)["project_id"], self.pid)


class MemoryHttpIntegrationTest(server_tests.Base):
    def group(self, name="测试项目", **extra):
        code, data, _ = self.request("POST", "/api/groups", dict(name=name, **extra))
        self.assertEqual(code, 201, data)
        return data["group"]

    def path(self, group):
        return "/api/groups/%s/memories" % group["id"]

    def test_crud_versions_history_and_delete_body(self):
        group = self.group()
        path = self.path(group)
        code, data, _ = self.request("POST", path, {"content": "预算为两万元", "kind": "decision"})
        self.assertEqual(code, 201, data)
        item = data["memory"]
        code, data, _ = self.request("PATCH", path + "/" + item["id"], {
            "version": item["version"], "content": "预算为三万元"})
        self.assertEqual(code, 200, data)
        updated = data["memory"]
        self.assertGreater(updated["version"], item["version"])
        self.assertEqual(self.request("PATCH", path + "/" + item["id"], {
            "version": item["version"], "content": "过时覆盖"})[0], 409)
        code, history, _ = self.request("GET", path + "/" + item["id"] + "/history")
        self.assertEqual(code, 200, history)
        self.assertGreaterEqual(len(history["history"]), 2)
        self.assertEqual(self.request("DELETE", path + "/" + item["id"], {"version": item["version"]})[0], 409)
        self.assertEqual(self.request("DELETE", path + "/" + item["id"], {"version": updated["version"]})[0], 200)
        code, data, _ = self.request("GET", path)
        self.assertEqual(code, 200)
        self.assertEqual(data["memories"], [])

    def test_group_scoped_routes_reject_foreign_memory(self):
        a, b = self.group("A"), self.group("B")
        code, data, _ = self.request("POST", self.path(a), {"content": "只属于 A"})
        self.assertEqual(code, 201, data)
        item = data["memory"]
        foreign = self.path(b) + "/" + item["id"]
        self.assertEqual(self.request("PATCH", foreign, {"version": item["version"], "content": "越界"})[0], 404)
        self.assertEqual(self.request("DELETE", foreign, {"version": item["version"]})[0], 404)
        self.assertEqual(self.request("GET", foreign + "/history")[0], 404)
        _, data, _ = self.request("GET", self.path(b))
        self.assertEqual(data["memories"], [])

    def test_project_linking_and_global_preferences_are_explicit(self):
        code, data, _ = self.request("POST", "/api/projects", {"name": "共享项目"})
        self.assertEqual(code, 201, data)
        project = data["project"]
        a = self.group("讨论一", project_id=project["id"])
        b = self.group("讨论二", project_id=project["id"])
        c = self.group("独立项目")
        self.request("POST", self.path(a), {"content": "这个项目预算两万元", "kind": "decision"})
        self.request("POST", self.path(a), {"content": "偏好简体中文", "kind": "preference", "scope": "global"})
        _, linked, _ = self.request("GET", self.path(b))
        self.assertEqual({x["content"] for x in linked["memories"]}, {"这个项目预算两万元", "偏好简体中文"})
        _, isolated, _ = self.request("GET", self.path(c))
        self.assertEqual([x["content"] for x in isolated["memories"]], ["偏好简体中文"])

    def test_source_is_derived_from_real_message_and_bad_source_rejected(self):
        group = self.group()
        msg = self.store.append_message(group["id"], {"kind": "chat", "author": "gpt", "text": "建议先做网页"})
        code, data, _ = self.request("POST", self.path(group), {
            "content": "确认先做网页", "kind": "decision", "source_seq": msg["seq"],
            "source": {"author": "user", "group_id": "other", "seq": 999}})
        self.assertEqual(code, 201, data)
        source = data["memory"]["source"]
        self.assertEqual(source["author"], "gpt")
        self.assertEqual(source["group_id"], group["id"])
        self.assertEqual(source["seq"], msg["seq"])
        self.assertIn(self.request("POST", self.path(group), {"content": "无来源", "source_seq": 999999})[0], (400, 404))

    def test_invalid_mutations_are_errors_without_persisting_memory(self):
        group = self.group()
        path = self.path(group)
        for body in ({"content": ""}, {"content": ["not text"]},
                     {"content": "x", "scope": "foreign"},
                     {"content": "x", "status": "arbitrary"},
                     {"content": "x", "kind": "arbitrary"}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", path, body)[0], 400)
        _, data, _ = self.request("GET", path)
        self.assertEqual(data["memories"], [])

    def test_invalid_project_binding_does_not_return_internal_error(self):
        group = self.group()
        path = "/api/groups/%s" % group["id"]
        for value in (["not an id"], {"id": "nope"}, "unknown-project", "*"):
            with self.subTest(value=value):
                self.assertEqual(self.request("PATCH", path, {"project_id": value})[0], 400)

    def test_context_excludes_pending_and_export_survives_json_roundtrip(self):
        group = self.group()
        self.request("POST", self.path(group), {"content": "用户已确认预算", "kind": "decision"})
        self.request("POST", self.path(group), {"content": "AI 待确认猜测", "status": "pending"})
        code, context, _ = self.request("GET", "/api/groups/%s/memory-context" % group["id"])
        self.assertEqual(code, 200, context)
        self.assertIn("用户已确认预算", context["text"])
        self.assertNotIn("AI 待确认猜测", context["text"])
        code, exported, headers = self.request("GET", "/api/memories/export")
        self.assertEqual(code, 200, exported)
        self.assertIn("attachment", headers.getheader("Content-Disposition"))
        copy = json.loads(json.dumps(exported, ensure_ascii=False))
        self.assertIn("schema_version", copy)
        self.assertEqual(len(copy["memories"]), 2)


if __name__ == "__main__":
    unittest.main()
