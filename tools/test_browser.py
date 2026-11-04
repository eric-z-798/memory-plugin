from qa_auth import private_data_directory
from qa_auth import BOOTSTRAP, TOKEN, urlopen as qa_urlopen, authenticate, authenticate_async
import json
import sys
import shutil
import atexit
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / 'work' / 'browser-qa'
ROOT.mkdir(parents=True, exist_ok=True)
data_dir = private_data_directory()
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
BASE = 'http://127.0.0.1:' + str(port)
server = subprocess.Popen([sys.executable, '-c', BOOTSTRAP, '--demo', '--no-browser', '--port', str(port), '--data-dir', data_dir.name],
    cwd=str(PROJECT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def cleanup():
    server.terminate()
    server.wait(timeout=10)
    data_dir.cleanup()
atexit.register(cleanup)
for _ in range(50):
    try:
        qa_urlopen(BASE + '/api/info').close()
        break
    except OSError:
        time.sleep(.1)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=shutil.which('chromium') or None, headless=True, args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1, reduced_motion='reduce')
    authenticate(page)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append('console: ' + message.text)
        if message.type == 'error' and 'status of 409' not in message.text else None)
    page.goto(BASE)
    expect(page.locator('#btn-new')).to_be_visible()

    def api(method, path, data=None):
        response = page.request.fetch(BASE + path, method=method,
            headers={'X-AIGC': '1'}, data=data)
        assert response.ok, (response.status, response.text())
        return response.json()

    def close():
        page.locator('.modal-head button[aria-label="关闭"]').click()

    def new_group(name, project=None, new_project=None):
        page.locator('#btn-new').click()
        page.get_by_label('群名', exact=True).fill(name)
        if new_project or project:
            page.locator('.more-options > summary').click()
        if new_project:
            page.locator('.project-picker select').select_option('__new__')
            page.get_by_label('新项目名称', exact=True).fill(new_project)
        elif project:
            page.locator('.project-picker select').select_option(project)
        page.get_by_role('button', name='创建', exact=True).click()
        expect(page.locator('.modal')).to_have_count(0)
        expect(page.locator('#group-title')).to_have_text(name)
        return page.url.split('#')[1]

    def open_memories():
        page.locator('#nav-memory' if page.viewport_size['width'] <= 860 else '#btn-memory').click()
        page.get_by_role('tab', name='列表', exact=True).click()

    def add_memory(content, kind='fact', scope='project'):
        page.get_by_role('button', name='＋ 添加记忆', exact=True).click()
        page.get_by_label('记忆内容', exact=True).fill(content)
        page.get_by_label('类型', exact=True).select_option(kind)
        page.get_by_label('共享范围', exact=True).select_option(scope)
        page.get_by_role('button', name='保存', exact=True).click()
        expect(page.locator('.memory-list .memory-card').filter(has_text=content)).to_be_visible()

    gid = new_group('记忆验收 · 产品组', new_project='共享记忆验收项目')
    group = api('GET', '/api/groups/' + gid)['group']
    project = group['project_id']
    open_memories()
    add_memory('首版项目预算为 20000 元。')
    add_memory('回答使用简体中文。', 'preference', 'global')
    memories = api('GET', '/api/groups/' + gid + '/memories')['memories']
    memory = next(m for m in memories if m['content'].startswith('首版'))
    card = page.locator('.memory-list .memory-card').filter(has_text='首版项目预算为 20000 元。')
    card.get_by_role('button', name='编辑', exact=True).click()
    page.get_by_label('记忆内容', exact=True).fill('首版项目预算为 30000 元，保留我的草稿。')
    api('PATCH', '/api/groups/' + gid + '/memories/' + memory['id'],
        {'version': memory['version'], 'content': '其他窗口将预算改为 25000 元。'})
    page.get_by_role('button', name='保存', exact=True).click()
    expect(page.locator('.memory-conflict')).to_be_visible()
    expect(page.get_by_label('记忆内容', exact=True)).to_have_value('首版项目预算为 30000 元，保留我的草稿。')
    expect(page.locator('.memory-conflict')).to_contain_text('25000')
    page.get_by_role('button', name='保留草稿，基于新版编辑').click()
    page.get_by_role('button', name='保存', exact=True).click()
    expect(page.locator('.memory-list .memory-card').filter(has_text='30000')).to_be_visible()
    page.locator('.memory-list .memory-card').filter(has_text='30000').get_by_role('button', name='历史', exact=True).click()
    expect(page.locator('.modal-body')).to_contain_text('25000')
    expect(page.locator('.modal-body')).to_contain_text('20000')
    page.get_by_role('button', name='返回记忆列表', exact=True).click()
    candidate = api('POST', '/api/groups/' + gid + '/memories',
        {'content': '下周先验证 Windows 启动。', 'kind': 'task', 'status': 'pending', 'scope': 'project'})['memory']
    page.get_by_role('button', name='刷新', exact=True).click()
    card = page.locator('.memory-list .memory-card').filter(has_text='下周先验证 Windows 启动。')
    expect(card).to_contain_text('待确认')
    card.get_by_role('button', name='核实并确认').click()
    page.get_by_role('button', name='保存', exact=True).click()
    expect(page.locator('.memory-list .memory-card').filter(has_text='下周先验证 Windows 启动。')).to_contain_text('已确认')
    page.get_by_role('button', name='查看 AI 会读到的记忆', exact=True).click()
    page.get_by_placeholder('输入接下来要讨论的问题').fill('预算')
    page.get_by_role('button', name='重新检索', exact=True).click()
    expect(page.locator('.memory-context')).to_contain_text('')
    page.wait_for_function("() => document.querySelector('.memory-context').value.includes('30000')")
    close()
    page.locator('#input').fill('先完成三个平台的记忆读写，再做自动提炼。')
    page.locator('#btn-send').click()
    user = page.locator('.msg.user').filter(has_text='先完成三个平台的记忆读写')
    expect(user).to_be_visible()
    user.get_by_role('button', name='存入记忆').click()
    expect(page.get_by_label('记忆内容', exact=True)).to_have_value('先完成三个平台的记忆读写，再做自动提炼。')
    page.get_by_role('button', name='保存', exact=True).click()
    expect(page.locator('.memory-list .memory-card').filter(has_text='先完成三个平台')).to_be_visible()
    close()
    # Wait until the demo round completes before examining group settings.
    page.wait_for_function("() => document.querySelector('#btn-stop').hidden", timeout=30000)
    page.locator('#btn-more').click()
    page.get_by_role('menuitem', name='群设置', exact=True).click()
    expect(page.locator('.project-picker select')).to_be_disabled()
    expect(page.locator('.project-picker')).to_contain_text('已有聊天记录')
    close()
    gid2 = new_group('记忆验收 · 新会话', project=project)
    open_memories()
    expect(page.locator('.memory-list .memory-card').filter(has_text='30000')).to_be_visible()
    expect(page.locator('.memory-list .memory-card').filter(has_text='先完成三个平台')).to_be_visible()
    # Search and status filters.
    page.get_by_label('搜索记忆', exact=True).fill('预算')
    expect(page.locator('.memory-list .memory-card')).to_have_count(1)
    page.get_by_label('搜索记忆', exact=True).fill('')
    expect(page.locator('.memory-list .memory-card')).to_have_count(len(api('GET', '/api/groups/' + gid2 + '/memories')['memories']))
    with page.expect_download() as download_info:
        page.get_by_role('button', name='导出记忆', exact=True).click()
    exported = download_info.value
    exported.save_as(ROOT / exported.suggested_filename)
    assert '30000' in (ROOT / exported.suggested_filename).read_text()
    page.screenshot(path=str(ROOT / 'memory-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(ROOT / 'memory-mobile.png'), full_page=True)
    assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'), 'horizontal overflow'
    card = page.locator('.memory-list .memory-card').filter(has_text='下周先验证 Windows 启动。')
    card.get_by_role('button', name='删除', exact=True).click()
    expect(page.locator('.modal-body')).to_contain_text('原聊天记录仍会保留')
    page.get_by_role('button', name='删除记忆', exact=True).click()
    expect(page.locator('.memory-list .memory-card').filter(has_text='下周先验证 Windows 启动。')).to_have_count(0)
    close()
    page.set_viewport_size({'width': 1440, 'height': 1000})
    gid3 = new_group('记忆验收 · 独立项目')
    open_memories()
    expect(page.locator('.memory-list .memory-card')).to_have_count(1)
    expect(page.locator('.memory-list .memory-card')).to_contain_text('回答使用简体中文。')
    assert not errors, errors
    print(json.dumps({'passed': True, 'groups': [gid, gid2, gid3], 'project': project,
        'checks': ['project create/link/isolation', 'memory CRUD/history', 'global scope',
                   'pending confirmation', '409 preserves draft and explicit rebase',
                   'save message with source', 'context search', 'JSON export',
                   'mobile 390px overflow', 'project switch lock', 'no console/page errors'],
        'errors': errors}, ensure_ascii=False))
    browser.close()
