"""Top-level message identity, strict JSON records and conservative recovery."""
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aigc.store import Store

class MessageIdentity(unittest.TestCase):
    def test_store_written_nested_seq_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
            original=s.append_message(gid,{'metadata':{'seq':2**53},'author':'user','kind':'user','text':'valid old record'})
            rows=Store(Path(tmp)).messages(gid)
            print('TOP_LEVEL_IDENTITY',original['seq'],[(m['seq'],m['kind']) for m in rows])
            self.assertIn(original,rows)
            self.assertEqual(rows,[original])

    def test_valid_key_order_nested_arrays_and_string_content(self):
        fragments=[('metadata',{'seq':2**53,'children':[{'seq':999}, {'deep':{'seq':2**53}}]}),
                   ('text','literal "seq":9007199254740992, braces { } and escaped \\"seq\\":88'),
                   ('seq',1)]
        for ordered in itertools.permutations(fragments):
            with self.subTest(order=[k for k,v in ordered]),tempfile.TemporaryDirectory() as tmp:
                s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
                entry=dict(ordered);entry.update(id='synthetic-original',author='user',kind='user',ts=0)
                path=s.group_dir(gid)/'messages.jsonl';raw=(json.dumps(entry,ensure_ascii=False)+'\n').encode();path.write_bytes(raw)
                for _ in range(2):self.assertEqual(Store(Path(tmp)).messages(gid),[entry])
                self.assertEqual(path.read_bytes(),raw)
                self.assertFalse(list(path.parent.glob('messages.corrupt-*')))
                s=Store(Path(tmp));self.assertEqual(s.append_message(gid,{'text':'new'})['seq'],2)

    def test_source_identity_and_bad_tail_preserve_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));group=s.create_group('synthetic');gid=group['id']
            original=s.append_message(gid,{'metadata':{'seq':2**53},'author':'user','kind':'user','text':'valid old record'})
            memory=s.memory.add(group['project_id'],'synthetic memory',source={'group_id':gid,'seq':original['seq'],'author':'user'})
            path=s.group_dir(gid)/'messages.jsonl'
            with path.open('ab') as f:f.write(b'{"seq":77,"text":')
            s=Store(Path(tmp));rows=s.messages(gid)
            self.assertEqual(rows[0],original);self.assertEqual(rows[-1]['seq'],78)
            self.assertEqual(s.memory.get(memory['id'])['source']['seq'],original['seq'])
            for expected in (79,80):
                rows.append(s.append_message(gid,{'text':'new'}));self.assertEqual(rows[-1]['seq'],expected)
                s=Store(Path(tmp));self.assertEqual(s.messages(gid),rows)

    def test_duplicate_keys_are_quarantined_and_all_root_ids_reserved(self):
        cases=[(b'{"seq":99,"seq":1,"text":"duplicate"}\n',99),
               (b'{"seq":1,"seq":99,"text":"duplicate"}\n',99),
               (b'{"seq":1,"seq":1,"text":"duplicate"}\n',1),
               (b'{"seq":1,"\\u0073eq":99,"text":"duplicate"}\n',99),
               (b'{"metadata":{"seq":9007199254740992,"seq":99},"seq":1,"text":"nested duplicate"}\n',1),
               (b'{"text":"first","text":"last","seq":1}\n',1)]
        for raw,high in cases:
            with self.subTest(raw=raw),tempfile.TemporaryDirectory() as tmp:
                s=Store(Path(tmp));gid=s.create_group('synthetic')['id'];path=s.group_dir(gid)/'messages.jsonl';path.write_bytes(raw)
                s=Store(Path(tmp));rows=s.messages(gid)
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['kind'],'system');self.assertEqual(rows[0]['seq'],high+1)
                self.assertFalse(rows[0]['recovery']['ignored_sequences'])
                self.assertTrue(any(p.read_bytes()==raw for p in path.parent.glob('messages.corrupt-*')))
                self.assertTrue(any(p.read_bytes()==raw for p in path.parent.glob('messages.backup-*')))
                for expected in (high+2,high+3):
                    rows.append(s.append_message(gid,{'text':'new'}));self.assertEqual(rows[-1]['seq'],expected)
                    s=Store(Path(tmp));self.assertEqual(s.messages(gid),rows)

    def test_malformed_prefix_parser_only_reserves_root_integer_tokens(self):
        cases=[(b'{"metadata":{"seq":9007199254740992},"seq":99,"text":',99),
               (b'{"text":"\\"seq\\":4444", "seq":99,"broken":',99),
               (b'{"seq":7,"seq":99,"text":',99),
               (b'{"seq":7,"metadata":{"seq":999},"broken":[ },"seq":99}',7),
               (b'{"metadata":{"bad":,"seq":777},"seq":99}',0),
               (b'{"seq":123.4,"text":',0),(b'{"seq":123xyz,"text":',0),
               (b'{"\\u0073eq":99,"text":',99),
               (b'{"seq":99,"text":"unterminated \\\"seq\\\":777',99),
               (b'{"metadata":[{"seq":9007199254740992}],"seq":99,"text":',99),
               (b'["seq",99]',0), (b'{0:0,"seq":99}',0),
               (b'{"broken":x,"seq":99}',0), (b'{"seq":99} junk {"seq":777}',99)]
        for raw,high in cases:
            with self.subTest(raw=raw),tempfile.TemporaryDirectory() as tmp:
                s=Store(Path(tmp));gid=s.create_group('synthetic')['id'];path=s.group_dir(gid)/'messages.jsonl';path.write_bytes(raw)
                s=Store(Path(tmp));rows=s.messages(gid)
                self.assertEqual(rows[0]['seq'],high+1)
                self.assertFalse(rows[0]['recovery']['ignored_sequences'])
                self.assertTrue(any(p.read_bytes()==raw for p in path.parent.glob('messages.corrupt-*')))
                self.assertEqual(s.append_message(gid,{'text':'new'})['seq'],high+2)

    def test_huge_nested_number_cannot_hide_later_root_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));gid=s.create_group('synthetic')['id'];path=s.group_dir(gid)/'messages.jsonl'
            raw=b'{"metadata":{"seq":'+b'9'*5000+b'},"seq":99,"text":'
            path.write_bytes(raw);rows=Store(Path(tmp)).messages(gid)
            self.assertEqual(rows[0]['seq'],100)
            self.assertFalse(rows[0]['recovery']['ignored_sequences'])

if __name__=='__main__':unittest.main(verbosity=2)
