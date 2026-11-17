/* ==========================================================================
   工作台 · 分页
   --------------------------------------------------------------------------
   以前主窗只有一屏：壁纸 + 三列面板全挤在一起，看风景和看信息互相打架。
   现在拆成「页」——首页只留风景（壁纸 + 时间天气），面板整体搬到「工作台」页，
   右侧一根竖排短横线当页签。

   这个文件是**分页的全部逻辑**，也是「加一页」的三个入口：

     ① 只想加一页静态内容（最常见）
        直接在 index.html 的 <div id="pages"> 里追一段：
            <section class="page" data-page="mypage"
                     data-label="我的页" data-hint="悬停时显示这句">
              …任意 HTML，照抄 .card.glass 就能和现有面板长一样…
            </section>
        导航条上自动多一条短横线，不用动一行 JS。

     ② 页面要用实时数据，或者有进入/离开时要收尾的东西
        照抄本文件末尾那段被注释掉的 WB.page 示例：
            WB.page({ id, label, hint, html, render(root, state), onEnter, onLeave })
        render 只在「这页可见」时被调用，切走就停，不用自己判断可见性。

     ③ 只想改某一页长什么样
        用 body[data-page="<id>"] 写 CSS 特例，不用碰 JS：
            body[data-page="home"] .topbar { … }

   三条约定（后来者只需要记这些）：
     · 同一时刻只有一页可见，靠 .page.is-active 控制，页面全部常驻 DOM
       —— 不销毁重建，所以原面板里那些 $("#id") 引用不会因为切页变成 null；
     · body 上始终有 data-page="<当前页 id>"；
     · 页签顺序 = DOM 顺序 = 注册顺序，想调顺序就调 DOM 顺序。
     · data-surface="photo|flat" 决定这页有没有壁纸：photo 是白字压照片，
       flat 是主题色纯底 + 深色字。不写默认 photo。
   ========================================================================== */
(() => {
  'use strict';

  const WB = (window.WB = window.WB || {});
  /* 记住停在哪页 —— 用 sessionStorage 而不是 localStorage，是故意的：
     刷新页面（F5、断线自愈的 reload）不该把人甩回首页，但**重新打开工作台
     应该从风景页开始**，这是一进来就想看到的东西。会话级存储正好卡在这条线上。 */
  const STORE_KEY = 'wb-page';

  const pages = [];                 // [{ id, label, hint, html, render, ... root }]
  const subs = [];                  // state 订阅者
  let currentId = '';

  const byId = (id) => pages.find((p) => p.id === id);
  const box = () => document.getElementById('pages');
  const rail = () => document.getElementById('rail');

  function fail(what, err) {
    // 一页写错不该把整个工作台带下水：吞掉异常，只留控制台线索
    console.error('[WB] ' + what + ' 出错：', err);
  }
  function safe(fn, what) {
    try { return fn(); } catch (e) { fail(what, e); return undefined; }
  }

  /* ------------------------------------------------------------ 建页 */

  /* 把登记过的页真正插进 DOM。刻意做成「按需」，这样在 boot 之前
     调用 WB.page 也不会因为 #pages 还没解析到而炸掉。 */
  function mount(rec) {
    if (rec.root) return rec.root;
    const parent = box();
    if (!parent) return null;
    const root = document.createElement('section');
    root.className = 'page';
    root.dataset.page = rec.id;
    if (rec.html) root.innerHTML = rec.html;
    parent.appendChild(root);
    rec.root = root;
    return root;
  }

  /* index.html 里手写的页：认领它们，不重建 */
  function adoptHandwritten() {
    const parent = box();
    if (!parent) return;
    parent.querySelectorAll(':scope > .page[data-page]').forEach((root) => {
      const id = root.dataset.page;
      if (!id || byId(id)) return;
      pages.push({
        id,
        label: root.dataset.label || id,
        hint: root.dataset.hint || '',
        wheel: root.dataset.wheel || '',
        surface: root.dataset.surface || 'photo',
        html: '',
        render: null, onEnter: null, onLeave: null,
        root,
      });
    });
  }

  /* ------------------------------------------------------------ 页签 */

  function renderRail() {
    const nav = rail();
    if (!nav) return;
    nav.textContent = '';
    nav.hidden = pages.length < 2;      // 只有一页的时候这根线没意义

    pages.forEach((rec) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'rail-btn';
      btn.dataset.page = rec.id;
      btn.setAttribute('aria-label', rec.label);
      if (rec.hint) btn.title = rec.hint;

      const label = document.createElement('span');
      label.className = 'rail-label';
      label.textContent = rec.label;

      const tick = document.createElement('span');
      tick.className = 'rail-tick';

      btn.append(label, tick);
      btn.addEventListener('click', () => go(rec.id));
      nav.appendChild(btn);
    });
    syncRail();
  }

  function syncRail() {
    const nav = rail();
    if (!nav) return;
    nav.querySelectorAll('.rail-btn').forEach((btn) => {
      const on = btn.dataset.page === currentId;
      btn.classList.toggle('on', on);
      if (on) btn.setAttribute('aria-current', 'page');
      else btn.removeAttribute('aria-current');
    });
  }

  /* ------------------------------------------------------------ 切换 */

  function paint(rec) {
    if (!rec || !rec.root || typeof rec.render !== 'function') return;
    safe(() => rec.render(rec.root, WB.state), '页面「' + rec.id + '」渲染');
  }

  function go(id, opts) {
    const o = opts || {};
    const next = byId(String(id));
    if (!next) { console.warn('[WB] 没有这一页：' + id); return; }
    if (!mount(next)) return;

    const prev = byId(currentId);
    if (prev === next && !o.force) return;

    if (prev) {
      prev.root.classList.remove('is-active');
      prev.root.setAttribute('aria-hidden', 'true');
      // 离开时把这一页自己的定时器/订阅收掉
      if (typeof prev.onLeave === 'function') safe(() => prev.onLeave(prev.root), prev.id + ' onLeave');
    }

    currentId = next.id;
    // 全量扫一遍而不是只摘 prev：index.html 里首页是带 is-active 写死的
    // （免得 JS 还没跑起来时先白屏一下），首次进入别的页时它得被摘掉。
    pages.forEach((p) => {
      if (!p.root || p === next) return;
      p.root.classList.remove('is-active');
      p.root.setAttribute('aria-hidden', 'true');
    });
    next.root.classList.add('is-active');
    next.root.removeAttribute('aria-hidden');
    document.body.dataset.page = next.id;
    /* 底面是「这页有没有照片」，写进 body 让 CSS 统一接管：
       photo = 壁纸层可见、字发白；flat = 壁纸退场、换成主题纯色底。
       新页在 HTML 上写 data-surface="flat" 即可，不用改这里。 */
    document.body.dataset.surface = next.surface || 'photo';

    syncRail();
    try {
      sessionStorage.setItem(STORE_KEY, next.id);
      // 底面也存一份：刷新时首页那段内联脚本要拿它，否则会先闪一帧壁纸
      sessionStorage.setItem(STORE_KEY + '-surface', next.surface || 'photo');
    } catch (e) { /* 隐私模式，忽略 */ }

    if (typeof next.onEnter === 'function') safe(() => next.onEnter(next.root), next.id + ' onEnter');
    paint(next);                        // 切进来的瞬间就用现有数据画一次，不等下一次轮询

    document.dispatchEvent(new CustomEvent('wb:page', {
      detail: { id: next.id, prev: prev ? prev.id : '' },
    }));
  }

  /* ------------------------------------------------------------- API */

  /* 注册一页。html 可以是一段字符串，也可以省略（表示这页在 index.html 里已经写好了）。
     render(root, state) 只在这页可见时被调用；onEnter/onLeave 用来开关定时器。 */
  WB.page = function (def) {
    if (!def || !def.id) throw new Error('[WB] page() 需要 id');
    const exist = byId(def.id);
    if (exist) {
      // 页面在 index.html 里写好了，逻辑可能放在别的文件里 —— 这时用
      // WB.page({id, render}) 补挂钩子，不重复建 DOM。
      // 原来这里直接 return，导致「先声明再注册」的写法挂不上钩子。
      if (typeof def.render === 'function') exist.render = def.render;
      if (typeof def.onEnter === 'function') exist.onEnter = def.onEnter;
      if (typeof def.onLeave === 'function') exist.onLeave = def.onLeave;
      if (def.surface) exist.surface = def.surface;
      return exist;
    }

    const rec = {
      id: String(def.id),
      label: def.label || String(def.id),
      hint: def.hint || '',
      wheel: def.wheel || '',
      surface: def.surface || 'photo',
      html: def.html || '',
      render: typeof def.render === 'function' ? def.render : null,
      onEnter: typeof def.onEnter === 'function' ? def.onEnter : null,
      onLeave: typeof def.onLeave === 'function' ? def.onLeave : null,
      root: null,
    };
    pages.push(rec);
    if (box()) { mount(rec); renderRail(); }
    return rec;
  };

  WB.go = go;
  WB.next = () => {
    const i = pages.findIndex((p) => p.id === currentId);
    if (i < 0 || i + 1 >= pages.length) return false;
    go(pages[i + 1].id);
    return true;
  };
  WB.prev = () => {
    const i = pages.findIndex((p) => p.id === currentId);
    if (i <= 0) return false;
    go(pages[i - 1].id);
    return true;
  };
  WB.pages = () => pages.map((p) => p.id);
  WB.home = () => (pages[0] && pages[0].id) || '';

  Object.defineProperty(WB, 'current', { get: () => currentId });

  /* app.js 每画完一遍就调这里一次：把最新 state 发给订阅者，
     并且只重画「当前可见」的那页 —— 藏着的页等切进来时再画。 */
  WB.publish = function (state) {
    WB.state = state;
    subs.forEach((fn) => safe(() => fn(state), 'onState 订阅'));
    const cur = byId(currentId);
    if (cur) paint(cur);
  };

  /* 订阅 state（新增页想自己取数据时用）。返回一个退订函数。 */
  WB.onState = function (fn) {
    if (typeof fn !== 'function') return () => {};
    subs.push(fn);
    if (WB.state) safe(() => fn(WB.state), 'onState 首次调用');
    return () => {
      const i = subs.indexOf(fn);
      if (i >= 0) subs.splice(i, 1);
    };
  };

  /* ------------------------------------------------- 滚动翻页（按页开启） */

  /* 只有标了 data-wheel="next" 的页才吃滚轮 —— 工作台页有列表要滚，
     绝不能让它顺手把页翻走。首页很适合：往下滚一下就进工作台。 */
  let lastWheel = 0;
  document.addEventListener('wheel', (ev) => {
    const rec = byId(currentId);
    if (!rec || rec.wheel !== 'next') return;
    if (ev.deltaY < 36) return;                    // 只认「往下滚」，往回滚不动页
    // 有弹层（抽屉 / 设置 / 选文件）压在上面时，滚轮是给它用的
    if (document.querySelector('.drawer.open, .modal:not([hidden])')) return;
    const now = Date.now();
    if (now - lastWheel < 900) return;             // 触控板一次滑动会连发几十个事件
    if (WB.next()) lastWheel = now;
  }, { passive: true });

  /* ------------------------------------------------------------ 启动 */

  adoptHandwritten();
  renderRail();

  (function boot() {
    // 深链优先（?page=desk，方便截图和调试），其次记住上次，最后回首页
    const want = new URLSearchParams(location.search).get('page');
    let id = want || '';
    if (!id) { try { id = sessionStorage.getItem(STORE_KEY) || ''; } catch (e) { id = ''; } }
    if (!byId(id)) id = WB.home();
    if (id) go(id, { force: true });
  })();

  /* ==========================================================================
     示例：新增一页（把下面这段的注释去掉就是可用的第三页）
     —— 页面用的是和现有面板一样的玻璃卡片，所以视觉上是同一个人做的。

     WB.page({
       id: 'stats',
       label: '统计',
       hint: '今年看了多少张图',
       html: `
         <div class="page-pad">
           <div class="card glass">
             <div class="card-head"><span>配图库</span></div>
             <div class="stat" id="statNum">—</div>
           </div>
         </div>`,
       render(root, s) {
         const n = root.querySelector('#statNum');
         if (n && s && s.photos) n.textContent = s.photos.total + ' 张';
       },
       onEnter(root) { console.log('进来了', root); },
       onLeave(root) { console.log('走了', root); },
     });
     ========================================================================== */
})();
