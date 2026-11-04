"""Deterministic slow-load, stale-response and draft isolation regressions; demo only."""
from qa_auth import BOOTSTRAP, TOKEN, urlopen as qa_urlopen, authenticate, authenticate_async
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from playwright.async_api import async_playwright, expect

ROOT=Path(__file__).resolve().parents[1]

async def main():
    with tempfile.TemporaryDirectory(prefix='xingshu-r3-') as data:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        proc=subprocess.Popen([sys.executable,'-c', BOOTSTRAP,'--demo','--no-browser','--port',str(port),'--data-dir',data],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:qa_urlopen(base+'/api/info').close();break
                except OSError:await asyncio.sleep(.1)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                ctx=await browser.new_context(viewport={'width':1440,'height':900})
                async def api(method,path,body=None):
                    response=await ctx.request.fetch(base+path,method=method,headers={'X-AIGC':'1','X-Xingshu-Token':TOKEN},data=body)
                    assert response.ok,(response.status,await response.text())
                    return await response.json()
                a=(await api('POST','/api/groups',{'name':'R3-A','members':['claude'],'auto_rounds':0}))['group']['id']
                b=(await api('POST','/api/groups',{'name':'R3-B','members':['claude'],'auto_rounds':0}))['group']['id']
                page=await ctx.new_page();errors=[]
                await authenticate_async(page)
                page.on('pageerror',lambda e:errors.append(str(e)))
                draft_a='A saved draft\nsecond line\nthird line\nfourth line'
                await page.add_init_script('localStorage.setItem('+json.dumps('aigc.draft.'+a)+','+json.dumps(draft_a)+');localStorage.setItem('+json.dumps('aigc.draft.'+b)+',"B saved draft");')
                input_=page.locator('#input')
                async def ready():
                    await expect(page.locator('#composer')).to_have_attribute('aria-busy','false')
                    await expect(input_).to_be_editable()
                async def select(name):
                    await page.locator('.group-item').filter(has=page.locator('.g-name',has_text=name)).click()
                entered=asyncio.Event();done=asyncio.Event()
                async def delay(route):
                    response=await route.fetch();entered.set()
                    await asyncio.sleep(1.5)
                    await route.fulfill(response=response);done.set()
                await page.route(base+'/api/groups/'+a,delay)
                await page.goto(base+'/#'+a);await entered.wait()
                await expect(input_).to_have_value(draft_a)
                assert await input_.evaluate('e=>e.readOnly')
                await expect(page.locator('#btn-send')).to_be_disabled()
                await expect(page.locator('[data-at="Claude"]')).to_be_disabled()
                await input_.focus();await page.keyboard.type('must not overwrite saved draft')
                await page.keyboard.press('Enter')
                await done.wait();await ready()
                await expect(input_).to_have_value(draft_a)
                assert await input_.evaluate('e=>e.scrollHeight<=e.clientHeight+2'), 'multiline restored draft must autosize'
                await page.unroute(base+'/api/groups/'+a,delay)
                await input_.fill('A edited draft');await select('R3-B');await ready()
                await expect(input_).to_have_value('B saved draft')
                await input_.fill('B edited draft');await select('R3-A');await ready()
                await expect(input_).to_have_value('A edited draft')
                # A stale 404 must not close B or replace its restored draft.
                await select('R3-B');await ready()
                entered=asyncio.Event();release=asyncio.Event();done=asyncio.Event()
                async def stale_error(route):
                    entered.set();await release.wait()
                    await route.fulfill(status=404,content_type='application/json',body='{"error":"synthetic stale 404"}');done.set()
                await page.route(base+'/api/groups/'+a,stale_error)
                await select('R3-A');await entered.wait()
                await select('R3-B');await ready();release.set();await done.wait();await asyncio.sleep(.1)
                await expect(page.locator('#group-title')).to_have_text('R3-B')
                await expect(input_).to_have_value('B edited draft')
                await page.unroute(base+'/api/groups/'+a,stale_error)
                # Two overlapping loads of the same group: only newest epoch wins.
                entered=asyncio.Event();release=asyncio.Event();done=asyncio.Event();requests=[]
                async def stale_success(route):
                    response=await route.fetch();requests.append(True)
                    if len(requests)==1:
                        body=await response.json();body['group']['name']='STALE TITLE'
                        entered.set();await release.wait()
                        await route.fulfill(status=200,content_type='application/json',body=json.dumps(body));done.set()
                    else:await route.fulfill(response=response)
                await page.route(base+'/api/groups/'+a,stale_success)
                await select('R3-A');await entered.wait();await select('R3-B');await ready()
                await select('R3-A');await ready();release.set();await done.wait();await asyncio.sleep(.1)
                await expect(page.locator('#group-title')).to_have_text('R3-A')
                await page.unroute(base+'/api/groups/'+a,stale_success)
                # A prior epoch's poll error must not close a freshly reopened same group.
                entered=asyncio.Event();release=asyncio.Event();done=asyncio.Event()
                async def stale_poll(route):
                    entered.set();await release.wait()
                    await route.fulfill(status=404,content_type='application/json',body='{"error":"stale poll"}');done.set()
                await page.route(base+'/api/groups/'+a+'/poll**',stale_poll,times=1)
                await asyncio.wait_for(entered.wait(),10)
                await select('R3-B');await ready();await select('R3-A');await ready()
                release.set();await done.wait();await asyncio.sleep(.15)
                await expect(page.locator('#group-title')).to_have_text('R3-A')
                await expect(input_).to_be_editable()
                # Delayed sends cannot clear a newer draft in this group or another group.
                for switch in (False,True):
                    if switch:await select('R3-A');await ready()
                    await input_.fill('outgoing synthetic '+str(switch))
                    entered=asyncio.Event();release=asyncio.Event();done=asyncio.Event();sent=[]
                    async def held_post(route):
                        sent.append(True);response=await route.fetch();entered.set();await release.wait()
                        await route.fulfill(response=response);done.set()
                    await page.route(base+'/api/groups/'+a+'/messages',held_post)
                    await page.locator('#btn-send').click();await entered.wait()
                    if switch:await select('R3-B');await ready()
                    await input_.fill('new draft must survive '+str(switch))
                    if not switch:
                        await page.keyboard.press('Enter')
                        await expect(page.locator('#btn-send')).to_be_disabled()
                    release.set();await done.wait();await asyncio.sleep(.15)
                    await expect(input_).to_have_value('new draft must survive '+str(switch))
                    assert len(sent)==1
                    await page.unroute(base+'/api/groups/'+a+'/messages',held_post)
                assert not errors,errors
                print(json.dumps({'delayed_load_seconds':1.5,'readonly_and_buttons':True,'draft_switch':True,'stale_404':True,'same_group_stale_success':True,'stale_poll_error':True,'same_group_new_edit':True,'cross_group_delayed_send':True,'page_errors':errors},indent=2))
                await browser.close()
        finally:proc.terminate();proc.wait(timeout=10)

asyncio.run(main())
