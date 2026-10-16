/* 工作台 · 全屏壁纸 + 悬浮面板 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  let state = null;
  let settings = null;
  let pollTimer = null;
  let photoIdx = 0;
  let current = -1;           // 当前打开的简报条目

  const THEMES = ['light', 'paper', 'dark'];
  const THEME_CN = { light: '暖白', paper: '羊皮纸', dark: '暗色' };

  /* --------------------------------------------------------- 天气图标 */
  const WX_ICONS = {
    sun: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="16" cy="16" r="6.4"/><path d="M16 2.6v3.4M16 26v3.4M2.6 16h3.4M26 16h3.4M6.5 6.5l2.4 2.4M23.1 23.1l2.4 2.4M25.5 6.5l-2.4 2.4M8.9 23.1l-2.4 2.4"/></svg>`,
    'cloud-sun': `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11.5" cy="11" r="4.2"/><path d="M11.5 2.8v2.4M4.2 11H1.8M18.8 11h2.4M6.3 5.8l1.7 1.7M6.3 16.2l1.7-1.7"/><path d="M11.5 24.5h13a4.8 4.8 0 0 0 0-9.6 6.4 6.4 0 0 0-11.9 1.9 4.1 4.1 0 0 0-1.1 7.7z"/></svg>`,
    cloud: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8.6 24.6h15.2a5.4 5.4 0 0 0 0-10.8 7.2 7.2 0 0 0-13.4 2.1 4.6 4.6 0 0 0-1.8 8.7z"/></svg>`,
    fog: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 17.6h14.4a4.8 4.8 0 0 0 0-9.6 6.6 6.6 0 0 0-12.3 1.9A4.2 4.2 0 0 0 9 17.6z"/><path d="M5 22.4h20M7.5 26.4h17"/></svg>`,
    drizzle: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8.6 18.6h15.2a5.4 5.4 0 0 0 0-10.8 7.2 7.2 0 0 0-13.4 2.1 4.6 4.6 0 0 0-1.8 8.7z"/><path d="M11 23v1.6M16 23.6v1.6M21 23v1.6"/></svg>`,
    rain: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8.6 17.6h15.2a5.4 5.4 0 0 0 0-10.8 7.2 7.2 0 0 0-13.4 2.1 4.6 4.6 0 0 0-1.8 8.7z"/><path d="M10.6 21.4l-1.4 4M16.4 21.4l-1.4 4M22.2 21.4l-1.4 4"/></svg>`,
    snow: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8.6 17.6h15.2a5.4 5.4 0 0 0 0-10.8 7.2 7.2 0 0 0-13.4 2.1 4.6 4.6 0 0 0-1.8 8.7z"/><path d="M11 23.4v3.2M9.6 25h2.8M21 23.4v3.2M19.6 25h2.8M16 25.6v2.6M15 26.9h2"/></svg>`,
    thunder: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.6 17.6h15.2a5.4 5.4 0 0 0 0-10.8 7.2 7.2 0 0 0-13.4 2.1 4.6 4.6 0 0 0-1.8 8.7z"/><path d="M17.6 20.4l-3.4 5h4.4l-2 4.4"/></svg>`,
  };

  /* ------------------------------------------------------------ 工具 */
  function toast(msg, kind) {
    const wrap = $('toastWrap');
    const t = el('div', 'toast' + (kind ? ' ' + kind : ''), msg);
    wrap.appendChild(t);
    setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 3400);
  }

  async function api(path, opts) {
    let res;
    try {
      res = await fetch(path, Object.assign({
        headers: { 'Content-Type': 'application/json' }, cache: 'no-store'
      }, opts || {}));
    } catch (e) {
      // fetch 本身抛错 = 连不上本地服务（进程没了 / 端口被占）
      noteOffline();
      throw new Error('本地服务没在运行');
    }
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const j = await res.json(); if (j.message) msg = j.message; } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    noteOnline();
    return res.json();
  }

  /* ---------------------------------------------- 服务断线：提示 + 自愈 */

  let offlineTimer = null;
  let reloading = false;

  async function pingOnce() {
    const r = await fetch('/api/ping?t=' + Date.now(), { cache: 'no-store' });
    const j = await r.json();
    return !!(j && j.app === 'workbench');
  }

  function offlineBanner() {
    let bar = $('offlineBar');
    if (bar) return bar;
    bar = el('div', 'offline-bar');
    bar.id = 'offlineBar';
    const text = el('div', 'offline-text', '和本地服务的连接断了');
    const hint = el('div', 'offline-hint',
      '正在每 2.5 秒自动重试，服务一回来这个页面会自己刷新。'
      + '要是一直连不上，双击桌面的「工作台」图标重新打开 —— 配置和缓存都还在原处，不会丢东西。');
    const btn = el('button', 'btn primary', '立即重试');
    btn.addEventListener('click', async () => {
      if (reloading) return;
      btn.disabled = true;
      btn.textContent = '正在重试…';
      let back = false;
      try { back = await pingOnce(); } catch (e) { back = false; }
      if (back) { reloading = true; location.reload(); return; }
      // 服务真的没在跑了 —— 页面里没有任何办法把它拉起来，只能说实话
      btn.disabled = false;
      btn.textContent = '立即重试';
      toast('服务没在运行，请双击桌面的「工作台」图标重开', 'warn');
    });
    const box = el('div', 'offline-body');
    box.append(text, hint);
    bar.append(box, btn);
    document.body.appendChild(bar);
    return bar;
  }

  function noteOffline() {
    const bar = offlineBanner();
    bar.classList.add('show');
    if (offlineTimer) return;
    // 一边等一边探：服务一回来就自己刷新，用户什么都不用做
    offlineTimer = setInterval(async () => {
      let back = false;
      try { back = await pingOnce(); } catch (e) { /* 还没起来，继续等 */ }
      if (!back) return;
      clearInterval(offlineTimer);
      offlineTimer = null;
      if (!reloading) { reloading = true; location.reload(); }
    }, 2500);
  }

  function noteOnline() {
    const bar = $('offlineBar');
    if (bar) bar.classList.remove('show');
    if (offlineTimer) { clearInterval(offlineTimer); offlineTimer = null; }
  }

  /* ------------------------------------------------------- 壁纸层 */

  /* 壁纸：后端已经按「今天的天气 + 时段 + 季节 + 天象事件 + 你的偏好」算好，
     这里只负责显示，并在右下角把「为什么是这张」摊开。 */
  function wallpaperList(s) {
    const ph = (s && s.photos) || {};
    const out = [];
    const push = (p) => {
      if (p && p.url && !out.some(x => x.file === p.file)) out.push(p);
    };
    push((ph.chosen || {}).main);
    (ph.hero || []).forEach(push);
    return out;
  }

  function renderWallpaper(s) {
    const list = wallpaperList(s);
    if (photoIdx >= list.length || photoIdx < 0) photoIdx = 0;
    const p = list[photoIdx];
    const wall = $('wallpaper');
    const box = $('wallCredit');
    box.textContent = '';

    if (!p) {
      box.appendChild(el('div', 'wall-src', s.photos && s.photos.total
        ? '配图库 ' + s.photos.total + ' 张' : '配图库为空，点「刷新」抓一批'));
      return;
    }
    const img = new Image();
    img.onload = () => {
      wall.style.backgroundImage = 'url("' + p.url + '")';
      wall.classList.add('on');
    };
    img.src = p.url;

    const chosen = (s.photos || {}).chosen || {};
    const first = photoIdx === 0;
    if (first && chosen.why) {
      box.appendChild(el('div', 'wall-why', chosen.why));
    } else if (p.why) {
      box.appendChild(el('div', 'wall-why', p.why));
    }
    const bits = [];
    if (p.title) bits.push(p.title);
    if (p.credit) bits.push(p.credit);
    const srcName = { astro: '天文台', apod: 'NASA APOD', nasa: 'NASA', local: '本人作品' }[p.src];
    if (srcName && !/天文台|NASA|本人/.test(bits.join(' '))) bits.push(srcName);
    box.appendChild(el('div', 'wall-src', bits.join('  ·  ') +
      (list.length > 1 ? '   （' + (photoIdx + 1) + '/' + list.length + '，按 N 换）' : '')));
  }

  /* -------------------------------------------------------- 顶栏 */

  function renderTop(s) {
    $('dateText').textContent = s.today.dateText;
    $('weekday').textContent = s.today.weekday;
    const chip = $('festivalChip');
    chip.textContent = s.today.festival || '';
    chip.hidden = !s.today.festival;
  }

  function renderWeather(s) {
    const w = s.weather || {};
    const inline = $('wxInline');
    if (!w.ok) {
      inline.hidden = true;
      $('wxSep').hidden = true;
      return;
    }
    inline.hidden = false;
    $('wxSep').hidden = false;
    $('wxIcon').innerHTML = WX_ICONS[w.icon] || WX_ICONS.cloud;
    $('wxCity').textContent = w.city || '';
    $('wxTemp').textContent = (w.temp != null ? Math.round(w.temp) + '°' : '');

    inline.title = [
      w.text,
      w.phase,
      w.feels != null ? '体感 ' + Math.round(w.feels) + '°' : '',
      w.rainChance != null ? '降水 ' + w.rainChance + '%' : '',
      w.sunrise ? '日出 ' + w.sunrise : '',
      w.sunset ? '日落 ' + w.sunset : '',
      w.tomorrow && w.tomorrow.text ? '明日 ' + w.tomorrow.text : ''
    ].filter(Boolean).join('  ·  ');
  }

  /* ------------------------------------------------------- 简报列表 */

  function renderNews(s) {
    const box = $('newsItems');
    box.textContent = '';
    const items = s.daily.items || [];
    const exp = s.status.expanding || {};
    const ready = items.filter(i => i.summary && !i.stub && i.summary.length > 120).length;

    if (exp.running) {
      $('newsMeta').textContent = '生成中 ' + exp.done + ' / ' + exp.total;
    } else if (items.length) {
      $('newsMeta').textContent = items.length + ' 条 · 约 ' +
        Math.round(s.daily.readMinutes || 15) + ' 分钟' +
        (ready ? ' · 已备 ' + ready : '');
    } else {
      $('newsMeta').textContent = '暂无';
    }

    if (!items.length) {
      box.appendChild(el('div', 'empty',
        '还没有抓到资讯。到「设置」里检查资讯源，或点右上角刷新重试。'));
      $('itemsPos').textContent = '—';
      return;
    }

    items.forEach((it, i) => {
      const b = el('button', 'nitem');
      b.dataset.idx = i;
      if (i === current) b.classList.add('open');
      b.appendChild(el('span', 'ncat', it.cat || '要闻'));
      b.appendChild(el('span', 'ntitle', it.title || ''));
      b.appendChild(el('span', 'nitem-idx',
        (it.summary && !it.stub) ? '●' : (exp.running ? '○' : '')));
      b.addEventListener('click', () => openDrawer(i));
      box.appendChild(b);
    });

    box.scrollTop = scrollIdx * rowStep();   // 刷新后保持窗口位置
    updatePos();
  }

  /* ------------------------------------------- 两行小窗口：滚轮一次翻一行 */

  let scrollIdx = 0;      // 窗口顶部显示的是第几条

  function rowStep() {
    const cs = getComputedStyle(document.documentElement);
    const h = parseFloat(cs.getPropertyValue('--row-h')) || 52;
    const g = parseFloat(cs.getPropertyValue('--row-gap')) || 3;
    return h + g;
  }

  function updatePos() {
    const total = (state && state.daily && state.daily.items || []).length;
    if (!total) { $('itemsPos').textContent = '—'; return; }
    if (total <= 2) { $('itemsPos').textContent = '1–' + total + ' / ' + total; return; }
    const first = scrollIdx + 1;
    const second = Math.min(total, scrollIdx + 2);
    $('itemsPos').textContent = first + '–' + second + ' / ' + total;
  }

  /** 用索引驱动，不读 scrollTop —— 免得和平滑滚动互相打架 */
  function scrollToIdx(i) {
    const items = (state && state.daily && state.daily.items) || [];
    const box = $('newsItems');
    const max = Math.max(0, items.length - 2);
    scrollIdx = Math.max(0, Math.min(max, i));
    box.scrollTop = scrollIdx * rowStep();
    updatePos();
  }

  let wheelLock = false;

  function bindWheel() {
    const box = $('newsItems');
    box.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const items = (state && state.daily && state.daily.items) || [];
      if (items.length <= 2) return;
      if (wheelLock) return;
      wheelLock = true;
      setTimeout(() => { wheelLock = false; }, 190);
      scrollToIdx(scrollIdx + (ev.deltaY > 0 ? 1 : -1));
    }, { passive: false });

    // 高亮那条滚进窗口时跟随一下，方便键盘/滚轮混用
    box.addEventListener('keydown', (ev) => {
      if (ev.key === 'ArrowDown') { ev.preventDefault(); ev.stopPropagation(); scrollToIdx(scrollIdx + 1); }
      if (ev.key === 'ArrowUp') { ev.preventDefault(); ev.stopPropagation(); scrollToIdx(scrollIdx - 1); }
      if (ev.key === 'Enter' && current >= 0) openDrawer(current);
    });
  }

  /* ---------------------------------------------------- 二级页面 */

  function fmtMinutes(m) {
    if (!m) return '';
    return m < 1 ? '不到 1 分钟' : '约 ' + (m >= 10 ? Math.round(m) : m.toFixed(1)) + ' 分钟';
  }

  /* 这条新闻是什么时候的 —— 一眼看出简报新不新鲜 */
  function relTime(at) {
    if (!at) return '';
    const t = new Date(at);
    if (isNaN(t.getTime())) return '';
    const mins = Math.round((Date.now() - t.getTime()) / 60000);
    if (mins < 0) return '';
    if (mins < 60) return mins + ' 分钟前';
    const hours = Math.round(mins / 60);
    if (hours < 24) return hours + ' 小时前';
    const days = Math.round(hours / 24);
    return days + ' 天前';
  }

  function paragraphs(text) {
    const frag = document.createDocumentFragment();
    (text || '').split(/\n{2,}/).forEach(seg => {
      const t = seg.trim();
      if (t) frag.appendChild(el('p', null, t));
    });
    return frag;
  }

  function openDrawer(idx) {
    const items = (state && state.daily && state.daily.items) || [];
    if (idx < 0 || idx >= items.length) return;
    current = idx;
    const it = items[idx];

    $('dCat').textContent = it.cat || '要闻';
    $('dIdx').textContent = '第 ' + (idx + 1) + ' / ' + items.length + ' 条';
    $('dTitle').textContent = it.title || '';
    $('dMeta').textContent = [it.source, relTime(it.at), it.why]
      .filter(Boolean).join('  ·  ');

    const ul = $('dPoints');
    ul.textContent = '';
    (it.points || []).forEach(p => ul.appendChild(el('li', null, p)));

    const link = $('dLink');
    link.href = it.url || '#';
    link.style.display = it.url ? '' : 'none';
    $('dLinkName').textContent = it.source ? it.source + ' · 打开原文' : '打开原文';

    $('dProg').textContent = items.length
      ? '全部 ' + items.length + ' 条约 ' + Math.round(state.daily.readMinutes || 15) + ' 分钟'
      : '';

    document.querySelectorAll('.nitem').forEach(n => {
      n.classList.toggle('open', Number(n.dataset.idx) === idx);
    });
    scrollToIdx(idx);          // 打开的这条滚进两行窗口

    $('drawer').classList.add('open');
    $('drawer').setAttribute('aria-hidden', 'false');
    const mask = $('drawerMask');
    mask.hidden = false;
    requestAnimationFrame(() => mask.classList.add('on'));

    showSummary(it, idx);
    $('drawer').querySelector('.drawer-body').scrollTop = 0;
  }

  function isReady(it) {
    const target = (state && state.daily && state.daily.perItemChars) || 600;
    return !!(it && it.summary && !it.stub &&
      it.summary.length >= Math.max(180, target * 0.5));
  }

  function paintSummary(it) {
    const box = $('dSummary');
    box.classList.remove('loading');
    box.textContent = '';
    box.appendChild(paragraphs(it.summary));
    $('dRead').textContent = fmtMinutes(it.mins) + ' · ' + it.summary.length + ' 字';
  }

  let expandTimer = null;

  function stopWatch() {
    if (expandTimer) { clearInterval(expandTimer); expandTimer = null; }
  }

  /** 打开某条时：已经生成好就直接显示；否则触发「一次生成全部」并盯着进度。 */
  async function showSummary(it, idx) {
    const box = $('dSummary');
    const read = $('dRead');
    stopWatch();

    if (isReady(it)) { paintSummary(it); return; }

    box.classList.add('loading');
    box.textContent = '正在生成全部简报的简述…';
    read.textContent = '目标每条 ' + ((state.daily && state.daily.perItemChars) || 600) + ' 字';

    try {
      await api('/api/expand-all', {
        method: 'POST', body: JSON.stringify({ index: idx })
      });
    } catch (e) {
      box.classList.remove('loading');
      box.textContent = '触发失败：' + e.message;
      return;
    }

    expandTimer = setInterval(async () => {
      const s = await refreshState();
      if (!s) return;
      const e = s.status.expanding || {};
      const cur = s.daily.items[current];
      if (!cur) return;

      if (isReady(cur)) paintSummary(cur);
      if (!e.running) {
        stopWatch();
        if (!isReady(cur)) {
          box.classList.remove('loading');
          box.textContent = '这条生成失败了，点右上角刷新重试。';
          read.textContent = '';
        }
        return;
      }
      if (!isReady(cur)) {
        box.classList.add('loading');
        box.textContent = '正在生成全部简述… ' + e.done + ' / ' + e.total +
          '　（要点可以先看，生成完这里会自动出现）';
        read.textContent = e.done + ' / ' + e.total;
      }
    }, 2000);
  }

  function closeDrawer() {
    stopWatch();
    $('drawer').classList.remove('open');
    $('drawer').setAttribute('aria-hidden', 'true');
    const mask = $('drawerMask');
    mask.classList.remove('on');
    setTimeout(() => { if (!mask.classList.contains('on')) mask.hidden = true; }, 320);
    current = -1;
    document.querySelectorAll('.nitem.open').forEach(n => n.classList.remove('open'));
  }

  /* ------------------------------------------------------- 启动器 */

  function renderLaunchers(s) {
    const box = $('launchers');
    box.textContent = '';
    const list = s.launchers || [];

    list.forEach((item, idx) => {
      const b = el('button', 'launcher');
      b.title = item.path || item.command || item.url || '';

      const icon = el('span', 'lac-icon');
      const im = el('img');
      im.src = '/launcher-icon/' + encodeURIComponent(item.id);
      im.alt = '';
      im.onerror = () => {
        im.remove();
        icon.classList.add('fallback');
        icon.style.background = item.accent || 'var(--accent)';
        icon.textContent = (item.title || '?').slice(0, 1);
      };
      icon.appendChild(im);

      const body = el('div', 'lac-body');
      body.appendChild(el('div', 'lac-title', item.title || item.id));
      if (item.subtitle) body.appendChild(el('div', 'lac-sub', item.subtitle));
      b.append(icon, body, el('div', 'lac-go', '›'));

      // 悬停出现的小工具：改名 / 上下移 / 移除
      const tools = el('div', 'lac-tools');
      const mk = (label, tip, act) => {
        const t = el('span', 'lac-tool', label);
        t.title = tip;
        t.addEventListener('click', (ev) => {
          ev.stopPropagation();
          act();
        });
        return t;
      };
      tools.appendChild(mk('↑', '往上挪', () => moveLauncher(item, -1)));
      tools.appendChild(mk('↓', '往下挪', () => moveLauncher(item, 1)));
      tools.appendChild(mk('改', '改名', () => renameLauncher(item, b)));
      tools.appendChild(mk('✕', '移除', () => removeLauncher(item)));
      b.appendChild(tools);

      b.addEventListener('click', () => launch(item, b));
      box.appendChild(b);
    });

    const add = el('button', 'launcher lac-add');
    add.appendChild(el('span', 'lac-icon fallback add', '＋'));
    const ab = el('div', 'lac-body');
    ab.appendChild(el('div', 'lac-title', '添加启动项'));
    ab.appendChild(el('div', 'lac-sub', list.length
      ? '选任意程序 / 文件 / 文件夹'
      : '还没有启动项，点这里挑一个'));
    add.append(ab);
    add.addEventListener('click', openPicker);
    box.appendChild(add);
  }

  async function launcherOp(payload) {
    try {
      const r = await api('/api/launchers', { method: 'POST', body: JSON.stringify(payload) });
      if (!r.ok) { toast(r.message || '操作失败', 'warn'); return null; }
      if (state) state.launchers = r.launchers;
      renderLaunchers(state);
      return r;
    } catch (e) {
      toast('操作失败：' + e.message, 'warn');
      return null;
    }
  }

  function moveLauncher(item, step) {
    launcherOp({ action: 'move', id: item.id, step });
  }

  function removeLauncher(item) {
    launcherOp({ action: 'remove', id: item.id })
      .then(r => { if (r) toast('已移除「' + (item.title || item.id) + '」'); });
  }

  function renameLauncher(item, node) {
    const title = node.querySelector('.lac-title');
    if (!title || node.querySelector('.lac-rename')) return;
    const input = el('input', 'lac-rename');
    input.value = item.title || '';
    input.maxLength = 24;
    title.replaceWith(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const next = input.value.trim();
      input.replaceWith(title);
      if (save && next && next !== item.title) {
        item.title = next;
        await launcherOp({ action: 'update', id: item.id, title: next });
      }
    };
    input.addEventListener('keydown', (ev) => {
      ev.stopPropagation();
      if (ev.key === 'Enter') finish(true);
      if (ev.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('click', (ev) => ev.stopPropagation());
  }

  /* -------------------------------------------------- 选文件（内置浏览器） */

  let pickDir = '';

  async function loadDir(path) {
    const list = $('pickList');
    list.textContent = '正在读取…';
    try {
      const r = await api('/api/fs' + (path ? '?path=' + encodeURIComponent(path) : ''));
      if (r.ok === false) { list.textContent = r.message || '读不了这个目录'; return; }
      pickDir = r.path || '';
      renderPicker(r);
    } catch (e) {
      list.textContent = '读不了：' + e.message;
    }
  }

  function renderPicker(r) {
    $('pickPath').textContent = r.path || '选择位置';
    $('pickMsg').textContent = '';

    const quick = $('pickQuick');
    quick.textContent = '';
    (r.quick || []).forEach(q => {
      const b = el('button', 'btn ghost pick-q', q.name);
      b.addEventListener('click', () => loadDir(q.path));
      quick.appendChild(b);
    });
    quick.hidden = !(r.quick || []).length;

    const list = $('pickList');
    list.textContent = '';
    if (r.parent && r.path) {
      const up = el('button', 'pick-row up');
      up.appendChild(el('span', 'pick-ic', '↰'));
      up.appendChild(el('span', 'pick-name', '上一级'));
      up.addEventListener('click', () => loadDir(r.parent));
      list.appendChild(up);
    }
    (r.dirs || []).forEach(d => {
      const b = el('button', 'pick-row');
      b.appendChild(el('span', 'pick-ic dir', '▸'));
      b.appendChild(el('span', 'pick-name', d.name));
      b.addEventListener('click', () => loadDir(d.path));
      list.appendChild(b);
    });
    (r.files || []).forEach(f => {
      const b = el('button', 'pick-row file');
      b.appendChild(el('span', 'pick-ic', (f.ext || '').replace('.', '').slice(0, 4) || '·'));
      b.appendChild(el('span', 'pick-name', f.name));
      b.appendChild(el('span', 'pick-go', '选它'));
      b.title = f.path;
      b.addEventListener('click', () => addLauncher(f.path));
      list.appendChild(b);
    });
    if (!list.childNodes.length) {
      list.appendChild(el('div', 'empty', '这个文件夹里没有可添加的东西（只列出程序/文档/图片等常见类型）。'));
    }
  }

  function openPicker() {
    $('pickModal').hidden = false;
    $('pickManual').value = '';
    loadDir('');
  }

  async function addLauncher(path, title) {
    const msg = $('pickMsg');
    msg.textContent = '正在添加…';
    const r = await launcherOp({ action: 'add', path, title: title || '' });
    if (!r) { msg.textContent = '添加失败'; return; }
    msg.textContent = r.message;
    setTimeout(() => { $('pickModal').hidden = true; }, 500);
    toast(r.message);
  }

  /* --------------------------------------------------- 下一个节日 */

  function renderFestivalOne(s) {
    const box = $('festivalOne');
    const cd = s.today.countdown || [];
    box.textContent = '';
    if (!cd.length) { box.hidden = true; return; }
    const c = cd[0];
    box.hidden = false;
    // 只显示「距几天 + 节日名」。曾经这里还跟一个具体日期（如 10月23日），
    // 但和「6 天」是重复信息，孤零零一个日期又没有上下文，看着莫名其妙 —— 去掉。
    box.appendChild(el('span', 'fe-days', c.inDays + ' 天'));
    box.appendChild(el('span', 'fe-title', c.title));
  }

  /* --------------------------------------------------------- 待办 */

  function renderTodos(s) {
    const box = $('todos');
    box.textContent = '';
    const list = s.todos || [];
    const left = list.filter(t => !t.done).length;
    $('todoMeta').textContent = list.length ? (left + ' / ' + list.length) : '空';

    if (!list.length) {
      box.appendChild(el('div', 'empty', '还没有待办。上面输入框敲一条，回车就加上了。'));
      return;
    }
    list.forEach(t => {
      const row = el('div', 'todo' + (t.done ? ' done' : ''));
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = !!t.done;
      cb.addEventListener('change', () => todoAction('toggle', { id: t.id }));
      const txt = el('span', 'todo-text', t.text);
      const del = el('button', 'todo-del', '✕');
      del.title = '删除';
      del.addEventListener('click', () => todoAction('remove', { id: t.id }));
      row.append(cb, txt, del);
      box.appendChild(row);
    });
  }

  async function todoAction(action, payload) {
    try {
      await api('/api/todos', {
        method: 'POST', body: JSON.stringify(Object.assign({ action }, payload))
      });
      await refreshState();
    } catch (e) { toast('待办操作失败：' + e.message, 'warn'); }
  }

  async function addTodo() {
    const inp = $('todoInput');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    await todoAction('add', { text });
  }

  /* --------------------------------------------------- 最近在做 */

  function renderQuote(s) {
    $('quote').textContent = s.daily.quote || '还没有内容，点右上角刷新生成。';
    // 这里曾经还拼一个简报缓存的生成时间（如 10-16 22:57）。但为了秒开，
    // 界面会先显示前一晚预生成的那份，于是今天的页面上挂着昨天的日期 —— 去掉。
    $('quoteFrom').textContent = s.daily.quoteFrom || '';
  }

  function renderStatus(s) {
    const dot = $('statusDot');
    dot.className = 'dot';
    if (s.status.generating) dot.classList.add('busy');
    else if (s.daily.aiError) dot.classList.add('err');

    let note = s.status.note || '就绪';
    if (s.daily.aiError) note = '模型报错（已兜底）：' + s.daily.aiError.slice(0, 66);
    $('statusNote').textContent = note;

    const meta = [];
    if (s.weather && s.weather.ok) meta.push(s.weather.city + ' ' + (s.weather.phase || ''));
    meta.push('配图 ' + (s.photos.total || 0) + ' 张');
    meta.push('候选 ' + (s.daily.poolSize || 0) + ' 条');
    meta.push((s.ai.model || '未配置模型') + ' · 端口 ' + s.status.port);
    $('statusMeta').textContent = meta.join('  ·  ');
  }

  function render(s) {
    state = s;
    renderWallpaper(s); renderTop(s); renderWeather(s);
    renderNews(s); renderFestivalOne(s); renderTodos(s);
    renderQuote(s); renderLaunchers(s); renderStatus(s);
  }

  /* -------------------------------------------------------- 刷新 */

  /* 内容指纹：只有真变了才重画，避免无谓的闪烁与滚动位置跳动 */
  function stateSig(s) {
    if (!s) return '';
    const w = s.weather || {};
    const ch = (s.photos || {}).chosen || {};
    return [
      s.status.rev, s.daily.generatedAt, s.daily.generatedBy,
      (s.daily.items || []).length,
      (s.daily.items || []).filter(i => isReady(i)).length,
      w.updatedAt || '', w.text || '', w.temp,
      ch.slot || '', (ch.main || {}).file || '',   // 换壁纸（含跨时段自动换）也要重画
      ((s.launchers || []).map(l => l.id).join(','))
    ].join('|');
  }

  let lastSig = '';

  async function refreshState() {
    try {
      const s = await api('/api/state');
      lastSig = stateSig(s);
      const keep = current;
      render(s);
      if (keep >= 0) {
        current = keep;
        document.querySelectorAll('.nitem').forEach(n => {
          n.classList.toggle('open', Number(n.dataset.idx) === keep);
        });
      }
      return s;
    } catch (e) {
      $('statusNote').textContent = '连不上本地服务：' + e.message;
      $('statusDot').className = 'dot err';
      return null;
    }
  }

  function schedulePoll() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      const s = await refreshState();
      if (s && !s.status.generating) { clearInterval(pollTimer); pollTimer = null; }
    }, 2500);
  }

  /* 闲时低速轮询：后台把天气/简报换成新的之后，界面自己会跟上 */
  async function idlePoll() {
    if (pollTimer) return;                  // 正在快速轮询就别插一脚
    try {
      const s = await api('/api/state');
      const sig = stateSig(s);
      if (sig === lastSig) return;
      lastSig = sig;
      const keep = current;
      render(s);
      if (keep >= 0) {
        current = keep;
        document.querySelectorAll('.nitem').forEach(n => {
          n.classList.toggle('open', Number(n.dataset.idx) === keep);
        });
      }
    } catch (e) { /* 断线已由 api() 处理 */ }
  }

  /* ---------------------------------------------------------- 动作 */

  async function launch(item, node) {
    node.classList.add('busy');
    try {
      const r = await api('/api/launch', { method: 'POST', body: JSON.stringify({ id: item.id }) });
      toast(r.message || (r.ok ? '已启动' : '启动失败'), r.ok ? '' : 'warn');
    } catch (e) {
      toast('调用失败：' + e.message, 'warn');
    } finally {
      setTimeout(() => node.classList.remove('busy'), 900);
    }
  }

  function nextPhoto() {
    const list = wallpaperList(state);
    if (list.length < 2) { toast('配图库里还没有别的图'); return; }
    photoIdx = (photoIdx + 1) % list.length;
    renderWallpaper(state);
    const p = list[photoIdx];
    toast('壁纸 ' + (photoIdx + 1) + ' / ' + list.length +
      (photoIdx === 0 ? '（回到算出来的那张）' : (p && p.title ? '：' + p.title.slice(0, 16) : '')));
  }

  function setTheme(next) {
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('wb-theme', next);
  }

  function cycleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    setTheme(next);
    toast('配色：' + THEME_CN[next]);
  }

  /* ------------------------------------------------------- 设置面板 */

  /* 设置面板完全由后端的 /api/settings-schema 驱动：有哪些字段、什么类型、
     标签与说明，全在 app/settings_schema.py 里。
     以前这里把每个字段手写两遍（读一遍、写一遍），加一个配置项要改
     index.html + fillSettings + saveSettings 三处，结果漏掉一大半
     （政经加权词、阅读速度、心跳阈值、窗口尺寸…全都改不了）。现在只加一处。 */

  let schemaGroups = null;
  let schemaFields = [];

  function getPath(obj, path) {
    return path.split('.').reduce((cur, k) => (
      cur && typeof cur === 'object' ? cur[k] : undefined), obj);
  }

  function setPath(obj, path, val) {
    const parts = path.split('.');
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!cur[parts[i]] || typeof cur[parts[i]] !== 'object') cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = val;
  }

  // 配置值 <-> 输入框文本。json 的 fromText 会抛错，由调用方兜住。
  const CODE = {
    lines: {
      toText: (v) => (Array.isArray(v) ? v : []).join('\n'),
      fromText: (t) => t.split('\n').map(s => s.trim()).filter(Boolean)
    },
    keywords: {
      toText: (v) => (Array.isArray(v) ? v : []).join('  '),
      fromText: (t) => t.split(/[\n,，、\s]+/).map(s => s.trim()).filter(Boolean)
    },
    feeds: {
      toText: (v) => (Array.isArray(v) ? v : [])
        .map(f => [f.cat || '要闻', f.name || '', f.url || ''].join(' | ')).join('\n'),
      fromText: (t) => t.split('\n').map(s => s.trim()).filter(Boolean).map(line => {
        const p = line.split('|').map(s => s.trim());
        if (p.length >= 3) return { cat: p[0], name: p[1], url: p[2] };
        if (p.length === 2) return { cat: '要闻', name: p[0], url: p[1] };
        return { cat: '要闻', name: p[0].replace(/^https?:\/\//, '').split('/')[0], url: p[0] };
      }).filter(f => f.url && f.name)
    },
    ann: {
      toText: (v) => (Array.isArray(v) ? v : [])
        .map(a => (a.date || '') + ' | ' + (a.title || '')).join('\n'),
      fromText: (t) => t.split('\n').map(s => s.trim()).filter(Boolean).map(line => {
        const i = line.indexOf('|');
        if (i < 0) return null;
        return { date: line.slice(0, i).trim(), title: line.slice(i + 1).trim() };
      }).filter(a => a && a.date && a.title)
    },
    json: {
      toText: (v) => JSON.stringify(v || [], null, 2),
      fromText: (t) => {
        const v = JSON.parse(t || '[]');
        if (!Array.isArray(v)) throw new Error('不是一个数组');
        return v;
      }
    },
  };
  const TEXTLIKE = ['lines', 'keywords', 'feeds', 'ann', 'json'];
  const ROWS = { lines: 3, keywords: 3, feeds: 7, ann: 3, json: 12 };

  function fieldRow(f, cfg) {
    const val = getPath(cfg, f.path);
    const wrap = el('div', 'fgrow');

    if (f.type === 'bool') {
      const lab = el('label', 'field fcheck');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = val == null ? !!f.default : !!val;
      box.dataset.path = f.path;
      box.dataset.type = 'bool';
      box.dataset.label = f.label;
      lab.append(box, el('span', 'fcheck-t', f.label));
      wrap.appendChild(lab);
    } else {
      const lab = el('label', 'field');
      lab.appendChild(el('span', null, f.label));
      let ctl;
      if (f.type === 'select') {
        ctl = el('select');
        (f.options || []).forEach(([v, t]) => {
          const o = el('option', null, t);
          o.value = v;
          ctl.appendChild(o);
        });
        ctl.value = val != null ? String(val) : (f.default != null ? String(f.default) : '');
      } else if (TEXTLIKE.indexOf(f.type) >= 0) {
        ctl = el('textarea');
        ctl.rows = ROWS[f.type] || 3;
        ctl.spellcheck = false;
        ctl.value = CODE[f.type].toText(val);
      } else if (f.type === 'int') {
        ctl = el('input');
        ctl.type = 'number';
        if (f.min != null) ctl.min = f.min;
        if (f.max != null) ctl.max = f.max;
        ctl.value = val != null ? val : (f.default != null ? f.default : '');
      } else {
        ctl = el('input');
        ctl.type = f.type === 'password' ? 'password' : 'text';
        if (f.ph) ctl.placeholder = f.ph;
        ctl.value = val != null ? val : '';
      }
      ctl.dataset.path = f.path;
      ctl.dataset.type = f.type;
      ctl.dataset.label = f.label;
      lab.appendChild(ctl);
      wrap.appendChild(lab);
    }
    if (f.hint) wrap.appendChild(el('div', 'fhint', f.hint));
    if (f.warn) wrap.appendChild(el('div', 'fwarn', f.warn));
    return wrap;
  }

  function renderLibrary(sum) {
    if (!sum) return null;
    const box = el('div', 'fgroup');
    box.appendChild(el('div', 'fgroup-h', '配图库现状'));
    // 没打标的数量也算出来：给个「还有多少张没属性」的直观数字
    const taggedRaw = sum.byTagged || {};
    const tagged = sum.tagged != null
      ? sum.tagged
      : Object.keys(taggedRaw).reduce((n, k) => n + taggedRaw[k], 0);
    const by = Object.keys(taggedRaw).join('/') || '—';
    box.appendChild(el('div', 'fhint',
      '共 ' + (sum.total || 0) + ' 张 · ' + (sum.mb || 0) + ' MB · 已打标 '
      + tagged + ' 张（' + by + '）'
      + (sum.total ? '，还有 ' + Math.max(0, sum.total - tagged) + ' 张待补' : '')));
    const line = (title, obj) => {
      const kv = Object.entries(obj || {});
      if (kv.length) {
        box.appendChild(el('div', 'fhint',
          title + '：' + kv.map(p => p[0] + ' ' + p[1]).join(' · ')));
      }
    };
    line('分类', sum.byCategory);
    line('来源', sum.bySource);
    return box;
  }

  function renderSettings(groups, cfg, path, library) {
    const body = $('cfgBody');
    body.textContent = '';
    const lib = renderLibrary(library);
    if (lib) { body.appendChild(lib); body.appendChild(el('hr', 'sep')); }
    (groups || []).forEach((g, gi) => {
      if (gi) body.appendChild(el('hr', 'sep'));
      const box = el('div', 'fgroup');
      box.appendChild(el('div', 'fgroup-h', g.title || ''));
      (g.fields || []).forEach(f => box.appendChild(fieldRow(f, cfg)));
      body.appendChild(box);
    });
    $('cfgPath').textContent = '配置文件：' + (path || '');
  }

  function collectSettings() {
    const cfg = JSON.parse(JSON.stringify(settings || {}));
    const bad = [];
    $('cfgBody').querySelectorAll('[data-path]').forEach(ctl => {
      const path = ctl.dataset.path;
      const type = ctl.dataset.type;
      let val;
      if (type === 'bool') {
        val = ctl.checked;
      } else if (type === 'int') {
        const f = schemaFields.filter(x => x.path === path)[0] || {};
        let n = parseInt(ctl.value, 10);
        if (isNaN(n)) n = f.default != null ? f.default : 0;
        if (f.min != null) n = Math.max(f.min, n);
        if (f.max != null) n = Math.min(f.max, n);
        val = n;
      } else if (type === 'select') {
        val = ctl.value;
      } else if (TEXTLIKE.indexOf(type) >= 0) {
        try {
          val = CODE[type].fromText(ctl.value);
        } catch (e) {
          bad.push((ctl.dataset.label || path) + '：' + e.message);
          return;
        }
      } else {
        val = ctl.value.trim();
      }
      setPath(cfg, path, val);
    });
    return { cfg: cfg, bad: bad };
  }

  async function openSettings() {
    $('cfgMsg').textContent = '';
    $('cfgMsg').className = 'modal-msg';
    try {
      const r = await api('/api/config');
      const s = await api('/api/settings-schema');
      settings = r.config;
      schemaGroups = s.groups || [];
      schemaFields = [];
      schemaGroups.forEach(g => (g.fields || []).forEach(f => schemaFields.push(f)));
      renderSettings(schemaGroups, r.config, r.path, s.library);
      $('modal').hidden = false;
    } catch (e) { toast('读取配置失败：' + e.message, 'warn'); }
  }

  async function saveSettings() {
    const msg = $('cfgMsg');
    const got = collectSettings();
    if (got.bad.length) {
      msg.textContent = '格式不对 → ' + got.bad.join('；');
      msg.className = 'modal-msg bad';
      return;
    }
    msg.textContent = '正在保存…';
    msg.className = 'modal-msg';
    try {
      await api('/api/config', {
        method: 'POST', body: JSON.stringify({ config: got.cfg })
      });
      if (got.cfg.theme) setTheme(got.cfg.theme);
      msg.textContent = '已保存，正在重新生成';
      msg.className = 'modal-msg ok';
      await refreshState();
      await api('/api/refresh', { method: 'POST', body: '{}' });
      schedulePoll();
      setTimeout(() => { $('modal').hidden = true; }, 700);
    } catch (e) {
      msg.textContent = '保存失败：' + e.message;
      msg.className = 'modal-msg bad';
    }
  }

  /* ------------------------------------------------------------ 绑定 */

  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.getAttribute('data-act');

    if (act === 'refresh') {
      toast('正在后台更新天气、壁纸与简报');
      try { await api('/api/refresh', { method: 'POST', body: '{}' }); schedulePoll(); }
      catch (e) { toast('刷新没发出去：' + e.message, 'warn'); }
    } else if (act === 'theme') {
      cycleTheme();
    } else if (act === 'nextPhoto') {
      nextPhoto();
    } else if (act === 'settings') {
      openSettings();
    } else if (act === 'closePick') {
      $('pickModal').hidden = true;
    } else if (act === 'pickManual') {
      const v = $('pickManual').value.trim();
      if (!v) { $('pickMsg').textContent = '先粘一个路径'; return; }
      addLauncher(v);
    } else if (act === 'closeModal') {
      $('modal').hidden = true;
    } else if (act === 'saveConfig') {
      saveSettings();
    } else if (act === 'closeDrawer') {
      closeDrawer();
    } else if (act === 'dPrev') {
      openDrawer(current > 0 ? current - 1 : (state.daily.items.length - 1));
    } else if (act === 'dNext') {
      openDrawer((current + 1) % state.daily.items.length);
    } else if (act === 'todoAdd') {
      addTodo();
    } else if (act === 'quit') {
      try { await api('/api/quit', { method: 'POST', body: '{}' }); } catch (e) { /* ignore */ }
      document.body.innerHTML =
        '<div style="display:grid;place-items:center;height:100vh;font:14px sans-serif;color:#999">' +
        '工作台已退出，关掉这个窗口即可。</div>';
    }
  });

  $('drawerMask').addEventListener('click', closeDrawer);

  $('todoInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); addTodo(); }
  });

  document.addEventListener('keydown', (ev) => {
    const tag = (ev.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea';

    if (ev.key === 'Escape') {
      if (!$('pickModal').hidden) { $('pickModal').hidden = true; return; }
      if (!$('modal').hidden) { $('modal').hidden = true; return; }
      if ($('drawer').classList.contains('open')) { closeDrawer(); return; }
    }
    if (typing) return;

    if ($('drawer').classList.contains('open')) {
      if (ev.key === 'ArrowLeft' && state) openDrawer(current > 0 ? current - 1 : state.daily.items.length - 1);
      if (ev.key === 'ArrowRight' && state) openDrawer((current + 1) % state.daily.items.length);
      return;
    }
    if (ev.key === 'n' || ev.key === 'N') nextPhoto();
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'r') {
      ev.preventDefault(); refreshState();
    }
  });

  // 关窗口不在这里退出！
  // 页面卸载事件（pagehide）在「重载」时同样触发，早先在这里发退出请求，
  // 结果按一下 F5 就把整个服务关掉了（还静默无声）。
  // 现在关窗口交给心跳看门狗：窗口没了 → 心跳停 → 服务自己收工。
  // 想立刻退出就用右上角那个「退出」按钮（走 /api/quit）。

  // 心跳：让后端的看门狗知道「窗口还开着」。
  // 但别把它的可靠性全押在这个定时器上 —— 最小化或被遮挡时浏览器会节流甚至
  // 冻结它，所以服务端现在以「窗口进程还在不在」为主判据（见 actions.window_alive），
  // 心跳只是辅助。下面两个监听是兜底：窗口一回到前台就立刻补一次。
  function pokeHeartbeat() {
    api('/api/ping', { method: 'POST', body: '{}' }).catch(() => {});
  }
  setInterval(pokeHeartbeat, 5000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pokeHeartbeat();
  });
  window.addEventListener('focus', pokeHeartbeat);

  setInterval(idlePoll, 12000);

  /* ------------------------------------------------------------ 时钟 */

  let lastDay = '';
  function tick() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    $('clock').textContent = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    const day = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
    if (lastDay && lastDay !== day) refreshState();
    lastDay = day;
  }

  /* ------------------------------------------------------------ 启动 */

  const QS = new URLSearchParams(location.search);
  const qsTheme = QS.get('theme');
  setTheme(THEMES.indexOf(qsTheme) >= 0 ? qsTheme : (localStorage.getItem('wb-theme') || 'light'));
  bindWheel();
  tick();
  setInterval(tick, 1000);
  refreshState().then(() => {
    if (state && state.status.generating) schedulePoll();
    const auto = QS.get('open');                 // 预览/深链：直接打开第 N 条的二级页面
    if (auto !== null && state) openDrawer(parseInt(auto, 10) || 0);
    if (QS.get('pick') !== null) openPicker();   // 深链：直接打开「添加启动项」
    if (QS.get('settings') !== null) openSettings();   // 深链：直接打开设置
  });

})();
