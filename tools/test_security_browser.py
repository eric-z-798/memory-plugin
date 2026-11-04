"""Isolated browser capability bootstrap, refresh/export and explicit user confirmation."""
import asyncio
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aigc import board
from aigc.store import Store
from aigc.engine import Engine
from aigc.members import Registry
from aigc.server import App,make_server
from playwright.async_api import async_playwright,expect

async def main():
    with tempfile.TemporaryDirectory(prefix='xingshu-security-browser-') as directory:
        store=Store(Path(directory));registry=Registry(demo=True,demo_delay=.01);engine=Engine(store,registry)
        group=store.create_group('synthetic confirmation',members=['claude','gpt']);gid=group['id']
        b=store.load_board(gid)
        board.apply_status(b,'claude',{'propose_decisions':['synthetic consensus']},['claude','gpt'])
        board.apply_status(b,'gpt',{'votes':[{'id':'D-001','vote':'agree'}]},['claude','gpt'])
        store.save_board(gid,b)
        single=store.create_group('synthetic single',members=['claude'])['id']
        b=store.load_board(single);board.apply_status(b,'claude',{'propose_decisions':['synthetic pending']},['claude']);store.save_board(single,b)
        app=App(store,engine,registry,demo=True);server=make_server('127.0.0.1',0,app)
        threading.Thread(target=server.serve_forever,daemon=True).start();base=f'http://127.0.0.1:{server.server_address[1]}'
        try:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                context=await browser.new_context();page=await context.new_page();errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                response=await context.request.get(base+'/api/settings');assert response.status==401
                await page.goto(base+'/?view=board#token='+app.access_token+'&group='+gid)
                await expect(page.locator('#item-D-001 .state')).to_have_text('AI 共识，未经群主确认')
                assert 'token=' not in page.url and app.access_token not in page.url
                await page.reload()
                await expect(page.locator('#item-D-001 .state')).to_have_text('AI 共识，未经群主确认')
                await page.locator('#item-D-001').get_by_role('button',name='群主确认',exact=True).click()
                await expect(page.locator('#item-D-001 .state')).to_have_text('群主已确认')
                assert store.load_board(gid)['decisions'][0]['confirmed_by']=='user'
                await page.locator('#btn-more').click()
                async with page.expect_download() as pending:
                    await page.get_by_role('menuitem',name='导出聊天记录').click()
                download=await pending.value;content=Path(await download.path()).read_text()
                assert '群主已确认' in content
                await page.goto(base+'/?view=board&case=single#'+single)
                await expect(page.locator('#item-D-001 .state')).to_have_text('讨论中')
                await page.locator('#item-D-001').get_by_role('button',name='群主确认',exact=True).click()
                await expect(page.locator('#item-D-001 .state')).to_have_text('群主已确认')
                other=await browser.new_context()
                assert (await other.request.get(base+'/api/settings')).status==401
                # Blocked storage still clears the URL and permits this in-memory session.
                private=await browser.new_context();private_page=await private.new_page()
                await private_page.add_init_script("Object.defineProperty(window,'sessionStorage',{get(){throw new Error('synthetic storage blocked')}})")
                await private_page.goto(base+'/#token='+app.access_token+'&group='+gid)
                await expect(private_page.locator('#input')).to_be_editable()
                assert app.access_token not in private_page.url
                assert not errors,errors
                print(json.dumps({'unauthorized_rejected':True,'fragment_removed':True,'refresh_authorized':True,'authenticated_export':True,'ai_vs_user_confirmation':True,'single_requires_user':True,'storage_blocked_memory_session':True,'pageerrors':errors}))
                await browser.close()
        finally:engine.shutdown();server.shutdown();server.server_close()

if __name__=='__main__':asyncio.run(main())
