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

  /* 「与你相关」四个字塞进 10px 的标签会被挤成「与你和相」，
     而且「与你」和「相关」在这个位置是同一件事 —— 列表里统一叫「相关」。 */
  const catLabel = (c) => {
    const s = String(c || '').trim();
    return s === '与你相关' ? '相关' : (s || '要闻');
  };

  let state = null;
  let settings = null;
  let pollTimer = null;
  let photoIdx = 0;
  let current = -1;           // 当前打开的简报条目
  let _bmIds = new Set();     // 已收藏的 id，渲染时比对（别在循环里反复建 Set）

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
    if (!wrap) return;
    const t = el('div', 'toast' + (kind ? ' ' + kind : ''), msg);
    wrap.appendChild(t);
    setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 3400);
  }
  /* 挂到 window：别的脚本（评分、收藏、页面插件）用的是 window.toast。
     以前没挂，那些地方写的是 `window.toast && window.toast(...)` —— 守卫把
     失败悄悄吃掉了，界面上一个字都不出。别删这一行。 */
  window.toast = toast;

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

  /* ------------------------------------------------- 壁纸：双层交叉淡入 */
  /* 这里踩过两个坑，改之前先看：
       · 以前每次 renderWallpaper 都 new Image() 再改 backgroundImage，
         连按 N 时回调乱序 —— 墙上是第 2 张、photoIdx 已经是第 3 张。
       · 只比文件名会漏掉「介绍后补」：文件没变、intro 从空变成有，文案却不刷新。
     现在 wallFile 是唯一真相：索引从它反查，图层和文案一次性提交。 */
  let wallFile = '';        // 墙上这张（唯一真相）
  let copyKey = '';         // 文案对应的 file|intro，变了才重排
  let wallSeq = 0;          // 换图序号：迟到的 onload 直接丢弃
  let wallSettle = 0;       // 交叉结束把图落到底层的定时器

  function paintWall(url) {
    const base = $('wallpaper');
    const top = $('wallpaperB');       // B 永远在上面，每次都当「入场层」
    if (!base || !top) return;
    const seq = ++wallSeq;
    const img = new Image();
    img.onload = () => {
      if (seq !== wallSeq) return;     // 期间又按了 N，这轮作废
      clearTimeout(wallSettle);
      top.style.backgroundImage = 'url("' + url + '")';
      top.classList.add('on');         // 从 0 淡到 1，盖住旧图 = 真交叉
      // 过渡走完再把图落到 A、撤掉 B：B 淡出时底下已经是同一张，看不见跳变
      wallSettle = setTimeout(() => {
        base.style.backgroundImage = top.style.backgroundImage;
        base.classList.add('on');
        top.classList.remove('on');
      }, 520);
    };
    img.onerror = () => { if (seq === wallSeq) toast('这张图没加载出来', 'warn'); };
    img.src = url;
  }

  /* 两段介绍：第一段「画面」当引子，第二段「科普」当正文。
     数据是 140-180 字、固定 \n\n 分两段；只有一段的老数据也能吃下。 */
  function introParts(p, chosen, first) {
    const txt = (p.intro || (first ? chosen.quote : '') || '').trim();
    if (!txt) return ['', '这张图的介绍还在生成，过一会儿再来看看。'];
    const parts = txt.split(/\n{2,}/).map((x) => x.trim()).filter(Boolean);
    if (parts.length <= 1) return [parts[0] || '', ''];
    return [parts[0], parts.slice(1).join('\n\n')];
  }

  /* 图名：中文标题优先；哈勃那批是英文原句，用来源名顶上，别把英文挂在首页 */
  function homeKicker(p) {
    const t = (p.title || '').trim();
    if (t && /[\u4e00-\u9fff]/.test(t)) return t;
    const name = {
      astro: '哈勃 · 天文台', apod: 'NASA · APOD', nasa: 'NASA',
      local: '本人作品', spotlight: '每日精选', bing: '必应壁纸',
    }[p.src];
    return name || '今日壁纸';
  }

  /* 图库空着时首页显示什么。
     这是陌生人看到的第一个界面 —— 一块纯黑 + 一行小字会让人以为没装好。
     所以给一段完整的「说明 + 两个出口」，并让 CSS 铺一层暮色渐变当底
     （body[data-wall="empty"]）。等第一张图到了，这段会被 renderHomeCopy 换掉。 */
  function renderHomeEmpty(s) {
    const box = $('homeCopy');
    if (!box) return;
    copyKey = 'empty';
    const total = (s.photos || {}).total || 0;
    const busy = (s.photos && s.photos.syncing) || (s.status && s.status.note || '').indexOf('配图') >= 0;

    $('homeKicker').textContent = total ? '图库是空的' : '第一次打开';
    $('homeLead').textContent = total
      ? '图库里有 ' + total + ' 张，但文件都不在了。重新抓一批，或者换一个文件夹。'
      : (busy ? '正在抓第一批风景，十几秒就好。' : '还没有配图。让它自己抓一批，或者挑一个你自己的文件夹。');
    $('homeNote').textContent = total
      ? '图只存在本机，不会上传到任何地方。'
      : '工作台每天从这里挑一张铺满屏幕，并写一段这张图的介绍。图全部存在本机 —— '
        + '没有账号，也没有上传。';

    const meta = $('homeMeta');
    meta.textContent = '';
    const btn = (text, act) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.act = act;
      b.textContent = text;
      return b;
    };
    meta.appendChild(btn(busy ? '再抓一次' : '抓一批图', 'fetchPhotos'));
    meta.appendChild(el('i', 'hm-sep'));
    meta.appendChild(btn('选个文件夹', 'settings'));
    meta.appendChild(el('i', 'hm-sep'));
    meta.appendChild(btn('→ 先去工作台', 'goDesk'));
    box.dataset.ready = '1';
    box.classList.remove('swap');
  }

  function renderHomeCopy(p, chosen, first) {
    const box = $('homeCopy');
    if (!box) return;
    const [lead, note] = introParts(p, chosen, first);
    const key = (p.file || '') + '|' + (p.intro || '').length;
    if (key === copyKey) return;       // 连 12 秒一次的轮询都不重排
    copyKey = key;

    const apply = () => {
      $('homeKicker').textContent = homeKicker(p);
      $('homeLead').textContent = lead || '这张图没有配文字。';
      $('homeNote').textContent = note;
      const meta = $('homeMeta');
      meta.textContent = '';
      if (p.credit) meta.appendChild(el('span', null, p.credit));
      meta.appendChild(el('i', 'hm-sep'));
      meta.appendChild(el('span', null, '按 N 换'));
      meta.appendChild(el('i', 'hm-sep'));
      const go = document.createElement('button');
      go.type = 'button';
      go.dataset.act = 'goDesk';
      go.textContent = '→ 工作台';
      meta.appendChild(go);
      box.classList.remove('swap');
    };

    // 首帧直接出现；之后换图先淡出 180ms 再换字，避免两段字重影
    if (box.dataset.ready === '1') {
      box.classList.add('swap');
      setTimeout(apply, 180);
    } else {
      box.dataset.ready = '1';
      apply();
    }
  }

  /* 工作台页没有壁纸，但「现在墙上是哪张、为什么是它」要能一眼看到 */
  function renderPeek(p, chosen) {
    const t = $('peekText');
    if (!t) return;
    const why = (photoIdx === 0 && chosen.why) || p.why || '';
    t.textContent = homeKicker(p) + (why ? '　·　' + why : '');
  }

  function renderWallpaper(s) {
    const list = wallpaperList(s);
    const chosen = (s.photos || {}).chosen || {};

    // 从 wallFile 反查索引：刷新后 hero 换序，photoIdx 也不会指到别的图
    if (wallFile) {
      const i = list.findIndex((x) => x.file === wallFile);
      if (i >= 0) photoIdx = i;
    }
    if (photoIdx >= list.length || photoIdx < 0) photoIdx = 0;
    const p = list[photoIdx];

    const box = $('wallCredit');
    box.textContent = '';

    if (!p) {
      // 第一次打开、或者图全被删了：首页不能是一块黑板。
      // 这里给一张 CSS 画的暮色底 + 一段说明 + 两个能立刻点的按钮，
      // 让它看起来像「还在等第一张图」，而不是「坏了」。
      box.appendChild(el('div', 'wall-src', s.photos && s.photos.total
        ? '配图库 ' + s.photos.total + ' 张' : '配图库为空'));
      document.body.dataset.wall = 'empty';
      // 上一张图可能是刚被删掉的那张，留着内联背景会盖住暮色底
      ['wallpaper', 'wallpaperB'].forEach((id) => {
        const n = $(id);
        if (n) { n.style.backgroundImage = ''; n.classList.remove('on'); }
      });
      wallFile = '';
      renderHomeEmpty(s);
      return;
    }
    delete document.body.dataset.wall;

    if (p.file !== wallFile) {
      wallFile = p.file;
      paintWall(p.url);
    }

    const first = photoIdx === 0;
    renderHomeCopy(p, chosen, first);
    renderPeek(p, chosen);

    /* 介绍必须跟着「现在墙上这张」走。
       这里踩过：这段以前写在 renderQuote() 里、只读 s.daily.quote（=主图那一张），
       而 renderQuote 又排在 renderWallpaper 之后执行 —— 结果按 N 换了几张图，
       墙上的字还是第一张的。现在按 photoIdx 取当前那张自己的 intro。 */
    const q = (p.intro || (first ? chosen.quote : '') || '').trim();
    const qEl = $('quote');
    if (qEl) qEl.textContent = q || '这张图的介绍正在生成……';
    const fromEl = $('quoteFrom');
    if (fromEl) fromEl.textContent = first ? (s.daily.quoteFrom || '') : (p.credit || p.src || '');
  }

  /* 今日壁纸弹层：大图 + 两段 + 上/下一张。翻到哪张就是哪张 —— 关掉不会跳回。 */
  function openWall() {
    const list = wallpaperList(state);
    const p = list[photoIdx];
    if (!p) return;
    const chosen = (state.photos || {}).chosen || {};
    const [lead, note] = introParts(p, chosen, photoIdx === 0);

    $('wallPreview').src = p.url;
    $('wallPreview').alt = homeKicker(p);
    $('wallCap').textContent = homeKicker(p);
    $('wallBody').textContent = [lead, note].filter(Boolean).join('\n\n');

    const meta = $('wallMeta');
    meta.textContent = '';
    const bits = [];
    if (p.credit) bits.push(p.credit);
    bits.push((photoIdx + 1) + ' / ' + list.length);
    bits.forEach((b, i) => {
      if (i) meta.appendChild(el('i', 'wm-sep'));
      meta.appendChild(el('span', null, b));
    });
    $('wallModal').hidden = false;
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
    const ready = items.filter(i => isReady(i)).length;
    const localN = items.filter(i => isReady(i) && i.summaryBy !== 'ai').length;

    // 分段标题里的 pill 只放条数：挤在胶囊里的字越多越不像"标签"。
    // 「已备 / 生成中」这类进度放到右侧的 feedHint 去。
    const hint = $('feedHint');
    if (exp.running) {
      $('newsMeta').textContent = exp.done + '/' + exp.total;
      if (hint && document.getElementById('feedBody').dataset.tab === 'news')
        hint.textContent = '生成中 ' + exp.done + ' / ' + exp.total;
    } else if (items.length) {
      $('newsMeta').textContent = items.length + ' 条';
      if (hint && document.getElementById('feedBody').dataset.tab === 'news')
        // 「已备 0 / 10」读起来像加载失败。一条都没好时直说要补，
        // 有进度时才报分数。
        hint.textContent = '约 ' + Math.round(s.daily.readMinutes || 15) + ' 分钟' +
          (ready === 0 ? '　·　简述待补'
            : (ready < items.length ? '　·　已备 ' + ready + '/' + items.length : ''));
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
      b.appendChild(el('span', 'ncat', catLabel(it.cat)));
      // ● 模型写的 / ◐ 本地重排 / ◈ 外部检索补的 / ○ 还没有
      // ◈ 是 2026-11-05 加的：简报不再只从本地候选池里挑，
      // 模型可以自己检索补充，这类条目要有个记号，方便一眼看出「今天有几条是搜来的」。
      // 只有 ◈ 值得占位：●/◐/○ 在 10px 下就是个看不清的小半圆，
      // 十行全是它的时候纯属噪音 —— 简述是谁写的放进 title 就够了。
      if (it.external) {
        const mark = el('span', 'nitem-idx', '◈');
        mark.title = '外部检索补进来的（不在本地候选池里）';
        b.appendChild(mark);
      }
      b.appendChild(el('span', 'ntitle', it.title || ''));
      b.title = [
        it.external ? '这条来自外部检索，不在本地候选池里' : '',
        isReady(it) ? (it.summaryBy === 'ai' ? '简述是模型写的' : '简述是本地拼的')
          : (exp.running ? '简述还在生成' : '简述待补'),
        it.rankWhy ? '排序：' + it.rankWhy : '',
      ].filter(Boolean).join('\n');
      // 评分：☆ 常驻可见（以前整组只在悬停时出现，等于没有入口 ——
      // 谁也不会把鼠标停在一条新闻上才发现能打分）；
      // ✕ 仍然只在悬停时露出，避免误点成「不感兴趣」。
      const rated = (it.rated === null || it.rated === undefined) ? null : Number(it.rated);
      const rate = el('span', 'nrate' + (rated !== null ? ' on' : ''));
      const rx = el('span', 'nr-btn nr-x', '✕');
      rx.dataset.r = '0';
      rx.title = '不感兴趣（记 0 分）';
      const rs = el('span', 'nr-btn nr-s',
        rated === null ? '☆' : (rated === 0 ? '✕' : String(rated)));
      if (rated === 0) rs.classList.add('zero');
      rs.title = rated === null
        ? '给这条打分（1–10）'
        : (rated === 0 ? '你标了不感兴趣，点击改分' : '你给了 ' + rated + ' 分，点击改分');
      rate.appendChild(rx);
      rate.appendChild(rs);

      // 收藏：常驻在评分右边。收藏是「以后再翻」，和打几分无关。
      const bkOn = bmHas(it.bmKey);
      const bk = el('span', 'nr-btn nr-b' + (bkOn ? ' on' : ''), bkOn ? '⚑' : '⚐');
      bk.title = bkOn ? '已收藏 · 点击取消' : '收藏（在「收藏」里看）';
      let bkBusy = false;          // 连点两下会「收藏→取消」，一次都没成还闪一下
      bk.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (bkBusy) return;
        bkBusy = true;
        // 按钮很窄，只放一个旗子：入库的文案（⚑ 已收藏）会把行撑开
        toggleBookmark('news', it, null, (on) => {
          bkBusy = false;
          bk.textContent = on ? '⚑' : '⚐';
          bk.classList.toggle('on', on);
          bk.title = on ? '已收藏 · 点击取消' : '收藏（在「收藏」里看）';
        });
        setTimeout(() => { bkBusy = false; }, 1200);   // 请求失败也要解锁
      });
      rate.appendChild(bk);

      rate.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const hit = ev.target.closest('[data-r]');
        if (hit) { submitRate(it, Number(hit.dataset.r), b); return; }
        const star = ev.target.closest('.nr-s');
        if (star) openRatePop(star, it);
      });
      b.appendChild(rate);

      b.addEventListener('click', () => openDrawer(i));
      box.appendChild(b);
    });

    // 条数变少时把位置夹回来，否则轮询刷新会停在一片空白上
    scrollIdx = Math.max(0, Math.min(scrollIdx, Math.max(0, items.length - visibleRows())));
    box.scrollTop = scrollIdx * rowStep();   // 刷新后保持窗口位置
    updatePos();
    paintRankNote();
  }

  /* ------------------------------------------- 两行小窗口：滚轮一次翻一行 */

  let scrollIdx = 0;      // 窗口顶部显示的是第几条

  function rowStep() {
    const cs = getComputedStyle(document.documentElement);
    const h = parseFloat(cs.getPropertyValue('--row-h')) || 52;
    const g = parseFloat(cs.getPropertyValue('--row-gap')) || 3;
    return h + g;
  }

  /** 一屏能完整放下几条。以前写死 2，是因为这块原本只有两行高；
      后来列表撑满整页，位置提示就变成「1–2 / 10」—— 明明看得到 7 条。 */
  function visibleRows() {
    const box = $('newsItems');
    const step = rowStep();
    if (!box || !step) return 2;
    const gap = parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue('--row-gap')) || 0;
    return Math.max(2, Math.floor((box.clientHeight + gap) / step));
  }

  function updatePos() {
    const total = (state && state.daily && state.daily.items || []).length;
    if (!total) { $('itemsPos').textContent = '—'; return; }
    const vis = Math.min(total, visibleRows());
    if (vis >= total) { $('itemsPos').textContent = '共 ' + total + ' 条'; return; }
    const first = Math.min(total, scrollIdx + 1);
    const last = Math.min(total, scrollIdx + vis);
    $('itemsPos').textContent = first + '–' + last + ' / ' + total;
  }

  /** 用索引驱动，不读 scrollTop —— 免得和平滑滚动互相打架 */
  function scrollToIdx(i) {
    const items = (state && state.daily && state.daily.items) || [];
    const box = $('newsItems');
    const max = Math.max(0, items.length - visibleRows());
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
      if (items.length <= visibleRows()) return;
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

    $('dCat').textContent = catLabel(it.cat);
    $('dIdx').textContent = '第 ' + (idx + 1) + ' / ' + items.length + ' 条';
    $('dTitle').textContent = it.title || '';
    $('dMeta').textContent = [it.source, relTime(it.at), it.why]
      .filter(Boolean).join('  ·  ');

    // 排序理由：让「为什么这条在前面」可见。空着就整行不占位。
    const rk = $('dRankWhy');
    rk.textContent = it.rankWhy ? '为什么排这里：' + it.rankWhy : '';
    rk.hidden = !it.rankWhy;
    rk.title = rk.textContent;

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
    paintRate(it);
    paintBmBtn($('dBookmark'), bmHas(it.bmKey));

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

    // 点开＝弱正向信号（后端按三分之一条评分算），让「我读了但没打分」的也留痕。
    // 每天只推 10 条，光靠 1-10 分攒样本太慢，这是唯一不打扰人的扩容方式。
    // 失败就算了 —— 读不影响。
    fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'open', title: it.title, source: it.source, cat: it.cat,
      }),
    }).catch(() => {});
  }

  function isReady(it) {
    const target = (state && state.daily && state.daily.perItemChars) || 600;
    // stub 只表示「这条是没模型时的兜底」，不代表简述不能用 ——
    // 以前把它一起判掉，兜底条目就永远卡在「正在生成」，永远显示不出来。
    if (!it || !it.summary) return false;
    // 与后端 ai.is_real_summary 同一套阈值：本地重排 30 字即算可用
    // （同样的材料重试一万遍也是同一个结果，重试纯浪费），
    // 模型级要 180 字（半个目标）以上。
    const need = (it.summaryBy === 'local') ? 30 : Math.max(180, target * 0.5);
    return it.summary.length >= need;
  }

  /** 简述是谁写的：local = 本地重排（零成本、即刻可用），ai = 模型写的。 */
  function byLabel(it) {
    return (it && it.summaryBy === 'ai') ? '模型' : '本地';
  }

  function paintSummary(it) {
    const box = $('dSummary');
    box.classList.remove('loading');
    box.textContent = '';
    box.appendChild(paragraphs(it.summary));
    $('dRead').textContent = fmtMinutes(it.mins) + ' · ' + it.summary.length +
      ' 字 · ' + byLabel(it) + (it.summaryBy === 'ai' ? '' : '（模型版由每日任务补写）');
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
          box.textContent = '这条还没写好。要点可以先看；点右上角「刷新」会再跑一轮，'
            + '仍不行的话会自动用本地材料拼一段可读的简述。';
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
      b.draggable = true;                 // 可以拖着换位置
      b.dataset.id = item.id;

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

      // 悬停出现的小工具：上下移 / 改名 / 移除。
      // 跟待办行同一套图标按钮（20px、hover 才出现、右侧对齐）——
      // 两处都是「一条内容 + 一排小操作」，长得不一样就是在提醒用户这是两套东西。
      const tools = el('div', 'lac-tools');
      const mk = (icon, tip, act, extra) => {
        const t = el('button', 'iconbtn' + (extra ? ' ' + extra : ''));
        t.innerHTML = icon;
        t.title = tip;
        t.addEventListener('click', (ev) => {
          ev.stopPropagation();
          act();
        });
        return t;
      };
      tools.appendChild(mk(TodoUI.ICON.up, '往上挪', () => moveLauncher(item, -1)));
      tools.appendChild(mk(TodoUI.ICON.down, '往下挪', () => moveLauncher(item, 1)));
      tools.appendChild(mk(TodoUI.ICON.pencil, '改名', () => renameLauncher(item, b), 'pen'));
      tools.appendChild(mk(TodoUI.ICON.x, '移除', () => removeLauncher(item), 'del'));
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

  async function launcherReorder(ids) {
    if (!ids || ids.length < 2) return;
    try {
      const r = await api('/api/launchers', {
        method: 'POST', body: JSON.stringify({ action: 'reorder', ids })
      });
      if (state) { state.launchers = r.launchers; renderLaunchers(state); }
      toast('顺序已调整');
    } catch (e) { toast('调整顺序失败：' + e.message, 'warn'); }
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

  /* 以前这里是「2 天」大号 + 「立冬」小号并排，读出来就是「2天-立冬」——
     两个名词硬凑在一起，既没有主语也没有介词，像个没写完的标签。
     现在补上「接下来」和「还有」，一行读完就是一句人话。 */
  function renderFestivalOne(s) {
    const box = $('festivalOne');
    const cd = s.today.countdown || [];
    box.textContent = '';
    if (!cd.length) { box.hidden = true; return; }
    const c = cd[0];
    box.hidden = false;
    box.classList.toggle('soon', c.inDays <= 7);
    box.appendChild(el('span', 'fe-kicker', '接下来'));
    box.appendChild(el('span', 'fe-title', c.title));
    box.appendChild(el('span', 'fe-days', '还有 ' + c.inDays + ' 天'));
    if (c.label) box.appendChild(el('span', 'fe-date', c.label));
    box.title = c.title + ' · ' + (c.label || '') + '（还有 ' + c.inDays + ' 天）';
  }

  /* --------------------------------------------------------- 待办 */

  /* 不限量：列表多长都照画，超出就滚。排序只做一件事 ——
     做完的沉到底（勾一条不会让别的条目乱跳），其余保持用户添加的顺序。 */
  function todoOrdered(list) {
    return list.filter(t => !t.done).concat(list.filter(t => t.done));
  }

  function todoWhenChip(t) {
    if (!t.remindAt) return null;
    const at = new Date(t.remindAt);
    let cls = 'todo-when';
    if (t.reminded && !t.done) cls += ' overdue';
    else if (!t.reminded && at - Date.now() < 3600e3 * 3) cls += ' soon';
    const chip = el('div', cls);
    chip.innerHTML = TodoUI.ICON.bell;
    chip.appendChild(el('span', null,
      t.reminded ? (t.done ? '提醒过 · 已完成' : '提醒过') : TodoUI.label(t.remindAt)));
    chip.title = String(t.remindAt).replace('T', ' ');
    return chip;
  }

  function todoRow(t) {
    const ringing = !!(state && state.reminders && (state.reminders.ring || [])
      .some(r => r.id === t.id));
    const row = el('div', 'todo' + (t.done ? ' done' : '') + (ringing ? ' ringing' : ''));
    row.dataset.id = t.id;
    row.draggable = true;                 // 可以拖着换位置
    row.style.setProperty('--todo-c', TodoUI.colorOf(t.color));

    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = !!t.done;
    cb.title = t.done ? '标为未完成' : '打勾完成';
    cb.addEventListener('change', () => todoSend('toggle', { id: t.id }));

    const main = el('div', 'todo-main');
    const text = el('div', 'todo-text', t.text);
    text.title = '双击改内容';
    text.addEventListener('dblclick', () => editTodoText(t, text));
    main.appendChild(text);
    const chip = todoWhenChip(t);
    if (chip) main.appendChild(chip);

    const acts = el('div', 'todo-acts');
    const color = el('button', 'iconbtn cbtn');
    color.type = 'button';
    color.appendChild(el('span', 'cdot'));
    color.title = '颜色：' + TodoUI.colorName(t.color) + '（点一下换）';
    color.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openTodoPalette(color, t);
    });
    const bell = el('button', 'iconbtn' + (t.remindAt ? ' armed' : ''));
    bell.innerHTML = t.remindAt ? TodoUI.ICON.bellOn : TodoUI.ICON.bell;
    bell.title = t.remindAt
      ? '提醒：' + String(t.remindAt).replace('T', ' ') + '（点一下改）'
      : '设个提醒';
    bell.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openTodoPicker(bell, t);
    });
    // 改名：一支笔。以前只有「双击文字」这条暗门，没人知道能改
    const pen = el('button', 'iconbtn pen');
    pen.innerHTML = TodoUI.ICON.pencil;
    pen.title = '改名';
    pen.addEventListener('click', (ev) => {
      ev.stopPropagation();
      editTodoText(t, text);
    });
    const del = el('button', 'iconbtn del');
    del.innerHTML = TodoUI.ICON.x;
    del.title = '删掉';
    del.addEventListener('click', () => todoSend('remove', { id: t.id }));
    acts.append(color, bell, pen, del);

    row.append(cb, main, acts);
    return row;
  }

  function renderTodoFoot(s) {
    const box = $('todoFoot');
    box.textContent = '';
    const rem = (s && s.reminders) || {};
    const stats = rem.stats || {};
    const next = (rem.next || [])[0];
    const left = el('span', 'tf-next');
    if (next) {
      left.innerHTML = '';
      left.append(el('span', null, '下一个提醒：'));
      left.append(el('b', null, TodoUI.label(next.remindAt) + ' ' + next.text));
      left.title = TodoUI.capText ? TodoUI.capText() : '';
    } else {
      left.append(el('span', null, '点某条右边的铃铛，就能给它定时提醒'));
    }
    const right = el('span', null, stats.armed ? stats.armed + ' 个提醒在等' : '');
    box.append(left, right);
  }

  /* 工作台页的今日速览：简报正文在「资讯」页，这里只取开头几行。
     原来这一页只有启动项和待办，右侧下方空掉半屏。 */
  function renderDeskBrief(s) {
    const box = $('deskBrief');
    if (!box) return;
    const brief = String((s.daily && s.daily.brief) || '').trim();
    const items = (s.daily && s.daily.items) || [];

    // 没配模型时 brief 是空的。以前整张卡收起来，右列就空一块；
    // 现在退一步：用本机就算得出来的统计顶上 —— 今天几条、各分类几条、
    // 大概几分钟读完。没有模型也能知道「今天值不值得点进去」。
    let text = brief;
    if (!text && items.length) {
      const by = {};
      items.forEach((i) => { const c = catLabel(i.cat); by[c] = (by[c] || 0) + 1; });
      const parts = Object.keys(by).map((c) => c + ' ' + by[c]);
      text = '今天 ' + items.length + ' 条：' + parts.join(' · ')
        + '。配一个模型，这里会换成一段四行的当日摘要。';
    }
    if (!text) { box.hidden = true; return; }
    box.hidden = false;
    const t = $('dbText');
    // 不硬截断：交给 CSS 的 line-clamp，换行和标点不会被切在中间。
    if (t) t.textContent = text;
  }

  function renderTodos(s) {
    const box = $('todos');
    box.textContent = '';
    const list = (s && s.todos) || [];
    const left = list.filter(t => !t.done).length;
    // 「3 / 3」会被读成「三条全做完了」，实际是「未完成 3 / 共 3」。
    // 没有完成项时只报条数；有完成项才用「剩 x / 共 y」。
    $('todoMeta').textContent = !list.length ? '空'
      : (left === list.length ? left + ' 条' : '剩 ' + left + ' / ' + list.length);

    // 空列表时整张卡缩到最小：不是「一张空的大卡片」，而是一句提示
    const card = box.closest('.todo-card');
    if (card) card.classList.toggle('is-empty', !list.length);

    if (!list.length) {
      box.appendChild(el('div', 'empty todo-empty', '还没有待办。'));
    } else {
      todoOrdered(list).forEach(t => box.appendChild(todoRow(t)));
    }
    renderTodoFoot(s);
    // 上限跟着后端走（capHours 是「系统支持的最大跨度」，不是配置里那个 horizon）
    const rem = (s && s.reminders) || {};
    if (TodoUI.setCap) TodoUI.setCap(rem.capHours || rem.horizonHours, rem.capAt);
  }

  async function todoSend(action, payload) {
    try {
      const r = await api('/api/todos', {
        method: 'POST', body: JSON.stringify(Object.assign({ action }, payload || {}))
      });
      if (r.ok === false) { toast(r.message || '操作失败', 'warn'); return null; }
      if (state) {
        state.todos = r.todos || state.todos;
        state.reminders = Object.assign({}, state.reminders, {
          ring: r.ring || [], stats: r.stats, next: r.next,
          horizonHours: r.horizonHours,
          capHours: r.capHours, capAt: r.capAt,
        });
        renderTodos(state);
      }
      fireReminders(r);
      if (r.message) toast(r.message);
      return r;
    } catch (e) {
      toast('待办操作失败：' + e.message, 'warn');
      return null;
    }
  }

  function openTodoPicker(anchor, t) {
    TodoUI.picker(anchor, t.remindAt, (at) => {
      todoSend('set_remind', { id: t.id, remindAt: at || '' });
    });
  }

  function openTodoPalette(anchor, t) {
    TodoUI.palette(anchor, t.color, (color) => {
      todoSend('set_color', { id: t.id, color });
    });
  }

  function editTodoText(t, node) {
    if (node.isContentEditable) return;
    node.contentEditable = 'true';
    node.focus();
    const range = document.createRange();
    range.selectNodeContents(node);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      node.contentEditable = 'false';
      const text = (node.textContent || '').trim();
      if (save && text && text !== t.text) await todoSend('edit', { id: t.id, text });
      else renderTodos(state);
    };
    node.addEventListener('keydown', (ev) => {
      ev.stopPropagation();
      if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
      if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
    });
    node.addEventListener('blur', () => finish(true));
  }

  async function addTodo() {
    const inp = $('todoInput');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    const r = await todoSend('add', { text });
    if (!r && text) inp.value = text;      // 失败把内容还给用户
  }

  /* 到点提醒：主窗和小窗谁先看到谁弹，弹完立刻收账（后端只认一次） */
  const rung = new Set();

  function fireReminders(payload) {
    const src = payload || (state && state.reminders) || {};
    const ring = src.ring || [];
    if (!ring.length) return;
    ring.forEach((r) => {
      if (rung.has(r.id)) return;
      rung.add(r.id);
      if (src.toast !== false) {
        TodoUI.notify({
          title: r.late ? '待办提醒（补发）' : '待办提醒',
          body: r.text + '\n' + TodoUI.clock(r.at) + ' · 到点了',
          tag: 'wb-todo-' + r.id + '-' + Date.now(),
          onclick: () => { try { window.focus(); } catch (e) { /* ignore */ } },
        });
      }
      if (src.sound !== false) TodoUI.chime();
      toast('⏰ ' + r.text, 'warn');
      ackReminder(r.id);
    });
  }

  async function ackReminder(id) {
    try {
      const r = await api('/api/todos', {
        method: 'POST', body: JSON.stringify({ action: 'reminder_ack', id })
      });
      if (r && r.ok !== false && state) {
        state.todos = r.todos || state.todos;
        state.reminders = Object.assign({}, state.reminders, {
          ring: r.ring || [], stats: r.stats, next: r.next,
        });
        renderTodos(state);
      }
    } catch (e) { /* 下一轮再收 */ }
  }

  async function pollTodos() {
    if (!state) return;
    try {
      const r = await api('/api/todos');
      if (r && r.ok !== false) {
        state.todos = r.todos || state.todos;
        state.reminders = Object.assign({}, state.reminders, {
          ring: r.ring || [], stats: r.stats, next: r.next,
          horizonHours: r.horizonHours,
        });
        state.mini = r.mini || state.mini;
        renderTodos(state);
        fireReminders(r);
      }
    } catch (e) { /* 断线由 api() 报 */ }
  }

  async function openMini() {
    try {
      const r = await api('/api/mini', { method: 'POST', body: JSON.stringify({ action: 'open' }) });
      if (state && r.mini) state.mini = r.mini;
      toast(r.message || '小窗已打开');
    } catch (e) {
      toast('小窗打不开：' + e.message, 'warn');
    }
  }

  /* --------------------------------------------------- 最近在做 */

  /* 「每天一句话」已退役，壁纸介绍改由 renderWallpaper() 渲染 —— 那段必须跟着
     photoIdx 走（按 N 换图时字要跟着换），单独一个 renderQuote 只读主图会把它盖回去。 */

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
    // 「配图 96 张 · 候选 10 条」是给自己看的心跳，不是给用户看的信息。
    // 他只关心两件：今天有没有东西读、模型配没配。
    const n = (s.daily.items || []).length;
    if (n) meta.push('今天 ' + n + ' 条');
    // 配了模型就报模型名；没配但简报里已经有正文（网页版代工写的），
    // 就别挂「简报只列标题」——那是句假话
    if (s.ai.model) meta.push(s.ai.model);
    else if (!String(s.daily.brief || '').trim()) meta.push('未配置模型 · 简报只列标题');
    $('statusMeta').textContent = meta.join('  ·  ');
  }

  /* ------------------------------------------- 涨星速递：新闻卡里的折叠区 */

  /* 涨星速递：从「简报卡里的折叠区」改成资讯页的第二段。
     拆页的理由见 index.html 里的注释 —— 620px 高塞不下两列 6 个模块。 */
  function renderGithub(s) {
    const box = $('ghList');
    if (!box) return;
    const repos = (s.daily && s.daily.github) || [];
    // 空的时候把胶囊收起来：一个「—」挂在标签上，读起来像没加载完
    const count = $('ghCount');
    if (count) {
      count.textContent = repos.length ? repos.length + ' 个' : '';
      count.hidden = !repos.length;
    }

    box.textContent = '';
    if (!repos.length) {
      box.appendChild(el('div', 'empty',
        '这一栏是让模型在写简报时顺带检索的「近期涨星项目」。'
        + '没配模型、或者模型这次没给出可靠结果，这里就空着 —— 不用当成出错。'));
      return;
    }

    repos.forEach((r) => {
      // 整行是 <a>：button 不能塞进 <a> 里（交互元素嵌套，点击会连链接一起触发），
      // 所以外面套一层，收藏按钮绝对定位压在右上角。
      const wrap = el('div', 'gh-item');
      const a = el('a', 'gh-row');
      a.href = r.url || ('https://github.com/' + (r.name || ''));
      a.target = '_blank';
      a.rel = 'noreferrer';

      const head = el('div', 'gh-rowhead');
      head.appendChild(el('span', 'gh-name', r.name || ''));
      const badge = [r.lang, r.stars].filter(Boolean).join(' · ');
      if (badge) head.appendChild(el('span', 'gh-badge', badge));
      a.appendChild(head);

      // 简述优先：note 是模型写的 2-3 句，desc 只有一行
      const brief = r.note || r.desc || '';
      if (brief) a.appendChild(el('p', 'gh-brief', brief));
      if (r.note && r.why) a.appendChild(el('span', 'gh-why', r.why));
      wrap.appendChild(a);

      const bkOn = bmHas(r.bmKey);
      // 仓库卡空间够，直接写「收藏」两个字：一个空心旗子在这排卡片上
      // 太像装饰，没人会去点。
      const bk = el('button', 'gh-bm' + (bkOn ? ' on' : ''), bkOn ? '已收藏' : '收藏');
      bk.type = 'button';
      bk.title = bkOn ? '已收藏 · 点击取消' : '收藏这个仓库';
      let bkBusy = false;
      bk.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (bkBusy) return;
        bkBusy = true;
        toggleBookmark('github', r, null, (on) => {
          bkBusy = false;
          bk.textContent = on ? '已收藏' : '收藏';
          bk.classList.toggle('on', on);
          bk.title = on ? '已收藏 · 点击取消' : '收藏这个仓库';
        });
        setTimeout(() => { bkBusy = false; }, 1200);
      });
      wrap.appendChild(bk);
      box.appendChild(wrap);
    });
  }

  /* -------------------------------------------------------- 评分 */

  let _pop = null;

  /** 这一条打过几分：null 没评过，0 不感兴趣，1-10 想看程度。 */
  function ratedOf(it) {
    const v = it && it.rated;
    return (v === null || v === undefined) ? null : Number(v);
  }

  /** 抽屉底部的评分条。0 单独放在最左边当「不感兴趣」，和 1-10 隔开一格。 */
  function paintRate(it) {
    const box = $('drScale');
    if (!box || !it) return;
    const cur = ratedOf(it);
    box.textContent = '';

    const zero = el('span', 'dr-i zero' + (cur === 0 ? ' on' : ''), '✕');
    zero.dataset.v = '0';
    zero.title = '不感兴趣（记 0 分）';
    box.appendChild(zero);

    for (let i = 1; i <= 10; i++) {
      const n = el('span', 'dr-i' + (cur === i ? ' on' : ''), String(i));
      n.dataset.v = String(i);
      n.title = i >= 8 ? '很想看' : i >= 5 ? '一般' : '不太想看';
      box.appendChild(n);
    }

    const note = $('drNote');
    if (note) {
      note.textContent = cur === null
        // 以前写「用来调整排序」，把这件事说小了：评分先决定哪 50 条能进模型视野，
        // 再决定这 10 条怎么排。只提排序，用户会以为它只动顺序不动内容。
        ? '只存本机：决定往后推什么、怎么排'
        : (cur === 0 ? '已标不感兴趣，点击可改' : '已评 ' + cur + ' 分，点击可改');
    }
  }

  function closeRatePop() {
    if (_pop) { _pop.remove(); _pop = null; }
  }

  function openRatePop(anchor, item) {
    closeRatePop();
    const pop = el('div', 'rate-pop');
    for (let i = 1; i <= 10; i++) {
      const n = el('span', 'rp-i', String(i));
      n.dataset.v = String(i);
      n.title = i >= 8 ? '很想看' : i >= 5 ? '一般' : '不太想看';
      pop.appendChild(n);
    }
    pop.addEventListener('click', (ev) => {
      const n = ev.target.closest('[data-v]');
      if (!n) return;
      ev.stopPropagation();
      submitRate(item, Number(n.dataset.v), anchor.closest('.nitem'));
      closeRatePop();
    });
    document.body.appendChild(pop);
    // 贴着**星星**弹，不是贴着整行 —— 按整行定位会弹到列表最左边，
    // 和刚点的那个按钮隔着半屏，眼睛会跟丢。
    // 垂直方向对齐星星中线：用 bottom + 6 会落到下一行，看着像点错了行。
    const r = anchor.getBoundingClientRect();
    const pw = pop.offsetWidth || 300;
    const ph = pop.offsetHeight || 40;
    const vw = window.innerWidth, vh = window.innerHeight;
    pop.style.left = Math.max(8, Math.min(r.right - pw, vw - pw - 8)) + 'px';
    pop.style.top = Math.max(8, Math.min(r.top + r.height / 2 - ph / 2, vh - ph - 8)) + 'px';
    _pop = pop;
    setTimeout(() => document.addEventListener('click', closeRatePop, { once: true }), 0);
  }

  async function submitRate(item, score, node) {
    try {
      const r = await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: 'rate', score,
          title: item.title, source: item.source, cat: item.cat, url: item.url,
        }),
      });
      const d = await r.json();
      if (!d.ok) { window.toast && window.toast(d.message || '评分失败', 'warn'); return; }

      // 三处要一起改：内存里的这条、列表里那一行、抽屉里的刻度。
      // 只改一处的后果是「列表显示 8 分，重新点开又变回 ☆」。
      item.rated = score;
      const idx = node ? Number(node.dataset.idx) : current;
      const row = document.querySelector('.nitem[data-idx="' + idx + '"]');
      if (row) {
        row.classList.add('rated');
        const s = row.querySelector('.nr-s');
        if (s) {
          s.textContent = score === 0 ? '✕' : String(score);
          s.classList.toggle('zero', score === 0);
          s.title = score === 0
            ? '你标了不感兴趣，点击改分'
            : '你给了 ' + score + ' 分，点击改分';
        }
        row.querySelector('.nrate').classList.add('on');
      }
      const items = (state && state.daily && state.daily.items) || [];
      if ($('drawer').classList.contains('open') && items[current] === item) paintRate(item);

      const n = d.summary && d.summary.n;
      paintRankNote(n);
      window.toast && window.toast(
        (score === 0 ? '已标不感兴趣' : '已评 ' + score + ' 分') +
        (d.summary && d.summary.active ? '　·　明天的简报会按它排'
          : (n ? '　·　已评 ' + n + ' 条' : '')), 'ok');
    } catch (e) {
      window.toast && window.toast('评分没发出去', 'warn');
    }
  }

  /** 列表底部那句提示：顺手把「已评几条」露出来，评分才有积累感。 */
  function paintRankNote(n) {
    const hint = $('itemsHint');
    if (!hint) return;
    const rk = (state && state.daily && state.daily.rank) || null;
    const cnt = (n === undefined || n === null) ? ((rk && rk.n) || 0) : n;
    if (!cnt) {
      hint.textContent = '点开看要点 · 给一条打分，简报会慢慢按你的口味排';
      hint.title = '';
      return;
    }
    const need = (rk && rk.need) || 3;
    const opens = (rk && rk.nOpen) || 0;
    hint.textContent = '点开看要点　·　已评 ' + cnt + ' 条'
      + (opens ? '、读过 ' + opens + ' 条' : '')
      + (rk && rk.active ? '，正在按你的偏好排' : '，满 ' + need + ' 条后开始按你的偏好排');
    hint.title = (rk && rk.text) || '';
  }

  /* ------------------------------------------------------------ 收藏
     收藏不是评分：评分是「这条好不好」（用来调排序），收藏是「我以后还要用」。
     两件事互不影响，所以各自一个按钮、各自一个文件。
     图标用旗子不用星星 —— ☆ 已经是评分的图标，两个星摆在一起没人分得清。 */

  function bmHas(key) {
    return !!key && _bmIds.has(key);
  }

  function bmApply(items) {
    if (state) state.bookmarks = items || [];
    _bmIds = new Set(((state && state.bookmarks) || []).map((b) => b.id));
    renderBookmarks(state);
  }

  function paintBmBtn(btn, on) {
    if (!btn) return;
    btn.textContent = on ? '⚑ 已收藏' : '⚐ 收藏';
    btn.title = on ? '已收藏，点击取消' : '收藏，以后在「收藏」里看';
    btn.classList.toggle('on', on);
  }

  async function toggleBookmark(kind, item, btn, after) {
    const payload = {
      action: 'toggle', kind,
      item: {
        title: item.title || item.name || '',
        url: item.url || '',
        source: item.source || '',
        cat: item.cat || '',
        // 仓库行没有 summary，只有模型写的 note 和源站 desc
        note: item.note || item.desc || '',
        summary: item.summary || '',
        lang: item.lang || '',
        stars: item.stars || '',
      },
    };
    try {
      const r = await fetch('/api/bookmarks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (!d.ok) {
        window.toast && window.toast(d.message || '收藏失败', 'warn');
        return;
      }
      bmApply(d.items);
      paintBmBtn(btn, d.on);
      if (after) after(d.on);
    } catch (e) {
      window.toast && window.toast('收藏没发出去', 'warn');
    }
  }

  async function removeBookmark(id) {
    try {
      const r = await fetch('/api/bookmarks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'remove', id }),
      });
      const d = await r.json();
      if (d.ok) bmApply(d.items);
    } catch (e) {
      window.toast && window.toast('删不掉，稍后再试', 'warn');
    }
  }

  /** 收藏时间写成「11月7日 18:40」——比「3 小时前」更适合回看旧东西 */
  function stampText(iso) {
    const t = Date.parse(iso || '');
    if (!t) return '';
    const d = new Date(t);
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 '
      + String(d.getHours()).padStart(2, '0') + ':'
      + String(d.getMinutes()).padStart(2, '0');
  }

  function renderBookmarks(s) {
    const box = $('bmList');
    if (!box) return;
    const list = (s && s.bookmarks) || [];
    const cnt = $('bmCount');
    if (cnt) {
      cnt.textContent = list.length ? String(list.length) : '';
      cnt.hidden = !list.length;
    }

    box.textContent = '';
    if (!list.length) {
      box.appendChild(el('div', 'empty',
        '还没有收藏。资讯点右侧的 ⚐，仓库点卡片右上角的「收藏」；打开一条后右上角也有。'));
      return;
    }

    list.forEach((b) => {
      const row = el('div', 'bm-row');
      const head = el('div', 'bm-head');
      head.appendChild(el('span', 'bm-tag' + (b.kind === 'github' ? ' repo' : ''),
        b.kind === 'github' ? '仓库' : '资讯'));

      const a = el('a', 'bm-title', b.title || '（没有标题）');
      a.href = b.url || '#';
      a.target = '_blank';
      a.rel = 'noreferrer';
      head.appendChild(a);

      const del = el('button', 'bm-del', '移除');
      del.type = 'button';
      del.title = '从收藏里去掉（原文链接还在）';
      del.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        removeBookmark(b.id);
      });
      head.appendChild(del);
      row.appendChild(head);

      const meta = [b.source, b.lang, b.stars, stampText(b.savedAt)]
        .filter(Boolean).join('  ·  ');
      if (meta) row.appendChild(el('div', 'bm-meta', meta));
      box.appendChild(row);
    });
  }

  /* ---------------------------------------------------- 资讯页：分段切换 */

  function bindFeedTabs() {
    const tabs = $('feedTabs');
    const body = $('feedBody');
    if (!tabs || !body) return;
    tabs.addEventListener('click', (ev) => {
      const b = ev.target.closest('.feed-tab');
      if (!b) return;
      const tab = b.dataset.tab || 'news';
      body.dataset.tab = tab;
      tabs.querySelectorAll('.feed-tab').forEach((x) =>
        x.classList.toggle('on', x.dataset.tab === tab));
      const hint = $('feedHint');
      if (hint) {
        hint.textContent = tab === 'news' ? '滚轮翻行'
          : tab === 'bm' ? '点标题回原文'
            : '点标题去 GitHub';
      }
    });
  }

  function render(s) {
    state = s;
    // 收藏先同步 id 集合，后面渲染资讯/涨星时才能判断每行的旗子是实心还是空心
    _bmIds = new Set(((s && s.bookmarks) || []).map((b) => b.id));
    renderWallpaper(s); renderTop(s); renderWeather(s);
    renderNews(s); renderGithub(s); renderBookmarks(s);
    renderFestivalOne(s); renderTodos(s);
    renderDeskBrief(s);
    renderLaunchers(s); renderStatus(s);
    /* 分页是后加的：把这一轮数据交给 pages.js，当前可见的那页会拿到
       (root, state) 重画一次。没装 pages.js 也不能影响主界面 —— 所以有判断。 */
    if (window.WB && typeof window.WB.publish === 'function') window.WB.publish(s);
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
      // 每图一介：当前这张图的介绍（每天一句话已废弃）——介绍就位也触发重画
      s.daily.quoteFor || '', (s.daily.quote || '').length,
      ((s.daily.github || []).map(g => g.name).join(',')),   // 涨星速递有更新也重画
      (s.daily.brief || '').length,                          // 简报摘要换了，工作台速览也要换
      ((s.bookmarks || []).map(b => b.id).join(',')),         // 收藏增删也要重画（旗子/列表）
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
    // 已经在启动就别再点：Trae / VS Code 这类冷启动要十几二十秒，
    // 900ms 就把卡片恢复成可点状态，用户会以为没反应，于是点第二次、第三次
    // —— 2026-11-18 的日志里，同一个程序被点了 9 次。
    if (node.classList.contains('busy')) return;
    const sub = node.querySelector('.lac-sub');
    const oldSub = sub ? sub.textContent : '';
    node.classList.add('busy');
    if (sub) sub.textContent = '启动中…';
    let hold = 1500;
    try {
      const r = await api('/api/launch', { method: 'POST', body: JSON.stringify({ id: item.id }) });
      toast(r.message || (r.ok ? '已启动' : '启动失败'), r.ok ? '' : 'warn');
      if (r.ok) hold = 6000;          // 先按住 6 秒，别让人连点
    } catch (e) {
      toast('调用失败：' + e.message, 'warn');
    } finally {
      setTimeout(() => {
        node.classList.remove('busy');
        if (sub && sub.isConnected && sub.textContent === '启动中…') sub.textContent = oldSub;
      }, hold);
    }
  }

  /* 换图统一走这里。
     680ms 内忽略连按：这段时间正在「文案淡出 180 + 图交叉 500」，
     再来一次会叠出三层图，文案也会先闪回旧图名（体验评审实测的卡点）。
     弹层里的上/下一张传 force=true —— 那是明确的逐张浏览，不该被节流。 */
  let lastStep = 0;
  function stepPhoto(delta, force) {
    const list = wallpaperList(state);
    if (list.length < 2) { toast('配图库里还没有别的图'); return false; }
    const now = Date.now();
    if (!force && now - lastStep < 680) return false;
    lastStep = now;
    photoIdx = (photoIdx + delta + list.length) % list.length;
    /* 先定「墙上这张」再渲染，免得 renderWallpaper 又按 wallFile 把索引反查回去。
       但这样一来 renderWallpaper 会认为「文件没变、不用换图」，所以这里得自己叫一次
       paintWall —— 漏了这一步的表现是：文案换了、图还停在上一张（踩过）。 */
    wallFile = list[photoIdx].file;
    paintWall(list[photoIdx].url);
    renderWallpaper(state);
    if (!$('wallModal').hidden) openWall();
    return true;
  }

  function nextPhoto() {
    if (!stepPhoto(1)) return;
    const list = wallpaperList(state);
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
    // 说明文字要跟着当前选项走。原来不管选「接口调用」还是「网页版」，
    // 底下都写着「网页版模式不花 token…」，选了接口的人会以为漏了一步。
    let hint = f.hint;
    if (f.path === 'ai.mode') {
      hint = String(val || 'api') === 'web'
        ? '不花 token：点「保存」后把提示词贴到聊天页，再把回答贴回来。'
        : '直接调用接口，按 token 计费；Key 只存在本机配置里。';
    }
    if (hint) wrap.appendChild(el('div', 'fhint', hint));
    if (f.warn) wrap.appendChild(el('div', 'fwarn', f.warn));
    return wrap;
  }

  function renderLibrary(sum) {
    if (!sum) return null;
    const box = el('div', 'fgroup');
    box.appendChild(el('div', 'fgroup-h', '配图库现状'));
    // 这一块以前是把 stats() 的原始字典直接摊开：14 个分类全列、连
    // 「繁花精选 / 长桥 / 黄昏」这种长尾标签也上，还带一个 (local) 内部标记。
    // 设置页不是调试面板 —— 只留「多少张、占多大、够不够用」三件事。
    const total = Number(sum.total || 0);
    const taggedRaw = sum.byTagged || {};
    const tagged = sum.tagged != null
      ? Number(sum.tagged)
      : Object.keys(taggedRaw).reduce((n, k) => n + Number(taggedRaw[k] || 0), 0);
    const lack = Math.max(0, total - tagged);
    box.appendChild(el('div', 'fhint',
      '共 ' + total + ' 张 · ' + (sum.mb || 0) + ' MB'
      + (total ? (lack ? '　·　' + lack + ' 张还没属性，轮换会先用官方简介兜底'
        : '　·　属性齐全') : '')));

    // 长尾合并成「其他」：Top N 之外的总数比逐个列出来有用
    const top = (obj, n) => {
      const kv = Object.entries(obj || {}).sort((a, b) => Number(b[1]) - Number(a[1]));
      if (!kv.length) return '';
      const head = kv.slice(0, n).map(p => p[0] + ' ' + p[1]);
      const rest = kv.slice(n).reduce((s, p) => s + Number(p[1] || 0), 0);
      if (rest) head.push('其他 ' + rest);
      return head.join(' · ');
    };
    // Top 5 不是随便定的：第 6 名开始就是「繁花精选 / 长桥 / 黄昏」这种
    // 非大类的长尾标签，和天象、地形并列会让人以为壁纸分成了十几种。
    const cat = top(sum.byCategory, 5);
    if (cat) box.appendChild(el('div', 'fhint', '分类：' + cat));
    const src = top(sum.bySource, 4);
    if (src) box.appendChild(el('div', 'fhint', '来源：' + src));
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
    // 完整路径里有 Windows 用户名，摆出来既长又没必要 —— 折成 %APPDATA%
    $('cfgPath').textContent = '配置文件：' + String(path || '')
      .replace(/^[A-Za-z]:\\Users\\[^\\]+\\AppData\\Roaming/i, '%APPDATA%')
      .replace(/^\/home\/[^/]+/i, '~');

    // 关于：版本 + 一句话说清它是什么。不放假链接 ——
    // 应用模式窗口点外链会把这个窗口导航走，整页就废了。
    const ab = $('aboutLine');
    if (ab) {
      const ver = (state && state.app && state.app.version) || '';
      ab.textContent = '工作台' + (ver ? ' ' + ver : '')
        + '　·　零依赖本地服务：数据全部在 %APPDATA%\\Workbench，不联网不上传，除了你自己配的资讯源与模型。';
    }
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
    } else if (act === 'fetchPhotos') {
      // 图库空着时首页那两个按钮之一。抓图是网络活，接口立刻返回、
      // 后台线程真去下，所以这里只提示一句，等轮询把新图带回来。
      btn.disabled = true;
      toast('正在抓图，十几秒后回来看看');
      try { await api('/api/photos', { method: 'POST', body: JSON.stringify({ pages: 3 }) }); schedulePoll(); }
      catch (e) { toast('抓图没发出去：' + e.message, 'warn'); }
      setTimeout(() => { btn.disabled = false; }, 4000);
    } else if (act === 'goDesk') {
      if (window.WB) window.WB.go('desk');
    } else if (act === 'goFeed') {
      if (window.WB) window.WB.go('feed');
    } else if (act === 'wallPeek') {
      openWall();
    } else if (act === 'closeWall') {
      $('wallModal').hidden = true;
    } else if (act === 'wallPrev') {
      stepPhoto(-1, true);
    } else if (act === 'wallNext') {
      stepPhoto(1, true);
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
    } else if (act === 'dBookmark') {
      const it = ((state && state.daily && state.daily.items) || [])[current];
      if (!it) return;
      const btn = $('dBookmark');
      toggleBookmark('news', it, null, (on) => {
        paintBmBtn(btn, on);
        // 列表里那颗旗子跟着变，不然抽屉说「已收藏」、列表还是空心
        const row = document.querySelector('.nitem[data-idx="' + current + '"]');
        const flag = row && row.querySelector('.nr-b');
        if (flag) {
          flag.textContent = on ? '⚑' : '⚐';
          flag.classList.toggle('on', on);
          flag.title = on ? '已收藏 · 点击取消' : '收藏（在「收藏」里看）';
        }
      });
    } else if (act === 'dPrev') {
      openDrawer(current > 0 ? current - 1 : (state.daily.items.length - 1));
    } else if (act === 'dNext') {
      openDrawer((current + 1) % state.daily.items.length);
    } else if (act === 'todoAdd') {
      addTodo();
    } else if (act === 'todoMini') {
      openMini();
    } else if (act === 'todoClearDone') {
      const done = (state && state.todos || []).filter(t => t.done).length;
      if (!done) { toast('没有已完成的项目'); return; }
      if (window.confirm('清掉 ' + done + ' 条已完成的待办？')) todoSend('clear_done');
    } else if (act === 'quit') {
      try { await api('/api/quit', { method: 'POST', body: '{}' }); } catch (e) { /* ignore */ }
      document.body.innerHTML =
        '<div style="display:grid;place-items:center;height:100vh;font:14px sans-serif;color:#999">' +
        '工作台已退出，关掉这个窗口即可。</div>';
    }
  });

  $('drawerMask').addEventListener('click', closeDrawer);

  const drScale = $('drScale');
  if (drScale) {
    drScale.addEventListener('click', (ev) => {
      const cell = ev.target.closest('.dr-i');
      if (!cell) return;
      const items = (state && state.daily && state.daily.items) || [];
      const it = items[current];
      if (it) submitRate(it, Number(cell.dataset.v), null);
    });
  }
  $('wallModal').addEventListener('click', (ev) => {
    if (ev.target === $('wallModal')) $('wallModal').hidden = true;   // 点空白处关
  });

  $('todoInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); addTodo(); }
  });

  /* 拖拽重排：快捷启动项和待办条目都能拖着换位置。
     监听挂在容器上，列表重渲染也不用重挂一遍。 */
  TodoUI.sortable($('launchers'), {
    item: '.launcher:not(.lac-add)',
    id: (el) => el.dataset.id,
    onEnd: (ids) => launcherReorder(ids),
  });
  TodoUI.sortable($('todos'), {
    item: '.todo',
    id: (el) => el.dataset.id,
    onEnd: (ids) => todoSend('reorder', { ids }),
  });

  // 第一次点任何地方：顺手把「系统通知」的授权问一遍、把声音解冻。
  // 没有用户手势时浏览器可能直接把授权弹窗吞掉，所以留到第一次点击。
  document.addEventListener('pointerdown', () => {
    TodoUI.armPermission();
    TodoUI.unlockAudio();
  }, { once: true });

  document.addEventListener('keydown', (ev) => {
    const tag = (ev.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea';

    if (ev.key === 'Escape') {
      if (!$('wallModal').hidden) { $('wallModal').hidden = true; return; }
      if (!$('pickModal').hidden) { $('pickModal').hidden = true; return; }
      if (!$('modal').hidden) { $('modal').hidden = true; return; }
      if ($('drawer').classList.contains('open')) { closeDrawer(); return; }
    }
    if (typing) return;

    // 翻页：← → 走遍所有页（页数和顺序由 pages.js 的注册表决定，
    // 这里不写死页面 id，加一页键盘也能翻到）。
    // 弹层开着时先别翻：设置面板后面换页会让人以为自己点错了；
    // 抽屉开着时 ← → 是「上一条 / 下一条」，更不能抢。
    const drawerOpen = $('drawer').classList.contains('open');
    const modalOpen = !$('modal').hidden || !$('pickModal').hidden || !$('wallModal').hidden;
    if (!modalOpen && !drawerOpen && window.WB) {
      if (ev.key === 'ArrowRight' && window.WB.next()) { ev.preventDefault(); return; }
      if (ev.key === 'ArrowLeft' && window.WB.prev()) { ev.preventDefault(); return; }
    }

    if (drawerOpen) {
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
  // 待办与提醒单独一条轻量轮询：不跟着 state 的 12 秒走，
  // 到点的提醒最多晚 8 秒 —— 也免得为了看一眼待办去重算壁纸和天气
  setInterval(pollTodos, 8000);

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
  bindFeedTabs();
  tick();
  setInterval(tick, 1000);
  refreshState().then(() => {
    if (state && state.status.generating) schedulePoll();
    if (state) fireReminders(state.reminders);   // 上次没接走的提醒，现在补上
    const auto = QS.get('open');                 // 预览/深链：直接打开第 N 条的二级页面
    if (auto !== null && state) openDrawer(parseInt(auto, 10) || 0);
    if (QS.get('pick') !== null) openPicker();   // 深链：直接打开「添加启动项」
    if (QS.get('settings') !== null) openSettings();   // 深链：直接打开设置
  });

})();
