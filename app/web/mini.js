/* 待办小窗（独立窗口）。

   只做四件事：显示、加、勾、设提醒。
   所有规则都在后端（可约上限、到点判定），这里不做第二份实现。 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  let data = { todos: [], ring: [], stats: {}, mini: {} };
  let pendingRemind = '';        // 「加一条顺带提醒」的时间
  let ringing = 0;               // 正在显示的那条提醒
  let editing = null;

  const SHOWN_KEY = 'mini.reminder.shown';
  let shown = [];
  try { shown = JSON.parse(localStorage.getItem(SHOWN_KEY) || '[]'); } catch (e) { shown = []; }

  /* 还在桌面上「挂着」的提醒。
     为什么不直接用后端的 ring：页面一弹完就把它 ack 掉了（不然两个窗口会各弹一次），
     所以后端的队列是空的。但提醒条不能跟着消失 —— 没点「知道了」它就一直在，
     关掉窗口再打开也还在（存 localStorage）。 */
  const ACTIVE_KEY = 'mini.reminder.active';
  let active = [];
  try { active = JSON.parse(localStorage.getItem(ACTIVE_KEY) || '[]'); } catch (e) { active = []; }

  function saveActive() {
    try { localStorage.setItem(ACTIVE_KEY, JSON.stringify(active)); } catch (e) { /* 隐私模式 */ }
  }

  /* ------------------------------------------------------------ 小工具 */

  function toast(msg, kind) {
    const t = el('div', 'toast' + (kind ? ' ' + kind : ''), msg);
    $('toastWrap').appendChild(t);
    setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 3000);
  }

  async function api(path, payload) {
    const res = await fetch(path, {
      method: payload ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: payload ? JSON.stringify(payload) : undefined,
    });
    let out = {};
    try { out = await res.json(); } catch (e) { out = {}; }
    if (!res.ok && !out.message) out.message = 'HTTP ' + res.status;
    return out;
  }

  /* -------------------------------------------------------------- 渲染 */

  function order(list) {
    // 没做的在上（保持原顺序），做完的沉到底 —— 勾掉一条不会让别的条目乱跳
    const left = list.filter((t) => !t.done);
    const done = list.filter((t) => t.done);
    return left.concat(done);
  }

  function whenChip(t) {
    if (!t.remindAt) return null;
    const at = new Date(t.remindAt);
    const soon = t.reminded ? (Date.now() - at.getTime() < 3600e3 * 12) : (at - Date.now() < 3600e3 * 3);
    const chip = el('div', 'when' + (t.reminded ? ' overdue' : (soon ? ' soon' : '')));
    chip.innerHTML = TodoUI.ICON.bell;
    const text = t.reminded
      ? (t.done ? '提醒过 · 已完成' : '提醒过')
      : TodoUI.label(t.remindAt);
    chip.appendChild(el('span', null, text));
    chip.title = t.remindAt.replace('T', ' ');
    return chip;
  }

  function row(t) {
    const r = el('div', 'row' + (t.done ? ' done' : '') + (ringing && ringing.id === t.id ? ' ringing' : ''));
    r.dataset.id = t.id;
    r.draggable = true;                 // 可以拖着换位置
    r.style.setProperty('--todo-c', TodoUI.colorOf(t.color));

    const cb = el('input', 'cb');
    cb.type = 'checkbox';
    cb.checked = !!t.done;
    cb.title = t.done ? '标为未完成' : '打勾完成';
    cb.addEventListener('change', () => send('toggle', { id: t.id }));

    const body = el('div', 'body');
    const text = el('div', 't', t.text);
    text.title = '双击改内容';
    text.addEventListener('dblclick', () => editText(t, text));
    body.appendChild(text);
    const chip = whenChip(t);
    if (chip) body.appendChild(chip);

    const acts = el('div', 'acts');
    const color = el('button', 'ibtn cbtn');
    color.type = 'button';
    color.appendChild(el('span', 'cdot'));
    color.title = '颜色：' + TodoUI.colorName(t.color) + '（点一下换）';
    color.addEventListener('click', (ev) => {
      ev.stopPropagation();
      TodoUI.palette(color, t.color, (key) => send('set_color', { id: t.id, color: key }));
    });
    const bell = el('button', 'ibtn' + (t.remindAt ? ' armed' : ''));
    bell.type = 'button';
    bell.innerHTML = t.remindAt ? TodoUI.ICON.bellOn : TodoUI.ICON.bell;
    bell.title = t.remindAt ? '改提醒时间' : '设个提醒';
    bell.addEventListener('click', (ev) => {
      ev.stopPropagation();
      TodoUI.picker(bell, t.remindAt, (at) => send('set_remind', { id: t.id, remindAt: at || '' }));
    });
    const pen = el('button', 'ibtn pen');
    pen.type = 'button';
    pen.innerHTML = TodoUI.ICON.pencil;
    pen.title = '改名';
    pen.addEventListener('click', (ev) => {
      ev.stopPropagation();
      editText(t, text);
    });
    const del = el('button', 'ibtn del');
    del.type = 'button';
    del.innerHTML = TodoUI.ICON.x;
    del.title = '删掉';
    del.addEventListener('click', () => send('remove', { id: t.id }));
    acts.append(color, bell, pen, del);

    r.append(cb, body, acts);
    return r;
  }

  function render() {
    const list = $('list');
    list.textContent = '';
    // 提醒条只认「还没被处理掉」的那几条：对应的待办被删了 / 勾掉了就自动撤下
    const byId = {};
    (data.todos || []).forEach((t) => { byId[t.id] = t; });
    active = active.filter((r) => {
      const t = byId[r.id];
      if (!t || t.done) return false;
      r.text = t.text;                 // 用户改过文字，提醒条也跟着改
      return true;
    });
    saveActive();
    ringing = active[0] ? byId[active[0].id] : 0;

    const items = order(data.todos || []);
    if (!items.length) {
      const empty = el('div', 'empty');
      empty.innerHTML = '还没有待办。<br>下面敲一条，回车记下。';
      list.appendChild(empty);
    } else {
      items.forEach((t) => list.appendChild(row(t)));
    }

    const st = data.stats || {};
    // 「3 / 3」在 380px 的小窗里会被读成「三条全做完」，其实左边是未完成数。
    // 这里只报未完成（标题已经写着「待办」），完成数交给下面那句。
    const left = st.left != null ? st.left : 0;
    $('count').textContent = String(left);
    $('count').title = '未完成 ' + left + ' 条，共 ' + (st.total || 0) + ' 条';
    const bits = ['共 ' + (st.total || 0) + ' 条', '未完成 ' + left];
    if (st.armed) bits.push(st.armed + ' 个提醒在路上');
    $('footNote').textContent = bits.join(' · ');

    renderRing();
    renderPin();
    permNote();
    // 内容变了就可能变高/变矮：提醒条冒出来、勾掉一条、删掉一条……
    reportSize();
  }

  function renderRing() {
    const box = $('ring');
    const r = active[0];
    box.textContent = '';
    if (!r) { box.hidden = true; return; }
    box.hidden = false;

    const h = el('div', 'ring-h');
    h.appendChild(el('span', null, r.late ? '补个提醒' : '到点了'));
    h.appendChild(el('span', null, '· ' + TodoUI.clock(r.at)));
    if (active.length > 1) h.appendChild(el('span', null, '（另有 ' + (active.length - 1) + ' 条）'));
    box.appendChild(h);
    box.appendChild(el('div', 'ring-t', r.text));

    const btns = el('div', 'ring-b');
    const mk = (label, solid, fn) => {
      const b = el('button', solid ? 'solid' : '', label);
      b.addEventListener('click', fn);
      return b;
    };
    btns.appendChild(mk('完成了', true, () => {
      send('toggle', { id: r.id }).then(() => drop(r.id));
    }));
    btns.appendChild(mk('10 分钟后再提', false, () => {
      send('snooze', { id: r.id, minutes: 10 }).then(() => drop(r.id));
    }));
    btns.appendChild(mk('知道了', false, () => drop(r.id)));
    box.appendChild(btns);
  }

  function drop(id) {
    active = active.filter((r) => r.id !== id);
    saveActive();
    render();
  }

  function renderPin() {
    const on = !!(data.mini && data.mini.pinned);
    $('pinBtn').classList.toggle('on', on);
    $('pinBtn').title = on ? '图钉：已钉在最上层（点一下解开）' : '图钉：钉在所有窗口之上';
  }

  function permNote() {
    const btn = $('permBtn');
    const st = TodoUI.permState();
    if (st === 'granted') { btn.hidden = true; return; }
    btn.hidden = false;
    btn.textContent = st === 'denied' ? '系统通知被拒绝了' : '开通系统通知';
    btn.title = st === 'denied'
      ? '浏览器里点了「不允许」。到窗口右上角 … → 权限 → 通知 里改回来'
      : '点一下授权：到点会弹 Windows 右下角通知';
  }

  /* ---------------------------------------------------------- 提醒弹窗 */

  function fireReminders() {
    // 提醒窗口的标题一个字都别动 —— 后端按标题「待办小窗」找句柄，
    // 判断窗口还在不在（前面加个圆点就会被当成窗口已关闭）。
    const list = (data.ring || []).slice();
    list.forEach((r) => {
      const key = r.id + '|' + r.at;
      // 先挂到提醒条上：这张条子要一直留着，直到用户点「完成 / 稍后 / 知道了」
      if (!active.some((a) => a.id === r.id)) {
        active.push({ id: r.id, text: r.text, at: r.at, late: !!r.late });
        saveActive();
      }
      if (shown.indexOf(key) >= 0) { ack(r.id); return; }
      shown.push(key);
      if (shown.length > 80) shown = shown.slice(-80);
      try { localStorage.setItem(SHOWN_KEY, JSON.stringify(shown)); } catch (e) { /* 隐私模式 */ }

      if (data.toast !== false) {
        TodoUI.notify({
          title: r.late ? '待办提醒（补发）' : '待办提醒',
          body: r.text + '\n' + TodoUI.clock(r.at) + ' · 到点了',
          tag: 'wb-todo-' + r.id + '-' + Date.now(),
          onclick: () => {
            fetch('/api/window', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"action":"open"}' }).catch(() => {});
          },
        });
      }
      if (data.sound !== false) TodoUI.chime();
      // 收账：主窗如果也开着，就不会再弹第二次（但小窗里的提醒条还在）
      ack(r.id);
    });
    if (list.length) render();
  }

  async function ack(id) {
    try { await api('/api/todos', { action: 'reminder_ack', id }); } catch (e) { /* 下次再收 */ }
  }

  /* ------------------------------------------------------------ 动作 */

  async function send(action, payload) {
    const out = await api('/api/todos', Object.assign({ action }, payload || {}));
    if (out.ok === false && out.message) { toast(out.message, 'warn'); return out; }
    if (out.todos) apply(out);
    if (out.message && action !== 'reminder_ack') toast(out.message);
    return out;
  }

  function apply(out) {
    data = Object.assign(data, out);
    if (out.theme) document.documentElement.setAttribute('data-theme', out.theme);
    // 上限跟着后端走，两个窗口用同一份数字
    if (TodoUI.setCap) TodoUI.setCap(data.capHours || data.horizonHours, data.capAt);
    // 列表最高能长多高。后端按工作区算的 px —— 这里不能用 vh，
    // 因为窗口高度本身由内容决定，用 vh 会自己参考自己。
    if (out.maxListPx) {
      document.documentElement.style.setProperty('--list-max', out.maxListPx + 'px');
    }
    render();
    fireReminders();
  }

  /* ------------------------------------- 窗口高度跟着内容走（动态自适应） */

  /*  页面不写死高度（mini.css 里 html/body/.wrap 都是 auto），所以量出来的
      内容高度就是窗口该有的高度；报给后端，由它调 SetWindowPos。
      为什么不在前端硬撑一个高度：窗口归 Windows 管，只有后端能改它。

      两条护栏：
      1. 防抖 220ms —— 一次渲染可能连着改好几处，别请求刷屏；
      2. `autoFit === false` 时整个停掉 —— 用户手动拖过大小，从此听他的
         （keeper 发现大小不是我们设的，会把 auto 关掉并写进配置）。      */
  let sizeTimer = 0;
  let lastSize = '';

  function reportSize(force) {
    if (data.autoFit === false) return;
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(async () => {
      const wrap = document.querySelector('.wrap');
      // 逐块量：列表用 scrollHeight（被窗口夹住时它仍返回内容的完整高度），
      // 其余用 offsetHeight。以前量 body.scrollHeight —— `.wrap` 是 100vh，
      // 量出来永远等于当前窗高，于是窗口既不长也不缩，底部那条脚注被切掉一截。
      let h0 = 0;
      if (wrap) {
        ['.bar', '.ring', '.list', '.add', '.foot'].forEach((sel) => {
          const node = wrap.querySelector(sel);
          if (!node) return;
          h0 += (sel === '.list') ? node.scrollHeight : node.offsetHeight;
        });
      }
      let h = Math.ceil(Math.max(Math.min(h0, 900), 120));
      h0 = Math.ceil(Math.min(h0, 900));   // 不带弹层的自然高度（超过 900 就让列表自己滚）
      // 窗口高度 = 内容高度 + 标题栏。后端 set_size 收的是**窗口**高度，
      // 少算这一截，最后一条（脚注）就会被窗口底边吃掉 —— 差值要现量，
      // 别硬编码：不同 DPI、不同主题的边框差得不少。
      const chrome = Math.max(0, (window.outerHeight || 0) - (window.innerHeight || 0));
      h += chrome;
      h0 += chrome;
      // 选时弹层是 position:fixed，不算在文档高度里 —— 不补这一条，
      // 待办很少时窗口只有一百多像素高，弹层会被窗口底边切掉。
      // 要量 .tui-body 的 scrollHeight：弹层自己有 max-height（会被当前窗高夹住），
      // 量它得到的是「被夹后的高度」，永远长不到位；内层没有 max-height，量到的才是自然高度。
      const pop = document.querySelector('.tui-pop');
      if (pop) {
        const inner = pop.querySelector('.tui-body');
        const nat = inner ? inner.scrollHeight : pop.scrollHeight;
        h = Math.max(h, Math.ceil(nat) + 20 + chrome);
      }
      // 只报高度。宽度不报：文字一换行宽度就变，报了会越缩越小
      // （实测 360 → 318，因为 scrollWidth 量到的是内容而非窗口）。
      const key = 'h' + h + 'n' + h0;
      if (!force && key === lastSize) return;
      lastSize = key;
      try {
        // h 是「现在该多高」（含弹层），h0 是「去掉弹层后的自然高度」——
        // 后者存进配置，下次开窗直接按它开，不用先开大再缩小。
        await api('/api/mini', { action: 'size', h, h0 });
        // 窗口变高之后视口也变了，弹层得按新视口重新夹一次
        if (pop && TodoUI.reflowPicker) setTimeout(() => TodoUI.reflowPicker(), 150);
      } catch (e) { /* 服务不在就等下一轮轮询 */ }
    }, 220);
  }

  function editText(t, node) {
    if (editing) return;
    editing = t.id;
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
      editing = null;
      node.contentEditable = 'false';
      const text = (node.textContent || '').trim();
      if (save && text && text !== t.text) await send('edit', { id: t.id, text });
      else render();
    };
    node.addEventListener('keydown', (ev) => {
      ev.stopPropagation();
      if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
      if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
    });
    node.addEventListener('blur', () => finish(true));
  }

  /* ------------------------------------------------------------ 绑定 */

  /* 待办条目可以拖着换位置（跟主窗同一套排序助手） */
  TodoUI.sortable($('list'), {
    item: '.row',
    id: (el) => el.dataset.id,
    onEnd: (ids) => send('reorder', { ids }),
  });

  $('addForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const inp = $('addInput');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    const keep = pendingRemind;
    pendingRemind = '';
    $('addBell').classList.remove('armed');
    const out = await send('add', { text, remindAt: keep || undefined });
    if (out && out.ok === false) { inp.value = text; return; }
    inp.focus();
  });

  $('addBell').addEventListener('click', (ev) => {
    ev.preventDefault();
    TodoUI.picker($('addBell'), pendingRemind, (at) => {
      pendingRemind = at || '';
      $('addBell').classList.toggle('armed', !!pendingRemind);
      $('addInput').focus();
      if (pendingRemind) toast('记下后就 ' + TodoUI.label(pendingRemind) + ' 提醒你');
    });
  });

  $('pinBtn').addEventListener('click', async () => {
    const out = await api('/api/mini', { action: 'toggle_pin' });
    if (out.mini) data.mini = out.mini;
    renderPin();
    if (out.message) toast(out.message);
  });

  $('mainBtn').addEventListener('click', () => {
    api('/api/window', { action: 'open' }).catch(() => {});
  });

  $('closeBtn').addEventListener('click', () => {
    api('/api/mini', { action: 'close' }).catch(() => {});
    setTimeout(() => window.close(), 260);   // 后端关不掉时（比如被别的实例接管）自己关
  });

  $('permBtn').addEventListener('click', async () => {
    const st = await TodoUI.askPerm();
    permNote();
    if (st === 'granted') toast('好，到点会弹系统通知');
    else if (st === 'denied') toast('被拒绝了：窗口右上角 … → 权限 → 通知 里允许', 'warn');
    else toast('没拿到授权，先用窗口里的提醒条', 'warn');
  });

  // 第一次点任何地方都顺手把系统通知授权问一遍，声音也顺便解锁
  document.addEventListener('pointerdown', () => {
    TodoUI.armPermission();
    TodoUI.unlockAudio();
  }, { once: true });

  /* ------------------------------------------------------------ 轮询 */

  async function poll() {
    try {
      const out = await api('/api/todos');
      if (!out || out.ok === false) return;
      apply(out);
    } catch (e) { /* 服务没了就等它回来 */ }
  }

  async function boot() {
    const theme = new URLSearchParams(location.search).get('theme');
    if (theme) document.documentElement.setAttribute('data-theme', theme);
    await poll();
    $('addInput').focus();
    // 小窗开着就是为了「到点提醒」，所以一进来就把系统通知的授权问一次；
    // 浏览器可能会把没有手势的请求直接吞掉，那就留到第一次点击再问（见下面）。
    if (TodoUI.permState() === 'default') {
      try { await TodoUI.askPerm(); } catch (e) { /* ignore */ }
      permNote();
    }
    reportSize(true);          // 开窗第一帧就把高度对齐，别让用户看着一扇空窗
  }

  boot();
  setInterval(poll, 4000);     // 小窗是「一直看着」的那个，轮询快一点

})();
