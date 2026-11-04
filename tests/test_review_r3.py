"""U: durable successful boundaries and bounded unread context; synthetic adapters only."""
import sys
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_engine
from aigc.engine import Engine
from aigc.store import Store
from aigc.members import CancelToken,MemberError
from aigc import prompts,board


def member_prompt(registry, member):
    return next(p for p in reversed(registry.prompts_of(member)) if '## 本轮群主消息' in p)


def full(prompt):
    return prompt.split('## 本轮群主消息（全文，不计入历史预算）',1)[1].split('## 现在轮到你发言',1)[0]


class ContextProgress(unittest.TestCase):
    setUp=test_engine.EngineTest.setUp
    tearDown=test_engine.EngineTest.tearDown
    wait=test_engine.EngineTest.wait

    def restart(self):
        self.engine.shutdown()
        self.store=Store(Path(self.tmp))
        self.reg=test_engine.ScriptedRegistry()
        self.engine=Engine(self.store,self.reg)

    def test_restart_and_new_member_do_not_resend_summarized_history(self):
        self.engine.update_group(self.gid,{'members':['claude'],'auto_rounds':0})
        for i in range(30):
            self.engine.post_user_message(self.gid,f'OLD_{i:02d}_'+'甲'*2980+f'_TAIL_{i:02d}')
            self.wait()
        self.assertGreater(self.store.load_board(self.gid)['summary_upto'],0)
        before=self.engine.model_preview(self.gid,'same sized new question')['routes']['claude']['complexity']['context_chars']
        progress=self.store.member_context(self.gid)['claude']['seen']
        self.restart()
        self.assertEqual(self.engine.runner(self.gid).seen['claude'],progress)
        self.engine.post_user_message(self.gid,'same sized new question');self.wait()
        after=member_prompt(self.reg,'claude')
        self.assertLessEqual(abs(len(after)-before),before*.1)
        self.assertNotIn('OLD_',full(after))
        self.engine.update_group(self.gid,{'members':['claude','gpt']})
        self.engine.post_user_message(self.gid,'same sized new question');self.wait()
        self.assertNotIn('OLD_',full(member_prompt(self.reg,'gpt')))

    def test_first_cancel_persists_anchor_for_all_members_across_restart(self):
        self.engine.update_group(self.gid,{'auto_rounds':0})
        self.reg.delays['claude']=3
        text='甲'*3000+'CANCELLED_TAIL'
        self.engine.post_user_message(self.gid,text)
        deadline=time.time()+3
        while not self.reg.prompts and time.time()<deadline:time.sleep(.01)
        self.engine.stop(self.gid);self.wait()
        progress=self.store.member_context(self.gid)
        self.assertEqual(set(progress),{'claude','gpt','gemini'})
        self.assertTrue(all('seen' not in v and v['unread_from']==1 for v in progress.values()))
        self.restart()
        self.engine.post_user_message(self.gid,'follow the earlier request');self.wait()
        for m in ('claude','gpt','gemini'):self.assertIn(text,member_prompt(self.reg,m))

    def test_queued_merged_triggers_survive_summary_advancement(self):
        self.engine.update_group(self.gid, {'members': ['claude'], 'auto_rounds': 0})
        entered, release = threading.Event(), threading.Event()
        snapshots = []

        def respond(prompt):
            if '书记员' in prompt:
                return 'synthetic summary'
            snapshots.append((prompt, self.store.load_board(self.gid)['summary_upto']))
            if len(snapshots) == 1:
                entered.set()
                if not release.wait(10):
                    raise MemberError('test release timed out')
            return 'synthetic reply' + test_engine.status()

        self.reg.behaviors['claude'] = respond
        self.engine.post_user_message(self.gid, 'first request')
        try:
            self.assertTrue(entered.wait(3))
            requests = [f'QUEUED_{i:02d}_' + '甲' * 2980 + f'_TAIL_{i:02d}' for i in range(30)]
            for text in requests:
                self.engine.post_user_message(self.gid, text)
            self.assertEqual(self.engine.state(self.gid)['queued'], 1)
        finally:
            release.set()
        self.wait()
        self.assertEqual(len(snapshots), 2)
        prompt, summary_upto = snapshots[1]
        self.assertGreater(summary_upto, 2, 'the first round must summarize queued requests before execution')
        for text in requests:
            self.assertIn(text, full(prompt))
        self.assertIn('本轮触发消息及必需上下文本身超过60000', prompt)

    def seed_backlog(self):
        self.engine.update_group(self.gid,{'members':['claude'],'auto_rounds':0})
        self.store.append_message(self.gid,{'kind':'user','author':'user','text':'seen seed'})
        self.store.complete_member_context(self.gid,'claude',1,None)
        for i in range(30):self.store.append_message(self.gid,{'kind':'user','author':'user','text':f'BACK_{i:02d}'+'甲'*19980})

    def test_failure_budget_does_not_advance_seen_and_survives_restart(self):
        self.seed_backlog();self.restart()
        def fail(p):
            self.assertLessEqual(len(p),prompts.MEMBER_CONTEXT_TARGET)
            raise MemberError('synthetic context failure')
        self.reg.behaviors['claude']=fail
        sizes=[]
        for _ in range(3):
            self.engine.post_user_message(self.gid,'same new instruction');self.wait()
            p=member_prompt(self.reg,'claude');sizes.append(len(full(p)))
            self.assertIn('未读全文预算不足',p)
            self.assertEqual(self.store.member_context(self.gid)['claude']['seen'],1)
        self.assertLessEqual(max(sizes)-min(sizes),10)
        self.restart();self.assertEqual(self.engine.runner(self.gid).seen['claude'],1)

    def test_success_retains_deferred_backlog_instead_of_marking_it_read(self):
        self.seed_backlog()
        runner=self.engine.runner(self.gid)
        trigger=self.store.append_message(self.gid,{'kind':'user','author':'user','text':'new trigger'})
        runner._trigger_seq=trigger['seq']
        self.assertTrue(runner._speak('claude','reply',CancelToken()))
        first=self.store.member_context(self.gid)['claude']
        self.assertEqual(first['seen'],trigger['seq'])
        self.assertGreater(first['unread_from'],1)
        expected=next(m['text'] for m in self.store.messages(self.gid) if m['seq']==first['unread_from'])
        self.restart();runner=self.engine.runner(self.gid)
        next_trigger=self.store.append_message(self.gid,{'kind':'user','author':'user','text':'next trigger'})
        runner._trigger_seq=next_trigger['seq']
        self.assertTrue(runner._speak('claude','reply',CancelToken()))
        self.assertIn(expected,member_prompt(self.reg,'claude'))
        self.assertGreater(self.store.member_context(self.gid)['claude']['unread_from'],first['unread_from'])

    def test_failed_progress_write_does_not_advance_memory_or_disk(self):
        self.engine.post_user_message(self.gid,'@claude first');self.wait()
        seen=self.engine.runner(self.gid).seen['claude']
        with patch.object(self.store,'complete_member_context',side_effect=OSError('synthetic progress write')):
            self.engine.post_user_message(self.gid,'@claude next');self.wait()
        self.assertEqual(self.engine.runner(self.gid).seen['claude'],seen)
        self.assertEqual(self.store.member_context(self.gid)['claude']['seen'],seen)
        self.assertNotIn('claude',self.engine.incomplete_members(self.gid))
        self.assertTrue(any('进度未保存' in m['text'] for m in self.store.messages(self.gid)))
        with self.assertRaises(ValueError):self.engine.retry_member(self.gid,'claude')

    def test_legacy_and_invalid_progress_fall_back_to_current_trigger(self):
        for i in range(30):self.store.append_message(self.gid,{'kind':'user','author':'user','text':'old '+str(i)+'甲'*3000})
        for bad in (None,{'claude':{'seen':999999,'unread_from':True}},['bad']):
            group=self.store.load_group(self.gid);group['member_context']=bad;self.store.save_group(group)
            self.restart();self.engine.update_group(self.gid,{'members':['claude'],'auto_rounds':0})
            self.engine.post_user_message(self.gid,'current legacy question');self.wait()
            self.assertNotIn('old ',full(member_prompt(self.reg,'claude')))


class PromptBounds(unittest.TestCase):
    def test_summary_floor_and_preview_context(self):
        rows=[{'seq':i,'author':'user','kind':'user','text':f'OLD_{i:02d}'+'甲'*3000} for i in range(1,31)]
        b=board.new_board();b['summary_upto']=20
        p=prompts.build_member_prompt({},b,rows,'gpt','reply',{},trigger_seq=30,seen_seq=1,unread_from=2)
        for i in range(1,21):self.assertNotIn(f'OLD_{i:02d}',full(p))
        self.assertLessEqual(len(p),prompts.MEMBER_CONTEXT_TARGET)

    def test_merged_trigger_batch_is_not_truncated_by_old_unread_budget(self):
        rows=[{'seq':i,'author':'user','kind':'user','text':f'TRIGGER_{i}'+'甲'*19980} for i in range(1,5)]
        p=prompts.build_member_prompt({},board.new_board(),rows,'gpt','reply',{},trigger_seq=1,seen_seq=0)
        for row in rows:self.assertIn(row['text'],p)
        self.assertIn('本轮触发消息及必需上下文本身超过60000',p)

    def test_unread_long_instruction_precedes_ai_history_in_budget(self):
        old='OLD_'+'甲'*19980+'TAIL'
        new='NEW_'+'乙'*19980+'TAIL'
        rows=[{'seq':1,'author':'user','kind':'user','text':old}]
        rows += [{'seq':i,'author':'gpt','kind':'chat','text':'AI'*1500} for i in range(2,26)]
        rows.append({'seq':26,'author':'user','kind':'user','text':new})
        p=prompts.build_member_prompt({},board.new_board(),rows,'claude','reply',{},trigger_seq=26,seen_seq=0,unread_from=1)
        self.assertIn(old,p);self.assertIn(new,p)
        self.assertLessEqual(len(p),prompts.MEMBER_CONTEXT_TARGET)

    def test_json_expansion_of_current_trigger_is_full_and_old_backlog_deferred(self):
        rows=[{'seq':1,'author':'user','kind':'user','text':'old'*10000},
              {'seq':2,'author':'user','kind':'user','text':'START'+'\x01'*19980+'END'}]
        state={}
        p=prompts.build_member_prompt({},board.new_board(),rows,'gpt','reply',{},trigger_seq=2,seen_seq=0,context_state=state)
        self.assertIn('START'+r'\u0001'*19980+'END',p)
        self.assertEqual(state['deferred_from'],1)
        self.assertNotIn(rows[0]['text'],p)

if __name__=='__main__':unittest.main(verbosity=2)
