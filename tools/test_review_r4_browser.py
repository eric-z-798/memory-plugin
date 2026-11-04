"""Real Chromium UI with synthetic storage failures and byte-preserving archive."""
import asyncio
from qa_auth import authenticate_async
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aigc.store import Store
from aigc.engine import Engine
from aigc.members import Registry
from aigc.server import App, make_server
from playwright.async_api import async_playwright, expect

async def main():
    with tempfile.TemporaryDirectory(prefix='xingshu-r4-') as data:
        store=Store(Path(data)); registry=Registry(demo=True,demo_delay=.01)
        engine=Engine(store,registry)
        app=App(store,engine,registry,demo=True)
        server=make_server('127.0.0.1',0,app)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_address[1]}'
        good=store.create_group('R4 healthy',members=['claude'])['id']
        engine.update_group(good,{'auto_rounds':0})
        bad=store.create_group('R4 broken')['id']; raw=b'{synthetic corrupt group\xff'
        (store.group_dir(bad)/'group.json').write_bytes(raw)
        try:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                page=await browser.new_page(viewport={'width':1440,'height':900}); errors=[]
                await authenticate_async(page,app.access_token)
                page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(base+'/#'+good)
                await expect(page.locator('#input')).to_be_editable()
                await page.locator('#input').fill('draft must survive unrelated archive')
                await page.locator('[data-archive-gid="'+bad+'"]').click()
                await page.get_by_role('button',name='移到 archive',exact=True).click()
                await expect(page.locator('[data-archive-gid="'+bad+'"]')).to_have_count(0)
                assert (store.archive_dir/bad/'group.json').read_bytes()==raw
                await expect(page.locator('#input')).to_have_value('draft must survive unrelated archive')
                # The same archive action works after opening the broken entry fails.
                bad2=store.create_group('second synthetic broken')['id']
                raw2=b'{second synthetic corruption'
                (store.group_dir(bad2)/'group.json').write_bytes(raw2)
                await page.reload()
                await expect(page.locator('#input')).to_be_editable()
                async with page.expect_response(lambda r: r.url == base+'/api/groups/'+bad2):
                    await page.locator('.group-item').filter(has_text='群数据需要恢复').click()
                await page.locator('[data-archive-gid="'+bad2+'"]').click()
                await page.get_by_role('button',name='移到 archive',exact=True).click()
                await expect(page.locator('[data-archive-gid="'+bad2+'"]')).to_have_count(0)
                assert (store.archive_dir/bad2/'group.json').read_bytes()==raw2
                await expect(page.locator('#input')).to_be_editable()
                await expect(page.locator('#input')).to_have_value('draft must survive unrelated archive')
                # Initial progress failure: input remains, no user message is committed.
                original=store.save_group
                def fail(group):
                    if group.get('member_context'): raise OSError('synthetic initial progress failure')
                    return original(group)
                with patch.object(store,'save_group',side_effect=fail):
                    async with page.expect_response(lambda r:r.request.method=='POST' and '/messages' in r.url) as response:
                        await page.locator('#btn-send').click()
                    assert (await response.value).status==500
                    await expect(page.locator('#input')).to_have_value('draft must survive unrelated archive')
                    assert not any(m['kind']=='user' for m in store.messages(good))
                await expect(page.locator('#btn-send')).to_be_enabled()
                with patch.object(store,'complete_member_context',side_effect=OSError('synthetic completion progress failure')):
                    await page.locator('#btn-send').click()
                    await expect(page.locator('#input')).to_have_value('')
                    for _ in range(100):
                        if not engine.state(good)['running'] and any(m['kind']=='chat' for m in store.messages(good)):break
                        await asyncio.sleep(.05)
                    await expect(page.get_by_text('已读进度未保存',exact=False)).to_be_visible(timeout=10000)
                await expect(page.get_by_role('button',name='补答此成员',exact=True)).to_have_count(0)
                assert sum(m['kind']=='chat' for m in store.messages(good))==1
                assert engine.incomplete_members(good)=={}
                # Normal UI deletion still archives the complete group.
                await page.locator('#btn-more').click()
                await page.get_by_role('menuitem',name='删除这个群').click()
                await page.get_by_role('button',name='删除',exact=True).click()
                await expect(page.locator('.group-item')).to_have_count(0)
                assert (store.archive_dir/good/'group.json').is_file()
                assert not errors,errors
                print(json.dumps({'corrupt_archive_bytes':True,'selected_corrupt_archive':True,'other_draft_preserved':True,'precommit_failure_no_message':True,'completed_reply_no_retry':True,'normal_delete':True,'pageerrors':errors}))
                await browser.close()
        finally:
            engine.shutdown();server.shutdown();server.server_close()

if __name__=='__main__':asyncio.run(main())
