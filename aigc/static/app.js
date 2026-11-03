/* 星枢 XINGSHU 前端：原生 JavaScript，不依赖任何外部库。 */
'use strict';

(() => {
  const MEMBERS = ['claude', 'gpt', 'gemini'];
  const NAMES = { claude: 'Claude', gpt: 'GPT', gemini: 'Gemini', user: '群主', system: '系统', unassigned: '未指定' };
  const DECISION_LABEL = { discussing: '讨论中', effective: '已生效', rejected: '已否决' };
  const TASK_LABEL = { proposed: '提议中', todo: '待办', doing: '进行中', done: '已完成', blocked: '阻塞', cancelled: '已取消' };
  const TASK_TAG = { mention: '被点名', parallel: '背靠背', review: '互评', continue: '继续讨论', vote: '补表态', debate: '针对分歧' };
  const REPLY_LENGTH = { short: '简短，约 300 字', medium: '适中，约 600 字', long: '详细，约 1200 字' };
  const MODEL_TIERS = { fast: '快速', balanced: '均衡', deep: '深入', manual: '手动' };
  const MEMORY_KINDS = { note: '笔记', fact: '事实', preference: '偏好', decision: '决定', task: '任务' };
  const WEB_LABEL = {
    claude: '允许搜索网页（WebSearch、WebFetch）',
    gpt: '实时联网搜索（不勾选时用 Codex 默认的缓存搜索）',
  };

  const S = {
    info: {}, groups: [], gid: null, group: null, board: null, rev: -1,
    seqs: new Set(), lastSeq: 0, state: { running: false, speaking: [] }, offset: 0,
    members: null, modelAvailability: {}, modelQuota: {}, pollTimer: null, online: true, effective: null, tab: 'chat',
    forceScroll: false, openFolds: new Set(['dec-closed']), menu: null, modalReturn: null, hasChat: false,
  };

  // ------------------------------------------------------------ 小工具
  const $ = (sel, root = document) => root.querySelector(sel);
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const pad = (n) => String(n).padStart(2, '0');
  const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const narrow = () => window.matchMedia('(max-width: 860px)').matches;

  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'html') node.innerHTML = v; // 只用于已转义过的 Markdown 结果
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      node.append(c.nodeType ? c : String(c));
    }
    return node;
  }

  function fmtClock(ts) {
    const d = new Date(ts * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function fmtDay(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return fmtClock(ts);
    return (d.getMonth() + 1) + '-' + d.getDate() + ' ' + fmtClock(ts);
  }
  function fmtAgo(ts) {
    const sec = Date.now() / 1000 - ts;
    if (sec < 60) return '刚刚';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
    const d = new Date(ts * 1000);
    if (d.toDateString() === new Date().toDateString()) return fmtClock(ts);
    return (d.getMonth() + 1) + '-' + d.getDate();
  }
  function fmtDur(sec) {
    sec = Math.max(0, Math.floor(sec));
    return Math.floor(sec / 60) + ':' + pad(sec % 60);
  }
  function fmtSeconds(sec) {
    sec = Math.round(sec);
    if (sec < 60) return sec + ' 秒';
    return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
  }

  function toast(text, bad) {
    const t = h('div', { class: 'toast' + (bad ? ' bad' : '') }, text);
    $('#toast-root').append(t);
    setTimeout(() => t.remove(), bad ? 6500 : 3200);
  }

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { /* 隐私模式等情况下不可用，忽略 */ }
    return null;
  }

  async function api(method, path, body) {
    const opt = { method, headers: { 'X-AIGC': '1' }, cache: 'no-store' };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opt);
    } catch (e) {
      const err = new Error('连接不上程序，请确认它还在运行');
      err.network = true;
      throw err;
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* 不是 JSON */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || ('请求失败（' + res.status + '）'));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const ICON_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  // ------------------------------------------------------------ Markdown（先转义，再加有限的格式）
  function link(url, label) {
    return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
  }

  function decorate(html) {
    // 只处理标签之外、链接之外的文字：编号变成可点击的引用，@成员高亮
    let inLink = false;
    return html.split(/(<[^>]+>)/g).map((seg) => {
      if (seg.startsWith('<')) {
        if (/^<a\b/i.test(seg)) inLink = true;
        else if (/^<\/a>/i.test(seg)) inLink = false;
        return seg;
      }
      if (inLink) return seg;
      return seg
        .replace(/(^|[^\w-])([DT]-\d{3})(?![\w-])/g, '$1<button type="button" class="ref" data-ref="$2">$2</button>')
        .replace(/[@＠](Claude|ChatGPT|GPT|Gemini|全体成员|全体|所有人|大家)/gi, '<span class="mention">$&</span>');
    }).join('');
  }

  function inline(text) {
    return String(text).split(/(`[^`\n]+`)/g).map((part) => {
      if (/^`[^`\n]+`$/.test(part)) return '<code>' + esc(part.slice(1, -1)) + '</code>';
      let t = esc(part);
      t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, label, url) => link(url, label));
      t = t.replace(/(^|[^"'=>\w/])(https?:\/\/[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]+)/g, (m, pre, url) => {
        // 网址只取 ASCII 字符，遇到中文或全角标点就结束；再去掉末尾的标点和转义后的引号
        let tail = '';
        const cut = url.search(/&(quot|#39|lt|gt);/);
        if (cut >= 0) { tail = url.slice(cut); url = url.slice(0, cut); }
        const trail = url.match(/(?:[.,;:!?'\]]|\)(?![^(]*\())+$/);
        if (trail && !(trail[0].includes(')') && url.includes('('))) {
          tail = trail[0] + tail;
          url = url.slice(0, -trail[0].length);
        }
        return url.length > 8 ? pre + link(url, url) + tail : m;
      });
      t = t.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?!\*)/g, '$1<em>$2</em>');
      return decorate(t);
    }).join('');
  }

  const RE = {
    fence: /^\s*(```|~~~)\s*([\w+#.-]*)\s*$/,
    head: /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/,
    hr: /^\s{0,3}([-*_])(\s*\1){2,}\s*$/,
    quote: /^\s{0,3}>\s?/,
    item: /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/,
    tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
  };
  const blank = (l) => /^\s*$/.test(l);
  const startsBlock = (l) => RE.fence.test(l) || RE.head.test(l) || RE.hr.test(l) || RE.quote.test(l) || RE.item.test(l);

  function listItem(line) {
    const m = line.match(RE.item);
    if (!m) return null;
    const ordered = /\d/.test(m[2]);
    return { indent: m[1].replace(/\t/g, '    ').length, ordered, start: ordered ? parseInt(m[2], 10) : 1, text: m[3] };
  }

  function parseList(lines, i) {
    const first = listItem(lines[i]);
    const base = first.indent;
    const items = [];
    while (i < lines.length) {
      const line = lines[i];
      if (blank(line)) {
        let j = i + 1;
        while (j < lines.length && blank(lines[j])) j++;
        const next = j < lines.length ? listItem(lines[j]) : null;
        if (next && next.indent >= base && (next.indent > base || next.ordered === first.ordered)) { i = j; continue; }
        break;
      }
      const it = listItem(line);
      if (it && it.indent <= base + 1 && it.indent >= base - 1) {
        if (it.ordered !== first.ordered) break;
        items.push({ text: it.text, sub: '' });
        i++;
        continue;
      }
      if (it && it.indent > base + 1 && items.length) {
        const nested = parseList(lines, i);
        items[items.length - 1].sub += nested.html;
        i = nested.next;
        continue;
      }
      if (!it && items.length && /^\s+\S/.test(line) && !startsBlock(line.trim())) {
        items[items.length - 1].text += '\n' + line.trim();
        i++;
        continue;
      }
      break;
    }
    const tag = first.ordered ? 'ol' : 'ul';
    const open = first.ordered && first.start !== 1 ? '<ol start="' + first.start + '">' : '<' + tag + '>';
    const body = items.map((it) => '<li>' + it.text.split('\n').map(inline).join('<br>') + it.sub + '</li>').join('');
    return { html: open + body + '</' + tag + '>', next: i };
  }

  function md(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m;
      if (blank(line)) { i++; continue; }
      if ((m = line.match(RE.fence))) {
        const close = new RegExp('^\\s*' + m[1] + '\\s*$');
        const buf = [];
        i++;
        while (i < lines.length && !close.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>');
        continue;
      }
      if ((m = line.match(RE.head))) {
        const level = Math.min(6, m[1].length + 2);
        out.push('<h' + level + '>' + inline(m[2]) + '</h' + level + '>');
        i++;
        continue;
      }
      if (RE.hr.test(line)) { out.push('<hr>'); i++; continue; }
      if (line.includes('|') && i + 1 < lines.length && lines[i + 1].includes('-') && RE.tableSep.test(lines[i + 1])) {
        const cells = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && !blank(lines[i])) rows.push(cells(lines[i++]));
        out.push('<table><thead><tr>' + head.map((c) => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>'
          + rows.map((r) => '<tr>' + head.map((_, k) => '<td>' + inline(r[k] || '') + '</td>').join('') + '</tr>').join('')
          + '</tbody></table>');
        continue;
      }
      if (RE.quote.test(line)) {
        const buf = [];
        while (i < lines.length && RE.quote.test(lines[i])) buf.push(lines[i++].replace(RE.quote, ''));
        out.push('<blockquote>' + md(buf.join('\n')) + '</blockquote>');
        continue;
      }
      if (listItem(line)) {
        const res = parseList(lines, i);
        out.push(res.html);
        i = res.next;
        continue;
      }
      const buf = [];
      while (i < lines.length && !blank(lines[i]) && !(buf.length && startsBlock(lines[i]))) buf.push(lines[i++]);
      out.push('<p>' + buf.map(inline).join('<br>') + '</p>');
    }
    return out.join('');
  }

  // ------------------------------------------------------------ 弹窗和菜单
  function closeModal() {
    $('#modal-root').replaceChildren();
    if (S.modalReturn && document.contains(S.modalReturn)) {
      try { S.modalReturn.focus(); } catch (e) { /* ignore */ }
    }
    S.modalReturn = null;
  }

  function openModal({ title, body, actions, wide }) {
    closeMenu();
    const error = h('p', { class: 'modal-error', role: 'alert' });
    let primaryBtn = null;
    const buttons = (actions || [{ label: '关闭' }]).map((a) => {
      const btn = h('button', { type: 'button', class: 'btn' + (a.primary ? ' primary' : '') + (a.danger ? ' danger' : '') }, a.label);
      if (a.primary) primaryBtn = btn;
      btn.addEventListener('click', async () => {
        if (!a.onClick) { closeModal(); return; }
        error.textContent = '';
        btn.disabled = true;
        try {
          const keep = await a.onClick();
          if (keep !== true && modal.isConnected) closeModal();
        } catch (e) {
          error.textContent = e.message;
        } finally {
          btn.disabled = false;
        }
      });
      return btn;
    });
    const modal = h('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      h('div', { class: 'modal-head' },
        h('h2', {}, title),
        h('button', { type: 'button', class: 'icon-btn', 'aria-label': '关闭', onclick: closeModal, html: ICON_CLOSE })),
      h('div', { class: 'modal-body' }, body),
      h('div', { class: 'modal-foot' }, error, buttons));
    modal.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && primaryBtn) { e.preventDefault(); primaryBtn.click(); }
      if (e.key === 'Tab') {
        const nodes = Array.from(modal.querySelectorAll('button, input, textarea, select, a[href], [tabindex="0"]'))
          .filter((n) => !n.disabled && n.getClientRects().length);
        if (nodes.length && e.shiftKey && document.activeElement === nodes[0]) { e.preventDefault(); nodes[nodes.length - 1].focus(); }
        else if (nodes.length && !e.shiftKey && document.activeElement === nodes[nodes.length - 1]) { e.preventDefault(); nodes[0].focus(); }
      }
    });
    const backdrop = h('div', { class: 'modal-backdrop' }, modal);
    backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) closeModal(); });
    if (!$('#modal-root').firstChild) S.modalReturn = document.activeElement;
    $('#modal-root').replaceChildren(backdrop);
    setTimeout(() => {
      if (!modal.isConnected) return;
      const f = Array.from(modal.querySelectorAll('.modal-body textarea, .modal-body input[type="text"], .modal-body select'))
        .find((node) => !node.disabled && node.getClientRects().length) || primaryBtn || modal.querySelector('button:not(:disabled)');
      if (f) f.focus();
    }, 30);
    return modal;
  }

  function closeMenu() {
    if (S.menu) { S.menu.remove(); S.menu = null; }
  }

  function openMenu(anchor, items) {
    closeMenu();
    const menu = h('div', { class: 'menu', role: 'menu' }, items.map((it) =>
      h('button', { type: 'button', role: 'menuitem', class: it.danger ? 'danger' : '', onclick: () => { closeMenu(); it.onClick(); } }, it.label)));
    document.body.append(menu);
    const r = anchor.getBoundingClientRect();
    const w = menu.offsetWidth;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w));
    const top = r.bottom + 4 + menu.offsetHeight > window.innerHeight ? Math.max(8, r.top - menu.offsetHeight - 4) : r.bottom + 4;
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    S.menu = menu;
    const first = menu.querySelector('button');
    if (first) first.focus();
  }

  document.addEventListener('mousedown', (e) => {
    if (S.menu && !S.menu.contains(e.target)) closeMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (S.menu) closeMenu();
      else if ($('#modal-root').firstChild) closeModal();
      else document.body.classList.remove('drawer-open');
    }
  });

  // 表单小部件
  function field(label, control, hint) {
    if (control && ['INPUT', 'TEXTAREA', 'SELECT'].includes(control.tagName)) control.setAttribute('aria-label', label);
    return h('label', { class: 'field' }, h('span', {}, label), control, hint ? h('small', {}, hint) : null);
  }
  function check(label, checked, name) {
    const input = h('input', { type: 'checkbox', name: name || null });
    input.checked = !!checked;
    return { node: h('label', { class: 'check' }, input, h('span', {}, label)), input };
  }
  function textarea(value, placeholder, rows) {
    const t = h('textarea', { rows: rows || 4, placeholder: placeholder || '' });
    t.value = value || '';
    return t;
  }
  function textInput(value, placeholder) {
    const t = h('input', { type: 'text', placeholder: placeholder || '' });
    t.value = value || '';
    return t;
  }
  function select(options, value) {
    const s = h('select', {}, options.map(([v, label]) => h('option', { value: v }, label)));
    s.value = value;
    return s;
  }
  function seatPicker(selected) {
    const chosen = new Set(selected);
    const box = h('div', { class: 'seat-picks', role: 'group', 'aria-label': '选择成员' });
    for (const m of MEMBERS) {
      const b = h('button', { type: 'button', class: 'seat m-' + m, 'aria-pressed': chosen.has(m) ? 'true' : 'false' }, NAMES[m]);
      b.addEventListener('click', () => {
        if (chosen.has(m)) chosen.delete(m); else chosen.add(m);
        b.setAttribute('aria-pressed', chosen.has(m) ? 'true' : 'false');
      });
      box.append(b);
    }
    return { node: box, value: () => MEMBERS.filter((m) => chosen.has(m)) };
  }
  function segPicker(options, value) {
    let current = value;
    const box = h('div', { class: 'seg', role: 'radiogroup' });
    const sync = () => box.querySelectorAll('button').forEach((b) => b.setAttribute('aria-checked', b.dataset.v === current ? 'true' : 'false'));
    for (const [v, label] of options) {
      const b = h('button', { type: 'button', role: 'radio', 'data-v': v }, label);
      b.addEventListener('click', () => { current = v; sync(); });
      box.append(b);
    }
    sync();
    return { node: box, value: () => current };
  }

  // ------------------------------------------------------------ 群列表
  async function refreshGroups() {
    try {
      const d = await api('GET', '/api/groups');
      S.groups = d.groups;
      renderGroupList();
    } catch (e) { /* 轮询会显示断线 */ }
  }

  function renderGroupList() {
    const nav = $('#group-list');
    if (!S.groups.length) {
      nav.replaceChildren(h('p', { class: 'sidebar-empty' }, '还没有群聊。点上面的按钮建一个。'));
      return;
    }
    nav.replaceChildren(...S.groups.map((g) => h('button', {
      type: 'button', class: 'group-item', 'aria-current': g.id === S.gid ? 'true' : 'false',
      onclick: () => selectGroup(g.id),
    },
    h('span', { class: 'g-name' }, g.running ? h('span', { class: 'g-live', title: '正在发言' }) : null, g.name),
    h('span', { class: 'g-time' }, fmtAgo(g.updated)),
    h('span', { class: 'g-preview' }, g.preview || '还没有消息'))));
  }

  // ------------------------------------------------------------ 打开一个群
  function showChrome(inGroup) {
    $('#btn-memory-side').disabled = !inGroup;
    $('#chat-head').hidden = !inGroup;
    $('#tabs').hidden = !inGroup;
    $('#stream-wrap').hidden = !inGroup;
    $('#composer').hidden = !inGroup;
    $('#welcome').hidden = inGroup;
    if (!inGroup) {
      $('#typing').hidden = true;
      setTab('chat');
    }
  }

  function showWelcome() {
    S.gid = null;
    S.group = null;
    S.board = null;
    clearTimeout(S.pollTimer);
    showChrome(false);
    renderBoard();
    renderBanner();
    renderGroupList();
    if (location.hash) history.replaceState(null, '', location.pathname);
  }

  async function selectGroup(gid) {
    document.body.classList.remove('drawer-open');
    if (S.gid === gid && S.group) return;
    clearTimeout(S.pollTimer);
    saveDraft();
    Object.assign(S, {
      gid, group: null, board: null, rev: -1, seqs: new Set(), lastSeq: 0, effective: null, hasChat: false,
      state: { running: false, speaking: [] }, forceScroll: true,
    });
    history.replaceState(null, '', '#' + gid);
    store('aigc.gid', gid);
    showChrome(true);
    renderGroupList();
    $('#stream-inner').replaceChildren(h('p', { class: 'meta' }, '正在加载……'));
    let d;
    const availabilitySnapshot = availabilityEpochSnapshot();
    try {
      d = await api('GET', '/api/groups/' + gid);
    } catch (e) {
      toast(e.message, true);
      if (e.status === 404) { await refreshGroups(); showWelcome(); }
      return;
    }
    if (S.gid !== gid) return;
    S.group = d.group;
    S.board = d.board;
    S.rev = d.board.rev || 0;
    S.state = d.state;
    acceptAvailability(d.state.model_availability, availabilitySnapshot);
    acceptQuota(d.state.model_quota);
    S.offset = d.now - Date.now() / 1000;
    $('#stream-inner').replaceChildren();
    appendMessages(d.messages);
    if (!d.messages.length) renderEmptyChat();
    const input = $('#input');
    input.value = store('aigc.draft.' + gid) || '';
    autosize();
    renderHeader();
    renderBoard();
    renderTyping();
    renderBanner();
    schedulePoll(800);
  }

  function renderEmptyChat() {
    $('#stream-inner').replaceChildren(h('div', { class: 'empty-chat', id: 'empty-chat' },
      h('h2', {}, '说点什么，开始讨论'),
      h('p', {}, '三家会轮流回答。想只问其中一位，就在消息里 @ 它，比如 @Gemini。')));
  }

  // ------------------------------------------------------------ 轮询
  function schedulePoll(delay) {
    clearTimeout(S.pollTimer);
    S.pollTimer = setTimeout(poll, delay);
  }

  async function poll() {
    const gid = S.gid;
    if (!gid) return;
    let next = S.state.running ? 1200 : 3500;
    const availabilitySnapshot = availabilityEpochSnapshot();
    try {
      const d = await api('GET', '/api/groups/' + gid + '/poll?after=' + S.lastSeq + '&rev=' + S.rev);
      if (gid !== S.gid) return;
      setOnline(true);
      applyPoll(d, availabilitySnapshot);
      next = S.state.running ? 1200 : 3500;
    } catch (e) {
      if (gid !== S.gid) return;
      if (e.status === 404) {
        toast('这个群已经不在了', true);
        await refreshGroups();
        showWelcome();
        return;
      }
      if (e.network) setOnline(false);
      next = 4000;
    }
    if (document.hidden) next = Math.max(next, 8000);
    schedulePoll(next);
  }

  function atBottom() {
    const stream = $('#stream');
    return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 160;
  }

  function applyPoll(d, availabilitySnapshot) {
    S.offset = d.now - Date.now() / 1000;
    if (d.group) S.group = d.group;
    const wasRunning = S.state.running;
    const stick = atBottom();
    S.state = d.state;
    acceptAvailability(d.state.model_availability, availabilitySnapshot);
    acceptQuota(d.state.model_quota);
    renderTyping();
    renderHeader();
    renderBanner();
    if (d.messages && d.messages.length) appendMessages(d.messages, stick);
    if (stick) $('#stream').scrollTop = $('#stream').scrollHeight;
    if (d.board) {
      S.board = d.board;
      S.rev = d.rev;
      renderBoard();
    }
    if (wasRunning !== S.state.running) refreshGroups();
  }

  function setOnline(on) {
    if (S.online === on) return;
    S.online = on;
    renderBanner();
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && S.gid) schedulePoll(100);
  });

  // ------------------------------------------------------------ 消息
  function effectChip(text) {
    const m = text.match(/[DT]-\d{3}/);
    const done = /生效|确认/.test(text);
    return h('button', { type: 'button', class: 'fx' + (done ? ' done' : ''), 'data-ref': m ? m[0] : null, title: m ? '在看板中查看' : null }, text);
  }

  function modelRouteNode(route, preview) {
    if (!route || typeof route !== 'object') return null;
    const automatic = route.mode === 'auto';
    const requested = route.requested_model || 'CLI 默认';
    const tier = MODEL_TIERS[route.tier] || (automatic ? '自动' : '手动');
    const source = route.source === 'group_override' ? '本群设置（优先）' : '成员与连接';
    const reports = Array.isArray(route.reported_models) ? route.reported_models.map(String).filter(Boolean) : [];
    if (route.reported_model && !reports.includes(String(route.reported_model))) reports.push(String(route.reported_model));
    const rows = [
      ['请求模型', requested],
      ['选择方式', automatic ? '自动 · ' + tier : '手动'],
      ['设置来源', source],
    ];
    if (route.reason) rows.push(['选择原因', String(route.reason)]);
    if (!preview) {
      if (route.simulation) rows.push(['运行情况', '演示模拟回复，未调用真实模型']);
      if (reports.length && route.report_kind === 'usage') rows.push(['CLI 报告使用模型', reports.join('、') + (reports.length > 1 ? '（可能包含多个模型）' : '')]);
      else if (reports.length && route.report_kind === 'configuration') rows.push(['CLI 配置回显', reports.join('、') + '（不代表实际后端模型）']);
      else rows.push(['实际模型回报', 'CLI 未报告']);
    }
    return h('details', { class: 'model-route' + (route.simulation ? ' simulated' : '') },
      h('summary', {},
        route.simulation ? h('span', { class: 'route-badge' }, '模拟') : null,
        h('span', {}, automatic ? '自动 · ' + tier : '手动'),
        h('span', { class: 'route-requested' }, '请求：' + requested),
        h('span', { class: 'route-expand' }, '详情')),
      h('dl', {}, rows.map(([label, value]) => [h('dt', {}, label), h('dd', {}, value)])),
      preview ? h('p', { class: 'meta' }, '此预览只计算选择结果，不调用模型、不消耗模型额度。') : null);
  }

  function incompleteNode(message) {
    if (!message.incomplete) return null;
    const member = message.retry_member;
    const gid = S.gid;
    const canRetry = MEMBERS.includes(member);
    const pending = (S.state.incomplete_members || {})[member];
    const current = canRetry && pending && Number(pending.seq) === Number(message.seq);
    return h('div', { class: 'incomplete-reply', 'data-incomplete-member': member || '', 'data-incomplete-gid': gid, 'data-incomplete-seq': message.seq },
      h('span', { class: 'incomplete-label' }, current ? '未完成' : '历史未完成记录'),
      canRetry ? h('button', { type: 'button', class: 'btn small', 'data-retry-member': member, 'data-retry-gid': gid, 'data-retry-seq': message.seq,
        hidden: !current,
        disabled: !!memberAvailability(member).paused,
        onclick: (event) => retryMember(gid, member, event.currentTarget) }, '补答此成员') : null,
      canRetry ? h('button', { type: 'button', class: 'link-btn incomplete-status-button', hidden: !current, onclick: openGroupModels }, '查看成员状态') : null,
      h('span', { class: 'incomplete-hint' }, current ? '补答只请求这位成员，读取最新共享上下文；不能恢复上次的内部推理。'
        : pending ? '此成员有更新的未完成记录，请在最新记录补答。' : '此成员当前没有待补答任务；这条历史失败记录仍会保留。'),
      canRetry ? h('span', { class: 'retry-pause-hint', 'data-retry-hint': member, hidden: !current || !memberAvailability(member).paused },
        '此成员已暂停，请先恢复调用。') : null);
  }

  async function retryMember(gid, member, button) {
    const pending = (S.state.incomplete_members || {})[member];
    if (S.gid !== gid || !pending || Number(pending.seq) !== Number(button.dataset.retrySeq)) {
      toast('待补答任务已变化，请查看此成员最新的未完成记录');
      return;
    }
    button.dataset.busy = 'true';
    button.disabled = true;
    try {
      await api('POST', '/api/groups/' + encodeURIComponent(gid) + '/retry/' + member, {});
      toast('已安排 ' + NAMES[member] + ' 单独补答');
      if (S.gid === gid) schedulePoll(200);
    } catch (e) { toast(e.message, true); }
    finally { delete button.dataset.busy; refreshRetryButtons(); }
  }

  function refreshRetryButtons() {
    document.querySelectorAll('[data-incomplete-member]').forEach((node) => {
      const member = node.dataset.incompleteMember;
      const pending = (S.state.incomplete_members || {})[member];
      const current = node.dataset.incompleteGid === S.gid && pending && Number(pending.seq) === Number(node.dataset.incompleteSeq);
      const paused = !!memberAvailability(member).paused;
      const button = node.querySelector('[data-retry-member]');
      if (button) {
        button.hidden = !current;
        button.disabled = !!button.dataset.busy || paused;
        button.title = paused ? '请先在模型设置中恢复此成员' : '只请求此成员补答，不自动调用其他成员';
      }
      node.querySelector('.incomplete-label').textContent = current ? '未完成' : '历史未完成记录';
      node.querySelector('.incomplete-hint').textContent = current
        ? '补答只请求这位成员，读取最新共享上下文；不能恢复上次的内部推理。'
        : pending ? '此成员有更新的未完成记录，请在最新记录补答。' : '此成员当前没有待补答任务；这条历史失败记录仍会保留。';
      const statusButton = node.querySelector('.incomplete-status-button');
      if (statusButton) statusButton.hidden = !current;
      const pauseHint = node.querySelector('[data-retry-hint]');
      if (pauseHint) pauseHint.hidden = !current || !paused;
    });
  }

  function messageNode(m) {
    if (m.kind === 'system' || m.incomplete) {
      const isError = /没能回复|出错了/.test(m.text);
      return h('div', { class: 'msg sys' + (m.subtle ? ' subtle' : '') + (isError ? ' error' : ''), 'data-seq': m.seq },
        h('span', { class: 'sys-text' }, m.text), incompleteNode(m), modelRouteNode(m.model_route));
    }
    if (m.author === 'user') {
      return h('div', { class: 'msg user m-user' + (m.board_note ? ' note' : ''), 'data-seq': m.seq },
        h('div', { class: 'msg-head' },
          m.board_note ? h('span', { class: 'tag' }, '看板操作') : null,
          h('time', {}, fmtDay(m.ts)),
          h('button', { type: 'button', class: 'link-btn msg-memory', onclick: () => editMemory(null, m) }, '存入记忆')),
        h('div', { class: 'msg-body md', html: md(m.text) }));
    }
    const tag = TASK_TAG[m.task];
    return h('div', { class: 'msg ai m-' + m.author, 'data-seq': m.seq },
      h('div', { class: 'msg-head' },
        h('span', { class: 'who' }, NAMES[m.author] || m.author),
        tag ? h('span', { class: 'tag' }, tag) : null,
        h('time', {}, fmtDay(m.ts)),
        m.elapsed ? h('span', { class: 'took' }, '用时 ' + fmtSeconds(m.elapsed)) : null,
        h('button', { type: 'button', class: 'link-btn msg-memory', onclick: () => editMemory(null, m) }, '存入记忆')),
      modelRouteNode(m.model_route),
      incompleteNode(m),
      h('div', { class: 'msg-body md', html: md(m.text) }),
      m.effects && m.effects.length ? h('div', { class: 'effects', 'aria-label': '看板变化' }, m.effects.map(effectChip)) : null,
      m.status_note ? h('div', { class: 'status-note' }, m.status_note) : null);
  }

  function appendMessages(list, stick) {
    const inner = $('#stream-inner');
    const stream = $('#stream');
    const nearBottom = stick === undefined ? atBottom() : stick;
    let added = 0;
    for (const m of list) {
      if (m.kind === 'chat' || m.kind === 'user') S.hasChat = true;
      if (S.seqs.has(m.seq)) continue;
      S.seqs.add(m.seq);
      S.lastSeq = Math.max(S.lastSeq, m.seq);
      inner.append(messageNode(m));
      added++;
    }
    if (!added) return;
    const empty = $('#empty-chat');
    if (empty) empty.remove();
    if (nearBottom || S.forceScroll) {
      S.forceScroll = false;
      stream.scrollTop = stream.scrollHeight;
      $('#new-pill').hidden = true;
    } else {
      $('#new-pill').hidden = false;
    }
  }

  function renderTyping() {
    const box = $('#typing');
    const st = S.state || {};
    $('#btn-stop').hidden = !st.running;
    $('#btn-continue').hidden = !!st.running;
    if (!S.gid || !st.running) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }
    box.hidden = false;
    const now = Date.now() / 1000 + S.offset;
    const speaking = (st.speaking || []).map((sp) => h('span', { class: 'm-' + sp.member },
      h('span', { class: 'pulse', 'aria-hidden': 'true' }),
      h('span', { class: 'who' }, NAMES[sp.member]),
      ' 正在想', h('time', {}, fmtDur(now - sp.since))));
    box.replaceChildren(h('div', { class: 'typing-inner' },
      speaking.length ? speaking : h('span', {}, '准备中……'),
      st.phase ? h('span', { class: 'phase' }, st.phase) : null,
      st.queued ? h('span', { class: 'phase' }, '还有 ' + st.queued + ' 轮在排队') : null));
  }

  // ------------------------------------------------------------ 顶栏
  const availabilityBusy = new Set();
  const availabilityEpoch = {};
  const quotaBusy = new Set();
  const PAUSE_REASONS = { quota: '额度限制', rate_limit: '调用频率限制', manual: '手动暂停' };

  function availabilityEpochSnapshot() {
    return Object.fromEntries(MEMBERS.map((member) => [member, availabilityEpoch[member] || 0]));
  }

  function acceptAvailability(values, snapshot) {
    if (!values) return;
    for (const member of MEMBERS) {
      if (values[member] && (!snapshot || snapshot[member] === (availabilityEpoch[member] || 0))) S.modelAvailability[member] = values[member];
    }
  }

  function acceptQuota(values) {
    if (!values) return;
    for (const member of MEMBERS) {
      if (!values[member]) continue;
      const existing = S.modelQuota[member];
      if (existing && existing.checked_at && values[member].checked_at && values[member].checked_at < existing.checked_at) continue;
      S.modelQuota[member] = values[member];
    }
  }

  function restorePanelFocus(node, hadFocus) {
    if (!hadFocus) return;
    node.tabIndex = -1;
    const target = node.querySelector('button:not(:disabled)') || node;
    target.focus({ preventScroll: true });
  }

  function memberAvailability(member) {
    return S.modelAvailability[member] || (S.members && S.members[member] && S.members[member].availability) || { paused: false };
  }

  function quotaPanel(member) {
    const node = h('div', { class: 'member-quota', 'data-quota-member': member });
    renderQuotaPanel(node);
    return node;
  }

  function quotaPercent(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100
      ? String(Math.round(value * 10) / 10) + '%' : '未知';
  }

  function quotaReset(value) {
    if (value == null || value === '') return '未知';
    const date = typeof value === 'number' ? new Date(value < 100000000000 ? value * 1000 : value) : new Date(value);
    return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString();
  }

  function renderQuotaPanel(node) {
    const member = node.dataset.quotaMember;
    const quota = S.modelQuota[member];
    const busy = quotaBusy.has(member);
    const signature = JSON.stringify([quota, busy, !!S.info.demo]);
    if (node.dataset.signature === signature) return;
    const hadFocus = node.contains(document.activeElement);
    node.dataset.signature = signature;
    const details = [];
    if (S.info.demo) details.push(h('p', { class: 'meta' }, '演示模式，不查询真实账号额度。'));
    else if (!quota) details.push(h('p', { class: 'meta' }, '剩余额度未知，尚未查询官方信息。'));
    else {
      if (quota.status === 'available') {
        const windows = Array.isArray(quota.windows) ? quota.windows : [];
        if (!windows.length) details.push(h('p', { class: 'meta' }, '官方未返回额度窗口，剩余额度未知。'));
        windows.forEach((window, index) => {
          const used = quotaPercent(window.used_percent);
          const remaining = quotaPercent(window.remaining_percent);
          const minutes = window.window_minutes;
          const duration = typeof minutes === 'number' && minutes > 0
            ? ' · ' + (minutes % 60 === 0 ? minutes / 60 + ' 小时窗口' : minutes + ' 分钟窗口') : '';
          details.push(h('div', { class: 'quota-window' },
            h('div', { class: 'quota-window-head' }, h('strong', {}, window.label || '额度窗口 ' + (index + 1)),
              duration ? h('span', { class: 'meta' }, duration) : null),
            h('div', { class: 'quota-percentages' }, h('span', {}, '已用 ' + used), h('span', {}, '剩余 ' + remaining)),
            used !== '未知' ? h('progress', { value: window.used_percent, max: 100, 'aria-label': (window.label || '额度窗口') + ' 已用比例' }) : null,
            h('p', { class: 'meta' }, '重置时间：' + quotaReset(window.resets_at))));
        });
      } else details.push(h('p', { class: quota.status === 'error' ? 'quota-error' : 'meta' },
        quota.status === 'error' ? '查询失败，剩余额度未知。' : '当前没有可安全读取的官方额度接口，剩余额度未知。'));
      if (quota.message) details.push(h('p', { class: 'quota-message' }, quota.message));
      if (quota.ordinary_usage_allowed === false) details.push(h('p', { class: 'quota-error' }, '官方返回：当前不允许常规调用。'));
      details.push(h('p', { class: 'meta quota-source' },
        '查询时间：' + (quota.checked_at ? quotaReset(quota.checked_at) : '未知') + ' · 来源：' + (quota.source || '未知')));
    }
    node.replaceChildren(h('div', { class: 'quota-head' }, h('strong', {}, '官方额度'),
      h('button', { type: 'button', class: 'btn small', disabled: busy || !!S.info.demo,
        'aria-label': NAMES[member] + ' 刷新官方额度', onclick: () => refreshQuota(member) }, busy ? '正在查询……' : '刷新官方额度')), ...details);
    restorePanelFocus(node, hadFocus);
  }

  async function refreshQuota(member) {
    if (quotaBusy.has(member) || S.info.demo) return;
    quotaBusy.add(member);
    refreshAvailabilityPanels();
    try {
      const data = await api('GET', '/api/members/' + member + '/quota?refresh=1');
      acceptQuota({ [member]: data.quota });
      await loadMembers(false);
    } catch (e) {
      S.modelQuota[member] = { status: 'error', message: e.message, windows: [], checked_at: Date.now() / 1000, source: '官方额度查询' };
    } finally { quotaBusy.delete(member); refreshAvailabilityPanels(); }
  }

  function availabilityPanel(member) {
    const node = h('div', { class: 'member-availability', 'data-availability-member': member });
    renderAvailabilityPanel(node);
    return node;
  }

  function renderAvailabilityPanel(node) {
    const member = node.dataset.availabilityMember;
    const availability = memberAvailability(member);
    const busy = availabilityBusy.has(member);
    const signature = JSON.stringify([availability.paused, availability.reason, availability.message, busy]);
    if (node.dataset.signature === signature) return;
    const hadFocus = node.contains(document.activeElement);
    node.dataset.signature = signature;
    node.classList.toggle('paused', !!availability.paused);
    const label = availability.paused ? '已暂停 · ' + (PAUSE_REASONS[availability.reason] || '等待恢复') : '未暂停';
    node.replaceChildren(...[
      h('div', { class: 'availability-state' }, h('span', { class: 'availability-indicator', 'aria-hidden': 'true' }),
        h('strong', {}, label),
        h('button', { type: 'button', class: 'btn small', disabled: busy,
          'aria-label': (availability.paused ? '恢复 ' : '暂停 ') + NAMES[member] + ' 调用',
          onclick: () => changeAvailability(member, !availability.paused) },
        busy ? '正在更新……' : availability.paused ? '恢复调用' : '暂停调用')),
      availability.paused && availability.message ? h('p', { class: 'availability-message' }, availability.message) : null].filter(Boolean));
    restorePanelFocus(node, hadFocus);
  }

  function refreshAvailabilityPanels() {
    document.querySelectorAll('[data-availability-member]').forEach(renderAvailabilityPanel);
    document.querySelectorAll('[data-quota-member]').forEach(renderQuotaPanel);
    refreshRetryButtons();
  }

  async function changeAvailability(member, paused) {
    if (availabilityBusy.has(member)) return;
    availabilityBusy.add(member);
    availabilityEpoch[member] = (availabilityEpoch[member] || 0) + 1;
    refreshAvailabilityPanels();
    try {
      const data = await api('POST', '/api/members/' + member + (paused ? '/pause' : '/resume'), {});
      availabilityEpoch[member]++;
      const availability = data.availability || data;
      if (typeof availability.paused === 'boolean') {
        S.modelAvailability[member] = availability;
        if (S.members && S.members[member]) S.members[member].availability = availability;
      } else await loadMembers(true);
      toast(NAMES[member] + (paused ? ' 已暂停后续调用，对所有群聊生效' : ' 已恢复，下一轮再尝试调用，对所有群聊生效'));
      renderHeader();
      renderBanner();
      if (S.gid) schedulePoll(200);
    } catch (e) { toast(e.message, true); }
    finally { availabilityBusy.delete(member); refreshAvailabilityPanels(); }
  }

  function memberIssue(m) {
    const availability = memberAvailability(m);
    if (availability.paused) return '已暂停（' + (PAUSE_REASONS[availability.reason] || '等待恢复') + '），可在模型设置中恢复后续调用';
    const det = S.members && S.members[m];
    if (!det || det.demo) return '';
    if (!det.installed) return '没有找到 ' + det.cli + '（命令 ' + det.command + '）';
    if (det.auth_ok === false) return det.auth;
    return '';
  }

  function renderHeader() {
    refreshAvailabilityPanels();
    const g = S.group;
    if (!g) return;
    $('#group-title').textContent = g.name;
    const mode = g.mode === 'parallel' ? '背靠背发言' + (g.parallel_review === false ? '' : '，答完互评') : '依次发言';
    const auto = g.auto_rounds ? '有人没表态或有分歧时自动续 ' + g.auto_rounds + ' 轮' : '不自动续轮';
    $('#group-sub').textContent = mode + '；' + auto;
    const speaking = new Set((S.state.speaking || []).map((s) => s.member));
    const seats = $('#seats');
    seats.replaceChildren(...MEMBERS.map((m) => {
      const inGroup = g.members.includes(m);
      const issue = memberIssue(m);
      const paused = memberAvailability(m).paused;
      const b = h('button', {
        type: 'button', class: 'seat m-' + m + (speaking.has(m) ? ' speaking' : '') + (paused ? ' paused' : ''),
        'aria-pressed': inGroup ? 'true' : 'false',
        title: (inGroup ? '点击让 ' + NAMES[m] + ' 退出本群' : '点击让 ' + NAMES[m] + ' 进群') + (issue ? '\n' + issue : ''),
      }, NAMES[m], paused ? h('span', { class: 'seat-paused' }, '暂停')
        : issue && inGroup ? h('span', { class: 'warn-dot', 'aria-label': '有问题' }) : null);
      b.addEventListener('click', quiet(() => toggleSeat(m)));
      return b;
    }));
    $('#mode-seg').querySelectorAll('button').forEach((b) => b.setAttribute('aria-checked', b.dataset.mode === g.mode ? 'true' : 'false'));
    const ats = $('#mention-row').querySelectorAll('.at-btn');
    ats.forEach((b) => { b.hidden = !g.members.includes(normalize(b.dataset.at)); });
  }

  function normalize(name) {
    return { claude: 'claude', gpt: 'gpt', gemini: 'gemini' }[String(name).toLowerCase()] || '';
  }

  function renderBanner() {
    const banner = $('#banner');
    const parts = [];
    let showSettings = false;
    if (!S.online) {
      parts.push('和程序的连接断开了，正在重试。如果关掉了程序窗口，请重新启动它。');
    } else {
      if (S.info.demo) parts.push('演示模式：成员的回复是模拟的，不调用真实命令行，也不消耗额度。');
      if (S.group) {
        const issues = S.group.members.map((m) => [m, memberIssue(m)]).filter(([, x]) => x);
        if (issues.length) {
          parts.push(issues.map(([m, x]) => NAMES[m] + '：' + x).join('；'));
          showSettings = true;
        }
      }
    }
    if (!parts.length) { banner.hidden = true; banner.replaceChildren(); return; }
    banner.hidden = false;
    const kids = [h('span', {}, parts.join(' '))];
    if (showSettings) {
      kids.push(h('button', { type: 'button', class: 'link-btn', onclick: openSetup }, '安装与登录'));
      kids.push(h('button', { type: 'button', class: 'link-btn', onclick: openSettings }, '打开「成员与连接」'));
    }
    banner.replaceChildren(...kids);
  }

  async function loadMembers(refresh) {
    const availabilitySnapshot = availabilityEpochSnapshot();
    try {
      const d = await api('GET', '/api/members' + (refresh ? '?refresh=1' : ''));
      S.members = {};
      d.members.forEach((m) => {
        S.members[m.id] = m;
        if (m.availability) acceptAvailability({ [m.id]: m.availability }, availabilitySnapshot);
      });
      renderBanner();
      renderHeader();
    } catch (e) { /* 忽略 */ }
  }

  // ------------------------------------------------------------ 看板
  function section(title, count, action) {
    return h('section', { class: 'b-sec' },
      h('div', { class: 'b-sec-head' },
        h('h3', {}, title, count ? h('span', { class: 'count' }, count) : null),
        action || null));
  }

  function fold(key, label, children) {
    const d = h('details', { class: 'fold' }, h('summary', {}, label), children);
    if (S.openFolds.has(key)) d.open = true;
    d.addEventListener('toggle', () => { if (d.open) S.openFolds.add(key); else S.openFolds.delete(key); });
    return d;
  }

  function votesRow(item, members) {
    return h('div', { class: 'votes', role: 'list', 'aria-label': '表态' }, members.map((m) => {
      const v = item.votes[m];
      const st = v ? v.vote : 'none';
      const mark = st === 'agree' ? '✓' : st === 'disagree' ? '✗' : '○';
      const label = st === 'agree' ? '同意' : st === 'disagree' ? '反对' : '未表态';
      return h('span', { class: 'vote m-' + m + ' ' + st, role: 'listitem', title: NAMES[m] + label },
        h('span', { class: 'v-mark', 'aria-hidden': 'true' }, mark), NAMES[m],
        h('span', { class: 'sr-only' }, label));
    }));
  }

  function reasons(item) {
    const list = Object.entries(item.votes || {}).filter(([, v]) => v.vote === 'disagree');
    if (!list.length) return null;
    return h('ul', { class: 'reasons' }, list.map(([m, v]) =>
      h('li', { class: 'm-' + m }, h('b', {}, NAMES[m] || m), ' 反对：', v.reason || '（未写理由）')));
  }

  function userNotes(item) {
    const notes = (item.user_notes || []).slice(-3);
    if (!notes.length) return null;
    return notes.map((n) => h('div', { class: 'user-note' + (n.pending ? ' pending' : '') },
      n.pending ? '你的意见（等成员回应）：' : '你说过：', n.text));
  }

  // 点击类操作的错误已经用提示显示过了，这里吞掉，避免控制台报未处理的异常
  const quiet = (fn) => (e) => { const r = fn(e); if (r && typeof r.catch === 'function') r.catch(() => {}); };

  function act(label, fn, cls) {
    return h('button', { type: 'button', class: 'link-btn' + (cls ? ' ' + cls : ''), onclick: quiet(fn) }, label);
  }

  function seqLink(seq) {
    if (!seq) return null;
    return h('button', { type: 'button', class: 'seq-link', 'data-seq': seq, title: '跳到聊天记录里的原话' }, '#' + seq);
  }

  function sealNote(d, members) {
    if (d.proposer === 'user') return '群主确定';
    const all = members.length && members.every((m) => d.votes[m] && d.votes[m].vote === 'agree');
    return all ? '全员同意' : '群主拍板';
  }

  function decisionNode(d, members) {
    const open = d.status === 'discussing';
    const label = d.withdrawn ? '已撤回' : DECISION_LABEL[d.status];
    const actions = [];
    if (open) {
      actions.push(act('提意见', () => commentItem(d)), act('直接生效', () => boardAction({ action: 'force', id: d.id }), 'strong'),
        act('否决', () => boardAction({ action: 'reject', id: d.id }), 'danger'));
    } else if (d.status === 'effective') {
      actions.push(act('提意见', () => commentItem(d)), act('重新讨论', () => boardAction({ action: 'reopen', id: d.id, respond: true })));
    } else {
      actions.push(act('重新讨论', () => boardAction({ action: 'reopen', id: d.id, respond: true })));
    }
    actions.push(act('更多', (e) => openMenu(e.currentTarget, [
      { label: '修改文字', onClick: () => editItemText(d) },
      { label: '删除', danger: true, onClick: () => confirmDelete(d) },
    ])));
    return h('article', { class: 'item st-' + d.status, id: 'item-' + d.id },
      h('div', { class: 'item-head' },
        h('span', { class: 'item-id' }, d.id),
        h('span', { class: 'state' + (open ? ' open' : '') }, label),
        h('span', {}, '提议：' + (NAMES[d.proposer] || d.proposer)), seqLink(d.seq)),
      h('p', { class: 'item-text', html: inline(d.text) }),
      open ? votesRow(d, members) : null,
      open ? reasons(d) : null,
      userNotes(d),
      h('div', { class: 'item-actions' }, actions),
      d.status === 'effective' ? h('span', { class: 'seal', 'aria-hidden': 'true' }, h('b', {}, '已生效'), h('small', {}, sealNote(d, members))) : null);
  }

  function taskNode(t, members) {
    const proposed = t.status === 'proposed';
    const active = ['todo', 'doing', 'blocked'].includes(t.status);
    const actions = [];
    if (proposed) {
      actions.push(act('提意见', () => commentItem(t)), act('确认分工', () => boardAction({ action: 'force', id: t.id }), 'strong'),
        act('取消', () => boardAction({ action: 'reject', id: t.id }), 'danger'));
    } else if (active) {
      const st = select([['todo', '待办'], ['doing', '进行中'], ['blocked', '阻塞'], ['done', '已完成'], ['cancelled', '取消']], t.status);
      st.setAttribute('aria-label', '修改状态');
      st.addEventListener('change', quiet(() => boardAction({ action: 'set_status', id: t.id, status: st.value })));
      const ow = select(MEMBERS.map((m) => [m, NAMES[m]]).concat([['user', '群主'], ['unassigned', '未指定']]), t.owner);
      ow.setAttribute('aria-label', '修改负责人');
      ow.addEventListener('change', quiet(() => boardAction({ action: 'set_owner', id: t.id, owner: ow.value })));
      actions.push(st, ow, act('提意见', () => commentItem(t)));
    } else {
      actions.push(act('重新打开', () => boardAction({ action: 'reopen', id: t.id, respond: true })));
    }
    actions.push(act('更多', (e) => openMenu(e.currentTarget, [
      { label: '修改内容', onClick: () => editItemText(t) },
      { label: '删除', danger: true, onClick: () => confirmDelete(t) },
    ])));
    const notes = (t.notes || []).slice(-2);
    return h('article', { class: 'item st-' + t.status + ' m-' + t.owner, id: 'item-' + t.id },
      h('div', { class: 'item-head' },
        h('span', { class: 'item-id' }, t.id),
        h('span', { class: 'state' + (proposed ? ' open' : '') + (t.status === 'blocked' ? ' blocked' : '') }, TASK_LABEL[t.status]),
        h('span', { class: 'owner m-' + t.owner }, '负责人 ' + (NAMES[t.owner] || t.owner)), seqLink(t.seq)),
      h('p', { class: 'item-text', html: inline(t.title) }),
      t.deliverable ? h('p', { class: 'item-sub' }, '交付：' + t.deliverable) : null,
      proposed ? votesRow(t, members) : null,
      proposed ? reasons(t) : null,
      notes.length ? h('div', { class: 'task-notes' }, notes.map((n) => h('div', {}, (NAMES[n.who] || n.who) + '：' + n.text, ' ', seqLink(n.seq)))) : null,
      userNotes(t),
      h('div', { class: 'item-actions' }, actions));
  }

  function renderBoard() {
    const body = $('#board-body');
    const rev = $('#board-rev');
    const b = S.board;
    if (!S.gid || !b || !S.group) {
      rev.textContent = '';
      body.replaceChildren(
        h('p', { class: 'b-empty' }, '看板是全群的共同记忆：群目标、决议、分工、进度和前情摘要都记在这里。'),
        h('p', { class: 'b-empty' }, '三家每次发言前都会先读它；决议在三家都同意后盖章生效，你也可以随时提意见或直接拍板。'));
      updateBadge(0);
      return;
    }
    const members = S.group.members;
    const scroll = body.scrollTop;
    const last = b.changelog && b.changelog.length ? b.changelog[b.changelog.length - 1].ts : 0;
    rev.textContent = last ? '更新于 ' + fmtDay(last) : '';

    // 群目标
    const goal = section('群目标', '', act('编辑', editGoal));
    goal.append(b.goal ? h('p', { class: 'goal-text' }, b.goal)
      : h('p', { class: 'goal-text placeholder' }, '还没写。写清楚想达成什么、范围和约束，三家会围绕它讨论。'));

    // 待你处理
    const needs = (b.need_user || []).filter((n) => !n.resolved);
    let needSec = null;
    if (needs.length) {
      needSec = section('待你处理', needs.length + ' 件');
      needSec.classList.add('needs');
      needs.forEach((n) => needSec.append(h('div', { class: 'need m-' + n.member },
        h('p', {}, h('span', { class: 'who' }, NAMES[n.member] || n.member), ' 想请你：', n.text, ' ', seqLink(n.seq)),
        h('div', { class: 'item-actions' },
          act('回复', () => replyNeed(n), 'strong'),
          act('标记已处理', () => boardAction({ action: 'resolve_need', id: n.id }))))));
    }

    // 决议
    const open = b.decisions.filter((d) => d.status === 'discussing');
    const eff = b.decisions.filter((d) => d.status === 'effective');
    const closed = b.decisions.filter((d) => d.status === 'rejected');
    const dec = section('决议', open.length ? open.length + ' 条讨论中' : (eff.length ? eff.length + ' 条已生效' : ''), act('添加决议', addDecision, 'strong'));
    if (!b.decisions.length) dec.append(h('p', { class: 'b-empty' }, '还没有决议。需要拍板的事由成员提出，在群的 AI 都同意后在这里盖章生效。'));
    open.forEach((d) => dec.append(decisionNode(d, members)));
    eff.forEach((d) => dec.append(decisionNode(d, members)));
    if (closed.length) dec.append(fold('dec-closed', '已否决或撤回的 ' + closed.length + ' 条', closed.map((d) => decisionNode(d, members))));

    // 任务
    const proposed = b.tasks.filter((t) => t.status === 'proposed');
    const active = b.tasks.filter((t) => ['todo', 'doing', 'blocked'].includes(t.status));
    const done = b.tasks.filter((t) => t.status === 'done');
    const cancelled = b.tasks.filter((t) => t.status === 'cancelled');
    const counts = [];
    if (proposed.length) counts.push(proposed.length + ' 项待确认');
    if (active.length) counts.push(active.length + ' 项进行中');
    const workable = active.filter((t) => ['todo', 'doing'].includes(t.status) && members.includes(t.owner));
    const tasks = section('分工与任务', counts.join('，'), h('span', { class: 'sec-actions' },
      workable.length ? act('让大家开工', kickOff) : null,
      act('分配任务', addTask, 'strong')));
    if (!b.tasks.length) tasks.append(h('p', { class: 'b-empty' }, '还没有任务。需要分工时，成员会提出任务和负责人，三家都同意后进入待办；你也可以直接分配。'));
    proposed.forEach((t) => tasks.append(taskNode(t, members)));
    active.forEach((t) => tasks.append(taskNode(t, members)));
    if (done.length) tasks.append(fold('task-done', '已完成 ' + done.length + ' 项', done.map((t) => taskNode(t, members))));
    if (cancelled.length) tasks.append(fold('task-cancelled', '已取消 ' + cancelled.length + ' 项', cancelled.map((t) => taskNode(t, members))));

    // 前情摘要
    const sum = section('前情摘要');
    if (b.summary) {
      sum.append(h('p', { class: 'summary-text' }, b.summary),
        h('p', { class: 'meta' }, '由 ' + (NAMES[b.summary_by] || '书记员') + ' 整理，涵盖到第 ' + b.summary_upto + ' 条消息'
          + (b.summary_ts ? '，' + fmtDay(b.summary_ts) : '')));
    } else {
      sum.append(h('p', { class: 'b-empty' }, '聊天变长以后，书记员会把较早的讨论压缩成摘要，三家发言时一并参考。'));
    }

    // 变更记录
    const logs = (b.changelog || []).slice(-40).reverse();
    const logSec = section('变更记录');
    logSec.append(fold('changelog', '最近 ' + logs.length + ' 条', h('ul', { class: 'log' }, logs.map((l) =>
      h('li', { class: 'm-' + l.who }, h('time', {}, fmtDay(l.ts)), h('span', {}, h('span', { class: 'who' }, NAMES[l.who] || l.who), l.text))))));

    body.replaceChildren(...[goal, needSec, dec, tasks, sum, logSec].filter(Boolean));
    body.scrollTop = scroll;
    updateBadge(needs.length);

    // 新生效的决议：盖章
    const now = new Set(eff.map((d) => d.id));
    if (S.effective) {
      now.forEach((id) => {
        if (!S.effective.has(id)) {
          const seal = document.querySelector('#item-' + id + ' .seal');
          if (seal) seal.classList.add('stamp');
        }
      });
    }
    S.effective = now;
  }

  function kickOff() {
    const input = $('#input');
    input.value = '请各位开工：用文字完成看板上分给自己的待办任务，完成后在状态更新里把任务标为完成，做不完就说明进展和卡点。';
    autosize();
    if (narrow()) setTab('chat');
    return send();
  }

  function focusMessage(seq) {
    if (narrow()) setTab('chat');
    const node = document.querySelector('.msg[data-seq="' + seq + '"]');
    if (!node) { toast('找不到第 ' + seq + ' 条消息'); return; }
    node.scrollIntoView({ block: 'center', behavior: reduceMotion() ? 'auto' : 'smooth' });
    node.classList.remove('flash');
    void node.offsetWidth;
    node.classList.add('flash');
  }

  function updateBadge(n) {
    const badge = $('#tab-badge');
    badge.hidden = !n;
    badge.textContent = n || '';
  }

  function focusItem(id) {
    if (narrow()) setTab('board');
    const node = document.getElementById('item-' + id);
    if (!node) { toast('看板上找不到 ' + id + '，可能已被删除'); return; }
    const details = node.closest('details');
    if (details) details.open = true;
    node.scrollIntoView({ block: 'center', behavior: reduceMotion() ? 'auto' : 'smooth' });
    node.classList.remove('flash');
    void node.offsetWidth;
    node.classList.add('flash');
  }

  // ------------------------------------------------------------ 看板操作
  async function boardAction(payload) {
    if (!S.gid) return;
    try {
      const d = await api('POST', '/api/groups/' + S.gid + '/board', payload);
      S.board = d.board;
      S.rev = d.board.rev;
      renderBoard();
      toast(d.result);
      schedulePoll(200);
    } catch (e) {
      toast(e.message, true);
      throw e;
    }
  }

  function quote(item) {
    return h('div', { class: 'user-note' }, item.id + '：' + (item.text || item.title));
  }

  function commentItem(item) {
    const text = textarea('', '比如：预算要控制在一万以内；或者：这条先不做，原因是……', 4);
    const respond = check('提交后让在群的 AI 先回应你的意见（推荐）', true);
    openModal({
      title: '对 ' + item.id + ' 提意见',
      body: [quote(item), h('p', {}, '提交后这一条会退回讨论，表态清零，成员需要回应你的意见并重新表态。'),
        field('你的意见', text), respond.node],
      actions: [{ label: '取消' }, {
        label: '提交意见', primary: true,
        onClick: () => {
          if (!text.value.trim()) throw new Error('请写下你的意见');
          return boardAction({ action: 'comment', id: item.id, text: text.value.trim(), respond: respond.input.checked });
        },
      }],
    });
  }

  function editItemText(item) {
    const text = textarea(item.text || item.title, '', 4);
    openModal({
      title: '修改 ' + item.id,
      body: [field('内容', text, '只改文字，不改变状态和表态。改动会记在变更记录里。')],
      actions: [{ label: '取消' }, {
        label: '保存', primary: true,
        onClick: () => boardAction({ action: 'edit', id: item.id, text: text.value.trim() }),
      }],
    });
  }

  function confirmDelete(item) {
    openModal({
      title: '删除 ' + item.id + '？',
      body: [quote(item), h('p', {}, '删除后看板上不再显示这一条（变更记录里会留下删除记录）。如果只是不同意，用「否决」或「取消」更合适。')],
      actions: [{ label: '取消' }, { label: '删除', danger: true, onClick: () => boardAction({ action: 'delete', id: item.id }) }],
    });
  }

  function editGoal() {
    const text = textarea(S.board.goal, '想达成什么？范围、约束、完成标准……', 6);
    const respond = check('保存后让在群的 AI 看一下新目标并回应', false);
    openModal({
      title: '群目标',
      body: [field('群目标', text), respond.node],
      actions: [{ label: '取消' }, {
        label: '保存', primary: true,
        onClick: () => boardAction({ action: 'set_goal', text: text.value.trim(), respond: respond.input.checked }),
      }],
    });
  }

  function addDecision() {
    const text = textarea('', '比如：第一版只支持网页端，移动端放到第二版', 4);
    const respond = check('添加后让在群的 AI 回应', false);
    openModal({
      title: '添加决议',
      body: [h('p', {}, '你直接添加的决议会立即生效，并通知到群里。想先听听三家的意见，可以直接在群里发消息。'),
        field('决议内容', text), respond.node],
      actions: [{ label: '取消' }, {
        label: '添加并生效', primary: true,
        onClick: () => {
          if (!text.value.trim()) throw new Error('请填写决议内容');
          return boardAction({ action: 'add_decision', text: text.value.trim(), respond: respond.input.checked });
        },
      }],
    });
  }

  function addTask() {
    const title = textInput('', '比如：整理三家竞品的定价');
    const owner = select(MEMBERS.map((m) => [m, NAMES[m]]).concat([['user', '群主（我自己）'], ['unassigned', '未指定']]), S.group.members[0] || 'unassigned');
    const deliverable = textInput('', '比如：一页对比表');
    const respond = check('添加后让在群的 AI 回应', false);
    openModal({
      title: '分配任务',
      body: [field('任务', title), h('div', { class: 'row' }, field('负责人', owner), field('交付物（可选）', deliverable)),
        h('p', {}, '你直接分配的任务会立即进入待办。'), respond.node],
      actions: [{ label: '取消' }, {
        label: '分配', primary: true,
        onClick: () => {
          if (!title.value.trim()) throw new Error('请填写任务内容');
          return boardAction({ action: 'add_task', title: title.value.trim(), owner: owner.value, deliverable: deliverable.value.trim(), respond: respond.input.checked });
        },
      }],
    });
  }

  function replyNeed(n) {
    const text = textarea('', '你的回复会发到群里', 4);
    const respond = check('发出后让在群的 AI 继续讨论', true);
    openModal({
      title: '回复 ' + (NAMES[n.member] || n.member),
      body: [h('div', { class: 'user-note' }, n.text), field('你的回复', text), respond.node],
      actions: [{ label: '取消' }, {
        label: '回复并标记已处理', primary: true,
        onClick: () => boardAction({ action: 'resolve_need', id: n.id, text: text.value.trim(), respond: respond.input.checked }),
      }],
    });
  }

  // ------------------------------------------------------------ 群操作
  function autosize() {
    const t = $('#input');
    t.style.height = 'auto';
    t.style.height = Math.min(220, t.scrollHeight + 2) + 'px';
  }

  function saveDraft() {
    if (!S.gid) return;
    const v = $('#input').value;
    store('aigc.draft.' + S.gid, v.trim() ? v : null);
  }

  async function send(e) {
    if (e) e.preventDefault();
    const input = $('#input');
    const text = input.value.trim();
    if (!text || !S.gid) return;
    const btn = $('#btn-send');
    btn.disabled = true;
    try {
      const d = await api('POST', '/api/groups/' + S.gid + '/messages', { text });
      input.value = '';
      autosize();
      store('aigc.draft.' + S.gid, null);
      S.forceScroll = true;
      S.state = Object.assign({}, S.state, { running: true });
      renderTyping();
      appendMessages([d.message]);
      schedulePoll(300);
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
      input.focus();
    }
  }

  async function patchGroup(changes, message) {
    const gid = S.gid;
    try {
      const d = await api('PATCH', '/api/groups/' + gid, changes);
      if (S.gid === gid) {
        S.group = d.group;
        renderHeader();
        renderBanner();
        schedulePoll(200);
      }
      if (message) toast(message);
      refreshGroups();
    } catch (e) {
      toast(e.message, true);
      throw e;
    }
  }

  function toggleSeat(m) {
    const set = new Set(S.group.members);
    const joining = !set.has(m);
    if (joining) set.add(m); else set.delete(m);
    return patchGroup({ members: MEMBERS.filter((x) => set.has(x)) }, NAMES[m] + (joining ? ' 已进群' : ' 已退出本群，之后不再发言和表态'));
  }

  async function continueDiscussion() {
    try {
      await api('POST', '/api/groups/' + S.gid + '/continue', {});
      S.state = Object.assign({}, S.state, { running: true });
      renderTyping();
      schedulePoll(300);
    } catch (e) { toast(e.message, true); }
  }

  async function stopRound() {
    try {
      await api('POST', '/api/groups/' + S.gid + '/stop', {});
      toast('已停止');
      schedulePoll(200);
    } catch (e) { toast(e.message, true); }
  }

  function cleanModelInput(value, label) {
    const model = value.trim();
    if (model && (model.length > 200 || !/^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]*(?:\[[1-9][0-9]*[kKmM]\])?$/.test(model))) {
      throw new Error(label + ' 应为不含空白、长度不超过 200 的模型 ID 或别名');
    }
    return model;
  }

  function groupModelFields(group) {
    const controls = {};
    const rows = MEMBERS.map((member) => {
      const current = (group.model_overrides || {})[member] || {};
      const mode = select([['inherit', '沿用成员设置'], ['auto', '自动选择档位'], ['manual', '手动指定模型']], current.mode || 'inherit');
      const model = textInput(current.model || '', '留空使用 CLI 默认模型');
      model.maxLength = 200;
      const modelField = field(NAMES[member] + ' 指定模型', model);
      const hint = h('p', { class: 'meta' });
      const sync = () => {
        modelField.hidden = mode.value !== 'manual';
        hint.textContent = mode.value === 'auto'
          ? '使用“成员与连接”中的三个模型档位；三个模型 ID 均须已配置。'
          : mode.value === 'manual' ? '本群指定优先于成员设置。留空时明确使用 CLI 默认模型。'
            : '本群跟随“成员与连接”中的选择方式和模型。';
      };
      mode.addEventListener('change', sync);
      sync();
      controls[member] = { mode, model };
      return h('div', { class: 'group-model-row m-' + member },
        h('div', { class: 'row' }, field(NAMES[member] + ' 模型策略', mode), modelField), hint,
        availabilityPanel(member), quotaPanel(member));
    });
    return {
      node: h('div', { class: 'group-model-fields' },
        h('p', { class: 'model-setting-note' }, '本群指定优先于成员设置。修改从下一轮开始生效。'), rows,
        h('p', { class: 'model-setting-note' }, '暂停和恢复影响所有群聊。暂停后其余成员可继续，该成员仍在群中，不会被算作同意。恢复后下一轮再尝试，不立即调用模型。'),
        h('p', { class: 'meta' }, '同一账号的模型可能共用额度，切换模型不保证恢复额度。')),
      value: () => {
        const result = {};
        for (const member of MEMBERS) {
          const control = controls[member];
          if (control.mode.value !== 'inherit') result[member] = {
            mode: control.mode.value, model: control.mode.value === 'manual' ? cleanModelInput(control.model.value, NAMES[member] + ' 指定模型') : '',
          };
        }
        return result;
      },
    };
  }

  function openGroupModels() {
    if (!S.group) return;
    const gid = S.gid;
    const controls = groupModelFields(S.group);
    const query = textarea($('#input').value || '', '输入一段准备讨论的问题，预览三家会选择哪个模型。', 3);
    query.maxLength = 12000;
    query.setAttribute('aria-label', '模型预览问题');
    const result = h('div', { class: 'model-preview-result', 'aria-live': 'polite' });
    const previewButton = h('button', { type: 'button', class: 'btn small' }, '预览已保存配置');
    let previewRequest = 0;
    const body = h('div', {}, controls.node,
      h('p', { class: 'model-setting-note' }, '自动选择只使用本地规则判断任务复杂度，不额外调用模型。模型 ID 必须受你的账号支持，节省额度并不保证。'),
      h('section', { class: 'model-preview' },
        h('h3', {}, '发送前预览'),
        h('p', { class: 'meta' }, '预览使用已保存配置，以上未保存的修改不会参与。预览本身不消耗模型额度。'),
        query, previewButton, result));
    const modal = openModal({ title: '本群模型', body, wide: true, actions: [
      { label: '关闭' },
      { label: '保存本群设置', primary: true, onClick: async () => {
        if (gid !== S.gid) throw new Error('当前群聊已改变，请重新打开模型设置');
        await patchGroup({ model_overrides: controls.value() }, '模型设置已保存，从下一轮生效');
      } },
    ] });
    previewButton.addEventListener('click', async () => {
      const request = ++previewRequest;
      previewButton.disabled = true;
      result.replaceChildren(h('p', { class: 'meta' }, '正在计算选择结果……'));
      try {
        const data = await api('POST', '/api/groups/' + encodeURIComponent(gid) + '/model-preview', { text: query.value.trim(), task: 'reply' });
        if (!modal.isConnected || request !== previewRequest) return;
        const providers = MEMBERS.filter((member) => (data.routes || {})[member] || (data.errors || {})[member]);
        result.replaceChildren(...providers.map((member) => h('article', { class: 'model-preview-card m-' + member },
          h('strong', { class: 'who' }, NAMES[member]),
          (data.errors || {})[member]
            ? h('p', { class: 'modal-error', role: 'alert' }, data.errors[member])
            : modelRouteNode(data.routes[member], true))));
        if (!providers.length) result.append(h('p', { class: 'meta' }, '暂无参与预览的成员，请先在群里开启至少一位。'));
      } catch (e) {
        if (modal.isConnected && request === previewRequest) result.replaceChildren(h('p', { class: 'modal-error', role: 'alert' }, e.message));
      } finally { if (request === previewRequest) previewButton.disabled = false; }
    });
    query.addEventListener('input', () => {
      previewRequest++;
      previewButton.disabled = false;
      result.replaceChildren(h('p', { class: 'meta' }, '问题已改变，请重新预览。'));
    });
  }

  function openGroupSettings() {
    const g = S.group;
    const name = textInput(g.name);
    const seats = seatPicker(g.members);
    const mode = segPicker([['sequential', '依次'], ['parallel', '背靠背']], g.mode);
    const review = check('背靠背答完后，让大家互评一轮', g.parallel_review !== false);
    const auto = select([0, 1, 2, 3, 4, 5].map((n) => [String(n), n ? '最多续 ' + n + ' 轮' : '不自动续轮']), String(g.auto_rounds == null ? 2 : g.auto_rounds));
    const length = select(Object.entries(REPLY_LENGTH), g.reply_length || 'medium');
    const sec = select([['rotate', '轮流担任']].concat(MEMBERS.map((m) => [m, NAMES[m]])), g.secretary || 'rotate');
    const lang = textInput(g.language || '简体中文');
    const project = projectPicker(g.project_id || g.id, g.name,
      S.hasChat ? '已有聊天记录，请新建群聊并选择该项目。' : S.state.running ? '讨论进行中，暂时不能切换项目。' : '');
    const modelFields = groupModelFields(g);
    openModal({
      title: '群设置',
      body: [
        field('群名', name),
        project.node,
        field('在群成员', seats.node, '不在群的成员不发言，也不参与表态。'),
        field('发言方式', mode.node, '依次：一个接一个，后面的人能看到前面的发言。背靠背：同时独立作答，互相看不到，适合要独立意见的问题。'),
        review.node,
        h('div', { class: 'row' },
          field('自动续轮', auto, '一轮结束后还有人没表态、或有反对意见时，自动再讨论几轮。'),
          field('每次发言长度', length)),
        h('div', { class: 'row' },
          field('书记员', sec, '负责把较早的讨论整理成前情摘要。'),
          field('讨论语言', lang)),
        h('details', { class: 'advanced group-model-settings' }, h('summary', {}, '本群模型选择'), modelFields.node),
      ],
      actions: [{ label: '取消' }, {
        label: '保存', primary: true,
        onClick: async () => patchGroup({
          name: name.value, members: seats.value(), mode: mode.value(), parallel_review: review.input.checked,
          auto_rounds: Number(auto.value), reply_length: length.value, secretary: sec.value, language: lang.value,
          project_id: await project.value(),
          model_overrides: modelFields.value(),
        }, '群设置已保存'),
      }],
    });
  }

  function openNewGroup() {
    const name = textInput('', '比如：AICRM 方案讨论');
    const goal = textarea('', '想达成什么？写清楚目标、范围和约束，三家会围绕它讨论', 4);
    const seats = seatPicker(MEMBERS);
    const mode = segPicker([['sequential', '依次'], ['parallel', '背靠背']], 'sequential');
    const first = textarea('', '可选。创建后立即发到群里，三家开始讨论', 3);
    const project = projectPicker('', '');
    openModal({
      title: '新建群聊',
      body: [
        field('群名', name),
        project.node,
        field('群目标（可选）', goal, '会写在共享看板最上面，三家每次发言前都会看到。'),
        field('成员', seats.node),
        field('发言方式', mode.node, '依次：一个接一个，后面的人能看到前面的发言。背靠背：同时独立作答，答完再互评。'),
        field('开场问题', first),
      ],
      actions: [{ label: '取消' }, {
        label: '创建', primary: true,
        onClick: async () => {
          if (!seats.value().length) throw new Error('至少选一位成员');
          const d = await api('POST', '/api/groups', {
            name: name.value.trim() || '新的群聊', goal: goal.value.trim(), members: seats.value(),
            mode: mode.value(), first_message: first.value.trim(),
            project_id: await project.value(),
          });
          await refreshGroups();
          await selectGroup(d.group.id);
        },
      }],
    });
  }

  function openMoreMenu(e) {
    const items = [];
    if (narrow()) {
      if (!S.state.running) items.push({ label: '继续讨论', onClick: continueDiscussion });
      const other = S.group.mode === 'parallel' ? 'sequential' : 'parallel';
      items.push({
        label: other === 'parallel' ? '改为背靠背发言' : '改为依次发言',
        onClick: () => patchGroup({ mode: other }, other === 'parallel' ? '改为背靠背发言' : '改为依次发言').catch(() => {}),
      });
    }
    openMenu(e.currentTarget, items.concat([
      { label: '本群模型', onClick: openGroupModels },
      { label: '共享记忆', onClick: openMemories },
      { label: '群设置', onClick: openGroupSettings },
      { label: '导出聊天记录和看板', onClick: exportGroup },
      { label: '删除这个群', danger: true, onClick: confirmDeleteGroup },
    ]));
  }

  function exportGroup() {
    const a = h('a', { href: '/api/groups/' + S.gid + '/export', download: (S.group.name || 'group') + '.md' });
    document.body.append(a);
    a.click();
    a.remove();
  }

  function confirmDeleteGroup() {
    openModal({
      title: '删除「' + S.group.name + '」？',
      body: [h('p', {}, '群会移到数据目录下的 archive 文件夹，不会彻底删除；需要时可以手动找回。')],
      actions: [{ label: '取消' }, {
        label: '删除', danger: true,
        onClick: async () => {
          await api('DELETE', '/api/groups/' + S.gid);
          S.gid = null;
          await refreshGroups();
          if (S.groups.length) await selectGroup(S.groups[0].id); else showWelcome();
          toast('已删除');
        },
      }],
    });
  }

  // ------------------------------------------------------------ 共享记忆
  const memoryView = { query: '', status: 'all' };

  function memoryPath(gid, id) {
    return '/api/groups/' + encodeURIComponent(gid) + '/memories' + (id ? '/' + encodeURIComponent(id) : '');
  }

  function projectPicker(current, currentName, lockedReason) {
    const choice = select(current ? [[current, currentName || '当前项目']] : [['', '独立项目（仅此群，之后可让新群加入）']], current);
    const name = textInput('', '新项目名称');
    name.maxLength = 80;
    const nameField = field('新项目名称', name);
    nameField.hidden = true;
    const hint = h('p', { class: 'meta', role: 'status' }, lockedReason || '正在读取项目……');
    const node = h('div', { class: 'project-picker' },
      field('所属项目', choice, '同一项目的群聊共享记忆；聊天记录和看板仍各自保留。'), nameField, hint);
    choice.disabled = !!lockedReason;
    let loaded = false;
    const ready = api('GET', '/api/projects').then((d) => {
      const options = current ? [] : [['', '独立项目（仅此群，之后可让新群加入）']];
      const projects = d.projects || [];
      if (current && !projects.some((p) => p.id === current)) options.push([current, currentName || '当前项目']);
      projects.forEach((p) => options.push([p.id, p.name]));
      options.push(['__new__', '＋ 新建共享项目']);
      choice.replaceChildren(...options.map(([v, label]) => h('option', { value: v }, label)));
      choice.value = current;
      loaded = true;
      hint.textContent = lockedReason || '选择已有项目，即可读取该项目已确认的记忆。';
    }).catch((e) => { hint.textContent = '项目列表加载失败：' + e.message + '。可关闭后重新打开。'; });
    choice.addEventListener('change', () => {
      nameField.hidden = choice.value !== '__new__';
      if (!nameField.hidden) name.focus();
    });
    return {
      node,
      value: async () => {
        if (lockedReason) return current;
        await ready;
        if (choice.value !== '__new__') return choice.value;
        if (!loaded) throw new Error('请等待项目列表加载完成');
        if (!name.value.trim()) throw new Error('请填写新项目名称');
        const d = await api('POST', '/api/projects', { name: name.value.trim() });
        choice.append(h('option', { value: d.project.id }, d.project.name));
        choice.value = d.project.id;
        nameField.hidden = true;
        return d.project.id;
      },
    };
  }

  function memorySource(memory) {
    const source = memory.source || {};
    const who = NAMES[source.author] || source.author || '手动保存';
    const label = source.seq ? who + ' · 消息 #' + source.seq : who;
    if (!source.group_id || !Number.isInteger(Number(source.seq)) || Number(source.seq) < 1) {
      return h('span', { class: 'meta' }, label);
    }
    return h('button', { type: 'button', class: 'link-btn memory-source', onclick: async () => {
      closeModal();
      await selectGroup(source.group_id);
      if (S.gid === source.group_id && S.group) focusMessage(Number(source.seq));
    } }, label + ' ↗');
  }

  async function openMemories() {
    if (!S.gid || !S.group) { toast('请先打开一个群聊'); return; }
    document.body.classList.remove('drawer-open');
    const gid = S.gid;
    const heading = h('div', { class: 'memory-overview' }, h('strong', {}, '正在读取项目……'));
    const search = textInput(memoryView.query, '搜索记忆，例如：预算、交付时间');
    search.setAttribute('aria-label', '搜索记忆');
    search.maxLength = 500;
    const status = select([['all', '全部状态'], ['confirmed', '已确认'], ['pending', '待确认']], memoryView.status);
    status.setAttribute('aria-label', '筛选记忆状态');
    const list = h('div', { class: 'memory-list', 'aria-live': 'polite' });
    const count = h('span', { class: 'meta' });
    const refresh = h('button', { type: 'button', class: 'btn small' }, '刷新');
    const body = h('div', { class: 'memory-browser' }, heading,
      h('p', { class: 'memory-description' }, '同一项目的群聊共享记忆；全局记忆会提供给所有项目。只有已确认的内容会进入后续对话。'),
      h('div', { class: 'memory-toolbar' }, search, status,
        h('button', { type: 'button', class: 'btn primary', onclick: () => editMemory() }, '添加记忆')),
      h('div', { class: 'memory-list-head' }, count, refresh), list,
      h('p', { class: 'meta' }, '记忆保存在本工具中，不会自动写入三家独立的官方 App。'));
    let request = 0;
    let timer;
    const load = async () => {
      const ticket = ++request;
      memoryView.query = search.value.trim();
      memoryView.status = status.value;
      refresh.disabled = true;
      list.setAttribute('aria-busy', 'true');
      if (!list.children.length) list.append(h('p', { class: 'memory-empty' }, '正在读取记忆……'));
      try {
        const d = await api('GET', memoryPath(gid) + '?q=' + encodeURIComponent(memoryView.query) + '&status=' + encodeURIComponent(status.value));
        if (ticket !== request || !body.isConnected) return;
        const entries = d.memories || [];
        heading.replaceChildren(h('span', { class: 'memory-symbol', 'aria-hidden': 'true' }, '◎'),
          h('div', {}, h('strong', {}, (d.project && d.project.name) || '项目记忆'),
            h('span', { class: 'meta' }, '跨会话保存 · 每轮按需读取')));
        count.textContent = entries.length + ' 条' + (memoryView.query ? '匹配结果' : '记忆') + ' · 含全局共享';
        list.replaceChildren(...entries.map((m) => h('article', { class: 'memory-card' + (m.status === 'pending' ? ' pending' : '') },
          h('div', { class: 'memory-meta' },
            h('span', { class: 'memory-state ' + m.status }, m.status === 'confirmed' ? '已确认' : '待确认'),
            h('span', { class: 'tag' }, MEMORY_KINDS[m.kind] || m.kind),
            h('span', { class: 'memory-scope' }, m.project_id === '*' ? '全局 · 所有项目' : '当前项目'),
            h('time', { class: 'meta' }, fmtDay(m.updated || m.created))),
          h('p', { class: 'memory-content' }, m.content),
          h('div', { class: 'memory-card-foot' }, memorySource(m),
            h('div', { class: 'item-actions' },
              m.status === 'pending' ? h('button', { type: 'button', class: 'link-btn strong', onclick: () => editMemory(m, null, true) }, '核实并确认') : null,
              h('button', { type: 'button', class: 'link-btn', onclick: () => editMemory(m) }, '编辑'),
              h('button', { type: 'button', class: 'link-btn', onclick: () => memoryHistory(m, gid) }, '历史'),
              h('button', { type: 'button', class: 'link-btn danger', onclick: () => deleteMemory(m, gid) }, '删除'))))));
        if (!entries.length) list.append(h('div', { class: 'memory-empty' },
          h('strong', {}, memoryView.query ? '没有找到匹配的记忆' : status.value === 'pending' ? '没有待确认内容' : '从一条重要信息开始'),
          h('p', {}, memoryView.query ? '试试更短的关键词，或切换状态筛选。' : '可以手动添加，也可以在聊天消息旁点击“存入记忆”。')));
      } catch (e) {
        if (ticket === request && body.isConnected) list.replaceChildren(h('p', { class: 'modal-error', role: 'alert' }, e.message));
      } finally {
        if (ticket === request) { refresh.disabled = false; list.removeAttribute('aria-busy'); }
      }
    };
    search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 250); });
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(timer); load(); } });
    status.addEventListener('change', load);
    refresh.addEventListener('click', load);
    openModal({ title: '共享记忆', body, wide: true, actions: [
      { label: '导出全部记忆', onClick: async () => { await exportMemories(); return true; } },
      { label: '预览上下文', onClick: () => { previewMemoryContext(gid, search.value); return true; } },
      { label: '关闭' },
    ] });
    await load();
  }

  function editMemory(memory, message, confirmCandidate) {
    if (!S.gid) return;
    const gid = S.gid;
    const original = memory ? memory.content : message ? message.text : '';
    const content = textarea(original, '保存一个明确的事实、决定、偏好或当前任务。', 7);
    content.maxLength = 4000;
    const length = h('span', { class: 'meta' });
    const updateLength = () => { length.textContent = content.value.length + ' / 4000 字'; };
    content.addEventListener('input', updateLength);
    updateLength();
    const kind = select(Object.entries(MEMORY_KINDS), memory ? memory.kind : 'note');
    const scope = select([['project', '当前项目'], ['global', '全局 · 所有项目']], memory && memory.project_id === '*' ? 'global' : 'project');
    scope.disabled = !!memory;
    const confirmed = check('我已核实，允许三家在后续对话中引用', confirmCandidate || !memory || memory.status === 'confirmed');
    const conflict = h('div', { class: 'memory-conflict', hidden: true, role: 'alert' });
    let version = memory && memory.version;
    let conflictUnresolved = false;
    const body = [
      memory ? memorySource(memory) : message ? h('p', { class: 'meta' }, '来源：' + (NAMES[message.author] || message.author) + ' · 消息 #' + message.seq) : null,
      field('记忆内容', content), length,
      original.length > 4000 ? h('p', { class: 'modal-error' }, '原消息超过 4000 字，请保留需要记住的部分后保存。') : null,
      h('div', { class: 'row' }, field('类型', kind), field('共享范围', scope)),
      h('p', { class: 'meta' }, '项目记忆可供同一项目的其他群聊读取。全局记忆将提供给所有项目，请只保存通用偏好。'),
      confirmed.node,
      message && message.author !== 'user' ? h('p', { class: 'meta' }, 'AI 的回答可能有误，请核实后再确认为记忆。') : null,
      conflict,
    ];
    const editorDialog = openModal({ title: memory ? confirmCandidate ? '核实这条记忆' : '编辑记忆' : '添加记忆', body, actions: [
      { label: '返回记忆列表', onClick: () => { openMemories(); return true; } },
      { label: '保存', primary: true, onClick: async () => {
        if (S.gid !== gid) throw new Error('当前群聊已改变，请重新打开记忆');
        if (conflictUnresolved) throw new Error('请先处理下方的版本冲突，你的草稿仍在。');
        const text = content.value.trim();
        if (!text) throw new Error('请填写记忆内容');
        if (text.length > 4000) throw new Error('内容最多 4000 字，请精简后保存');
        const payload = { content: text, kind: kind.value, status: confirmed.input.checked ? 'confirmed' : 'pending' };
        if (memory) payload.version = version;
        else {
          payload.scope = scope.value;
          if (message) payload.source_seq = message.seq;
        }
        try {
          await api(memory ? 'PATCH' : 'POST', memoryPath(gid, memory && memory.id), payload);
          toast(confirmed.input.checked ? '已保存，后续对话可读取这条记忆' : '已保存为待确认，暂不用于回答');
          if (editorDialog.isConnected && S.gid === gid) openMemories();
          return true;
        } catch (e) {
          if (memory && e.status === 409) {
            conflictUnresolved = true;
            conflict.hidden = false;
            conflict.replaceChildren(h('strong', {}, '这条记忆已被其他操作更新。你的草稿已保留。'));
            try {
              const d = await api('GET', memoryPath(gid));
              const latest = (d.memories || []).find((m) => m.id === memory.id);
              if (!latest) conflict.append(h('p', {}, '记忆已删除。你可以复制草稿后返回列表。'));
              else {
                const resolve = (useLatest) => {
                  if (useLatest) {
                    content.value = latest.content;
                    kind.value = latest.kind;
                    confirmed.input.checked = latest.status === 'confirmed';
                    updateLength();
                  }
                  version = latest.version;
                  conflictUnresolved = false;
                  conflict.replaceChildren(h('p', {}, '已基于最新版本继续编辑。请核对内容，再点击保存。'));
                };
                conflict.append(h('p', { class: 'meta' }, '服务器上的最新内容：'), h('p', { class: 'memory-content' }, latest.content),
                  h('div', { class: 'item-actions' },
                    h('button', { type: 'button', class: 'btn small', onclick: () => resolve(true) }, '采用最新内容'),
                    h('button', { type: 'button', class: 'btn small', onclick: () => resolve(false) }, '保留草稿，基于新版编辑')));
              }
            } catch (loadError) { conflict.append(h('p', {}, '无法读取最新版本：' + loadError.message)); }
          }
          throw e;
        }
      } },
    ] });
  }

  function deleteMemory(memory, gid) {
    const dialog = openModal({ title: '删除这条记忆？', body: [
      h('p', { class: 'memory-content' }, memory.content),
      memory.project_id === '*' ? h('p', { class: 'memory-warning' }, '这是全局记忆，删除将影响所有项目。') : null,
      h('p', {}, '这条记忆及其修改历史将被删除。原聊天记录仍会保留，已经发送给模型的内容也不会被撤回。'),
    ], actions: [
      { label: '返回', onClick: () => { openMemories(); return true; } },
      { label: '删除记忆', danger: true, onClick: async () => {
        try { await api('DELETE', memoryPath(gid, memory.id), { version: memory.version }); }
        catch (e) {
          if (e.status === 409) e.message = '记忆已经更新，本次未删除。请返回列表核对最新内容后再操作。';
          throw e;
        }
        toast('记忆及修改历史已删除');
        if (dialog.isConnected && S.gid === gid) openMemories();
        return true;
      } },
    ] });
  }

  async function memoryHistory(memory, gid) {
    const body = h('div', {}, h('p', {}, '正在读取历史……'));
    openModal({ title: '记忆修改历史', body, wide: true, actions: [
      { label: '返回记忆列表', onClick: () => { openMemories(); return true; } },
    ] });
    try {
      const d = await api('GET', memoryPath(gid, memory.id) + '/history');
      if (!body.isConnected) return;
      const versions = d.history || [];
      body.replaceChildren(...versions.slice().sort((a, b) => b.version - a.version).map((entry) => {
        const m = entry.snapshot || entry;
        return h('article', { class: 'memory-card' },
          h('div', { class: 'memory-meta' }, h('strong', {}, '版本 ' + (entry.version || m.version)),
            h('span', { class: 'meta' }, m.status === 'confirmed' ? '已确认' : '待确认'),
            h('time', { class: 'meta' }, fmtDay(entry.updated || entry.created || m.updated || m.created))),
          h('p', { class: 'memory-content' }, m.content));
      }));
      if (!versions.length) body.append(h('p', {}, '暂无修改历史。'));
    } catch (e) { if (body.isConnected) body.replaceChildren(h('p', { class: 'modal-error', role: 'alert' }, e.message)); }
  }

  async function previewMemoryContext(gid, query) {
    const question = textInput(query || '', '输入接下来要讨论的问题');
    question.maxLength = 1000;
    const output = textarea('', '', 15);
    output.readOnly = true;
    output.setAttribute('aria-label', '将提供给模型的记忆上下文');
    output.className = 'memory-context';
    const meta = h('p', { class: 'meta', role: 'status' }, '正在检索……');
    const refresh = h('button', { type: 'button', class: 'btn' }, '重新检索');
    const body = h('div', {}, h('p', { class: 'memory-description' }, '这里只预览共享记忆。实际发言还会附上本群看板、摘要和最近聊天。'),
      h('div', { class: 'memory-toolbar' }, question, refresh), meta, output);
    let request = 0;
    const load = async () => {
      const ticket = ++request;
      refresh.disabled = true;
      output.value = '';
      meta.textContent = '正在检索……';
      try {
        const d = await api('GET', '/api/groups/' + encodeURIComponent(gid) + '/memory-context?q=' + encodeURIComponent(question.value.trim()));
        if (!body.isConnected || ticket !== request) return;
        output.value = d.text || '';
        meta.textContent = (d.items || []).length + ' 条已确认记忆' + (d.truncated ? ' · 已按长度预算截取部分内容' : '') + (output.value ? '' : ' · 暂无相关内容');
      } catch (e) { if (body.isConnected && ticket === request) meta.textContent = '检索失败：' + e.message; }
      finally { if (ticket === request) refresh.disabled = false; }
    };
    refresh.addEventListener('click', load);
    question.addEventListener('input', () => {
      request++;
      refresh.disabled = false;
      output.value = '';
      meta.textContent = '问题已改变，请重新检索。';
    });
    question.addEventListener('keydown', (e) => { if (e.key === 'Enter') load(); });
    openModal({ title: '预览记忆上下文', body, wide: true, actions: [
      { label: '返回记忆列表', onClick: () => { openMemories(); return true; } },
      { label: '复制上下文', primary: true, onClick: async () => {
        if (!output.value) throw new Error('当前没有可复制的内容');
        try {
          if (!navigator.clipboard) throw new Error('clipboard unavailable');
          await navigator.clipboard.writeText(output.value);
          toast('上下文已复制');
        } catch (e) { output.focus(); output.select(); toast('已选中内容，请使用系统复制操作'); }
        return true;
      } },
    ] });
    await load();
  }

  async function exportMemories() {
    const data = await api('GET', '/api/memories/export');
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' }));
    const a = h('a', { href: url, download: 'shared-memory-' + new Date().toISOString().slice(0, 10) + '.json' });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('已导出全部项目的记忆与历史');
  }

  // ------------------------------------------------------------ 安装、登录与联网设置
  const SETUP_STATUS = { launching: '正在打开终端', running: '流程进行中', completed: '流程结束', failed: '流程失败', cancelled: '已取消' };
  const PROXY_MODES = { system: '继承启动环境（默认）', direct: '直连（清除代理环境）', custom: '自定义代理' };

  function cleanSetupProxy(mode, value) {
    const url = value.trim();
    if (mode !== 'custom') return { mode, url: '' };
    let parsed;
    try { parsed = new URL(url); } catch (e) { throw new Error('请输入完整的 HTTP(S) 代理地址，例如 http://127.0.0.1:7890'); }
    const endpoint = url.match(/^https?:\/\/(?:\[[^\]]+\]|[^@:/?#\\]+):(\d{1,5})\/?$/i);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || !endpoint
        || !['', '/'].includes(parsed.pathname) || parsed.search || parsed.hash
        || /\s/.test(url) || Number(endpoint[1]) < 1 || Number(endpoint[1]) > 65535) {
      throw new Error('代理必须使用 HTTP 或 HTTPS、明确填写端口，且不能包含用户名、密码、路径或 SOCKS 协议');
    }
    return { mode, url };
  }

  function setupCommand(label, command) {
    const code = h('textarea', { class: 'setup-command', rows: 2, readonly: true, 'aria-label': label });
    code.value = command || '当前平台未提供此命令';
    const copy = h('button', { type: 'button', class: 'btn small', disabled: !command }, '复制命令');
    copy.addEventListener('click', async () => {
      try {
        if (!navigator.clipboard) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(command);
        toast('命令已复制，请在运行本工具的电脑上执行');
      } catch (e) { code.focus(); code.select(); toast('已选中命令，请使用系统复制操作'); }
    });
    return h('div', { class: 'setup-command-block' }, h('div', { class: 'setup-command-head' }, h('strong', {}, label), copy), code);
  }

  async function openSetup() {
    document.body.classList.remove('drawer-open');
    const body = h('div', { class: 'setup-wizard' }, h('p', {}, '正在读取安装环境……'));
    let overview = null;
    let proxyMode = null;
    let proxyUrl = null;
    let proxyForm = null;
    let proxyBusy = false;
    let verificationBusy = false;
    let verifyAgain = false;
    let verificationError = '';
    let overviewRequest = 0;
    let proxyEditVersion = 0;
    const jobs = {};
    const jobErrors = {};
    const actionBusy = new Set();
    const networkBusy = new Set();
    const networkResults = {};
    const timers = {};
    const cards = {};
    const finished = new Set();
    const proxyNotice = h('p', { class: 'meta setup-proxy-notice', role: 'status' });
    const proxyPlatformNote = h('p', { class: 'hint-box setup-network-note', hidden: true });
    const hostNotice = h('div', { class: 'hint-box setup-host-notice', role: 'status' });
    const verifyButton = h('button', { type: 'button', class: 'btn small' }, '重新检查安装与登录');
    let modal;

    function running(job) { return job && ['launching', 'running'].includes(job.status); }

    function acceptJob(member, job) {
      if (!job) return;
      const old = jobs[member];
      if (old && old.id === job.id && old.updated_at > job.updated_at) return;
      if (old && old.id !== job.id && old.created_at > job.created_at) return;
      jobs[member] = job;
    }

    function renderCard(member) {
      const card = cards[member];
      if (!card || !overview) return;
      const info = (overview.members || {})[member] || {};
      const job = jobs[member];
      const active = running(job);
      const busy = actionBusy.has(member) || proxyBusy;
      const detection = S.members && S.members[member];
      card.installed.textContent = info.installed ? '已检测到 ' + (info.command || 'CLI') : '尚未检测到 CLI';
      card.installed.className = 'setup-install-state ' + (info.installed ? 'installed' : 'missing');
      card.setup.disabled = !overview.can_launch || busy || active;
      card.login.disabled = !overview.can_launch || !info.installed || busy || active;
      card.network.disabled = !!S.info.demo || busy || networkBusy.has(member);
      card.network.textContent = networkBusy.has(member) ? '正在测试网络……' : '保存并测试网络';
      card.cancel.hidden = !active;
      card.cancel.disabled = actionBusy.has(member);
      card.skip.textContent = info.installed ? '已检测到 CLI；一键流程会跳过安装，直接进入登录。' : '一键流程会先安装 CLI，再打开登录。';
      const jobSignature = JSON.stringify([job && job.status, job && job.message, jobErrors[member]]);
      if (card.job.dataset.signature !== jobSignature) card.job.replaceChildren(...(job ? [
        h('strong', { class: 'setup-job-state ' + job.status }, SETUP_STATUS[job.status] || '状态未知'),
        job.message ? h('p', {}, job.message) : null,
        active ? h('p', { class: 'meta' }, '请在打开的终端和官方浏览器页面完成操作。关闭此面板不代表取消流程。') : null,
        active ? h('p', { class: 'meta' }, '取消会请求停止当前流程；已经安装的软件不会回滚或卸载。') : null,
        job.status === 'completed' ? h('p', { class: 'meta' }, '流程结束不等于登录成功，请查看下方核验结果。') : null,
        jobErrors[member] ? h('p', { class: 'setup-error' }, jobErrors[member]) : null,
      ].filter(Boolean) : []));
      card.job.dataset.signature = jobSignature;
      card.job.hidden = !job;
      if (verificationBusy) card.auth.textContent = '正在核验命令与登录状态……';
      else if (verificationError) card.auth.textContent = '暂时无法核验登录：' + verificationError;
      else if (S.info.demo || (detection && detection.demo)) card.auth.textContent = '演示模式，不代表真实安装或登录状态。';
      else if (!detection) card.auth.textContent = '登录状态尚未核验。';
      else if (!detection.installed) card.auth.textContent = '未检测到命令；安装后请重新检查，必要时重新启动本工具。';
      else if (member === 'gemini') card.auth.textContent = 'Gemini 登录状态无法自动确认。请在官方 CLI 中核对；需要实际测试时可到“成员与连接”手动测试，会消耗额度。';
      else if (detection.auth_ok === true) card.auth.textContent = '已核验登录：' + (detection.auth || 'CLI 已确认账号登录');
      else card.auth.textContent = detection.auth || '登录状态未确认，请完成官方登录后重新检查。';
      const network = networkResults[member];
      card.networkResult.replaceChildren(...(network ? [
        h('strong', { class: network.ok ? 'setup-network-ok' : 'setup-error' }, network.ok ? '网络检查通过' : '网络检查未通过'),
        h('p', {}, network.message || '未返回检查说明'),
        h('p', { class: 'meta' }, '目标：' + (network.endpoint || '未知') + ' · 联网方式：' + (PROXY_MODES[network.proxy_mode] || network.proxy_mode || '未知')),
        network.checked_at ? h('p', { class: 'meta' }, '检查时间：' + quotaReset(network.checked_at)) : null,
        h('p', { class: 'meta' }, '网络可达不代表已经登录，也不代表账号还有模型额度。'),
      ].filter(Boolean) : []));
      card.networkResult.hidden = !network;
    }

    function renderAll() {
      MEMBERS.forEach(renderCard);
      verifyButton.disabled = verificationBusy;
      verifyButton.textContent = verificationBusy ? '正在核验……' : '重新检查安装与登录';
      if (proxyMode) {
        proxyMode.disabled = proxyBusy || actionBusy.size > 0;
        proxyUrl.disabled = proxyBusy || actionBusy.size > 0;
        proxyForm.hidden = proxyMode.value !== 'custom';
      }
      if (overview) {
        proxyPlatformNote.hidden = !overview.network_note;
        proxyPlatformNote.textContent = overview.network_note || '';
        hostNotice.textContent = overview.can_launch
          ? '终端将打开在运行本工具的电脑上。请亲自在官方浏览器页面完成账号登录，本网页不收集密码或验证码。'
          : (overview.reason || '当前环境无法打开本机终端。') + ' 可以复制下方官方命令，在运行本工具的电脑终端中操作。';
      }
    }

    async function saveProxy(announce) {
      if (!proxyMode) throw new Error('设置仍在加载，请稍等');
      if (proxyBusy) throw new Error('正在保存联网设置，请稍等');
      const proxy = cleanSetupProxy(proxyMode.value, proxyUrl.value);
      proxyBusy = true;
      renderAll();
      try {
        const data = await api('POST', '/api/setup/proxy', { proxy });
        const saved = data.proxy || proxy;
        proxyMode.value = saved.mode;
        proxyUrl.value = saved.url || '';
        proxyNotice.textContent = '已保存：' + (PROXY_MODES[saved.mode] || saved.mode) + '。后续启动的 CLI 与安装流程使用此设置。';
        if (announce) toast('联网设置已保存');
        return saved;
      } finally { proxyBusy = false; renderAll(); }
    }

    async function verifyMembers() {
      if (!modal.isConnected) return;
      if (verificationBusy) { verifyAgain = true; return; }
      verificationBusy = true;
      verificationError = '';
      renderAll();
      const availabilitySnapshot = availabilityEpochSnapshot();
      try {
        const data = await api('GET', '/api/members?refresh=1');
        if (!modal.isConnected) return;
        S.members = {};
        data.members.forEach((member) => {
          S.members[member.id] = member;
          if (member.availability) acceptAvailability({ [member.id]: member.availability }, availabilitySnapshot);
        });
        renderHeader();
        renderBanner();
      } catch (e) { verificationError = e.message; }
      finally {
        verificationBusy = false;
        if (modal.isConnected) {
          renderAll();
          if (verifyAgain) { verifyAgain = false; await verifyMembers(); }
        }
      }
    }

    async function refreshOverview() {
      const request = ++overviewRequest;
      const data = await api('GET', '/api/setup');
      if (!modal.isConnected || request !== overviewRequest) return;
      overview = data;
      for (const member of MEMBERS) acceptJob(member, ((data.members || {})[member] || {}).active_job);
      renderAll();
      for (const member of MEMBERS) {
        // An active job may have been started in another tab, or just disappeared
        // from the overview after finishing. Read its authoritative final state.
        if (running(jobs[member])) pollJob(member, jobs[member].id, 500);
      }
    }

    async function finishJob(member, job) {
      if (finished.has(job.id)) return;
      finished.add(job.id);
      if (!modal.isConnected) return;
      try { await refreshOverview(); } catch (e) { jobErrors[member] = '无法刷新安装状态：' + e.message; renderCard(member); }
      if (modal.isConnected) await verifyMembers();
    }

    function pollJob(member, jobId, delay) {
      clearTimeout(timers[member]);
      timers[member] = setTimeout(async () => {
        if (!modal.isConnected || !jobs[member] || jobs[member].id !== jobId) return;
        try {
          const data = await api('GET', '/api/setup/jobs/' + encodeURIComponent(jobId));
          if (!modal.isConnected || !jobs[member] || jobs[member].id !== jobId) return;
          acceptJob(member, data.job);
          delete jobErrors[member];
          renderCard(member);
          if (running(jobs[member])) pollJob(member, jobId, 1600);
          else await finishJob(member, jobs[member]);
        } catch (e) {
          if (!modal.isConnected) return;
          jobErrors[member] = '暂时无法获取流程状态：' + e.message + '。正在重试。';
          renderCard(member);
          pollJob(member, jobId, 4000);
        }
      }, delay || 600);
    }

    async function start(member, action) {
      if (!overview || !overview.can_launch || actionBusy.has(member)) return;
      actionBusy.add(member);
      delete jobErrors[member];
      cards[member].error.textContent = '';
      renderAll();
      try {
        await saveProxy(false);
        if (!modal.isConnected) return;
        const data = await api('POST', '/api/setup/' + member + '/start', { action });
        acceptJob(member, data.job);
        if (!modal.isConnected) return;
        renderCard(member);
        if (running(data.job)) pollJob(member, data.job.id);
        else await finishJob(member, data.job);
      } catch (e) {
        if (modal.isConnected) {
          jobErrors[member] = e.message;
          cards[member].error.textContent = e.message;
        }
      } finally { actionBusy.delete(member); if (modal.isConnected) renderAll(); }
    }

    async function cancel(member) {
      const job = jobs[member];
      if (!running(job) || actionBusy.has(member)) return;
      actionBusy.add(member);
      cards[member].error.textContent = '';
      let submitted = false;
      renderCard(member);
      try {
        await api('POST', '/api/setup/jobs/' + encodeURIComponent(job.id) + '/cancel', {});
        submitted = true;
        if (!modal.isConnected) return;
        jobErrors[member] = '已请求取消，正在确认流程是否停止。';
        renderCard(member);
        const data = await api('GET', '/api/setup/jobs/' + encodeURIComponent(job.id));
        acceptJob(member, data.job);
        if (!modal.isConnected) return;
        delete jobErrors[member];
        clearTimeout(timers[member]);
        renderCard(member);
        if (running(data.job)) pollJob(member, job.id);
        else await finishJob(member, data.job);
      } catch (e) {
        if (modal.isConnected) {
          cards[member].error.textContent = (submitted ? '取消已提交，但暂时无法确认是否停止：' : '') + e.message;
          if (submitted && running(jobs[member])) pollJob(member, job.id, 1600);
        }
      }
      finally { actionBusy.delete(member); if (modal.isConnected) renderCard(member); }
    }

    async function testNetwork(member) {
      if (networkBusy.has(member) || S.info.demo) return;
      networkBusy.add(member);
      const editVersion = proxyEditVersion;
      delete networkResults[member];
      cards[member].error.textContent = '';
      renderCard(member);
      try {
        await saveProxy(false);
        if (!modal.isConnected) return;
        const data = await api('POST', '/api/setup/' + member + '/network', {});
        if (modal.isConnected && proxyEditVersion === editVersion) networkResults[member] = data;
      } catch (e) { if (modal.isConnected && proxyEditVersion === editVersion) cards[member].error.textContent = e.message; }
      finally { networkBusy.delete(member); if (modal.isConnected) renderCard(member); }
    }

    modal = openModal({ title: '安装与登录', body, wide: true, actions: [
      { label: '关闭' },
      { label: '保存联网设置', primary: true, onClick: async () => { await saveProxy(true); return true; } },
    ] });
    let settings;
    try {
      [overview, settings] = await Promise.all([api('GET', '/api/setup'), api('GET', '/api/settings')]);
    } catch (e) {
      if (modal.isConnected) body.replaceChildren(h('p', { class: 'setup-error', role: 'alert' }, '无法读取安装设置：' + e.message),
        h('button', { type: 'button', class: 'btn', onclick: openSetup }, '重新加载'));
      return;
    }
    if (!modal.isConnected) return;
    const proxy = settings.proxy || { mode: 'system', url: '' };
    proxyMode = select([['system', '继承启动环境（默认）'], ['direct', '直连（清除代理环境）'], ['custom', '自定义 HTTP(S) 代理']], proxy.mode || 'system');
    proxyUrl = textInput(proxy.url || '', '例如 http://127.0.0.1:7890');
    proxyUrl.maxLength = 512;
    proxyForm = field('代理地址', proxyUrl, '仅填你已在使用的代理地址。不会自动探测端口；不支持 SOCKS 或带用户名、密码的地址。');
    proxyForm.hidden = proxyMode.value !== 'custom';
    const proxyChanged = () => {
      proxyEditVersion++;
      MEMBERS.forEach((member) => { delete networkResults[member]; });
      proxyForm.hidden = proxyMode.value !== 'custom';
      proxyNotice.textContent = '联网设置尚未保存。开始安装、登录或网络测试时会先保存当前设置。';
      renderAll();
    };
    proxyMode.addEventListener('change', proxyChanged);
    proxyUrl.addEventListener('input', proxyChanged);
    const sections = MEMBERS.map((member) => {
      const info = (overview.members || {})[member] || {};
      const installed = h('span', { class: 'setup-install-state' });
      const setupButton = h('button', { type: 'button', class: 'btn primary', onclick: () => start(member, 'setup'), 'aria-label': NAMES[member] + ' 一键安装并登录' }, '一键安装并登录');
      const loginButton = h('button', { type: 'button', class: 'btn', onclick: () => start(member, 'login'), 'aria-label': NAMES[member] + ' 仅打开登录' }, '仅打开登录');
      const networkButton = h('button', { type: 'button', class: 'btn small', onclick: () => testNetwork(member), 'aria-label': NAMES[member] + ' 保存并测试网络' }, '保存并测试网络');
      const cancelButton = h('button', { type: 'button', class: 'btn small danger', hidden: true, onclick: () => cancel(member), 'aria-label': '取消 ' + NAMES[member] + ' 安装登录流程' }, '取消流程');
      const skip = h('p', { class: 'meta' });
      const job = h('div', { class: 'setup-job', role: 'status', hidden: true });
      const auth = h('p', { class: 'setup-auth', role: 'status' });
      const error = h('p', { class: 'setup-error', role: 'alert' });
      const networkResult = h('div', { class: 'setup-network-result', role: 'status', hidden: true });
      const prerequisites = (info.prerequisites || []).map((text) => h('li', {}, String(text)));
      const official = info.install_url && /^https?:\/\//i.test(info.install_url)
        ? h('a', { href: info.install_url, target: '_blank', rel: 'noopener noreferrer' }, info.install_url) : null;
      cards[member] = { installed, setup: setupButton, login: loginButton, network: networkButton,
        cancel: cancelButton, skip, job, auth, error, networkResult };
      acceptJob(member, info.active_job);
      return h('section', { class: 'setup-member m-' + member, 'data-setup-member': member },
        h('div', { class: 'setup-member-head' }, h('h3', {}, NAMES[member]), installed),
        h('p', { class: 'setup-disk-note' }, '安装将下载并执行官方安装程序，写入 CLI 文件，并可能更新命令搜索路径。登录信息由官方 CLI 自行保存。'),
        official ? h('p', { class: 'setup-official' }, '官方安装来源：', official) : null,
        prerequisites.length ? h('ul', { class: 'setup-prerequisites' }, prerequisites) : null,
        h('div', { class: 'setup-actions' }, setupButton, loginButton, cancelButton), skip,
        info.login_note ? h('p', { class: 'meta' }, info.login_note) : null,
        job, auth, error,
        h('div', { class: 'setup-network-actions' }, networkButton,
          S.info.demo ? h('span', { class: 'meta' }, '演示模式不测试真实网络') : null), networkResult,
        h('details', { class: 'setup-manual' }, h('summary', {}, '在自己的终端中操作'),
          setupCommand(NAMES[member] + ' 官方安装命令', info.install_command),
          setupCommand(NAMES[member] + ' 登录命令', info.login_command),
          h('p', { class: 'meta' }, '取消流程不会卸载已安装的文件。手动运行的终端不会自动继承本工具的联网设置。')));
    });
    verifyButton.addEventListener('click', async () => {
      try { await refreshOverview(); await verifyMembers(); }
      catch (e) { toast(e.message, true); }
    });
    body.replaceChildren(
      h('p', { class: 'setup-intro' }, '先选联网方式，再为需要的成员打开安装和登录流程。所有操作发生在运行本工具的电脑上。'),
      hostNotice,
      h('section', { class: 'setup-proxy' }, h('h3', {}, '联网设置'),
        field('联网方式', proxyMode), proxyPlatformNote, proxyForm, proxyNotice,
        h('p', { class: 'meta' }, '只设置本工具启动的后续进程的代理环境，不修改系统网络或 CLI 全局配置。部分 CLI 仍可能读取系统代理或 PAC；浏览器登录页使用浏览器自己的网络设置。会为本地登录回调保留直连设置。')),
      h('div', { class: 'setup-check-head' }, h('h3', {}, '安装与登录'), verifyButton),
      ...sections,
      h('p', { class: 'meta' }, '不会自动发送模型测试消息。手动“测试连通”会消耗账号额度。'),
      h('button', { type: 'button', class: 'btn', onclick: openSettings }, '打开成员与连接（高级设置）'));
    renderAll();
    for (const member of MEMBERS) {
      if (running(jobs[member])) pollJob(member, jobs[member].id);
    }
  }

  // ------------------------------------------------------------ 成员与连接
  async function openSettings() {
    document.body.classList.remove('drawer-open');
    const body = h('div', {}, h('p', {}, '正在检测三家命令行，稍等几秒……'));
    const forms = {};
    let globalOnly = null;
    let advWindow = null;
    let advBatch = null;

    async function save(quiet) {
      if (!globalOnly) throw new Error('还在检测，请稍等');
      const payload = {
        members: {}, membership_only: globalOnly.input.checked,
        recent_window: Number(advWindow.value), summary_batch: Number(advBatch.value),
      };
      for (const m of MEMBERS) {
        const f = forms[m];
        const profiles = {};
        for (const tier of ['fast', 'balanced', 'deep']) profiles[tier] = cleanModelInput(f.profiles[tier].value, NAMES[m] + ' ' + MODEL_TIERS[tier]);
        if (f.routingMode.value === 'auto' && Object.values(profiles).some((model) => !model)) {
          throw new Error(NAMES[m] + ' 自动模式需要填写快速、均衡、深入三个模型 ID');
        }
        payload.members[m] = {
          command: f.command.value.trim(), model: cleanModelInput(f.model.value, NAMES[m] + ' 手动模型'), extra_args: f.extra.value.trim(),
          timeout: Number(f.timeout.value) || 600, web: f.web ? f.web.input.checked : true,
          routing: { mode: f.routingMode.value, profiles },
        };
      }
      await api('PUT', '/api/settings', payload);
      if (!quiet) toast('已保存，模型选择从下一轮生效');
      loadMembers(true);
    }

    openModal({
      title: '成员与连接', body, wide: true,
      actions: [{ label: '关闭' }, { label: '保存', primary: true, onClick: () => save(false) }],
    });

    let settings;
    let det;
    const availabilitySnapshot = availabilityEpochSnapshot();
    try {
      [settings, det] = await Promise.all([api('GET', '/api/settings'), api('GET', '/api/members?refresh=1')]);
    } catch (e) {
      body.replaceChildren(h('p', {}, '检测失败：' + e.message));
      return;
    }
    S.members = {};
    det.members.forEach((m) => {
      S.members[m.id] = m;
      if (m.availability) acceptAvailability({ [m.id]: m.availability }, availabilitySnapshot);
    });
    renderBanner();
    renderHeader();

    const nodes = [];
    nodes.push(h('div', { class: 'setup-entry' },
      h('div', {}, h('strong', {}, '需要安装、登录或配置代理？'), h('p', { class: 'meta' }, '打开引导面板；这里保留命令路径、模型等高级设置。')),
      h('button', { type: 'button', class: 'btn', onclick: openSetup }, '安装与登录')));
    if (S.info.demo) nodes.push(h('div', { class: 'hint-box' }, '当前是演示模式：不会调用真实命令行。真实使用时，去掉 --demo 重新启动程序。'));
    else nodes.push(h('p', {}, '三家都通过各自的官方命令行调用，用的是你在这台电脑上登录的会员账号。先在终端里装好并登录，再回来点「测试连通」。'));

    for (const info of det.members) {
      const m = info.id;
      const conf = settings.members[m];
      const statusLines = [];
      if (info.installed) {
        statusLines.push(h('div', {}, h('span', { class: 'ok' }, '✓ 已安装 '), info.version || '', info.path ? h('span', {}, '（', h('code', {}, info.path), '）') : null));
      } else {
        statusLines.push(h('div', {}, h('span', { class: 'bad' }, '✗ 没有找到 '), h('code', {}, info.command), ' 命令'));
      }
      if (info.installed) {
        const cls = info.auth_ok === true ? 'ok' : info.auth_ok === false ? 'bad' : 'unk';
        const mark = info.auth_ok === true ? '✓ ' : info.auth_ok === false ? '✗ ' : '? ';
        statusLines.push(h('div', {}, h('span', { class: cls }, mark), info.auth));
      }
      (info.warnings || []).forEach((w) => statusLines.push(h('div', {}, h('span', { class: 'unk' }, '! '), w)));
      const hint = !info.installed && !info.demo ? h('div', { class: 'hint-box' },
        h('div', {}, '安装方法：'), (info.install || []).map((line) => h('div', {}, h('code', {}, line))),
        h('div', {}, '装好后：', info.login)) : null;

      const f = {
        command: textInput(conf.command, '留空自动查找 ' + info.command),
        model: textInput(conf.model, '留空用默认模型'),
        routingMode: select([['manual', '手动指定'], ['auto', '自动选择档位']], (conf.routing || {}).mode || 'manual'),
        profiles: {},
        extra: textInput(conf.extra_args, '一般不用填'),
        timeout: h('input', { type: 'number', min: '30', max: '3600', step: '30' }),
        web: WEB_LABEL[m] ? check(WEB_LABEL[m], conf.web) : null,
      };
      f.model.maxLength = 200;
      const tierFields = ['fast', 'balanced', 'deep'].map((tier) => {
        const input = textInput(((conf.routing || {}).profiles || {})[tier] || '', '填写账号可用的模型 ID');
        input.maxLength = 200;
        f.profiles[tier] = input;
        return field(NAMES[m] + ' · ' + MODEL_TIERS[tier], input);
      });
      const manualField = field(NAMES[m] + ' 手动模型', f.model, '留空时使用 CLI 默认模型。');
      const profiles = h('details', { class: 'model-profile-details' },
        h('summary', {}, '自动模式的三个模型档位'),
        h('div', { class: 'model-profiles' }, tierFields),
        h('p', { class: 'meta' }, '三个档位均须填写模型 ID。各群选择自动模式时，也使用这里的档位。'));
      const syncRouting = () => {
        manualField.hidden = f.routingMode.value === 'auto';
        profiles.open = f.routingMode.value === 'auto';
      };
      f.routingMode.addEventListener('change', syncRouting);
      syncRouting();
      f.timeout.value = conf.timeout;
      forms[m] = f;
      const result = h('span', { class: 'ping-result', role: 'status' });
      const pingBtn = h('button', { type: 'button', class: 'btn small' }, '测试连通');
      pingBtn.addEventListener('click', async () => {
        pingBtn.disabled = true;
        result.className = 'ping-result';
        result.textContent = '正在测试，可能要几十秒……';
        try {
          await save(true);
          availabilityEpoch[m] = (availabilityEpoch[m] || 0) + 1;
          const r = await api('POST', '/api/members/' + m + '/ping', {});
          result.className = 'ping-result ' + (r.ok ? 'ok' : 'bad');
          result.textContent = r.ok ? '✓ 回复：' + r.reply + '（用时 ' + r.elapsed + ' 秒）' : '✗ ' + r.error;
        } catch (e) {
          result.className = 'ping-result bad';
          result.textContent = '✗ ' + e.message;
        } finally {
          availabilityEpoch[m] = (availabilityEpoch[m] || 0) + 1;
          await loadMembers(false);
          pingBtn.disabled = false;
        }
      });
      nodes.push(h('section', { class: 'member-card m-' + m },
        h('h3', {}, NAMES[m], h('small', {}, info.cli)),
        h('div', { class: 'member-status' }, statusLines),
        availabilityPanel(m),
        quotaPanel(m),
        hint,
        field('命令路径（可选）', f.command),
        h('div', { class: 'member-routing' },
          h('div', { class: 'row' }, field(NAMES[m] + ' 模型选择', f.routingMode), manualField), profiles),
        h('div', { class: 'row' }, field('超时（秒）', f.timeout), field('附加参数（可选）', f.extra)),
        f.web ? f.web.node : null,
        h('div', {}, pingBtn, result)));
    }

    globalOnly = check('优先使用已登录账号，检查已知 API 路由（推荐）', settings.membership_only !== false);
    advWindow = h('input', { type: 'number', min: '4', max: '60' });
    advWindow.value = settings.recent_window;
    advBatch = h('input', { type: 'number', min: '2', max: '40' });
    advBatch.value = settings.summary_batch;
    nodes.push(h('section', { class: 'member-card' },
      h('h3', {}, '通用'),
      globalOnly.node,
      h('p', { class: 'meta' }, 'Gemini 的计费方式仍需在官方客户端确认。'),
      h('p', { class: 'model-setting-note' }, '暂停和恢复影响所有群聊，恢复后下一轮再尝试。同一账号可能共用额度，切换模型不保证恢复额度。'),
      h('p', { class: 'model-setting-note' }, '自动选择使用本地规则判断任务复杂度，不额外调用模型。模型 ID 必须受你的账号支持，节省额度并不保证。修改从下一轮开始生效，本群指定优先。'),
      h('details', { class: 'advanced' }, h('summary', {}, '记忆设置'),
        h('div', { class: 'row' },
          field('原样附上的最近消息条数', advWindow, '更早的内容由书记员压缩成前情摘要。'),
          field('每积累多少条更新一次摘要', advBatch)))));
    body.replaceChildren(...nodes);
  }

  // ------------------------------------------------------------ 手机布局
  function setTab(tab) {
    S.tab = tab;
    document.body.classList.toggle('tab-board', tab === 'board');
    document.querySelectorAll('#tabs button').forEach((b) => b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false'));
    if (tab === 'chat') {
      const stream = $('#stream');
      requestAnimationFrame(() => { stream.scrollTop = stream.scrollHeight; $('#new-pill').hidden = true; });
    }
  }

  // ------------------------------------------------------------ 启动
  function bind() {
    $('#btn-new').addEventListener('click', openNewGroup);
    $('#btn-new-2').addEventListener('click', openNewGroup);
    $('#btn-settings').addEventListener('click', openSettings);
    $('#btn-settings-2').addEventListener('click', openSettings);
    $('#btn-setup').addEventListener('click', openSetup);
    $('#btn-setup-2').addEventListener('click', openSetup);
    $('#btn-memory').addEventListener('click', openMemories);
    $('#btn-models').addEventListener('click', openGroupModels);
    $('#btn-memory-side').addEventListener('click', openMemories);
    $('#btn-continue').addEventListener('click', continueDiscussion);
    $('#btn-stop').addEventListener('click', stopRound);
    $('#btn-more').addEventListener('click', openMoreMenu);
    $('#composer').addEventListener('submit', send);
    const openDrawer = () => document.body.classList.add('drawer-open');
    $('#btn-menu').addEventListener('click', openDrawer);
    $('#btn-menu-2').addEventListener('click', openDrawer);
    $('#scrim').addEventListener('click', () => document.body.classList.remove('drawer-open'));
    $('#mode-seg').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-mode]');
      if (!b || !S.group || b.dataset.mode === S.group.mode) return;
      patchGroup({ mode: b.dataset.mode }, b.dataset.mode === 'parallel' ? '改为背靠背发言' : '改为依次发言').catch(() => {});
    });
    $('#tabs').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-tab]');
      if (b) setTab(b.dataset.tab);
    });
    const input = $('#input');
    input.addEventListener('input', () => { autosize(); });
    input.addEventListener('blur', saveDraft);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        send();
      }
    });
    $('#mention-row').addEventListener('click', (e) => {
      const b = e.target.closest('[data-at]');
      if (!b) return;
      const at = '@' + b.dataset.at + ' ';
      const start = input.selectionStart || 0;
      input.value = input.value.slice(0, start) + at + input.value.slice(input.selectionEnd || start);
      input.focus();
      input.selectionStart = input.selectionEnd = start + at.length;
      autosize();
    });
    const stream = $('#stream');
    stream.addEventListener('scroll', () => {
      if (stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60) $('#new-pill').hidden = true;
    });
    $('#new-pill').addEventListener('click', () => {
      stream.scrollTop = stream.scrollHeight;
      $('#new-pill').hidden = true;
    });
    document.addEventListener('click', (e) => {
      const ref = e.target.closest('.ref, .fx[data-ref]');
      if (ref && ref.dataset.ref) focusItem(ref.dataset.ref);
      const seq = e.target.closest('.seq-link');
      if (seq) focusMessage(seq.dataset.seq);
    });
    window.addEventListener('beforeunload', saveDraft);
  }

  async function init() {
    bind();
    try {
      S.info = await api('GET', '/api/info');
    } catch (e) {
      toast(e.message, true);
    }
    await refreshGroups();
    loadMembers(false);
    const want = location.hash.slice(1) || store('aigc.gid');
    const target = S.groups.find((g) => g.id === want) || S.groups[0];
    if (target) selectGroup(target.id); else showWelcome();
    setInterval(refreshGroups, 10000);
    setInterval(() => { if (S.state.running) renderTyping(); }, 1000);
  }

  init();
})();
