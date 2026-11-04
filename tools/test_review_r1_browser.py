"""Real Chromium input/viewport regressions. Synthetic demo data only; no real CLI."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=Path(os.environ.get('XINGSHU_QA_OUTPUT', str(ROOT/'work'/'r1-browser')))
OUTPUT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='xingshu-r1-') as data:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    proc=subprocess.Popen([sys.executable,'run.py','--demo','--no-browser','--port',str(port),'--data-dir',data],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try: urllib.request.urlopen(base+'/api/info').close(); break
            except OSError: time.sleep(.1)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
            ctx=browser.new_context(**pw.devices['iPhone 13'])
            page=ctx.new_page(); errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            def api(method,path,body=None):
                r=page.request.fetch(base+path,method=method,headers={'X-AIGC':'1'},data=body)
                assert r.ok,(r.status,r.text())
                return r.json()
            g=api('POST','/api/groups',{'name':'R1 合成浏览器测试','first_message':'请讨论合成测试','auto_rounds':0})['group']['id']
            for _ in range(100):
                if not api('GET',f'/api/groups/{g}')['state']['running']: break
                time.sleep(.1)
            page.goto(base+'/#'+g)
            expect(page.locator('#composer')).to_have_attribute('aria-busy','false')
            expect(page.locator('#input')).to_be_editable()
            # The actual mention button inserts directly after Chinese text.
            before_messages=api('GET',f'/api/groups/{g}')['messages']
            before_seq=max(m['seq'] for m in before_messages)
            page.locator('#input').fill('请')
            page.locator('[data-at="Claude"]').click()
            expect(page.locator('#input')).to_have_value('请@Claude ')
            page.locator('#btn-send').click()
            for _ in range(100):
                state=api('GET',f'/api/groups/{g}')
                if any(m['seq']>before_seq and m.get('kind')=='chat' for m in state['messages']) and not state['state']['running']: break
                time.sleep(.1)
            assert [m['author'] for m in state['messages'] if m['seq']>before_seq and m.get('kind')=='chat']==['claude']
            ai=page.locator('.msg.ai .msg-memory').first
            ai.click()
            checkbox=page.get_by_label('我已核实，允许三家在后续对话中引用')
            expect(checkbox).not_to_be_checked()
            page.get_by_label('记忆内容',exact=True).fill('AI来源未主动确认合成记录')
            page.get_by_role('button',name='保存',exact=True).click()
            expect(page.locator('.memory-workspace')).to_be_visible()
            items=api('GET',f'/api/groups/{g}/memories')['memories']
            assert next(m for m in items if m['content']=='AI来源未主动确认合成记录')['status']=='pending'
            page.locator('.modal-head button[aria-label="关闭"]').click()
            ai.click(); checkbox.check()
            page.get_by_label('记忆内容',exact=True).fill('AI来源主动确认合成记录')
            page.get_by_role('button',name='保存',exact=True).click()
            expect(page.locator('.memory-workspace')).to_be_visible()
            items=api('GET',f'/api/groups/{g}/memories')['memories']
            assert next(m for m in items if m['content']=='AI来源主动确认合成记录')['status']=='confirmed'
            page.locator('.modal-head button[aria-label="关闭"]').click()
            # Exactly 41 graph nodes, including an explicitly marked demo candidate.
            for m in items: api('DELETE',f'/api/groups/{g}/memories/{m["id"]}',{'version':m['version']})
            for i in range(41):
                api('POST',f'/api/groups/{g}/memories',{'content':('（演示候选）' if i==0 else '合成记录')+str(i),'status':'pending' if i==0 else 'confirmed','kind':('fact','preference','decision','task','note')[i%5] if os.environ.get('XINGSHU_QA_MIXED')=='1' else 'note'})
            page.locator('#nav-memory').click()
            nodes=page.locator('.atlas-node'); expect(nodes).to_have_count(41)
            stage=page.locator('.atlas-stage'); stage.scroll_into_view_if_needed()
            page.wait_for_timeout(150)
            cdp=ctx.new_cdp_session(page)
            def touch(dx,dy):
                b=stage.bounding_box(); x=b['x']+b['width']/2; y=b['y']+b['height']/2
                dx=max(-b['width']*.35,min(b['width']*.35,dx));dy=max(-b['height']*.35,min(b['height']*.35,dy))
                cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
                for step in range(1,7):
                    cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x+dx*step/6,'y':y+dy*step/6}]})
                cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
            assert stage.evaluate("e=>getComputedStyle(e).touchAction")=='pan-y'
            scroll_before=stage.evaluate('e=>e.scrollTop')
            touch(0,-110);page.wait_for_timeout(300)
            scroll_after=stage.evaluate('e=>e.scrollTop')
            assert scroll_after>scroll_before, (scroll_before,scroll_after)
            stage.evaluate('e=>e.scrollTop=0')
            # Type blocks occupy disjoint vertical ranges; no crossing between groups.
            blocks=nodes.evaluate_all("ns=>ns.map(n=>({y:parseFloat(n.style.top),color:n.style.getPropertyValue('--node-color')}))")
            kinds=list(dict.fromkeys(n['color'] for n in blocks))
            for left,right in zip(kinds,kinds[1:]):
                assert max(n['y'] for n in blocks if n['color']==left)<min(n['y'] for n in blocks if n['color']==right)
            geometry=page.evaluate('''() => {
              const nodes=[...document.querySelectorAll('.atlas-node')].map(n=>n.getBoundingClientRect());
              const z=document.querySelector('.atlas-zoom').getBoundingClientRect();
              const stage=document.querySelector('.atlas-stage').getBoundingClientRect();
              return {overlap:nodes.reduce((n,a,i)=>n+nodes.slice(i+1).filter(b=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top).length,0),
                covered:nodes.filter(a=>(a.top+a.bottom)/2>=stage.top&&(a.top+a.bottom)/2<=stage.bottom&&(a.left+a.right)/2>=z.left&&(a.left+a.right)/2<=z.right&&(a.top+a.bottom)/2>=z.top&&(a.top+a.bottom)/2<=z.bottom).length,
                overflow:document.documentElement.scrollWidth>innerWidth};
            }''')
            assert geometry=={'overlap':0,'covered':0,'overflow':False},geometry
            assert page.locator('.atlas-node[tabindex="0"]').count()==1
            # Real keyboard events traverse all 41 targets and keep focus visible.
            page.locator('.atlas-node[tabindex="0"]').focus()
            seen=set()
            for _ in range(41):
                seen.add(page.locator('.atlas-node:focus').get_attribute('data-memory-id'))
                assert page.locator('.atlas-node[tabindex="0"]').count()==1
                page.wait_for_timeout(20)
                # Native scrolling clips off-screen nodes, but the focused node is hit-testable.
                assert page.locator('.atlas-node:focus').evaluate("n=>{const r=n.getBoundingClientRect();return n.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}")
                label=page.locator('.atlas-node:focus .atlas-node-label').bounding_box();bounds=stage.bounding_box()
                assert label['x']>=bounds['x']-1 and label['x']+label['width']<=bounds['x']+bounds['width']+1
                assert label['y']>=bounds['y']-1 and label['y']+label['height']<=bounds['y']+bounds['height']+1

                page.keyboard.press('ArrowRight')
            assert len(seen)==41
            page.keyboard.press('Tab')
            assert page.locator('.atlas-node:focus').count()==0
            demo=page.locator('.atlas-node').filter(has=page.locator('.atlas-node-label',has_text='演示'))
            demo.focus(); page.keyboard.press('Enter')
            expect(demo.locator('.atlas-node-label')).to_be_visible()
            expect(demo.locator('.atlas-node-label')).to_contain_text('演示')
            page.screenshot(path=str(OUTPUT/'mobile-100.png'),full_page=True)
            page.get_by_role('button',name='复位星图',exact=True).click()
            for _ in range(7): page.get_by_role('button',name='放大星图',exact=True).click()
            expect(page.locator('.atlas-zoom-value')).to_have_text('240%')
            stage.scroll_into_view_if_needed()
            before=page.locator('.atlas-space').get_attribute('style');touch(100,70)
            assert page.locator('.atlas-space').get_attribute('style')!=before,'touch did not pan'
            reachable=0
            for i in range(41):
                for attempt in range(20):
                    b=stage.bounding_box(); n=nodes.nth(i).bounding_box()
                    cx=n['x']+n['width']/2;cy=n['y']+n['height']/2
                    if b['x']+10<cx<b['x']+b['width']-10 and b['y']+10<cy<b['y']+b['height']-10:
                        reachable+=1;break
                    touch(b['x']+b['width']/2-cx,b['y']+b['height']/2-cy)
                else: raise AssertionError(f'node {i} unreachable by touch')
            assert reachable==41
            last=nodes.nth(40); rect=last.bounding_box()
            page.touchscreen.tap(rect['x']+rect['width']/2,rect['y']+rect['height']/2)
            expect(last).to_have_attribute('aria-pressed','true')
            page.screenshot(path=str(OUTPUT/'mobile-240.png'),full_page=True)
            # Resize the same UI to desktop and recheck overflow, target overlap and controls.
            page.set_viewport_size({'width':1440,'height':900});page.wait_for_timeout(200)
            page.get_by_role('button',name='复位星图',exact=True).click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(OUTPUT/'desktop.png'),full_page=True)
            assert not errors,errors
            print(json.dumps({'browser':browser.version,'mobile':'iPhone 13 emulation','nodes':41,'100_percent':geometry,'touch_reachable_at_240':reachable,'keyboard_visited':len(seen),'native_scroll_at_100':[scroll_before,scroll_after],'mixed_type_blocks':len(kinds),'chinese_mention_button':'Claude only','AI_default':'pending; explicit checkbox confirmed','page_errors':errors},ensure_ascii=False,indent=2))
            browser.close()
    finally:
        proc.terminate();proc.wait(timeout=10)
