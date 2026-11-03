/* Offline read-only API for the mobile interface preview. No model or server calls. */
'use strict';
(() => {
  const data = window.XINGSHU_PREVIEW_DATA;
  const base = '/api/groups/' + data.gid;
  const denied = '这是界面预览，实际操作请使用本机完整版';
  const copy = value => JSON.parse(JSON.stringify(value));
  const response = (value, status = 200) => new Response(JSON.stringify(value), {
    status, headers: { 'Content-Type': 'application/json; charset=utf-8' }
  });
  const nativeFetch = window.fetch.bind(window);
  const snapshot = path => copy(data.snapshots[path]);

  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, window.location.href);
    const method = String(options.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    if (!url.pathname.startsWith('/api/')) {
      return nativeFetch(input, options);
    }
    if (url.origin !== window.location.origin) return response({ error: denied }, 403);
    const path = url.pathname;
    if (method === 'POST' && path === base + '/model-preview') {
      // The captured configuration is manual/default for all three members.
      // A question cannot change those selections. No remote evaluation occurs.
      const preview = copy(data.modelPreviews.fast);
      for (const route of Object.values(preview.routes)) {
        route.simulation = true;
        route.reason = '示例配置为手动沿用 CLI 默认模型；此界面预览不连接真实账号。';
        delete route.complexity;
      }
      return response(preview);
    }
    if (method !== 'GET' && method !== 'HEAD') return response({ error: denied }, 403);
    if (path === base + '/poll') {
      const group = snapshot(base);
      return response({ ...group, messages: [], rev: group.board.rev, now: Date.now() / 1000 });
    }
    if (path === base + '/memories') {
      const result = snapshot(path);
      const query = (url.searchParams.get('q') || '').trim().toLowerCase();
      const status = url.searchParams.get('status') || 'all';
      result.memories = result.memories.filter(item =>
        (!query || item.content.toLowerCase().includes(query)) &&
        (status === 'all' || item.status === status));
      return response(result);
    }
    if (path === base + '/memory-context') {
      const result = snapshot(path);
      const query = (url.searchParams.get('q') || '').trim().toLowerCase();
      // Preview uses a simple substring filter; the full application uses its real retriever.
      result.items = result.items.filter(item => !query || item.content.toLowerCase().includes(query));
      result.text = result.items.length ? '手机界面预览：以下是示例中的已确认记忆。\n' + result.items.map(item => JSON.stringify(item)).join('\n') : '';
      return response(result);
    }
    if (Object.prototype.hasOwnProperty.call(data.snapshots, path)) {
      const result = snapshot(path);
      if (path === base) result.now = Date.now() / 1000;
      return response(result);
    }
    return response({ error: '界面预览中没有这项示例数据' }, 404);
  };

  // Prevent the regular download link from navigating to a server-only route.
  document.addEventListener('click', event => {
    const anchor = event.target.closest && event.target.closest('a[href]');
    if (!anchor) return;
    const url = new URL(anchor.href, window.location.href);
    if (url.pathname === base + '/export') {
      event.preventDefault();
      const group = data.snapshots[base];
      const text = '# 星枢体验室（界面预览 · 示例对话）\n\n' + group.messages.map(message =>
        '## ' + ({ user: '群主', claude: 'Claude', gpt: 'GPT', gemini: 'Gemini' }[message.author] || message.author) + '\n\n' + message.text).join('\n\n');
      const blobUrl = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
      const download = document.createElement('a');
      download.href = blobUrl;
      download.download = '星枢体验室-示例对话.md';
      download.click();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    }
  });
  window.XINGSHU_PREVIEW_READ_ONLY = true;
})();
