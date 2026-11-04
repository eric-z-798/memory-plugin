import sys, tempfile, threading, shutil
from pathlib import Path
sys.path.insert(0, '/workspace/memory-plugin')
from aigc.store import Store
from aigc.engine import Engine
from aigc.members import Registry
from aigc.server import App, make_server
from playwright.sync_api import sync_playwright, expect
with tempfile.TemporaryDirectory() as tmp:
    store = Store(Path(tmp))
    registry = Registry(demo=True)
    engine = Engine(store, registry)
    gid = store.create_group('synthetic', members=['claude'])['id']
    app = App(store, engine, registry, demo=True)
    app.access_token = 'synthetic-A'
    server = make_server('127.0.0.1', 0, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}/'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=shutil.which('chromium'), headless=True, args=['--no-sandbox'])
            page = browser.new_page()
            page.goto(base + '#token=synthetic-A&amp;group=' + gid)
            expect(page.locator('#input')).to_be_editable()
            page.wait_for_timeout(1500)
            app.access_token = 'synthetic-B'
            page.goto(base + '#token=synthetic-B&amp;group=' + gid)
            page.wait_for_timeout(500)
            stored = page.evaluate('sessionStorage.getItem("xingshu.accessToken")')
            print('stored:', stored, 'new token remains in URL:', 'synthetic-B' in page.url, flush=True)
            browser.close()
            assert stored == 'synthetic-B', 'new startup link did not replace stale token'
    finally:
        engine.shutdown()
        server.shutdown()
        server.server_close()
