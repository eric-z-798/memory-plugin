"""Sequence recovery must survive decimal conversion and carry boundaries."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aigc.store import Store, StoreError

SAFE = 2 ** 53 - 1

class SequenceRecovery(unittest.TestCase):
    def test_original_4300_digit_repro(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp)); gid = s.create_group('synthetic')['id']
            path = s.group_dir(gid) / 'messages.jsonl'
            path.write_bytes(b'{"seq":' + b'9' * 4300 + b',"text":')
            Store(Path(tmp)).messages(gid)

    def test_digit_matrix_preserves_bytes_neighbors_and_two_restarts(self):
        before_limit = sys.get_int_max_str_digits() if hasattr(sys, 'get_int_max_str_digits') else None
        for digits in (4299, 4300, 4301, 5000):
            for complete in (False, True):
                with self.subTest(digits=digits,complete=complete), tempfile.TemporaryDirectory() as tmp:
                    s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
                    first=s.append_message(gid,{'kind':'user','author':'user','text':'valid before'})
                    path=s.group_dir(gid)/'messages.jsonl'
                    bad=b'{"seq":'+b'9'*digits+b',"text":'+(b'"unsupported"}\n' if complete else b'\n')
                    second={'seq':2,'kind':'user','author':'user','text':'valid after','ts':0}
                    with path.open('ab') as f:
                        f.write(bad);f.write((json.dumps(second)+'\n').encode())
                    original=path.read_bytes();s=Store(Path(tmp));messages=s.messages(gid)
                    self.assertEqual(messages[:2],[first,second])
                    notice=messages[-1]
                    self.assertEqual(notice['seq'],3)
                    self.assertEqual(notice['recovery']['ignored_sequences'],[{'line':2,'digits':digits,'reason':'outside_exact_sequence_range'}])
                    self.assertIn('未采用',notice['text'])
                    self.assertTrue(any(p.read_bytes()==bad for p in path.parent.glob('messages.corrupt-*')))
                    self.assertTrue(any(p.read_bytes()==original for p in path.parent.glob('messages.backup-*')))
                    for turn in range(2):
                        new=s.append_message(gid,{'kind':'user','author':'user','text':'new '+str(turn)})
                        messages.append(new)
                        self.assertEqual(new['seq'],4+turn)
                        s=Store(Path(tmp))
                        self.assertEqual(s.messages(gid),messages)
                        self.assertFalse(s.list_groups()[0].get('error'))
        self.assertEqual(sys.get_int_max_str_digits() if hasattr(sys, 'get_int_max_str_digits') else None,before_limit)

    def test_normal_salvaged_sequence_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
            first=s.append_message(gid,{'text':'valid'})
            path=s.group_dir(gid)/'messages.jsonl'
            with path.open('ab') as f:f.write(b'{"seq":99,"text":')
            s=Store(Path(tmp));messages=s.messages(gid)
            self.assertEqual(messages[0],first)
            self.assertEqual(messages[-1]['seq'],100)
            for seq in (101,102):
                messages.append(s.append_message(gid,{'text':'new'}))
                self.assertEqual(messages[-1]['seq'],seq)
                s=Store(Path(tmp));self.assertEqual(s.messages(gid),messages)

    def test_safe_sequence_exhaustion_rejects_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
            path=s.group_dir(gid)/'messages.jsonl'
            raw=(json.dumps({'seq':SAFE,'text':'valid last','ts':0})+'\n').encode();path.write_bytes(raw)
            s=Store(Path(tmp));self.assertEqual(s.messages(gid)[0]['seq'],SAFE)
            with self.assertRaisesRegex(StoreError,'序号'):
                s.append_message(gid,{'text':'cannot allocate'})
            self.assertEqual(path.read_bytes(),raw)

    def test_full_safe_highwater_corrupt_tail_does_not_drop_valid_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
            path=s.group_dir(gid)/'messages.jsonl'
            raw=(json.dumps({'seq':SAFE,'text':'valid last','ts':0})+'\n').encode()+b'{broken'
            path.write_bytes(raw)
            with self.assertRaisesRegex(StoreError,'序号'):
                Store(Path(tmp)).messages(gid)
            self.assertEqual(path.read_bytes(),raw)
            self.assertTrue(any(p.read_bytes()==raw for p in path.parent.glob('messages.backup-*')))

    def test_exact_range_edges_and_escaped_key(self):
        for key in (b'"seq"', b'"\\u0073eq"'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
                path=s.group_dir(gid)/'messages.jsonl'
                raw=b'{'+key+b':'+str(SAFE+1).encode()+b',"text":"outside"}\n'
                path.write_bytes(raw)
                messages=Store(Path(tmp)).messages(gid)
                self.assertEqual(messages[0]['seq'],1)
                self.assertEqual(messages[0]['recovery']['ignored_sequences'],[{'line':1,'digits':16,'reason':'outside_exact_sequence_range'}])
                self.assertTrue(any(p.read_bytes()==raw for p in path.parent.glob('messages.corrupt-*')))
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));gid=s.create_group('synthetic')['id']
            path=s.group_dir(gid)/'messages.jsonl'
            path.write_bytes((json.dumps({'seq':SAFE-1,'text':'last but one','ts':0})+'\n').encode())
            s=Store(Path(tmp))
            self.assertEqual(s.append_message(gid,{'text':'last'})['seq'],SAFE)
            raw=path.read_bytes()
            with self.assertRaisesRegex(StoreError,'序号'):
                Store(Path(tmp)).append_message(gid,{'text':'overflow'})
            self.assertEqual(path.read_bytes(),raw)

if __name__ == '__main__':unittest.main(verbosity=2)
