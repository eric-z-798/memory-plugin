#!/usr/bin/env node
/**
 * DOM-only regression checks for the Xingshu UI. This does not launch a browser
 * and cannot verify layout, visual quality, or physical touch targets.
 *
 * Optional QA dependency, not an application runtime dependency:
 *   npm install --prefix /tmp/xingshu-domqa jsdom
 *   NODE_PATH=/tmp/xingshu-domqa/node_modules node tools/test_ui_dom.cjs \
 *     [optional /path/to/xingshu-preview/dist]
 * Pass XINGSHU_UI_STATIC to check a packaged static source directory instead.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const staticRoot = process.env.XINGSHU_UI_STATIC || path.resolve(__dirname, '../aigc/static');
const previewRoot = path.resolve(process.argv[2] || path.join(__dirname, '../tests/fixtures/ui-preview'));
const tick = () => new Promise(resolve => setTimeout(resolve, 15));
let assertions = 0;
const check = (value, message) => { assert.ok(value, message); assertions++; };

async function launch(search = '', empty = false) {
  const dom = new JSDOM(fs.readFileSync(path.join(staticRoot, 'index.html'), 'utf8'), {
    url: 'https://xingshu.test/' + search, runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const w = dom.window;
  const errors = [];
  w.addEventListener('error', event => errors.push(event.error?.stack || event.message));
  w.Response = Response; w.Request = Request; w.Headers = Headers;
  w.fetch = async () => { throw Error('Unexpected outbound network request'); };
  w.matchMedia = query => ({ matches: query.includes('max-width'), media: query,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  w.scrollTo = () => {};
  w.HTMLElement.prototype.scrollIntoView = function() {};
  w.HTMLElement.prototype.scrollTo = function() {};
  w.navigator.clipboard = { writeText: async () => {} };
  w.eval(fs.readFileSync(path.join(previewRoot, 'static/preview-snapshots.js'), 'utf8'));
  if (empty) w.XINGSHU_PREVIEW_DATA.snapshots['/api/groups'] = { groups: [] };
  w.eval(fs.readFileSync(path.join(previewRoot, 'static/preview-data.js'), 'utf8'));
  const gid = w.XINGSHU_PREVIEW_DATA.gid;
  const memories = w.XINGSHU_PREVIEW_DATA.snapshots['/api/groups/' + gid + '/memories'].memories;
  for (const script of w.document.querySelectorAll('script[src]')) {
    const file = script.getAttribute('src').split('/').pop();
    if (file.startsWith('preview-')) continue;
    w.eval(fs.readFileSync(path.join(staticRoot, file), 'utf8'));
  }
  const $ = selector => w.document.querySelector(selector);
  const $$ = selector => [...w.document.querySelectorAll(selector)];
  const click = selector => {
    const item = $(selector);
    check(item, 'Missing control: ' + selector);
    check(!item.disabled, 'Disabled control: ' + selector);
    item.click();
  };
  const wait = async predicate => {
    const deadline = Date.now() + 2500;
    while (!predicate()) {
      if (Date.now() > deadline) throw Error('Timed out waiting for DOM state. Errors: ' + errors.join('\n'));
      await tick();
    }
  };
  const close = () => click('.modal-head button[aria-label="关闭"]');
  const tile = title => {
    const item = $$('.settings-tile').find(button => button.querySelector('h3')?.textContent === title);
    check(item, 'Settings entry: ' + title); item.click();
  };
  const healthy = () => check(errors.length === 0, errors.join('\n'));
  await wait(() => empty ? !$('#welcome').hidden : $('#group-title').textContent === '星枢体验室');
  await tick();
  return { dom, w, $, $$, click, wait, close, tile, healthy, memories };
}

async function ordinaryFlow() {
  const q = await launch();
  const { w, $, $$, click, wait, close, tile, memories } = q;
  try {
    check($('#stream-inner').textContent.includes('Claude'), 'Seeded conversation renders');
    ['chat', 'memory', 'board', 'settings'].forEach(name => check($('#nav-' + name), 'Navigation: ' + name));
    click('#btn-menu'); check(w.document.body.classList.contains('drawer-open'), 'Drawer opens');
    click('#scrim'); check(!w.document.body.classList.contains('drawer-open'), 'Drawer closes');
    click('#nav-board'); check(w.document.body.classList.contains('tab-board'), 'Progress view opens');
    check($('#nav-board').getAttribute('aria-current') === 'page', 'Progress navigation active');
    click('#nav-chat'); check(!w.document.body.classList.contains('tab-board'), 'Chat view returns');

    click('#nav-memory'); await wait(() => $$('.atlas-node').length === Math.min(memories.length, 160));
    check($('.modal.memory-workspace'), 'Memory workspace opens');
    check(!$('.memory-explore').hidden && $('.memory-list').hidden, 'Graph is default memory view');
    check($('#nav-memory').getAttribute('aria-current') === 'page', 'Memory navigation active');
    check($$('.memory-list .memory-card').length === memories.length, 'List uses the same records as graph');
    const pending = memories.find(item => item.status === 'pending');
    check(pending, 'Fixture has pending memory');
    click('.atlas-node[data-memory-id="' + pending.id + '"]');
    check($('.memory-detail .memory-content').textContent === pending.content, 'Node selection shows matching content');
    check($('.memory-detail .memory-card').classList.contains('pending'), 'Pending status retained in details');
    check($('.memory-detail').textContent.includes('核实并确认'), 'Pending memory confirmation action retained');
    const selectedNode = $('.atlas-node[aria-pressed="true"]');
    selectedNode.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    check($('.atlas-node[aria-pressed="true"]').dataset.memoryId !== pending.id, 'Keyboard selection moves to another memory');
    click('[aria-label="放大星图"]'); check($('.atlas-zoom-value').textContent === '120%', 'Zoom control changes scale');
    click('[aria-label="复位星图"]'); check($('.atlas-zoom-value').textContent === '100%', 'Reset restores scale');
    click('.memory-view-switch [data-mode="list"]'); check(!$('.memory-list').hidden && $('.memory-explore').hidden, 'List view is available');
    click('.memory-view-switch [data-mode="atlas"]');
    const status = $('[aria-label="筛选记忆状态"]');
    status.value = 'pending'; status.dispatchEvent(new w.Event('change', { bubbles: true }));
    await wait(() => $$('.atlas-node').length === memories.filter(item => item.status === 'pending').length);
    check($$('.atlas-node').every(node => node.classList.contains('is-pending')), 'State filter affects the graph');
    status.value = 'all'; status.dispatchEvent(new w.Event('change', { bubbles: true }));
    await wait(() => $$('.atlas-node').length === Math.min(memories.length, 160));
    const search = $('[aria-label="搜索记忆"]');
    search.value = '首版'; search.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await wait(() => $$('.atlas-node').length === memories.filter(item => item.content.includes('首版')).length);
    check($$('.memory-list .memory-content').every(node => node.textContent.includes('首版')), 'Search affects the list and graph together');
    search.value = '__no_such_memory_52f9__'; search.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await wait(() => $$('.atlas-node').length === 0);
    check(!$('.atlas-empty').hidden && $('.atlas-space').hidden, 'Empty search has a clear graph empty state');
    search.value = ''; search.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await wait(() => $$('.atlas-node').length === Math.min(memories.length, 160));
    close(); check($('#nav-chat').getAttribute('aria-current') === 'page', 'Close restores the current main navigation');

    click('#nav-settings'); check($$('.settings-tile').length === 3, 'Settings opens a simple three-entry hub');
    tile('连接 AI 账号'); await wait(() => $$('.setup-member').length === 3);
    check($$('.setup-steps li').length === 3, 'Connection guide has three steps');
    check($('.setup-proxy').tagName === 'DETAILS' && !$('.setup-proxy').open, 'Default proxy controls are collapsed');
    close(); click('#nav-settings'); tile('模型与额度');
    await wait(() => $$('.connection-advanced').length === 3);
    check($$('.connection-advanced').every(details => !details.open), 'Technical model connection fields are collapsed');
    check($$('.connection-advanced input').length >= 9, 'Advanced connection inputs remain available');
    close(); click('#nav-settings'); tile('使用指南');
    check($$('.app-guide section').length === 4, 'Novice guide is available');
    close();
    click('#btn-new'); await tick();
    const extras = $('.more-options');
    check(extras?.tagName === 'DETAILS' && !extras.open, 'New discussion advanced options are collapsed');
    const firstFields = $$('.modal-body input, .modal-body textarea, .modal-body select').filter(field => !field.closest('details'));
    check(firstFields.length === 2, 'New discussion starts with only name and opening question');
    check(extras.querySelector('.project-picker') && extras.querySelector('.seat-picks') && extras.querySelector('.seg'), 'Project, members, and discussion mode remain available');
    close(); q.healthy();
  } finally { q.dom.window.close(); }
}

async function deepLinkAndEmptyFlow() {
  const q = await launch('?view=memory');
  try {
    await q.wait(() => q.$$('.atlas-node').length > 0);
    check(q.$('.memory-workspace'), 'Memory deep link opens directly');
    check(q.$('#nav-memory').getAttribute('aria-current') === 'page', 'Deep link selects memory navigation');
    q.healthy();
  } finally { q.dom.window.close(); }
  const empty = await launch('', true);
  try {
    empty.click('#nav-memory');
    check(empty.$('#modal-root').textContent.includes('创建第一个群聊'), 'No-project memory entry explains next step');
    check(!empty.$('.memory-workspace'), 'Empty project does not invent saved memories');
    empty.click('#nav-chat'); empty.click('#nav-board');
    check(!empty.w.document.body.classList.contains('tab-board'), 'No-project progress navigation is safe');
    empty.healthy();
  } finally { empty.dom.window.close(); }
}

(async () => {
  await ordinaryFlow();
  await deepLinkAndEmptyFlow();
  console.log(`PASS: ${assertions} DOM assertions covering navigation, memory graph/list/search/status/details/zoom/empty states, settings, onboarding, deep links and no-project entry.`);
  console.log('DOM only: no browser launched; layout, visual styling and real-device touch behavior remain unverified.');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
