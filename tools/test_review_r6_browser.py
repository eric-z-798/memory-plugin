"""Private file bootstrap and visible, acknowledged legacy web migration."""
import asyncio,copy,json,shutil,sys,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa_auth import private_data_directory
from aigc.store import Store,DEFAULT_SETTINGS
from aigc.security import PrivateStartupFiles
from aigc.engine import Engine
from aigc.members import Registry
from aigc.server import App,make_server
from playwright.async_api import async_playwright,expect

async def main():
 with private_data_directory() as tmp:
  old=copy.deepcopy(DEFAULT_SETTINGS);old['members']['gpt']['web']=False
  (Path(tmp)/'settings.json').write_text(json.dumps(old))
  store=Store(Path(tmp));reg=Registry(demo=True,demo_delay=.01);engine=Engine(store,reg)
  gid=store.create_group('synthetic',members=['claude'])['id']
  app=App(store,engine,reg,demo=True);app.access_token='synthetic-private-bootstrap'
  server=make_server('127.0.0.1',0,app);threading.Thread(target=server.serve_forever,daemon=True).start()
  url=f'http://127.0.0.1:{server.server_address[1]}/#token={app.access_token}&group={gid}'
  try:
   with PrivateStartupFiles(tmp,url) as files:
    async with async_playwright() as pw:
     browser=await pw.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
     page=await browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
     await page.goto(files.page.as_uri())
     await expect(page.locator('#input')).to_be_editable()
     assert 'token=' not in page.url
     await page.locator('#btn-settings').click()
     await expect(page.get_by_text('旧版联网关闭值的来源未知',exact=False)).to_be_visible()
     web=page.get_by_label('实时联网搜索（不勾选时用 Codex 默认的缓存搜索）')
     await expect(web).to_be_checked()
     await page.locator('.member-card.m-gpt .connection-advanced > summary').click()
     await web.uncheck()
     await page.get_by_role('button',name='保存',exact=True).click()
     assert not Store(Path(tmp)).load_settings()['members']['gpt']['web']
     await page.locator('#btn-settings').click()
     await expect(page.get_by_text('旧版联网关闭值的来源未知',exact=False)).to_be_visible()
     await page.get_by_role('button',name='知道了，不再提示').click()
     await page.get_by_role('button',name='关闭',exact=True).last.click()
     await page.locator('#btn-settings').click()
     await expect(page.get_by_text('旧版联网关闭值的来源未知',exact=False)).to_have_count(0)
     await expect(page.get_by_label('实时联网搜索（不勾选时用 Codex 默认的缓存搜索）')).not_to_be_checked()
     assert not errors,errors
     print(json.dumps({'private_file_bootstrap':True,'fragment_removed':True,'unknown_provenance_visible':True,'explicit_false_preserved':True,'notice_ack_persisted':True,'pageerrors':errors}))
     await browser.close()
  finally:engine.shutdown();server.shutdown();server.server_close()
if __name__=='__main__':asyncio.run(main())
