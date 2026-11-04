"""Synthetic same-document startup links, stale response isolation and write warning."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aigc.store import Store
from aigc.engine import Engine
from aigc.members import Registry
from aigc.server import App, make_server
from playwright.async_api import async_playwright, expect

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        store=Store(Path(tmp)); reg=Registry(demo=True,demo_delay=.01); engine=Engine(store,reg)
        gid=store.create_group('synthetic',members=['claude'])['id']
        app=App(store,engine,reg,demo=True); app.access_token='synthetic-A'
        server=make_server('127.0.0.1',0,app)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_address[1]}/'
        try:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                for blocked, stale_status in ((False,200),(False,500),(True,200),(True,500)):
                    app.access_token='synthetic-A'
                    context=await browser.new_context(); page=await context.new_page(); errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    if blocked:
                        await page.add_init_script("Object.defineProperty(window,'sessionStorage',{get(){throw Error('synthetic blocked')}})")
                    await page.goto(base+'#token=synthetic-A&group='+gid)
                    await expect(page.locator('#input')).to_be_editable()
                    await page.locator('#input').fill('synthetic preserved draft')
                    entered=asyncio.Event(); release=asyncio.Event()
                    async def late(route):
                        entered.set(); await release.wait()
                        try: await route.fulfill(status=stale_status,json={'message':{'id':'stale','seq':999,'kind':'user','author':'user','text':'STALE RESPONSE'}})
                        except Exception: pass # Navigation aborts the old request.
                    await page.route('**/api/groups/*/messages',late)
                    await page.locator('#input').press('Enter'); await asyncio.wait_for(entered.wait(),5)
                    app.access_token='synthetic-B'
                    await page.goto(base+'#token=synthetic-B&group='+gid)
                    await expect(page.locator('#input')).to_be_editable()
                    await expect(page.locator('#input')).to_have_value('synthetic preserved draft')
                    release.set(); await page.wait_for_timeout(300)
                    await expect(page.locator('#input')).to_have_value('synthetic preserved draft')
                    assert 'token=' not in page.url and 'synthetic-B' not in page.url
                    assert await page.get_by_text('STALE RESPONSE',exact=True).count()==0
                    if not blocked: assert await page.evaluate('sessionStorage.getItem("xingshu.accessToken")')=='synthetic-B'
                    assert not errors,errors
                    await context.close()
                # Complete user append with failed fsync must not look like a
                # rejected send, nor silently start a model call.
                context=await browser.new_context();page=await context.new_page()
                await page.goto(base+'#token=synthetic-B&group='+gid)
                await expect(page.locator('#input')).to_be_editable()
                original=os.fsync; fired=[]
                def fail(fd):
                    path=store.group_dir(gid)/'messages.jsonl'
                    if path.exists() and os.fstat(fd).st_ino==path.stat().st_ino and not fired:
                        if b'"kind": "user"' in path.read_bytes():
                            fired.append(True);raise OSError('synthetic browser fsync failure')
                    return original(fd)
                with patch('aigc.store.os.fsync',side_effect=fail):
                    await page.locator('#input').fill('synthetic uncertain send')
                    async with page.expect_response('**/messages') as response:
                        await page.locator('#input').press('Enter')
                    assert (await response.value).status==202
                    await expect(page.locator('#input')).to_have_value('')
                    await expect(page.get_by_text('耐久性未知',exact=False).first).to_be_visible()
                assert fired and not engine.state(gid)['running']
                assert sum(m['kind']=='user' for m in store.messages(gid))==1
                assert not any(m['kind']=='chat' for m in store.messages(gid))
                print(json.dumps({'hashchange':True,'fragment_removed':True,'storage_blocked':True,'stale_send_isolated':True,'draft_preserved':True,'uncertain_user_write_202_visible':True}))
                await browser.close()
        finally:
            engine.shutdown();server.shutdown();server.server_close()
if __name__=='__main__':asyncio.run(main())
