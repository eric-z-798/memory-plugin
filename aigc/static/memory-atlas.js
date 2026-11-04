/* 星枢记忆宇宙。可视节点只来自已保存记忆；连线表示类型归属。 */
'use strict';
(() => {
  const TYPES = {
    fact: { label: '重要事实', color: '#67e8f9', x: 23, y: 27 },
    preference: { label: '我的偏好', color: '#a78bfa', x: 76, y: 25 },
    decision: { label: '已经决定', color: '#fb8a79', x: 23, y: 72 },
    task: { label: '接下来做', color: '#8de0b8', x: 78, y: 70 },
    note: { label: '灵感笔记', color: '#f0cb81', x: 51, y: 83 },
  };
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const svgEl = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    return node;
  };
  function create({ onSelect } = {}) {
    const root = el('section', 'memory-atlas');
    root.setAttribute('aria-label', '记忆星图');
    const head = el('div', 'atlas-heading');
    const headingText = el('div');
    headingText.append(el('span', 'atlas-eyebrow', 'MEMORY UNIVERSE'), el('h3', '', '把重要的事，连成你的星图'));
    const live = el('span', 'atlas-live', '持续积累');
    head.append(headingText, live);
    const stage = el('div', 'atlas-stage');
    stage.setAttribute('aria-label', '记忆分布；点击亮点查看记忆，也可切换到列表');
    const space = el('div', 'atlas-space');
    const svg = svgEl('svg', { viewBox: '0 0 700 600', preserveAspectRatio: 'none', 'aria-hidden': 'true', class: 'atlas-lines' });
    const core = el('div', 'atlas-core');
    core.setAttribute('aria-hidden', 'true');
    core.append(el('span', 'atlas-core-icon', '✦'), el('strong', '', '共同记忆'), el('span', 'atlas-core-count', '0 条记忆'));
    const layer = el('div', 'atlas-node-layer');
    space.append(svg, core, layer);
    stage.append(space);
    const empty = el('div', 'atlas-empty');
    empty.hidden = true;
    const emptyTitle = el('strong', '', '你的第一颗记忆，等你点亮');
    empty.append(el('span', 'atlas-empty-spark', '✦'), emptyTitle,
      el('p', '', '记下一条偏好、一个决定，或一件不想反复解释的事。'));
    stage.append(empty);
    const zoom = el('div', 'atlas-zoom');
    let scale = 1, panX = 0, panY = 0, drag = null, moved = false;
    let canvasW = 0, canvasH = 0;
    const limits = () => ({ x: Math.max(0, (canvasW * scale - stage.clientWidth) / 2),
                           y: Math.max(0, (canvasH * scale - stage.clientHeight) / 2) });
    const zoomValue = el('span', 'atlas-zoom-value', '100%');
    const applyTransform = () => {
      const bound = limits();
      panX = Math.max(-bound.x, Math.min(bound.x, panX));
      panY = Math.max(-bound.y, Math.min(bound.y, panY));
      stage.style.touchAction = scale > 1 || bound.y > 0 ? 'none' : 'pan-y';
      space.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
      zoomValue.textContent = Math.round(scale * 100) + '%';
    };
    for (const [label, text, handler] of [
      ['缩小星图', '−', () => { scale = Math.max(.8, scale - .2); applyTransform(); }],
      ['放大星图', '+', () => { scale = Math.min(2.4, scale + .2); applyTransform(); }],
      ['复位星图', '复位', () => { scale = 1; panX = 0; panY = 0; applyTransform(); }],
    ]) {
      const b = el('button', 'atlas-control', text);
      b.type = 'button'; b.setAttribute('aria-label', label); b.addEventListener('click', handler); zoom.append(b);
    }
    zoom.insertBefore(zoomValue, zoom.children[2]);
    // Controls live outside the clipped, pannable canvas.

    stage.addEventListener('pointerdown', event => {
      if (event.button !== 0 || (event.pointerType === 'touch' && stage.style.touchAction !== 'none')) return;
      moved = false;
      drag = { x: event.clientX, y: event.clientY, px: panX, py: panY };
      stage.classList.add('is-dragging');
    });
    stage.addEventListener('pointermove', event => {
      if (!drag) return;
      if (Math.abs(event.clientX - drag.x) + Math.abs(event.clientY - drag.y) > 5) moved = true;
      if (moved && stage.setPointerCapture) stage.setPointerCapture(event.pointerId);
      panX = drag.px + event.clientX - drag.x;
      panY = drag.py + event.clientY - drag.y;
      applyTransform();
    });
    const endDrag = () => { drag = null; stage.classList.remove('is-dragging'); };
    stage.addEventListener('pointerup', endDrag); stage.addEventListener('pointercancel', endDrag);
    const legend = el('div', 'atlas-legend');
    const explanation = el('p', 'atlas-explanation', '每个亮点是一条记忆 · 连线表示同一类型，不代表因果关系');
    root.append(head, stage, zoom, legend, explanation);
    let selected = null, entries = [], buttons = new Map(), lines = new Map();
    const select = (id, focus = false) => {
      selected = id;
      for (const [key, button] of buttons) {
        const active = key === id;
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
        button.tabIndex = active ? 0 : -1;
        if (lines.get(key)) lines.get(key).classList.toggle('is-selected', active);
        if (active && focus) {
          panX = (50 - parseFloat(button.style.left)) * canvasW * scale / 100;
          panY = (50 - parseFloat(button.style.top)) * canvasH * scale / 100;
          applyTransform();
          button.focus({ preventScroll: true });
        }
      }
      const entry = entries.find(item => item.id === id);
      if (onSelect) onSelect(entry || null);
    };
    function setEntries(next, { filtered = false } = {}) {
      entries = next.slice(); buttons = new Map(); lines = new Map();
      svg.replaceChildren(); layer.replaceChildren(); legend.replaceChildren();
      empty.hidden = entries.length > 0; space.hidden = !entries.length;
      zoom.hidden = !entries.length;
      emptyTitle.textContent = filtered ? '暂时没有匹配的记忆' : '你的第一颗记忆，等你点亮';
      empty.querySelector('p').textContent = filtered ? '试试更短的关键词，或者切换记忆状态。' : '记下一条偏好、一个决定，或一件不想反复解释的事。';
      core.querySelector('.atlas-core-count').textContent = entries.length + ' 条记忆';
      const shown = entries.slice(0, 160);
      const width = stage.clientWidth || 350, height = stage.clientHeight || 330;
      const columns = Math.max(1, Math.floor(width / 52));
      canvasW = width;
      canvasH = Math.max(height, Math.ceil(shown.length / columns) * 52 + 32);
      space.style.width = canvasW + 'px'; space.style.height = canvasH + 'px';
      space.style.left = '50%'; space.style.top = '50%';
      space.style.marginLeft = -canvasW / 2 + 'px'; space.style.marginTop = -canvasH / 2 + 'px';
      let position = 0;
      applyTransform();
      const groups = {};
      for (const item of shown) {
        const kind = TYPES[item.kind] ? item.kind : 'note';
        (groups[kind] ||= []).push(item);
      }
      for (const [kind, values] of Object.entries(groups)) {
        const type = TYPES[kind];
        const hubLine = svgEl('path', {d: `M350 300 Q${type.x * 7} 300 ${type.x * 7} ${type.y * 6}`, stroke: type.color, class: 'atlas-hub-line', fill: 'none'});
        svg.append(hubLine);
        const hub = el('div', 'atlas-hub');
        hub.style.left = type.x + '%'; hub.style.top = type.y + '%'; hub.style.setProperty('--node-color', type.color);
        hub.append(el('span', 'atlas-hub-dot'), el('strong', '', type.label));
        layer.append(hub);
        const legendItem = el('span', 'atlas-legend-item', type.label + ' ' + values.length);
        legendItem.style.setProperty('--node-color', type.color); legend.append(legendItem);
        values.forEach((item, index) => {
          // Non-overlapping 44px targets, with room to pan dense constellations.
          const x = ((position % columns) + .5) * 100 / columns;
          const y = (16 + (Math.floor(position / columns) + .5) * 52) * 100 / canvasH;
          position++;
          const line = svgEl('line', {x1:type.x * 7,y1:type.y * 6,x2:x * 7,y2:y * 6,stroke:type.color,class:'atlas-memory-line'});
          svg.append(line); lines.set(item.id,line);
          const button = el('button', 'atlas-node' + (item.status === 'pending' ? ' is-pending' : ''));
          button.type = 'button'; button.dataset.memoryId = item.id;
          button.style.left = x + '%'; button.style.top = y + '%'; button.style.setProperty('--node-color', type.color);
          button.setAttribute('aria-label', (item.status === 'pending' ? '待确认：' : '已确认：') + item.content);
          button.setAttribute('aria-pressed', 'false');
          const brief = item.content;
          button.title = brief;
          button.append(el('span', 'atlas-node-dot'), el('span', 'atlas-node-label', brief.slice(0, 12) + (brief.length > 12 ? '…' : '')));
          button.addEventListener('click', event => { if (!moved || event.detail === 0) select(item.id); moved = false; });
          button.addEventListener('keydown', event => {
            if (!['ArrowRight','ArrowLeft','ArrowUp','ArrowDown'].includes(event.key)) return;
            event.preventDefault(); const all = [...buttons.keys()];
            const direction = ['ArrowRight','ArrowDown'].includes(event.key) ? 1 : -1;
            select(all[(all.indexOf(item.id) + direction + all.length) % all.length], true);
          });
          buttons.set(item.id, button); layer.append(button);
        });
      }
      if (entries.length > shown.length) legend.append(el('span', 'atlas-overflow', `星图展示前 ${shown.length} 条，其余可在列表查看`));
      select(entries.some(item => item.id === selected) ? selected : shown[0]?.id || null);
    }
    if (typeof ResizeObserver !== 'undefined') {
      let previousWidth = 0;
      new ResizeObserver(() => {
        if (stage.clientWidth && stage.clientWidth !== previousWidth) {
          previousWidth = stage.clientWidth;
          setEntries(entries);
        }
      }).observe(stage);
    }
    return { element: root, setEntries, select };
  }
  window.XingshuMemoryAtlas = { create };
})();
