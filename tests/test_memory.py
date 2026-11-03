"""Memory persistence, isolation, retrieval and concurrent change guarantees."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc.memory import MemoryConflict, MemoryError, MemoryStore


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aigc-memory-")
        self.root = Path(self.tmp.name)
        self.memory = MemoryStore(self.root)
        self.memory.ensure_project("project-a", "项目 A")
        self.memory.ensure_project("project-b", "项目 B")

    def tearDown(self):
        self.tmp.cleanup()

    def test_project_isolation_global_and_reopen(self):
        a = self.memory.add("project-a", "A 项目预算为 2 万元", source={"group_id": "g-a", "seq": 1})
        self.memory.add("project-b", "B 项目预算为 8 万元")
        shared = self.memory.add("*", "回答使用简体中文", kind="preference")
        restarted = MemoryStore(self.root)
        found = restarted.list_memories("project-a")
        self.assertEqual({m["id"] for m in found}, {a["id"], shared["id"]})
        self.assertEqual(restarted.get(a["id"])["source"]["seq"], 1)
        self.assertEqual(len(restarted.list_memories("project-a", include_global=False)), 1)
        self.assertNotIn("8 万元", restarted.context("project-a", "预算")["text"])
        self.assertEqual(len(restarted.projects()), 2)

    def test_chinese_and_english_search_without_embedding(self):
        budget = self.memory.add("project-a", "本项目预算为两万元，首版仅支持网页")
        self.memory.add("project-a", "PostgreSQL 数据库保存客户资料")
        self.assertEqual(self.memory.list_memories("project-a", "预算是多少")[0]["id"], budget["id"])
        self.assertEqual(len(self.memory.list_memories("project-a", "postgresql")), 1)
        self.assertEqual(self.memory.list_memories("project-a", "南极企鹅"), [])
        self.assertEqual(self.memory.context("project-a", "南极企鹅")["items"], [])

    def test_pending_excluded_until_confirmed(self):
        proposed = self.memory.add("project-a", "可能预算 3 万", status="pending")
        self.assertFalse(self.memory.context("project-a")["items"])
        updated = self.memory.update(proposed["id"], {"content": "预算确认 2 万", "status": "confirmed"}, 1)
        self.assertEqual(updated["version"], 2)
        ctx = self.memory.context("project-a")
        self.assertEqual(ctx["items"][0]["content"], "预算确认 2 万")
        self.assertNotIn("可能预算", ctx["text"])

    def test_versions_keep_history_and_prevent_stale_write_and_delete(self):
        item = self.memory.add("project-a", "预算 2 万", kind="decision")
        updated = self.memory.update(item["id"], {"content": "预算 3 万"}, 1)
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(MemoryConflict):
            self.memory.update(item["id"], {"content": "预算 4 万"}, 1)
        with self.assertRaises(MemoryConflict):
            self.memory.delete(item["id"], 1)
        self.assertEqual([m["content"] for m in self.memory.history(item["id"])], ["预算 3 万", "预算 2 万"])
        self.assertEqual(self.memory.context("project-a")["items"][0]["content"], "预算 3 万")
        with self.assertRaises(MemoryError):
            self.memory.update(item["id"], {"content": "x"}, True)

    def test_duplicate_retry_and_deleted_request_cannot_resurrect(self):
        item = self.memory.add("project-a", "delete-this-secret", source={"text": "private source"}, idempotency_key="request-one")
        again = self.memory.add("project-a", "delete-this-secret", source={"text": "private source"}, idempotency_key="request-one")
        self.assertEqual(item, again)
        with self.assertRaises(MemoryConflict):
            self.memory.add("project-a", "other content", idempotency_key="request-one")
        self.memory.update(item["id"], {"content": "updated-secret"}, 1)
        self.memory.delete(item["id"], 2)
        for token in ("delete-this-secret", "updated-secret", "private source"):
            self.assertNotIn(token, json.dumps(self.memory.export()))
            # secure_delete also wipes removed records from SQLite's free pages.
            self.assertNotIn(token.encode(), self.memory.path.read_bytes())
        self.assertFalse(self.memory.list_memories("project-a"))
        with self.assertRaises(MemoryError):
            self.memory.history(item["id"])
        with self.assertRaises(MemoryConflict):
            MemoryStore(self.root).add("project-a", "delete-this-secret", source={"text": "private source"}, idempotency_key="request-one")

    def test_context_budget_counts_heading_source_and_full_records(self):
        self.memory.add("project-a", "预算确认" * 100, source={"excerpt": "原文" * 3000})
        self.memory.add("project-a", "简短决定", kind="decision")
        for budget in (0, 1, 30, 200, 500, 1200, 6000):
            context = self.memory.context("project-a", budget=budget)
            self.assertLessEqual(len(context["text"]), budget)
            for item in context["items"]:
                self.assertIn(item["content"], context["text"])
                self.assertIn(item["id"], context["text"])
        self.assertTrue(self.memory.context("project-a", budget=30)["truncated"])
        full = self.memory.context("project-a")
        self.assertFalse(full["truncated"])
        self.assertEqual(len(full["items"]), 2)

    def test_context_includes_durable_preferences_but_not_unrelated_notes(self):
        note = self.memory.add("project-a", "旧讨论里的香蕉")
        decision = self.memory.add("project-a", "首版只做网页", kind="decision")
        pref = self.memory.add("*", "使用中文", kind="preference")
        ids = {m["id"] for m in self.memory.context("project-a", "数据库")['items']}
        self.assertEqual(ids, {decision["id"], pref["id"]})
        self.assertNotIn(note["id"], ids)

    def test_concurrent_updates_from_two_instances_have_one_winner(self):
        item = self.memory.add("project-a", "原计划")
        barrier = threading.Barrier(2)
        results = []

        def modify(text):
            local = MemoryStore(self.root)
            barrier.wait()
            try:
                local.update(item["id"], {"content": text}, 1)
                results.append("ok")
            except MemoryConflict:
                results.append("conflict")

        threads = [threading.Thread(target=modify, args=(str(n),)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["ok", "conflict"])
        self.assertEqual(self.memory.get(item["id"])["version"], 2)

    def test_context_is_detached_snapshot_and_revision_changes(self):
        item = self.memory.add("project-a", "预算 2 万")
        before = self.memory.context("project-a")
        self.memory.update(item["id"], {"content": "预算 3 万"}, 1)
        after = self.memory.context("project-a")
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(before["items"][0]["content"], "预算 2 万")
        before["items"][0]["content"] = "external mutation"
        self.assertEqual(self.memory.get(item["id"])["content"], "预算 3 万")

    def test_validation_and_export(self):
        self.assertEqual(self.memory.ensure_project("project-a", "other name")["name"], "项目 A")
        for content in (None, 12, "", "x" * 4001):
            with self.assertRaises(MemoryError):
                self.memory.add("project-a", content)
        with self.assertRaises(MemoryError):
            self.memory.add("missing", "x")
        with self.assertRaises(MemoryError):
            self.memory.add("project-a", "x", source={"bad": float("nan")})
        with self.assertRaises(MemoryError):
            self.memory.add("../escape", "x")
        with self.assertRaises(MemoryError):
            self.memory.context("project-a", budget=-1)
        item = self.memory.add("project-a", "有效内容")
        exported = self.memory.export()
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["memories"][0]["id"], item["id"])
        self.assertEqual(len(exported["history"]), 1)


if __name__ == "__main__":
    unittest.main()
