/* 待办的共用零件：时间怎么念、选时弹层、系统通知、提示音。

   主窗（桌面上的大面板）和待办小窗（独立窗口）是两个页面，
   但这些事必须一模一样 —— 同一句话换个说法、最后一个字不同，
   用户会以为是两个功能。所以只写一份，两边都引这个文件。 */
(() => {
  'use strict';

  const HORIZON_MAX = 72;          // 兜底值；真实上限由 TodoUI.setCap() 从后端同步
  const pad = (n) => String(n).padStart(2, '0');

  // 上限（小时）与它对应的最晚时刻。两个窗口各自 setCap，弹层只用这一个来源，
  // 免得「主窗能约一年、小窗只能约三天」这种两边不一致。
  let cap = { hours: HORIZON_MAX, at: '' };

  function setCap(hours, atISO) {
    const h = Number(hours);
    if (Number.isFinite(h) && h > 0) cap.hours = h;
    if (atISO) cap.at = String(atISO);
  }

  function toLocal(iso) {
    if (!iso) return null;
    const d = new Date(String(iso));
    return isNaN(d.getTime()) ? null : d;
  }

  /** 2026-10-22T09:30 -> 09:30 */
  function clock(iso) {
    const d = toLocal(iso);
    return d ? pad(d.getHours()) + ':' + pad(d.getMinutes()) : '';
  }

  /** 人话版：12 分钟后 / 今天 21:30 / 明天 09:00 / 10月23日 09:00 */
  function label(iso, ref) {
    const d = toLocal(iso);
    if (!d) return '';
    const now = ref || new Date();
    const delta = (d - now) / 1000;
    if (delta < -60) return clock(iso) + '（已过）';
    if (delta < 60) return '就是现在';
    if (delta < 3600) return Math.floor(delta / 60) + ' 分钟后';
    const day = 86400000;
    const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((startOf(d) - startOf(now)) / day);
    const hm = clock(iso);
    if (days === 0) return '今天 ' + hm;
    if (days === 1) return '明天 ' + hm;
    if (days === 2) return '后天 ' + hm;
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm;
  }

  /** datetime-local 输入框要的值（本地时间，精确到分钟） */
  function inputValue(iso) {
    const d = toLocal(iso) || new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function horizonMax() {
    const d = new Date(Date.now() + cap.hours * 3600 * 1000);
    return inputValue(d.toISOString());
  }

  /** 上限的人话：讲日期，不讲「还剩几个小时」——10 年约到哪天，用户要看到日期 */
  function capText() {
    const d = toLocal(cap.at);
    if (!d) return '';
    const full = d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日';
    return '最多可约到 ' + full + ' ' + clock(cap.at);
  }

  /* ------------------------------------------------------------ 系统通知 */

  const KEEP = new Set();          // 通知对象一旦被回收，点击就再也到不了 onclick
  let audioReady = false;
  let actx = null;

  function permState() {
    if (typeof Notification === 'undefined') return 'unsupported';
    return Notification.permission;
  }

  async function askPerm() {
    if (typeof Notification === 'undefined') return 'unsupported';
    if (Notification.permission !== 'default') return Notification.permission;
    try {
      return await Notification.requestPermission();
    } catch (e) {
      return 'denied';
    }
  }

  /** 第一条点击里悄悄申请一次 —— 没有用户手势时浏览器可能直接把弹窗吞掉 */
  function armPermission() {
    if (permState() === 'default') askPerm();
  }

  function notify(opts) {
    const o = opts || {};
    if (permState() !== 'granted') return null;
    try {
      const n = new Notification(o.title || '待办提醒', {
        body: o.body || '',
        tag: o.tag || ('todo-' + Date.now()),
        requireInteraction: true,   // 横幅不自动消失：点得到才算数
        silent: true,               // 声音我们自己发，别叠两层
      });
      KEEP.add(n);
      const drop = () => KEEP.delete(n);
      n.onclose = drop;
      n.onerror = drop;
      n.onclick = () => {
        try { window.focus(); } catch (e) { /* 无窗口焦点就不管 */ }
        try { if (o.onclick) o.onclick(); } catch (e) { /* ignore */ }
        drop();
        try { n.close(); } catch (e) { /* ignore */ }
      };
      if (KEEP.size > 20) KEEP.delete(KEEP.values().next().value);
      return n;
    } catch (e) {
      return null;
    }
  }

  /* ------------------------------------------------------------ 拖拽重排 */

  /** 让一列条目可以拖着换位置。
   *
   *  opts.item   条目选择器（比如 '.todo' / '.launcher'）
   *  opts.id     从元素取 id 的函数
   *  opts.onEnd  拖完的回调：(ids) => {}，ids 是拖完后的顺序
   *
   *  做法：dragover 时把被拖的元素直接插到目标位置（所见即所得），
   *  松手时读一遍 DOM 顺序发回后端。不用第三方库。
   *  监听挂在容器上（不是每个条目上），所以重新渲染列表也不用重挂。
   */
  function sortable(container, opts) {
    if (!container || !opts) return;
    let dragEl = null;
    const itemOf = (t) => (t && t.closest) ? t.closest(opts.item) : null;

    container.addEventListener('dragstart', (ev) => {
      const it = itemOf(ev.target);
      if (!it) return;
      // 从条目里的小按钮上起手的不算拖动（那是点按钮，不是拖条目）
      if (ev.target.closest && ev.target.closest('.lac-tools, .todo-acts, .acts')) {
        ev.preventDefault();
        return;
      }
      dragEl = it;
      it.classList.add('dragging');
      try {
        ev.dataTransfer.effectAllowed = 'move';
        ev.dataTransfer.setData('text/plain', opts.id(it));
      } catch (e) { /* 某些环境没有 dataTransfer */ }
    });

    container.addEventListener('dragover', (ev) => {
      if (!dragEl) return;
      ev.preventDefault();
      try { ev.dataTransfer.dropEffect = 'move'; } catch (e) { /* ignore */ }
      const target = itemOf(ev.target);
      if (!target || target === dragEl) return;
      const r = target.getBoundingClientRect();
      const below = (ev.clientY - r.top) > r.height / 2;   // 落在下半 → 排它后面
      const ref = below ? target.nextSibling : target;
      if (ref !== dragEl) container.insertBefore(dragEl, ref);
    });

    container.addEventListener('drop', (ev) => {
      if (dragEl) ev.preventDefault();
    });

    container.addEventListener('dragend', () => {
      if (!dragEl) return;
      dragEl.classList.remove('dragging');
      dragEl = null;
      const ids = Array.from(container.querySelectorAll(opts.item)).map(opts.id);
      if (opts.onEnd) opts.onEnd(ids);
    });
  }

  /* -------------------------------------------------------------- 提示音 */

  /** 两声短音，现场合成 —— 不带音频文件，也不用等加载 */
  function chime() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      if (!actx) actx = new AC();
      if (actx.state === 'suspended') actx.resume();
      const t0 = actx.currentTime;
      [[880, 0], [1174.7, 0.16]].forEach(([hz, at]) => {
        const osc = actx.createOscillator();
        const gain = actx.createGain();
        osc.type = 'sine';
        osc.frequency.value = hz;
        gain.gain.setValueAtTime(0.0001, t0 + at);
        gain.gain.exponentialRampToValueAtTime(0.16, t0 + at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.30);
        osc.connect(gain).connect(actx.destination);
        osc.start(t0 + at);
        osc.stop(t0 + at + 0.34);
      });
      audioReady = true;
    } catch (e) { /* 没声音也不能耽误提醒 */ }
  }

  function unlockAudio() {
    if (audioReady) return;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      if (!actx) actx = new AC();
      if (actx.state === 'suspended') actx.resume();
      const osc = actx.createOscillator();
      const gain = actx.createGain();
      gain.gain.value = 0.00001;
      osc.connect(gain).connect(actx.destination);
      osc.start();
      osc.stop(actx.currentTime + 0.02);
      audioReady = true;
    } catch (e) { /* ignore */ }
  }

  /* --------------------------------------------------------- 选时弹层 */

  let opened = null;
  let _reflow = null;      // 开着的弹层的重新定位函数（窗口大小变了要重夹）

  function closePicker() {
    if (!opened) return;
    const { node, onKey, onDown } = opened;
    opened = null;
    _reflow = null;
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('mousedown', onDown, true);
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  /** 按当前视口重新夹一次弹层位置。窗口被动态改过大小之后要调它。 */
  function reflowPicker() {
    if (_reflow) _reflow();
  }

  const ICON = {
    drop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.6s6 6.4 6 10.1a6 6 0 0 1-12 0C6 10 12 3.6 12 3.6z"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 6-2.2 7-2.2 7h16.4S18 14 18 8z"/><path d="M10.3 20a2 2 0 0 0 3.4 0"/></svg>',
    bellOn: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 6-2.2 7-2.2 7h16.4S18 14 18 7z"/><path d="M10.3 20a2 2 0 0 0 3.4 0" fill="none"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.4"/><path d="M12 7.6V12l3 1.8"/></svg>',
    pencil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10.4-10.4a2.1 2.1 0 0 0-3-3L5 17z"/><path d="M13.6 5.4l3 3"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>',
    trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V4.8h6V7"/><path d="M6.6 7l.7 11.6a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9L17.4 7"/></svg>',
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M6 11l6-6 6 6"/></svg>',
    down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M6 13l6 6 6-6"/></svg>',
  };

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /* ------------------------------------------------------- 小时/分钟翻动轮 */

  /** 一列可以滚的列表，中间那条高亮带就是当前选中项。
   *
   *  为什么不用 <input type="time">：那个控件各机器长得不一样、不能滚，
   *  而且在 360 宽的小窗里经常被系统样式撑变形。自己画一列反而可控。
   *
   *  行高由 CSS 定（em），所以「等比例放大」是自动的 —— 弹层整体变大时，
   *  轮子、字号、间距按同一个比例走，不会出现「字大了间距没跟上」。
   */
  function wheel(label, count, fmt, initial, onChange) {
    const col = el('div', 'tui-wheel');
    col.appendChild(el('div', 'tui-wlab', label));

    const sc = el('div', 'tui-wscroll');
    sc.tabIndex = 0;
    sc.setAttribute('role', 'spinbutton');
    sc.setAttribute('aria-label', label);
    const inner = el('div', 'tui-winner');
    const items = [];
    for (let i = 0; i < count; i++) {
      const it = el('div', 'tui-witem', fmt(i));
      it.addEventListener('click', () => set(i, false));
      inner.appendChild(it);
      items.push(it);
    }
    sc.appendChild(inner);
    col.appendChild(sc);

    let itemPx = 0;
    let idx = Math.max(0, Math.min(count - 1, initial | 0));
    let raf = 0;

    const measure = () => { itemPx = items[0] ? items[0].offsetHeight : 0; };
    const paint = () => items.forEach((it, i) => it.classList.toggle('on', i === idx));

    function set(i, smooth) {
      i = Math.max(0, Math.min(count - 1, i));
      if (i === idx && itemPx) { paint(); return; }
      idx = i;
      if (!itemPx) measure();
      try { sc.scrollTo({ top: i * itemPx, behavior: smooth ? 'smooth' : 'auto' }); }
      catch (e) { sc.scrollTop = i * itemPx; }
      paint();
      onChange(i);
    }

    sc.addEventListener('scroll', () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (!itemPx) return;
        const i = Math.max(0, Math.min(count - 1, Math.round(sc.scrollTop / itemPx)));
        if (i !== idx) { idx = i; paint(); onChange(i); }
      });
    }, { passive: true });

    sc.addEventListener('keydown', (ev) => {
      if (ev.key === 'ArrowUp') { ev.preventDefault(); set(idx - 1, true); }
      else if (ev.key === 'ArrowDown') { ev.preventDefault(); set(idx + 1, true); }
    });

    // 字体晚到 / 窗口缩放都会改行高：量一次 + ResizeObserver 兜底，
    // 否则「选中的项」会偏离中间那条带子。
    const place = () => { measure(); sc.scrollTop = idx * itemPx; paint(); };
    requestAnimationFrame(place);
    setTimeout(place, 140);
    if (window.ResizeObserver && items[0]) {
      try { new ResizeObserver(place).observe(items[0]); } catch (e) { /* ignore */ }
    }
    return { el: col, set };
  }

  /**
   * 弹出「什么时候提醒我」。
   * anchor: 被点的按钮；cur: 现在的时间（可空）；
   * onPick(iso | null) —— null 表示取消定时。
   *
   * 界面只有「小时｜分钟翻动轮 + 今天/明天/后天」：按用户要求，
   * 「10 分钟后 / 30 分钟后」那批相对时间预设已全部撤掉。
   */
  function picker(anchor, cur, onPick) {
    closePicker();
    const box = document.createElement('div');
    box.className = 'tui-pop';
    const body = document.createElement('div');
    body.className = 'tui-body';

    // ---- 标题行：图标 + 一句话 + 关闭
    const head = document.createElement('div');
    head.className = 'tui-pop-h';
    const hIcon = document.createElement('span');
    hIcon.className = 'tui-hicon';
    hIcon.innerHTML = ICON.clock;
    const hText = document.createElement('span');
    hText.className = 'tui-htext';
    hText.textContent = cur ? '改个提醒时间' : '什么时候提醒你';
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'tui-x';
    x.innerHTML = ICON.x;
    x.title = '关掉';
    x.addEventListener('click', closePicker);
    head.append(hIcon, hText, x);
    body.appendChild(head);

    // 没有「10 分钟后 / 30 分钟后」那批快捷预设了（用户明确要求删掉）——
    // 定时只剩一条路：翻小时轮 + 翻分钟轮 + 选今天/明天/后天。
    const sep = document.createElement('div');
    sep.className = 'tui-sep';
    sep.appendChild(document.createElement('span')).textContent = '翻到几点';
    body.appendChild(sep);

    // ---- 翻动轮：小时一列、分钟一列
    const now = new Date();
    let start = toLocal(cur);
    if (!start) {
      start = new Date(now);
      start.setSeconds(0, 0);
      start.setMinutes(start.getMinutes() + (30 - start.getMinutes() % 30));   // 默认下一个整点/半点
    }
    const dayDiff = (d) => Math.round(
      (new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
        - new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()) / 86400000);

    let dayOffset = Math.max(0, dayDiff(start));
    let hour = start.getHours();
    let minute = start.getMinutes();
    const capAt = toLocal(cap.at);

    const wheels = document.createElement('div');
    wheels.className = 'tui-wheels';
    const wh = wheel('小时', 24, (i) => pad(i), hour, (i) => { hour = i; sync(); });
    const wm = wheel('分钟', 60, (i) => pad(i), minute, (i) => { minute = i; sync(); });
    const colon = el('div', 'tui-colon', ':');
    wheels.append(wh.el, colon, wm.el);
    body.appendChild(wheels);

    // ---- 日期档：今天 / 明天 / 后天。超过三天的走下面的「自定义时间」
    const days = document.createElement('div');
    days.className = 'tui-days';
    const chips = [];
    [['今天', 0], ['明天', 1], ['后天', 2]].forEach(([text, off]) => {
      const b = el('button', 'tui-chip tui-day', text);
      b.type = 'button';
      b.addEventListener('click', () => { dayOffset = off; sync(); });
      days.appendChild(b);
      chips.push({ off, b });
    });

    // ---- 结果预览：这一行是给「翻完了到底定在几点」一个确定答案
    const preview = document.createElement('div');
    preview.className = 'tui-preview';

    // ---- 自定义时间（给超过三天的远日期留的口子，默认收起）
    const more = el('button', 'tui-more', '自定义时间 ▾');
    more.type = 'button';
    const row = document.createElement('div');
    row.className = 'tui-row';
    row.hidden = true;
    const inp = document.createElement('input');
    inp.type = 'datetime-local';
    inp.step = '60';
    const go2 = document.createElement('button');
    go2.type = 'button';
    go2.className = 'tui-go';
    go2.textContent = '用这个';
    go2.addEventListener('click', () => {
      if (!inp.value) return;
      closePicker();
      onPick(inp.value);
    });
    inp.addEventListener('keydown', (ev) => {
      ev.stopPropagation();
      if (ev.key === 'Enter') { ev.preventDefault(); go2.click(); }
    });
    row.append(inp, go2);
    more.addEventListener('click', () => {
      row.hidden = !row.hidden;
      more.textContent = row.hidden ? '自定义时间 ▾' : '自定义时间 ▴';
      if (!row.hidden) { try { inp.focus({ preventScroll: true }); } catch (e) { inp.focus(); } }
      reflow();
    });

    // ---- 底部：上限说明 + 取消 + 定时
    const foot = document.createElement('div');
    foot.className = 'tui-foot';
    const hint = document.createElement('span');
    hint.className = 'tui-hint';
    const clr = document.createElement('button');
    clr.type = 'button';
    clr.className = 'tui-clr';
    clr.textContent = '取消定时';
    clr.addEventListener('click', () => { closePicker(); onPick(null); });
    const go = document.createElement('button');
    go.type = 'button';
    go.className = 'tui-go tui-ok';
    go.textContent = '定时';
    go.addEventListener('click', () => {
      const d = build();
      if (!d) return;
      closePicker();
      onPick(inputValue(d));
    });

    let over = false;
    function build() {
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + dayOffset,
                         hour, minute, 0, 0);
      // 翻到过去的时间没意义：顺延到明天（日期档跟着亮）
      if (d.getTime() - Date.now() < 60e3) {
        d.setDate(d.getDate() + 1);
        dayOffset = dayDiff(d);
      }
      over = !!(capAt && d.getTime() > capAt.getTime());
      return d;
    }

    function sync() {
      const d = build();
      chips.forEach((c) => c.b.classList.toggle('on', c.off === dayOffset));
      const iso = inputValue(d);
      preview.textContent = '';
      preview.appendChild(el('b', null, label(iso)));
      preview.appendChild(el('span', null,
        ' · ' + (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + clock(iso)));
      inp.value = iso;
      inp.min = inputValue(now);
      inp.max = horizonMax();
      go.disabled = over;
      hint.textContent = over ? '超出上限：' + capText() : (capText() || ('最多能约 ' + cap.hours + ' 小时内'));
      hint.classList.toggle('warn', over);
    }

    body.append(days, preview, more, row);
    // 顺序：上限说明在左，[取消定时] 与 [定时] 在右。没设过定时的就没有取消。
    foot.append(hint, clr, go);
    if (!cur) clr.remove();
    body.appendChild(foot);
    box.appendChild(body);
    document.body.appendChild(box);

    function reflow() {
      const r = anchor.getBoundingClientRect();
      const w = box.offsetWidth || 252;
      const h = box.offsetHeight || 300;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let left = Math.min(Math.max(8, r.left), Math.max(8, vw - w - 8));
      let top = r.bottom + 6;
      if (top + h > vh - 8) top = vh - h - 8;
      if (top < 8) top = 8;
      box.style.left = left + 'px';
      box.style.top = top + 'px';
    }
    reflow();
    requestAnimationFrame(reflow);
    setTimeout(reflow, 140);       // 字体/行高就位后再夹一次，免得算出错高度
    _reflow = reflow;              // 交给外面：小窗高度变过之后重新夹

    const onKey = (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); closePicker(); } };
    const onDown = (ev) => {
      if (opened && (opened.node.contains(ev.target) || anchor.contains(ev.target))) return;
      closePicker();
    };
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('mousedown', onDown, true);
    opened = { node: box, onKey, onDown };
    sync();
  }

  /* ------------------------------------------------------------ 莫奈色板 */

  /* 名字给眼睛看，hex 给界面用；后端只认 key（见 store.TODO_COLORS）。
     新加的待办默认莫奈绿，老条目在配置迁移时也统一落到 green。 */
  const PALETTE = [
    { key: 'green',  name: '莫奈绿', hex: '#A4E2B8' },
    { key: 'blue',   name: '睡莲蓝', hex: '#ACD7FC' },
    { key: 'yellow', name: '麦穗黄', hex: '#FFED7E' },
    { key: 'orange', name: '落日橙', hex: '#FFCD86' },
    { key: 'pink',   name: '玫瑰粉', hex: '#FEBFBF' },
  ];
  const COLOR_HEX = {};
  const COLOR_NAME = {};
  PALETTE.forEach((c) => { COLOR_HEX[c.key] = c.hex; COLOR_NAME[c.key] = c.name; });

  function colorOf(key) { return COLOR_HEX[key] || COLOR_HEX.green; }
  function colorName(key) { return COLOR_NAME[key] || COLOR_NAME.green; }

  /** 弹出色板。anchor: 被点的色点；cur: 当前色名；onPick(key)。 */
  function palette(anchor, cur, onPick) {
    closePicker();
    const box = document.createElement('div');
    box.className = 'tui-pop tui-pal';
    const body = document.createElement('div');
    body.className = 'tui-body';

    const head = document.createElement('div');
    head.className = 'tui-pop-h';
    const hIcon = document.createElement('span');
    hIcon.className = 'tui-hicon';
    hIcon.innerHTML = ICON.drop;
    const hText = document.createElement('span');
    hText.className = 'tui-htext';
    hText.textContent = '挑个颜色';
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'tui-x';
    x.innerHTML = ICON.x;
    x.title = '关掉';
    x.addEventListener('click', closePicker);
    head.append(hIcon, hText, x);
    body.appendChild(head);

    const sep = document.createElement('div');
    sep.className = 'tui-sep';
    sep.appendChild(document.createElement('span')).textContent = '莫奈色系';
    body.appendChild(sep);

    const grid = document.createElement('div');
    grid.className = 'tui-swatches';
    PALETTE.forEach((c) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'tui-sw' + (c.key === cur ? ' on' : '');
      b.style.setProperty('--sw', c.hex);
      b.title = c.name;
      const dot = document.createElement('span');
      dot.className = 'tui-swdot';
      b.append(dot, el('span', 'tui-swname', c.name));
      b.addEventListener('click', () => { closePicker(); onPick(c.key); });
      grid.appendChild(b);
    });
    body.appendChild(grid);
    box.appendChild(body);
    document.body.appendChild(box);

    const reflow = () => {
      const r = anchor.getBoundingClientRect();
      const w = box.offsetWidth || 330;
      const h = box.offsetHeight || 180;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const left = Math.min(Math.max(8, r.left), Math.max(8, vw - w - 8));
      let top = r.bottom + 6;
      if (top + h > vh - 8) top = r.top - h - 6;     // 下面放不下就翻到上面
      if (top < 8) top = 8;
      box.style.left = left + 'px';
      box.style.top = top + 'px';
    };
    reflow();
    requestAnimationFrame(reflow);
    _reflow = reflow;

    const onKey = (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); closePicker(); } };
    const onDown = (ev) => {
      if (opened && (opened.node.contains(ev.target) || anchor.contains(ev.target))) return;
      closePicker();
    };
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('mousedown', onDown, true);
    opened = { node: box, onKey, onDown };
  }

  window.TodoUI = {
    clock, label, inputValue, horizonMax, setCap, capText,
    picker, palette, PALETTE, colorOf, colorName,
    closePicker, reflowPicker, sortable,
    permState, askPerm, armPermission, notify, chime, unlockAudio,
    ICON, HORIZON_MAX,
  };
})();
