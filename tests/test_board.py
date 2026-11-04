"""共享看板：状态更新解析、共识规则、群主操作。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc import board as B  # noqa: E402

M = ["claude", "gpt", "gemini"]


class ExtractStatus(unittest.TestCase):
    def test_basic(self):
        vis, st, err = B.extract_status('我的看法如下。\n\n<<<STATUS\n{"summary": "s", "votes": []}\nSTATUS>>>')
        self.assertEqual(vis, "我的看法如下。")
        self.assertEqual(st["summary"], "s")
        self.assertIsNone(err)

    def test_fenced_with_label_and_trailing_comma(self):
        text = '正文\n\n**状态更新：**\n```json\n<<<STATUS\n{"summary": "s", "votes": [],}\nSTATUS>>>\n```'
        vis, st, err = B.extract_status(text)
        self.assertEqual(vis, "正文")
        self.assertEqual(st["summary"], "s")
        self.assertIsNone(err)

    def test_missing_closing_marker(self):
        vis, st, err = B.extract_status('正文\n<<<STATUS\n{"summary": "x"}')
        self.assertEqual(vis, "正文")
        self.assertEqual(st["summary"], "x")

    def test_no_status(self):
        self.assertEqual(B.extract_status("只有正文"), ("只有正文", None, None))

    def test_bad_json_keeps_text(self):
        vis, st, err = B.extract_status("正文\n<<<STATUS\n{oops}\nSTATUS>>>")
        self.assertEqual(vis, "正文")
        self.assertIsNone(st)
        self.assertIn("格式有误", err)

    def test_smart_quotes(self):
        vis, st, err = B.extract_status("正文\n<<<STATUS\n{“summary”: “x”}\nSTATUS>>>")
        self.assertEqual(st, {"summary": "x"})

    def test_last_block_wins_and_all_removed(self):
        text = ('前\n<<<STATUS\n{"summary": "first"}\nSTATUS>>>\n中\n'
                '<<<STATUS\n{"summary": "second"}\nSTATUS>>>')
        vis, st, err = B.extract_status(text)
        self.assertEqual(st["summary"], "second")
        self.assertNotIn("STATUS", vis)
        self.assertIn("中", vis)


class Consensus(unittest.TestCase):
    def setUp(self):
        self.b = B.new_board("目标")

    def test_propose_then_everyone_agrees(self):
        fx = B.apply_status(self.b, "claude", {"propose_decisions": ["采用方案 B"]}, M)
        self.assertIn("提议 D-001", fx)
        d = self.b["decisions"][0]
        self.assertEqual(d["status"], "discussing")
        self.assertEqual(d["votes"]["claude"]["vote"], "agree")
        B.apply_status(self.b, "gpt", {"votes": [{"id": "D-001", "vote": "agree"}]}, M)
        self.assertEqual(d["status"], "discussing")
        fx = B.apply_status(self.b, "gemini", {"votes": {"d-001": "同意"}}, M)
        self.assertEqual(d["status"], "effective")
        self.assertIn("D-001 AI 共识", fx)
        self.assertEqual(d["confirmed_by"], "ai_consensus")

    def test_loose_ids_are_understood(self):
        B.apply_status(self.b, "claude", {"propose_decisions": ["X"]}, M)
        B.apply_status(self.b, "gpt", {"votes": [{"id": "D-1", "vote": "agree"}]}, M)
        B.apply_status(self.b, "gemini", {"votes": [{"id": "d001", "vote": "yes"}]}, M)
        self.assertEqual(self.b["decisions"][0]["status"], "effective")
        self.assertEqual(B.normalize_id("T_12"), "T-012")
        self.assertIsNone(B.normalize_id("X-1"))

    def test_disagreement_blocks_and_can_change(self):
        B.apply_status(self.b, "claude", {"propose_decisions": ["采用方案 B"]}, M)
        B.apply_status(self.b, "gpt", {"votes": [{"id": "D-001", "vote": "disagree", "reason": "成本太高"}]}, M)
        B.apply_status(self.b, "gemini", {"votes": [{"id": "D-001", "vote": "agree"}]}, M)
        d = self.b["decisions"][0]
        self.assertEqual(d["status"], "discussing")
        self.assertEqual(d["votes"]["gpt"]["reason"], "成本太高")
        B.apply_status(self.b, "gpt", {"votes": [{"id": "D-001", "vote": "agree"}]}, M)
        self.assertEqual(d["status"], "effective")

    def test_disagree_without_reason_is_marked(self):
        B.apply_status(self.b, "claude", {"propose_decisions": ["X"]}, M)
        B.apply_status(self.b, "gpt", {"votes": [{"id": "D-001", "vote": "反对"}]}, M)
        self.assertEqual(self.b["decisions"][0]["votes"]["gpt"]["reason"], "（未写理由）")

    def test_duplicate_proposal_counts_as_agree(self):
        B.apply_status(self.b, "claude", {"propose_decisions": ["采用方案B"]}, M)
        fx = B.apply_status(self.b, "gpt", {"propose_decisions": ["采用方案 B。"]}, M)
        self.assertEqual(len(self.b["decisions"]), 1)
        self.assertIn("同意 D-001", fx)
        self.assertEqual(self.b["decisions"][0]["votes"]["gpt"]["vote"], "agree")

    def test_task_flow_and_owner_only_updates(self):
        B.apply_status(self.b, "claude", {"propose_tasks": [{"title": "调研竞品", "owner": "Gemini", "deliverable": "对比表"}]}, M)
        t = self.b["tasks"][0]
        self.assertEqual((t["id"], t["owner"], t["status"]), ("T-001", "gemini", "proposed"))
        B.apply_status(self.b, "gpt", {"votes": [{"id": "T-001", "vote": "agree"}]}, M)
        fx = B.apply_status(self.b, "gemini", {"votes": [{"id": "T-001", "vote": "agree"}]}, M)
        self.assertEqual(t["status"], "todo")
        self.assertIn("T-001 AI 分工共识", fx)
        B.apply_status(self.b, "gpt", {"task_updates": [{"id": "T-001", "status": "done"}]}, M)
        self.assertEqual(t["status"], "todo")  # 不是负责人，忽略
        B.apply_status(self.b, "gemini", {"task_updates": [{"id": "T-001", "status": "进行中", "note": "开始了"}]}, M)
        self.assertEqual(t["status"], "doing")
        B.apply_status(self.b, "gemini", {"task_updates": {"id": "T-001", "status": "done"}}, M)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["notes"][0]["text"], "开始了")

    def test_proposed_task_cannot_be_updated(self):
        B.apply_status(self.b, "claude", {"propose_tasks": [{"title": "A", "owner": "claude"}]}, M)
        B.apply_status(self.b, "claude", {"task_updates": [{"id": "T-001", "status": "done"}]}, M)
        self.assertEqual(self.b["tasks"][0]["status"], "proposed")

    def test_invalid_owner_becomes_unassigned(self):
        B.apply_status(self.b, "claude", {"propose_tasks": [{"title": "A", "owner": "copilot"}]}, M)
        self.assertEqual(self.b["tasks"][0]["owner"], "unassigned")

    def test_withdraw_only_by_proposer(self):
        B.apply_status(self.b, "claude", {"propose_decisions": ["旧方案"]}, M)
        B.apply_status(self.b, "gpt", {"withdraw": ["D-001"]}, M)
        self.assertEqual(self.b["decisions"][0]["status"], "discussing")
        fx = B.apply_status(self.b, "claude", {"withdraw": ["D-001"], "propose_decisions": ["新方案"]}, M)
        self.assertEqual(self.b["decisions"][0]["status"], "rejected")
        self.assertTrue(self.b["decisions"][0]["withdrawn"])
        self.assertEqual(self.b["decisions"][1]["text"], "新方案")
        self.assertIn("撤回 D-001", fx)

    def test_votes_on_closed_items_ignored(self):
        B.apply_status(self.b, "claude", {"propose_decisions": ["X"]}, ["claude"])
        self.assertEqual(self.b["decisions"][0]["status"], "discussing")
        B.user_action(self.b, "force", {"id":"D-001"}, ["claude"])
        self.assertEqual(self.b["decisions"][0]["confirmed_by"], "user")
        fx = B.apply_status(self.b, "gpt", {"votes": [{"id": "D-001", "vote": "disagree", "reason": "晚了"}]}, M)
        self.assertEqual(fx, [])

    def test_recheck_after_member_leaves(self):
        B.apply_status(self.b, "claude", {"propose_decisions": ["X"]}, M)
        B.apply_status(self.b, "gpt", {"votes": [{"id": "D-001", "vote": "agree"}]}, M)
        self.assertEqual(self.b["decisions"][0]["status"], "discussing")
        msgs = B.recheck_all(self.b, ["claude", "gpt"])
        self.assertEqual(self.b["decisions"][0]["status"], "effective")
        self.assertTrue(msgs)

    def test_need_user(self):
        B.apply_status(self.b, "gpt", {"need_user": "预算上限是多少？"}, M)
        B.apply_status(self.b, "gemini", {"need_user": "无"}, M)
        self.assertEqual(len(self.b["need_user"]), 1)
        self.assertEqual(self.b["need_user"][0]["member"], "gpt")

    def test_garbage_status_is_harmless(self):
        self.assertEqual(B.apply_status(self.b, "gpt", ["not", "a", "dict"], M), [])
        self.assertEqual(B.apply_status(self.b, "gpt", {"votes": "agree", "propose_tasks": 5}, M), [])


class UserActions(unittest.TestCase):
    def setUp(self):
        self.b = B.new_board("目标")
        B.apply_status(self.b, "claude", {"propose_decisions": ["方案 B"]}, M)
        B.apply_status(self.b, "gpt", {"votes": [{"id": "D-001", "vote": "agree"}]}, M)
        B.apply_status(self.b, "gemini", {"votes": [{"id": "D-001", "vote": "agree"}]}, M)

    def test_comment_sends_item_back(self):
        d = self.b["decisions"][0]
        self.assertEqual(d["status"], "effective")
        result, note = B.user_action(self.b, "comment", {"id": "D-001", "text": "预算要减半"}, M)
        self.assertEqual(d["status"], "discussing")
        self.assertEqual(d["votes"], {})
        self.assertTrue(d["user_notes"][0]["pending"])
        self.assertIn("对 D-001 的意见", note)
        md = B.render_markdown(self.b, M)
        self.assertIn("【群主意见·工具标注】：预算要减半", md)
        self.assertIn("Claude 未表态", md)
        for m in M:
            B.apply_status(self.b, m, {"votes": [{"id": "D-001", "vote": "agree"}]}, M)
        self.assertEqual(d["status"], "effective")
        self.assertFalse(d["user_notes"][0]["pending"])

    def test_force_reject_reopen(self):
        B.apply_status(self.b, "claude", {"propose_tasks": [{"title": "写文档", "owner": "gpt"}]}, M)
        B.user_action(self.b, "force", {"id": "T-001"}, M)
        self.assertEqual(self.b["tasks"][0]["status"], "todo")
        B.user_action(self.b, "reject", {"id": "D-001"}, M)
        self.assertEqual(self.b["decisions"][0]["status"], "rejected")
        B.user_action(self.b, "reopen", {"id": "D-001"}, M)
        self.assertEqual(self.b["decisions"][0]["status"], "discussing")

    def test_user_items_take_effect_immediately(self):
        B.user_action(self.b, "add_decision", {"text": "只做网页版"}, M)
        self.assertEqual(self.b["decisions"][-1]["status"], "effective")
        B.user_action(self.b, "add_task", {"title": "联系客户", "owner": "user"}, M)
        self.assertEqual((self.b["tasks"][-1]["status"], self.b["tasks"][-1]["owner"]), ("todo", "user"))

    def test_resolve_need(self):
        B.apply_status(self.b, "gpt", {"need_user": "预算？"}, M)
        result, note = B.user_action(self.b, "resolve_need", {"id": "N-001", "text": "一万"}, M)
        self.assertTrue(self.b["need_user"][0]["resolved"])
        self.assertIn("一万", note)

    def test_errors(self):
        with self.assertRaises(ValueError):
            B.user_action(self.b, "comment", {"id": "D-001", "text": " "}, M)
        with self.assertRaises(ValueError):
            B.user_action(self.b, "force", {"id": "D-999"}, M)
        with self.assertRaises(ValueError):
            B.user_action(self.b, "launch_rockets", {"id": "D-001"}, M)

    def test_changelog_is_capped(self):
        for i in range(400):
            B.log(self.b, "user", "x%d" % i)
        self.assertEqual(len(self.b["changelog"]), B.CHANGELOG_LIMIT)
        self.assertEqual(self.b["changelog"][-1]["text"], "x399")


if __name__ == "__main__":
    unittest.main()
