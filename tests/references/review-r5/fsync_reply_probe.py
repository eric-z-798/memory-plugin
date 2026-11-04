import sys, os
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = ['/workspace/memory-plugin', '/workspace/memory-plugin/tests']
import test_engine
from aigc.store import Store
t = test_engine.EngineTest()
t.setUp()
try:
    t.engine.update_group(t.gid, {'members': ['claude'], 'auto_rounds': 0})
    real = os.fsync
    fired = []
    def fail(fd):
        target = os.readlink('/proc/self/fd/' + str(fd))
        if target.endswith('/messages.jsonl') and not fired:
            raw = Path(target).read_bytes()
            if b'"kind": "chat"' in raw:
                fired.append(True)
                raise OSError('synthetic fsync error after full chat write/flush')
        return real(fd)
    with patch('aigc.store.os.fsync', side_effect=fail):
        t.engine.post_user_message(t.gid, 'synthetic request')
        t.wait()
    rows = Store(Path(t.tmp)).messages(t.gid)
    before = [m for m in rows if m.get('kind') == 'chat']
    print('fsync injection fired', bool(fired), 'complete chats reread from disk', len(before), 'pending', t.engine.incomplete_members(t.gid), flush=True)
    t.engine.retry_member(t.gid, 'claude')
    t.wait()
    after = [m for m in Store(Path(t.tmp)).messages(t.gid) if m.get('kind') == 'chat']
    print('after retry complete chats', len(after), 'model calls', len(t.reg.prompts), flush=True)
    assert len(after) == 1, 'complete persisted reply must not be offered a duplicate model retry'
finally:
    t.tearDown()
