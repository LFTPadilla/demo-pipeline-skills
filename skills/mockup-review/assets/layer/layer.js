(function () {
  'use strict';
  // ---- Config and storage ----
  const CFG = (() => { try { return JSON.parse(document.getElementById('cmt-config').textContent); } catch (e) { return {}; } })();
  const PROJECT = CFG.project || 'prototype', VERSION = CFG.version || 'v1', TITLE = CFG.title || document.title || PROJECT;
  const FRAME = !!CFG.frame, API = CFG.apiBase || './api/comments';   // frame mode: the app under review runs in an iframe of the same origin
  const KEY = 'cmt:' + PROJECT + ':v2', HKEY = 'cmt:' + PROJECT + ':hw', PKEY = 'cmt:' + PROJECT + ':prefs', QKEY = 'cmt:' + PROJECT + ':q';
  const RKEY = new URLSearchParams(location.search).get('k') || '';
  const LIM = { text: 4000, author: 80, where: 200, path: 1000, reactors: 50 };
  const REACTIONS = ['👍', '✅', '👀', '❤️', '🎉', '❓'];   // a fixed set, the same list as the Worker and the proxy
  const PALETTE = REACTIONS.concat(['😀', '🙏', '🔥', '⚠️']);   // emoji that the text button can insert
  let storageOk = true;
  const read = (k, def) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : def; } catch (e) { storageOk = false; return def; } };
  const write = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { storageOk = false; } };

  // Sanitizers shared by load, import and server data. Same limits as the Worker.
  const str = (v, max) => String(v == null ? '' : v).slice(0, max);
  const num = v => (Number.isFinite(+v) ? +v : 0);
  const date = v => (v && !isNaN(Date.parse(v)) ? new Date(v).toISOString() : new Date().toISOString());
  const okId = v => typeof v === 'string' && /^[a-z0-9]{4,40}$/.test(v);
  function cleanReactions(v) { // { emoji: [names] }: only the fixed set, unique names, no empty list
    const out = {};
    for (const e of REACTIONS) {
      const l = [...new Set((v && Array.isArray(v[e]) ? v[e] : []).map(n => str(n, LIM.author)).filter(Boolean))].slice(0, LIM.reactors);
      if (l.length) out[e] = l;
    }
    return out;
  }
  const withRx = v => { const rx = cleanReactions(v); return Object.keys(rx).length ? { reactions: rx } : {}; };
  function cleanReply(r) {
    if (!r || !okId(r.id) || !String(r.text == null ? '' : r.text).trim()) return null;
    return { id: r.id, author: str(r.author || 'anonymous', LIM.author), text: str(r.text, LIM.text), createdAt: date(r.createdAt), ...withRx(r.reactions) };
  }
  function clean(c) {
    if (!c || !okId(c.id) || !String(c.text == null ? '' : c.text).trim()) return null;
    return { id: c.id, n: Number.isInteger(c.n) && c.n > 0 ? c.n : 0, screen: str(c.screen || 'main', 64), version: str(c.version || 'v1', 20), path: str(c.path, LIM.path),
      fx: num(c.fx), fy: num(c.fy), pageX: num(c.pageX), pageY: num(c.pageY), where: str(c.where, LIM.where),
      text: str(c.text, LIM.text), author: str(c.author || 'anonymous', LIM.author), createdAt: date(c.createdAt), resolved: !!c.resolved,
      ...withRx(c.reactions),
      ...(c.appVersion ? { appVersion: str(c.appVersion, 40) } : {}),   // version of the app under review. Only frame mode sets it
      replies: (Array.isArray(c.replies) ? c.replies : []).map(cleanReply).filter(Boolean) };
  }
  const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  // Pin numbers are stable: n is set once at creation and never changes, so a delete does not renumber later pins.
  const byAge = (a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : a.id < b.id ? -1 : 1);
  const grp = c => c.screen + '|' + c.version;
  // hw is the highest number ever used per screen and version. A delete never frees a number, so "#3" never points to another comment.
  let hw = read(HKEY, {});
  if (!hw || typeof hw !== 'object' || Array.isArray(hw)) hw = {};
  const nextN = (list, c) => Math.max(+hw[grp(c)] || 0, list.reduce((m, x) => (grp(x) === grp(c) && x.n > m ? x.n : m), 0)) + 1;
  const used = c => { if (c.n > (+hw[grp(c)] || 0)) hw[grp(c)] = c.n; };
  function backfill(list) { // old comments without n get the next free number, in creation order
    let changed = false;
    [...list].sort(byAge).forEach(c => { if (!c.n) { c.n = nextN(list, c); used(c); changed = true; } });
    return changed;
  }

  // ---- State ----
  let items = read(KEY, []);
  items = (Array.isArray(items) ? items : []).map(clean).filter(Boolean);
  if (backfill(items)) write(KEY, items);
  items.forEach(used);
  let queue = read(QKEY, []);
  if (!Array.isArray(queue)) queue = [];
  let prefs = read(PKEY, {});
  if (!prefs || typeof prefs !== 'object') prefs = {};
  let showHint = !prefs.seen;
  prefs.seen = true;
  let lastSeen = prefs.lastSeen || new Date().toISOString();   // the last time this reviewer opened the panel. Newer comments of other people count as new
  prefs.lastSeen = lastSeen;
  prefs.mode = prefs.mode || (CFG.startMode === 'browse' ? 'browse' : 'comment');   // startMode only sets the first visit. A saved choice wins
  write(PKEY, prefs);
  let mode = '', syncMsg = '', statusMsg = '', busy = false;       // mode: '' until detected, then 'local' or 'shared'
  let filter = 'open', viewVer = VERSION, nums = {};
  let openId = null, editing = null, pop = null, popPt = null;
  const drafts = {};
  let lostDraft = '', hashDone = false, ready = !FRAME;   // lostDraft: unsent composer text that a click elsewhere or Esc closed. ready: the first screen is loaded (hash links wait for it)
  const expanded = new Set();
  let meta = null;   // answer of /__review/meta (frame mode), or null
  // The app version that comments carry. A comment is stale when it names another version and is still open.
  const metaVer = () => (FRAME && meta && typeof meta.version === 'string' && /^[A-Za-z0-9._+-]{1,40}$/.test(meta.version) ? meta.version : '');
  const isStale = c => !!c.appVersion && !!metaVer() && c.appVersion !== metaVer() && !c.resolved;
  const stillText = () => 'Still an issue in ' + metaVer();
  const VIEWPORTS = { desktop: null, tablet: [834, 1112], mobile: [390, 844] };

  // ---- Screens ----
  // Frame mode: tabs come from cmt-config frame.screens and switch the iframe src. The screen id is then "path:" + iframe pathname.
  const sections = FRAME ? [] : [...document.querySelectorAll('[data-screen]')];
  const screens = FRAME ? (Array.isArray(CFG.frame.screens) ? CFG.frame.screens : []).filter(s => s && s.id && s.src).map(s => ({ id: String(s.id), title: String(s.title || s.id), src: String(s.src), note: str(s.note, 300), highlight: str(s.highlight, 200), prefill: Array.isArray(s.prefill) ? s.prefill.filter(x => x && typeof x === 'object').slice(0, 10) : [] }))
    : sections.map(s => ({ id: s.dataset.screen, title: s.dataset.title || s.dataset.screen }));
  const pathId = p => ('path:' + p).slice(0, 64);
  const routeOf = u => u.pathname + (/^#\//.test(u.hash) ? u.hash : '');   // a hash route such as #/orders is part of the screen
  const srcPath = s => { try { return routeOf(new URL(s, location.href)); } catch (e) { return s; } };
  const screenOf = id => screens.find(s => s.id === id || (FRAME && pathId(srcPath(s.src)) === id));
  const screenTitle = id => (screenOf(id) || { title: id === 'main' ? 'Page' : FRAME && id.startsWith('path:') ? id.slice(5) : id }).title;
  const hashParam = k => { const m = new RegExp('[#&]' + k + '=([^&]+)').exec(location.hash); return m ? decodeURIComponent(m[1]) : ''; };
  const hashScreen = () => hashParam('screen');
  const LINK = hashParam('comment');   // read once: showing a screen rewrites the hash
  let screen = screens.some(s => s.id === hashScreen()) ? hashScreen() : screens.length ? screens[0].id : 'main';

  // Tiny DOM helper. Names that are DOM properties are set as properties, the rest as attributes.
  const h = (tag, a, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(a || {})) { if (k in e) e[k] = v; else e.setAttribute(k, v); }
    e.append(...kids);
    return e;
  };
  const btn = (t, fn, cls) => h('button', { type: 'button', className: 'cmt-btn ' + (cls || ''), onclick: fn }, t);
  const fmt = s => new Date(s).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' });
  const txt = n => ((n.innerText || n.textContent) || '').replace(/\s+/g, ' ').trim();   // innerText keeps the space between two inline links
  const ago = s => { const m = Math.round((Date.now() - Date.parse(s)) / 60000); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : m < 43200 ? Math.round(m / 1440) + ' d ago' : fmt(s); };
  const when = s => h('time', { dateTime: s, title: fmt(s) }, ago(s));   // relative time, full date on hover
  const oneLine = s => s.replace(/\s*\n\s*/g, ' ');
  const isEl = n => !!n && n.nodeType === 1;   // instanceof Element fails for nodes of the iframe
  const frameEl = FRAME ? document.getElementById('cmt-frame') || document.querySelector('iframe') : null;
  const frameDoc = () => { try { return frameEl.contentDocument || null; } catch (e) { return null; } };
  const hostDoc = () => (FRAME ? frameDoc() : document);   // the document that holds the anchors
  const offset = () => (FRAME ? frameEl.getBoundingClientRect() : { left: 0, top: 0, right: 0, bottom: 0 });

  // ---- Anchoring: CSS path plus fractional offset inside the element ----
  function pathOf(el) {
    const d = el.ownerDocument, p = [];
    for (; el && el !== d.body; el = el.parentElement) {
      if (el.id && d.querySelectorAll('#' + CSS.escape(el.id)).length === 1) { p.unshift('#' + CSS.escape(el.id)); return p.join(' > '); }
      let i = 1;
      for (let s = el.previousElementSibling; s; s = s.previousElementSibling) if (s.localName === el.localName) i++;
      p.unshift(el.localName + ':nth-of-type(' + i + ')');
    }
    return ['body'].concat(p).join(' > ');
  }
  function anchorEl(c) {
    const d = hostDoc();
    try { return d && c.path ? d.querySelector(c.path) : null; } catch (e) { return null; /* bad selector: use fallback */ }
  }
  function pt(c) { // document coordinates of a comment (of the wrapper page in frame mode), or null when an old anchor is gone
    const el = anchorEl(c);
    if (el && el.getClientRects().length) {
      const r = el.getBoundingClientRect(), o = offset();
      const x = o.left + scrollX + r.left + c.fx * r.width, y = o.top + scrollY + r.top + c.fy * r.height;
      return { x, y, out: FRAME && (x < o.left + scrollX || x > o.right + scrollX || y < o.top + scrollY || y > o.bottom + scrollY) };
    }
    if (FRAME && c.path) return null;   // element anchors only make sense inside the live app. Page comments have no path.
    return c.version === VERSION ? { x: c.pageX, y: c.pageY } : null;
  }
  function whereOf(el) { // "Heading › text of the element": readable without the pin
    const d = el.ownerDocument;
    let head = '';
    for (let s = el.closest('section'); s && !head; s = s.parentElement && s.parentElement.closest('section')) {
      const hd = s.querySelector('h1,h2,h3');
      if (hd) head = txt(hd);
    }
    if (FRAME && !head) { const h1 = d.querySelector('h1'); if (h1) head = txt(h1); }
    let t = el;
    while (t && t !== d.body && !txt(t)) t = t.parentElement;
    let s = t ? txt(t) : '';
    if (s.length > 50) s = s.slice(0, 49) + '…';
    return [head, s === head ? '' : s].filter(Boolean).join(' › ') || 'Page';
  }

  // ---- Data operations: apply locally first, then save or send ----
  function apply(op) {
    const c = items.find(i => i.id === (op.cid || (op.c && op.c.id)));
    if (op.t === 'add') {
      if (!c) { if (!op.c.n || items.some(i => grp(i) === grp(op.c) && i.n === op.c.n)) op.c.n = nextN(items, op.c); items.push(op.c); used(op.c); }
      return;
    }
    if (!c) return;
    if (op.t === 'reply') { if (!c.replies.some(r => r.id === op.r.id)) c.replies.push(op.r); }
    else if (op.t === 'edit') { const t = op.rid ? c.replies.find(r => r.id === op.rid) : c; if (t) t.text = op.text; }
    else if (op.t === 'resolve') c.resolved = op.resolved;
    else if (op.t === 'react') { const t = op.rid ? c.replies.find(r => r.id === op.rid) : c; if (t) setReaction(t, op.emoji, op.name, op.on); }
    else if (op.t === 'del') { if (op.rid) c.replies = c.replies.filter(r => r.id !== op.rid); else items = items.filter(i => i !== c); }
  }
  function setReaction(t, e, name, on) { // idempotent: "on" is the wanted state, so a replayed operation changes nothing
    const l = ((t.reactions || {})[e] || []).filter(n => n !== name);
    if (on) l.push(name);
    const rx = Object.assign({}, t.reactions, { [e]: l.slice(-LIM.reactors) });
    if (!l.length) delete rx[e];
    if (Object.keys(rx).length) t.reactions = rx; else delete t.reactions;
  }
  function run(op) {
    apply(op);
    save();
    if (mode === 'shared') { queue.push(op); write(QKEY, queue); updateStatus('Syncing…'); syncSoon(); }
    render();
  }
  function save() { write(KEY, items); write(HKEY, hw); updateStatus(); }
  function request(op) { // [method, query string, body] for one queued operation
    const base = { project: PROJECT };
    if (op.t === 'add') { const b = Object.assign(base, op.c, { type: 'comment' }); if (!b.appVersion) delete b.appVersion; return ['POST', '', b]; }   // the Worker rejects unknown keys
    if (op.t === 'reply') return ['POST', '', Object.assign(base, op.r, { type: 'reply', commentId: op.cid })];
    if (op.t === 'edit') return ['PATCH', '', Object.assign(base, { id: op.cid, text: op.text, author: op.author }, op.rid ? { replyId: op.rid } : {})];   // a null replyId would name a reply that does not exist
    if (op.t === 'resolve') return ['PATCH', '', Object.assign(base, { id: op.cid, resolved: op.resolved, author: op.author })];
    if (op.t === 'react') return ['PATCH', '', Object.assign(base, { id: op.cid, react: op.emoji, on: op.on, author: op.name }, op.rid ? { replyId: op.rid } : {})];
    const q = new URLSearchParams({ id: op.cid, author: op.author || '' });
    if (op.rid) q.set('replyId', op.rid);
    return ['DELETE', '&' + q, null];
  }
  async function call(method, extra, body) {
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 8000);
    try {
      const res = await fetch(API + '?project=' + encodeURIComponent(PROJECT) + (extra || ''), {
        method, cache: 'no-store', signal: ctl.signal,
        headers: Object.assign({ 'x-review-key': RKEY }, body ? { 'content-type': 'application/json' } : {}),
        body: body ? JSON.stringify(body) : undefined });
      let j = null;
      try { j = await res.json(); } catch (e) { /* not JSON */ }
      if (!res.ok) throw Object.assign(new Error((j && j.error) || 'HTTP ' + res.status), { status: res.status });
      return j;
    } finally { clearTimeout(timer); }
  }
  async function flush() {
    while (queue.length) {
      try { await call(...request(queue[0])); }
      catch (e) {
        const perm = e.status >= 400 && e.status < 500 && ![401, 408, 429].includes(e.status);
        if (!perm) throw e;
        if (e.status !== 409) syncMsg = 'Rejected: ' + e.message; // 409 means the server already has it
      }
      queue.shift();
      write(QKEY, queue);
    }
  }
  async function sync() {
    if (mode !== 'shared' || busy) return;
    busy = true;
    syncMsg = '';
    updateStatus('Syncing…');
    try {
      await flush();
      if (!queue.length) {
        const j = await call('GET');
        if (!queue.length && Array.isArray(j.comments)) {
          const before = JSON.stringify(items);
          items = j.comments.map(clean).filter(Boolean);
          backfill(items);
          items.forEach(used);
          if (JSON.stringify(items) !== before) { save(); render(); }
          openFromHash();
        }
      }
      updateStatus(queue.length ? 'Syncing…' : syncMsg || 'Synced');
    } catch (e) {
      updateStatus(e.status === 401 ? 'Review key rejected' : 'Offline — saved locally');
    } finally { busy = false; }
    if (queue.length) setTimeout(sync, 1500);
  }
  const syncSoon = () => setTimeout(sync, 50);
  async function detect() { // shared mode needs the comments API to answer 200 with {comments:[]}
    let denied = false;
    try {
      const j = await call('GET');
      if (j && Array.isArray(j.comments)) mode = 'shared';
    } catch (e) { denied = e.status === 401; }
    mode = mode || 'local';
    root.dataset.mode = mode;
    if (mode === 'local' && denied) notice('key', (RKEY ? 'The review key in this link is wrong.' : 'This link has no review key (?k=).') + ' Comments are saved only on this device, and other reviewers cannot see them. Ask for the full link. To move comments, use Download JSON here, then Import JSON on the full link.');
    if (mode === 'shared') { await sync(); setInterval(sync, 20000); addEventListener('focus', sync); }
    updateStatus();
    updateNote();
  }

  // ---- UI shell ----
  const root = h('div', { id: 'cmt-root', className: 'cmt-ui' });
  const pinsBox = h('div'), hl = h('div', { className: 'cmt-hl' });
  const list = h('div', { className: 'cmt-list' });
  const note = h('div', { className: 'cmt-note', role: 'status' });
  const banner = h('div', { className: 'cmt-banner', role: 'alert' });
  const notices = {}, dismissed = {};
  function notice(k, t) { // persistent warnings above the toolbar. Each one has a Dismiss button.
    if (t) notices[k] = t; else delete notices[k];
    const keys = Object.keys(notices).filter(x => !dismissed[x]);
    banner.classList.toggle('cmt-show', keys.length > 0);
    banner.replaceChildren(...keys.map(x => h('div', { className: 'cmt-row' }, h('span', { className: 'cmt-sp' }, notices[x]), btn('Dismiss', () => { dismissed[x] = true; notice(); }, 'cmt-sm'))));
  }
  const statusEl = h('span', { className: 'cmt-status', role: 'status' });
  const bBrowse = btn('Browse', () => setMode('browse'), 'cmt-seg');
  const bComment = btn('Comment', () => setMode('comment'), 'cmt-seg');
  const bCount = btn('Comments (0)', () => togglePanel(), '');
  bCount.setAttribute('aria-controls', 'cmt-panel');
  // Frame mode only: highlight toggle, viewport selector, reset button, tab note and toast.
  const bHl = FRAME ? btn('Highlights', () => { prefs.highlight = prefs.highlight === false; write(PKEY, prefs); updateFrameUi(); }, '') : null;
  if (bHl) { bHl.id = 'cmt-hl-toggle'; bHl.hidden = true; }
  const vpBtns = FRAME ? Object.keys(VIEWPORTS).map(n => { const b = btn(n[0].toUpperCase() + n.slice(1), () => setViewport(n), 'cmt-seg cmt-vp'); b.dataset.vp = n; return b; }) : [];
  const vpGrp = FRAME ? h('div', { id: 'cmt-viewport', className: 'cmt-seggrp', role: 'group', 'aria-label': 'Viewport' }, ...vpBtns) : null;
  const bReset = FRAME ? btn('Reset demo data', () => resetDemo(), '') : null;
  if (bReset) { bReset.id = 'cmt-reset'; bReset.hidden = true; }
  const screenNote = FRAME ? h('div', { id: 'cmt-screen-note', className: 'cmt-screen-note cmt-ui', role: 'note', hidden: true }) : null;
  const toastEl = FRAME ? h('div', { id: 'cmt-toast', className: 'cmt-toast cmt-ui', role: 'status', 'aria-live': 'polite' }) : null;
  // The name is asked once, in the first composer. After that the toolbar shows it, and the pencil edits it in place.
  const nameBtn = btn('', () => editName(), 'cmt-sm cmt-nm');
  const newBadge = h('span', { className: 'cmt-new', role: 'status', hidden: true });
  const updateName = () => { nameBtn.textContent = prefs.name ? 'Commenting as ' + prefs.name + ' ✎' : 'Set your name ✎'; nameBtn.title = 'Change your name. Old comments keep the name they were written with.'; };
  function editName() {
    const inp = h('input', { type: 'text', maxLength: LIM.author, placeholder: 'Your name', 'aria-label': 'Your name', value: prefs.name || '' });
    let done = false;
    const end = ok => {
      if (done) return;
      done = true;
      const v = inp.value.trim();
      if (ok && v) { prefs.name = v; write(PKEY, prefs); }
      inp.replaceWith(nameBtn);
      updateName();
      render();
      nameBtn.focus({ preventScroll: true });
    };
    inp.onkeydown = e => { if (e.key === 'Enter' || e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); end(e.key === 'Enter'); } };
    inp.onblur = () => end(true);
    nameBtn.replaceWith(inp);
    inp.focus();
    inp.select();
  }
  const bar = h('div', { className: 'cmt-bar', role: 'toolbar', 'aria-label': 'Review comments' }, banner, note,
    h('div', { className: 'cmt-seggrp', role: 'group', 'aria-label': 'Mode' }, bBrowse, bComment), bCount,
    FRAME ? btn('Page comment', () => pageComment(), '') : '', bHl || '', vpGrp || '', bReset || '',
    btn('Copy as Markdown', e => copyText(e.currentTarget, exportMarkdown()), 'cmt-primary cmt-wide'), newBadge, nameBtn, statusEl);
  const filterBtns = ['open', 'resolved', 'all'].map(f => btn(f, () => { filter = f; render(); }, 'cmt-seg'));
  const verSel = h('select', { 'aria-label': 'Version', onchange: () => { viewVer = verSel.value; render(); } });
  const fileIn = h('input', { type: 'file', accept: 'application/json,.json', hidden: true, onchange: () => importFile(fileIn) });
  const panel = h('aside', { id: 'cmt-panel', className: 'cmt-panel', 'aria-label': 'Comments' },
    h('div', { className: 'cmt-phead' },
      h('div', { className: 'cmt-row' }, h('strong', { className: 'cmt-sp' }, 'Comments'), btn('Close', () => togglePanel(false))),
      CFG.reviewNote ? h('div', { className: 'cmt-meta' }, CFG.reviewNote) : '',
      h('div', { className: 'cmt-row' }, verSel),
      h('div', { className: 'cmt-seggrp', role: 'group', 'aria-label': 'Filter' }, ...filterBtns),
      h('div', { className: 'cmt-row' }, btn('Copy as Markdown', e => copyText(e.currentTarget, exportMarkdown()), 'cmt-sm'),
        btn('Download JSON', downloadJSON, 'cmt-sm'), btn('Import JSON', () => fileIn.click(), 'cmt-sm'), fileIn)), list);
  const tabs = h('cmt-tabs', { className: 'cmt-ui', role: 'tablist', 'aria-label': 'Screens' },
    ...screens.map(s => h('button', { type: 'button', className: 'cmt-tab', role: 'tab', 'data-id': s.id, onclick: () => showScreen(s.id, true) }, s.title)));

  function setMode(m) { showHint = false; prefs.mode = m; write(PKEY, prefs); closePop(); hideHl(); syncMode(); }
  function syncMode() {
    const on = prefs.mode === 'comment';
    document.documentElement.classList.toggle('cmt-on', on);
    if (FRAME && boundDoc) { try { boundDoc.documentElement.classList.toggle('cmt-on', on); } catch (e) { /* old document is gone */ } }
    bComment.setAttribute('aria-pressed', on);
    bBrowse.setAttribute('aria-pressed', !on);
    updateNote();
  }
  function togglePanel(force) {
    bCount.setAttribute('aria-expanded', panel.classList.toggle('cmt-open', force));
    lastSeen = prefs.lastSeen = new Date().toISOString();   // opening or closing the panel marks everything as seen
    write(PKEY, prefs);
    render();
  }
  function updateNote() {
    note.textContent = !storageOk && mode !== 'shared' ? 'Not saved: browser storage is blocked. Comments stay in this tab only. Use Copy as Markdown before you leave.'
      : showHint && prefs.mode === 'comment' ? 'Comment mode is on. Click anything to comment. Press C for Browse mode.' + (CFG.reviewNote ? ' ' + CFG.reviewNote : '')
      : showHint && prefs.mode === 'browse' ? 'Press C or click Comment to leave feedback.' + (CFG.reviewNote ? ' ' + CFG.reviewNote : '')
      : '';
  }
  function updateStatus(s) {
    if (s) statusMsg = s;
    statusEl.textContent = mode === 'shared' ? (!storageOk && /Offline/.test(statusMsg) ? 'Offline — not saved' : statusMsg || 'Synced')
      : mode === 'local' ? (storageOk ? 'Saved on this device' : 'Not saved') : '';
    updateNote();
  }
  function showScreen(id, user) {
    if (FRAME) return gotoFrame(id);
    if (!screens.some(s => s.id === id)) return;
    screen = id;
    sections.forEach(s => { s.hidden = s.dataset.screen !== id; });
    tabs.querySelectorAll('.cmt-tab').forEach(t => t.setAttribute('aria-selected', t.dataset.id === id));
    try { history.replaceState(null, '', location.pathname + location.search + '#screen=' + encodeURIComponent(id)); } catch (e) { /* ignore */ }
    if (user) { closePop(); scrollTo(0, 0); }
    render();
  }

  // ---- Frame mode: the app runs in an iframe of the same origin ----
  let started = false, navigating = 0, boundDoc = null, boundObs = null, laterT = 0, pinKey = '', pendingJump = null;
  // Only http(s) URLs load in the iframe. A screen id from a comment or from the URL hash must also stay on this origin:
  // "path:javascript:..." would otherwise run a script with the rights of this page. The config is the author's, so it may name another origin.
  const urlOf = (s, same) => { try { const u = new URL(s, location.href); return /^https?:$/.test(u.protocol) && (!same || u.origin === location.origin) ? u.href : ''; } catch (e) { return ''; } };
  const srcOf = id => { const s = screenOf(id); return s ? urlOf(s.src) : id.startsWith('path:') ? urlOf(id.slice(5), true) : ''; };
  function gotoFrame(id) {
    const src = srcOf(id);
    if (!src) return;
    closePop();
    started = true;
    navigating = Date.now() + 8000;   // the poll ignores the old page until the new one loads
    screen = id;
    let same = false;
    try { const a = new URL(frameEl.contentWindow.location.href), b = new URL(src); same = a.href !== b.href && a.href.split('#')[0] === b.href.split('#')[0]; } catch (e) { /* not readable */ }
    frameEl.src = src;
    if (same) navigating = 0;   // only the hash changes: the browser fires no load event, so do not wait for one
    render();
  }
  function syncFrame() { // runs on load and every 500 ms: follows full loads and client-side history changes
    if (!started || navigating > Date.now()) return;
    const d = frameDoc();
    let p = null;
    try { if (d && d.location.protocol !== 'about:') p = routeOf(d.location); } catch (e) { /* not readable */ }
    notice('frame', d ? null : 'This app runs on another origin, so pins cannot attach to its elements. Serve the app and this page from one origin, for example through review-proxy.py. Page comments still work.');
    if (p === null) return;
    const fresh = bindFrame(d);
    if (pathId(p) !== screen) { screen = pathId(p); closePop(); render(); }
    else if (fresh) relayout();   // a new document: its anchors exist now
    maybePrefill(d);
  }
  function onFrameLoad() {
    navigating = 0;
    syncFrame();
    const c = pendingJump && items.find(i => i.id === pendingJump);
    pendingJump = null;
    if (c) scrollToAnchor(c);
  }
  function scrollToAnchor(c) {
    const el = anchorEl(c);
    if (!el) return;
    const w = el.ownerDocument.defaultView;
    w.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + w.scrollY - w.innerHeight / 2));
  }
  function bindFrame(d) { // listen inside the iframe document. A full load replaces the document, so bind again
    if (d === boundDoc) return false;
    if (boundObs) boundObs.disconnect();
    boundDoc = d;
    const w = d.defaultView;
    for (const [t, f] of [['mousemove', onMove], ['mouseout', onOut], ['click', onClick], ['submit', onSubmit], ['keydown', onKey]]) w.addEventListener(t, f, true);
    w.addEventListener('scroll', () => { hideHl(); relayout(); }, true);   // the outline would stay where the element was
    w.addEventListener('resize', relayout);
    watchWrites(w);
    const st = d.createElement('style');
    st.textContent = 'html.cmt-on *{cursor:crosshair!important}';
    (d.head || d.documentElement).append(st);
    boundObs = new MutationObserver(() => { if (!laterT) laterT = setTimeout(() => { laterT = 0; relayout(); }, 120); });
    boundObs.observe(d.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
    syncMode();
    return true;
  }
  function pageComment() { // a comment on the page, not on an element. Works when the iframe is on another origin
    if (!mode) return;
    const k = items.filter(c => c.screen === screen && c.version === VERSION && !c.path).length;
    composer({ path: '', fx: 0, fy: 0, pageX: Math.max(40, innerWidth - 40), pageY: 70 + 40 * k, where: 'Page', version: VERSION });
  }

  // ---- Frame mode extras: toast, tab guidance, viewport, reset, meta ----
  let toastT = 0, roAt = 0;
  function toast(msg) { // one element, shown for 4 seconds
    toastEl.textContent = msg;
    toastEl.classList.add('cmt-show');
    clearTimeout(toastT);
    toastT = setTimeout(() => toastEl.classList.remove('cmt-show'), 4000);
  }
  function readOnlyHit(status, flag) { // the proxy marks its read-only 403 with X-Review-Read-Only: 1
    if (status !== 403 || flag !== '1' || Date.now() - roAt < 2000) return;
    roAt = Date.now();
    toast('Read-only review: this change was not sent to the app.');
  }
  function watchWrites(w) { // wrap fetch and XMLHttpRequest of one iframe window. R4: an app that saved window.fetch earlier is not seen
    try {
      const f = w.fetch;
      if (typeof f === 'function') w.fetch = function () {
        const p = f.apply(w, arguments);
        p.then(r => readOnlyHit(r.status, r.headers.get('X-Review-Read-Only')), () => {});
        return p;
      };
    } catch (e) { /* leave fetch alone */ }
    try {
      const proto = w.XMLHttpRequest.prototype, send = proto.send;
      proto.send = function () {
        this.addEventListener('load', () => { try { readOnlyHit(this.status, this.getResponseHeader('X-Review-Read-Only')); } catch (e) { /* ignore */ } });
        return send.apply(this, arguments);
      };
    } catch (e) { /* leave XHR alone */ }
  }
  function markHl() { // mark the elements of the active tab's highlight selector inside the iframe
    const d = frameDoc();
    if (!d) return;
    try {
      const s = screenOf(screen), sel = s && s.highlight;
      let want = [];
      if (sel && prefs.highlight !== false) { try { want = [...d.querySelectorAll(sel)]; } catch (e) { /* bad selector: mark nothing */ } }
      const keep = new Set(want);
      d.querySelectorAll('[data-cmt-hl]').forEach(el => { if (!keep.has(el)) el.removeAttribute('data-cmt-hl'); });
      want.forEach(el => { if (!el.hasAttribute('data-cmt-hl')) el.setAttribute('data-cmt-hl', ''); });   // set only if absent: the MutationObserver must not loop
      if (want.length && !d.getElementById('cmt-hl-style')) {
        const st = d.createElement('style');
        st.id = 'cmt-hl-style';
        st.textContent = '[data-cmt-hl]{outline:3px solid #f59e0b!important;outline-offset:2px!important}';
        (d.head || d.documentElement).append(st);
      }
    } catch (e) { /* the document is gone */ }
  }
  // ---- Frame mode: prefill. A screen can fill the app's search box by itself, so the reviewer does not meet an empty page ----
  // Each step is { selector, value, submit, delayMs, waitForSelector, keyEvents } or { click, waitForSelector, optional }. It runs once each time the reviewer arrives at the screen.
  let pfRun = null, pfMsg = { id: '', text: '' }, synth = false;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function pfSay(id, text) { pfMsg = { id, text }; updateFrameUi(); }
  async function pfWait(d, sel, run) { // the element, or null after 15 s, on a bad selector, or when this run is over
    for (const end = Date.now() + 15000; Date.now() < end && pfRun === run && boundDoc === d;) {
      try { const el = d.querySelector(sel); if (el) return el; } catch (e) { return null; }
      await sleep(100);
    }
    return null;
  }
  const pfKey = (w, el, type, key, code, kc) => el.dispatchEvent(new w.KeyboardEvent(type, { key, code, keyCode: kc, which: kc, charCode: type === 'keypress' ? kc : 0, bubbles: true, cancelable: true }));
  async function pfType(w, el, text, keys) { // optional key events around each character. The character goes in with execCommand('insertText'), whose input events the browser marks as real: Reflex ignores input events from the native setter
    const set = Object.getOwnPropertyDescriptor(el.tagName === 'TEXTAREA' ? w.HTMLTextAreaElement.prototype : w.HTMLInputElement.prototype, 'value').set;
    const d = el.ownerDocument;
    el.focus();
    try { el.select(); } catch (e) {}
    let exec = true;
    const chars = [...text];
    for (let i = 0; i < chars.length; i++) {
      const ch = chars[i], code = /[a-z]/i.test(ch) ? 'Key' + ch.toUpperCase() : /\d/.test(ch) ? 'Digit' + ch : '', kc = ch.toUpperCase().charCodeAt(0);
      if (keys) { pfKey(w, el, 'keydown', ch, code, kc); pfKey(w, el, 'keypress', ch, code, kc); }   // off by default: an app with an on-key-down handler (Reflex) breaks when every character fires it
      const want = chars.slice(0, i + 1).join('');
      if (exec) { try { exec = d.execCommand('insertText', false, ch) && el.value === want; } catch (e) { exec = false; } }
      if (!exec || el.value !== want) {   // fallback: the native setter and a plain input event (React accepts it)
        set.call(el, want);
        el.dispatchEvent(new w.InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
      }
      if (keys) pfKey(w, el, 'keyup', ch, code, kc);
      await sleep(10);
    }
    el.dispatchEvent(new w.Event('change', { bubbles: true }));
  }
  function pfSubmit(w, el, how) {
    if (how === 'none') return;
    const [key, kc] = how === 'Tab' ? ['Tab', 9] : ['Enter', 13];
    for (const t of key === 'Enter' ? ['keydown', 'keypress', 'keyup'] : ['keydown', 'keyup']) pfKey(w, el, t, key, key, kc);
    if (key === 'Tab') el.blur();
  }
  async function runPrefill(s, d) {
    const run = pfRun = {}, w = d.defaultView, live = () => pfRun === run && boundDoc === d;
    let failed = '';
    pfSay(s.id, 'Loading sample data…');
    try {
      for (const st of s.prefill) {
        const sel = String(st.click || st.selector || '');
        let el = null;
        if (st.click && st.optional === true) { try { el = d.querySelector(sel); } catch (e) { el = null; } if (!el) continue; }   // an optional click is skipped at once when its element is absent, for example a tab that exists only after data loads
        else el = await pfWait(d, sel, run);
        if (!live()) return;
        if (!el) { failed = sel; break; }
        if (st.click) {
          synth = true;
          try { el.focus(); for (const t of ['mousedown', 'mouseup', 'click']) el.dispatchEvent(new w.MouseEvent(t, { bubbles: true, cancelable: true, view: w, button: 0 })); } finally { synth = false; }
        } else {
          await pfType(w, el, String(st.value == null ? '' : st.value), st.keyEvents === true);
          await sleep(Math.min(10000, Math.max(0, +st.delayMs || 0)));
          if (!live()) return;
          pfSubmit(w, el, st.submit || 'Enter');
        }
        if (st.waitForSelector) {
          const ok = await pfWait(d, String(st.waitForSelector), run);
          if (!live()) return;
          if (!ok) { failed = st.waitForSelector; break; }
        }
      }
    } catch (e) { failed = failed || 'the app'; }
    finally { if (pfRun === run) { pfRun = null; pfSay(s.id, failed ? 'Sample data did not load (' + failed + ' not found). Reload this tab to try again.' : ''); } }
  }
  let pfSeen = { doc: null, id: '' };
  function maybePrefill(d) { // called by every poll. It starts once for each arrival at a screen: a new document, or another screen in the same document
    const s = screenOf(screen), id = s ? s.id : '';
    if (pfSeen.doc === d && pfSeen.id === id) return;
    pfSeen = { doc: d, id };
    if (s && s.prefill.length) runPrefill(s, d);
  }
  function updateFrameUi() {
    if (!FRAME) return;
    const s = screenOf(screen);
    const nt = [s && pfMsg.id === s.id ? pfMsg.text : '', s && s.note].filter(Boolean).join(' ');
    screenNote.textContent = nt;
    screenNote.hidden = !nt;
    bHl.hidden = !(s && s.highlight);
    bHl.setAttribute('aria-pressed', prefs.highlight !== false);
    bReset.hidden = !(meta && meta.resetEnabled === true);
    const v = curViewport();
    vpBtns.forEach(b => b.setAttribute('aria-pressed', b.dataset.vp === v));
    markHl();
  }
  const curViewport = () => (Object.prototype.hasOwnProperty.call(VIEWPORTS, prefs.viewport) ? prefs.viewport : 'desktop');
  function applyViewport() {
    const v = curViewport(), size = VIEWPORTS[v];
    for (const n of Object.keys(VIEWPORTS)) document.documentElement.classList.toggle('cmt-vp-' + n, n === v);
    frameEl.style.cssText = size ? 'flex:none;height:' + size[1] + 'px;width:' + size[0] + 'px;max-width:100%;align-self:center' : '';
  }
  function setViewport(n) {
    if (!Object.prototype.hasOwnProperty.call(VIEWPORTS, n)) return;
    prefs.viewport = n;
    write(PKEY, prefs);
    applyViewport();
    closePop();
    updateFrameUi();
    relayout();
    requestAnimationFrame(relayout);   // the iframe reflows after this frame
  }
  async function resetDemo() {
    if (!window.confirm('Reset the demo data? Everything reviewers changed in the app goes back to its start state. Comments stay.')) return;
    bReset.disabled = true;
    bReset.textContent = 'Resetting…';
    try {
      const res = await fetch('/__review/reset', { method: 'POST', credentials: 'same-origin', headers: { 'X-Review-Reset': '1' } });
      let j = null;
      try { j = await res.json(); } catch (e) { /* not JSON */ }
      if (res.ok && j && j.ok === true) {
        if (typeof window.cmtLogin === 'function') { try { await window.cmtLogin(); } catch (e) { /* the app shows its login page */ } }
        try { frameEl.contentWindow.location.reload(); } catch (e) { frameEl.src = frameEl.src; }
        toast('Demo data reset.');
      } else if (res.status === 429) toast('Reset is limited to once every 30 seconds. Try again in ' + ((j && j.retryAfterSeconds) || res.headers.get('Retry-After') || 30) + ' s.');
      else toast('Reset failed: ' + ((j && j.error) || 'HTTP ' + res.status));
    } catch (e) { toast('Reset failed: no answer from the review server.'); }
    bReset.disabled = false;
    bReset.textContent = 'Reset demo data';
  }
  async function loadMeta() {
    let m = null;
    try {
      const res = await fetch('/__review/meta', { credentials: 'same-origin', cache: 'no-store' });
      const j = res.status === 200 ? await res.json() : null;
      if (j && typeof j === 'object' && !Array.isArray(j)) m = j;
    } catch (e) { m = null; }
    const changed = JSON.stringify(m) !== JSON.stringify(meta);
    meta = m;
    if (changed) render();
    else updateFrameUi();
  }

  // ---- Popover: composer for a new comment, or a thread ----
  function keepDraft() { if (pop && pop._ta && pop._ta.value.trim()) lostDraft = pop._ta.value; if (pop) pop._ta = null; }   // a composer that closes with text keeps it for the next one
  function closePop(discard) { if (!discard) keepDraft(); if (pop) pop.remove(); pop = popPt = openId = editing = null; }
  function placePop() {
    if (!pop) return;
    const p = popPt() || { x: scrollX + innerWidth / 2 - 126, y: scrollY + innerHeight / 3 };
    const w = pop.offsetWidth, ht = pop.offsetHeight, vw = document.documentElement.clientWidth, floor = scrollY + innerHeight - bar.offsetHeight - 20;
    const left = Math.min(Math.max(p.x - 14, scrollX + 8), scrollX + vw - w - 8);
    let top = p.y + 18;
    if (top + ht > floor) top = p.y - 18 - ht;
    pop.style.left = left + 'px';
    pop.style.top = Math.max(Math.min(top, floor - ht), scrollY + 8) + 'px';   // always inside the window, also when the pin is out of view
  }
  function setPop(label, kids, getPt) {
    keepDraft();
    if (pop) pop.remove();
    popPt = getPt;
    pop = h('div', { className: 'cmt-pop', role: 'dialog', 'aria-label': label, tabIndex: -1 }, ...kids);
    root.append(pop);
    placePop();
    return pop;
  }
  // Enter saves, Shift+Enter adds a line, Ctrl or Cmd+Enter also saves. Enter during text composition (IME) is left alone.
  const enterSaves = fn => e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target.matches('textarea,input')) { e.preventDefault(); fn(); } };
  function composer(draft) {
    keepDraft();   // a click elsewhere replaces an open composer: its text moves to the new one
    const ta = h('textarea', { rows: 3, placeholder: 'Write a comment', 'aria-label': 'Comment', maxLength: LIM.text, value: lostDraft });
    const back = !!lostDraft;
    lostDraft = '';
    const nm = prefs.name ? null : h('input', { type: 'text', placeholder: 'Your name', maxLength: LIM.author, 'aria-label': 'Your name' });
    let saving = false;
    const save = async () => {
      if (saving) return;
      const text = ta.value.trim();
      if (nm && !nm.value.trim()) return nm.focus();
      if (!text) return ta.focus();
      if (nm) { prefs.name = nm.value.trim(); write(PKEY, prefs); updateName(); }
      saving = true;
      if (FRAME) await loadMeta();   // the app may have a new version since the last refresh. Stamp the comment with the current one
      const c = clean(Object.assign({ id: newId(), text, author: prefs.name, screen, version: VERSION, appVersion: metaVer(), replies: [] }, draft));
      closePop(true);
      run({ t: 'add', c });
      openId = c.id;
      renderPop();
    };
    const [emo, pal] = emojiBtn(ta);
    const p = setPop('New comment', [back ? h('div', { className: 'cmt-meta' }, 'Your unsent text is back.') : '', nm, ta, h('div', { className: 'cmt-row' }, btn('Save', save, 'cmt-primary'), btn('Cancel', () => closePop(true)), emo,
      h('span', { className: 'cmt-meta' }, 'Enter saves. Shift+Enter adds a line.')), pal].filter(Boolean), () => pt(draft));
    p._ta = ta;
    p.onkeydown = enterSaves(save);
    (nm || ta).focus({ preventScroll: true });
  }
  let reactPick = null;   // id of the entry whose reaction palette is open
  function react(c, r, e, on) {
    reactPick = null;
    if (!prefs.name) { note.textContent = 'Type your name in the toolbar, then react again.'; return editName(); }
    run({ t: 'react', cid: c.id, rid: r && r.id, emoji: e, on, name: prefs.name });
  }
  function reactRow(c, r, ro) { // chips with counts under a comment or reply. The title of a chip lists the names
    const it = r || c, mine = e => ((it.reactions || {})[e] || []).includes(prefs.name);
    const chips = REACTIONS.filter(e => (it.reactions || {})[e]).map(e => h('button', { type: 'button', className: 'cmt-chip', disabled: ro, 'aria-pressed': mine(e), title: it.reactions[e].join(', '),
      'aria-label': e + ' ' + it.reactions[e].length + ': ' + it.reactions[e].join(', '), onclick: () => react(c, r, e, !mine(e)) }, e + ' ' + it.reactions[e].length));
    const add = ro ? '' : h('button', { type: 'button', className: 'cmt-chip cmt-add', title: 'Add reaction', 'aria-label': 'Add reaction', onclick: () => { reactPick = reactPick === it.id ? null : it.id; renderPop(); } }, '☺ +');
    const pal = reactPick === it.id ? h('div', { className: 'cmt-palette', role: 'group', 'aria-label': 'Reactions' },
      ...REACTIONS.map(e => h('button', { type: 'button', className: 'cmt-chip', 'aria-label': 'React ' + e, onclick: () => react(c, r, e, !mine(e)) }, e))) : '';
    return h('div', { className: 'cmt-react' }, ...chips, add, pal);
  }
  function emojiBtn(ta) { // a button that opens a small palette. A click inserts the emoji at the cursor
    const pal = h('div', { className: 'cmt-palette', hidden: true, role: 'group', 'aria-label': 'Emoji' }, ...PALETTE.map(e => h('button', { type: 'button', className: 'cmt-chip', 'aria-label': 'Insert ' + e,
      onclick: () => { ta.setRangeText(e, ta.selectionStart, ta.selectionEnd, 'end'); ta.dispatchEvent(new Event('input')); ta.focus({ preventScroll: true }); } }, e)));
    const tog = btn('😀', () => { pal.hidden = !pal.hidden; tog.setAttribute('aria-expanded', !pal.hidden); }, 'cmt-sm cmt-emo');
    tog.setAttribute('aria-label', 'Add emoji');
    tog.setAttribute('aria-expanded', 'false');
    return [tog, pal];
  }
  function entry(c, r, ro) { // one comment or reply, with Edit and Delete for the author
    const it = r || c, id = it.id, own = !ro && !!prefs.name && it.author === prefs.name;
    if (editing === id) {
      const ta = h('textarea', { rows: 3, value: it.text, 'aria-label': 'Edit text', maxLength: LIM.text });
      const saveEdit = () => { const t = ta.value.trim(); if (!t) return ta.focus(); editing = null; run({ t: 'edit', cid: c.id, rid: r && r.id, text: t, author: it.author }); };
      ta.onkeydown = enterSaves(saveEdit);
      return h('div', { className: 'cmt-entry' }, ta, h('div', { className: 'cmt-row' }, btn('Save', saveEdit, 'cmt-primary'), btn('Cancel', () => { editing = null; renderPop(); })));
    }
    return h('div', { className: 'cmt-entry' + (r ? ' cmt-reply' : '') },
      h('div', { className: 'cmt-meta' }, h('b', {}, it.author), ' · ', when(it.createdAt)),
      h('p', { className: 'cmt-text' }, it.text),
      reactRow(c, r, ro),
      own ? h('div', { className: 'cmt-row' }, btn('Edit', () => { editing = id; renderPop(); }, 'cmt-sm'),
        btn('Delete', () => run({ t: 'del', cid: c.id, rid: r && r.id, author: it.author }), 'cmt-sm')) : '');
  }
  function renderPop() { // thread view of items[openId]
    const c = items.find(i => i.id === openId);
    if (!c) return closePop();
    const had = pop && pop.contains(document.activeElement); // keep keyboard focus across a rebuild
    const ro = c.version !== VERSION;
    const ta = h('textarea', { rows: 2, placeholder: 'Reply', 'aria-label': 'Reply', maxLength: LIM.text, value: drafts[c.id] || '', oninput: () => { drafts[c.id] = ta.value; } });
    const nm = prefs.name ? null : h('input', { type: 'text', placeholder: 'Your name', maxLength: LIM.author, 'aria-label': 'Your name' });
    const post = (text, clearDraft) => { // add a reply. Without a saved name it asks for one first. Returns false then.
      if (nm && !nm.value.trim()) { nm.focus(); return false; }
      if (nm) { prefs.name = nm.value.trim(); write(PKEY, prefs); updateName(); }
      if (clearDraft) delete drafts[c.id];
      run({ t: 'reply', cid: c.id, r: cleanReply({ id: newId(), author: prefs.name, text }) });
      return true;
    };
    const reply = () => {
      const text = ta.value.trim();
      if (nm && !nm.value.trim()) return nm.focus();
      if (!text) return ta.focus();
      post(text, true);
    };
    const stale = isStale(c);
    const actions = stale ? h('div', { className: 'cmt-row' }, h('em', { className: 'cmt-tag cmt-from' }, 'from ' + c.appVersion),
      ...(ro || c.replies.some(r => r.text.startsWith(stillText())) ? [] : [
        btn('Still an issue', () => { post(stillText()); }, 'cmt-sm cmt-still'),
        btn('Resolved in ' + metaVer(), () => { if (post('Resolved in ' + metaVer())) run({ t: 'resolve', cid: c.id, resolved: true, author: prefs.name }); }, 'cmt-sm cmt-fixed')])) : '';
    ta.onkeydown = enterSaves(reply);
    const head = h('div', { className: 'cmt-row' }, h('b', {}, 'Comment ' + nums[c.id]), h('span', { className: 'cmt-meta cmt-sp' }, (ro ? 'from ' + c.version + ' · ' : '') + (c.resolved ? 'Resolved' : 'Open')),
      ro ? '' : btn(c.resolved ? 'Reopen' : 'Resolve', () => run({ t: 'resolve', cid: c.id, resolved: !c.resolved, author: prefs.name }), 'cmt-sm'),
      btn('Link', e => copyText(e.currentTarget, linkOf(c)), 'cmt-sm'), btn('Close', () => closePop(), 'cmt-sm'));
    const kids = [head, actions, entry(c, null, ro), ...c.replies.map(r => entry(c, r, ro))].filter(Boolean);
    if (!ro) { const [emo, pal] = emojiBtn(ta); kids.push(...[nm, ta].filter(Boolean), h('div', { className: 'cmt-row' }, btn('Reply', reply, 'cmt-primary'), emo), pal); }
    const old = pop && pop.dataset.thread === c.id ? pop.scrollTop : 0;
    setPop('Comment ' + nums[c.id], kids, () => pt(c)).dataset.thread = c.id;
    pop.scrollTop = old;
    if (had) pop.focus({ preventScroll: true });
  }

  // ---- Render ----
  function numbering() { // pin numbers are the stored n: stable across deletes
    const out = {};
    items.forEach(c => { out[c.id] = c.n; });
    return out;
  }
  const inView = c => viewVer === 'all' || c.version === viewVer;
  const screenRank = id => { const i = screens.indexOf(screenOf(id)); return i < 0 ? 999 : i; };
  function ordered() { // comments shown in the panel, also used by [ and ]
    return items.filter(c => inView(c) && (filter === 'all' || (filter === 'resolved') === c.resolved))
      .sort((a, b) => screenRank(a.screen) - screenRank(b.screen) || (a.screen < b.screen ? -1 : a.screen > b.screen ? 1 : 0) || (a.version < b.version ? -1 : a.version > b.version ? 1 : 0) || nums[a.id] - nums[b.id]);
  }
  function placePins() {
    for (const p of pinsBox.children) { const q = pt(p._c); if (q) { p.style.left = q.x + 'px'; p.style.top = q.y + 'px'; p.hidden = !!q.out; } }
  }
  const pinItems = () => items.filter(c => c.screen === screen && c.version === (viewVer === 'all' ? VERSION : viewVer) && pt(c));
  function render() {
    nums = numbering();
    const pins = pinItems();
    pinKey = pins.map(c => c.id).join();
    if (FRAME) tabs.querySelectorAll('.cmt-tab').forEach(t => t.setAttribute('aria-selected', !!screenOf(screen) && screenOf(screen).id === t.dataset.id));
    pinsBox.replaceChildren(...pins.map(c => {
      const p = h('button', { type: 'button', className: 'cmt-pin' + (c.resolved ? ' cmt-done' : '') + (c.version !== VERSION ? ' cmt-old' : '') + (isStale(c) ? ' cmt-stale' : ''),
        'aria-label': 'Comment ' + nums[c.id] + ', ' + (c.resolved ? 'resolved' : 'open'), onclick: () => jump(c) }, String(nums[c.id]));
      p._c = c;
      return p;
    }));
    placePins();
    const vers = [...new Set(items.map(c => c.version).concat(VERSION))];
    verSel.replaceChildren(...vers.map(v => h('option', { value: v, selected: v === viewVer }, v + (v === VERSION ? ' (current)' : ''))), h('option', { value: 'all', selected: viewVer === 'all' }, 'All versions'));
    const base = items.filter(inView);
    filterBtns.forEach((b, i) => {
      const f = ['open', 'resolved', 'all'][i], n = f === 'all' ? base.length : base.filter(c => (f === 'resolved') === c.resolved).length;
      b.textContent = f[0].toUpperCase() + f.slice(1) + ' (' + n + ')';
      b.setAttribute('aria-pressed', filter === f);
    });
    const rows = ordered(), groups = [];   // one group per screen and version: the list order is the group order, and the numbers count inside a group
    for (const c of rows) {
      if (!groups.length || groups[groups.length - 1].key !== grp(c)) groups.push({ key: grp(c), c, kids: [] });
      groups[groups.length - 1].kids.push(item(c));
    }
    list.replaceChildren(...groups.map(g => h('div', { className: 'cmt-grp' },
      h('h3', { className: 'cmt-gh', title: screenTitle(g.c.screen) }, screenTitle(g.c.screen) + (viewVer === 'all' || g.c.version !== VERSION ? ' · ' + g.c.version : '')), ...g.kids)));
    if (!rows.length) list.append(h('p', { className: 'cmt-empty' }, 'No ' + (filter === 'all' ? '' : filter + ' ') + 'comments here.'));
    bCount.textContent = 'Comments (' + items.filter(c => c.version === VERSION).length + ')';
    const fresh = items.reduce((n, c) => n + [c, ...c.replies].filter(x => x.author !== prefs.name && x.createdAt > lastSeen).length, 0);
    newBadge.hidden = !fresh || panel.classList.contains('cmt-open');
    newBadge.textContent = fresh + ' new';
    fit();
    if (openId) renderPop();
    updateStatus();
    updateFrameUi();
  }
  function item(c) { // one panel row: a jump button (where, text clamped to 3 lines, meta line) and a Show more toggle for long text
    const more = btn('', () => { if (!expanded.delete(c.id)) expanded.add(c.id); it.classList.toggle('cmt-exp'); fit(); }, 'cmt-sm cmt-more');
    const it = h('div', { className: 'cmt-item' + (c.resolved ? ' cmt-done' : '') + (c.id === openId ? ' cmt-cur' : '') + (expanded.has(c.id) ? ' cmt-exp' : '') },
      h('button', { type: 'button', className: 'cmt-ijump', onclick: () => jump(c) },
        h('span', { className: 'cmt-ihead' }, h('b', {}, '#' + nums[c.id]), h('span', { className: 'cmt-where', title: c.where }, c.where)),
        h('span', { className: 'cmt-itext' }, c.text),
        h('span', { className: 'cmt-imeta' }, h('span', { className: 'cmt-au' }, c.author), when(c.createdAt),
          c.reactions ? h('span', { className: 'cmt-rx', title: REACTIONS.filter(e => c.reactions[e]).map(e => e + ' ' + c.reactions[e].join(', ')).join('\n') }, REACTIONS.filter(e => c.reactions[e]).map(e => e + c.reactions[e].length).join(' ')) : '',
          c.replies.length ? h('em', { className: 'cmt-tag' }, c.replies.length + (c.replies.length === 1 ? ' reply' : ' replies')) : '',
          c.resolved ? h('em', { className: 'cmt-tag' }, 'Resolved') : '',
          c.version !== VERSION ? h('em', { className: 'cmt-tag' }, 'from ' + c.version) : '',
          isStale(c) ? h('em', { className: 'cmt-tag cmt-from' }, 'from ' + c.appVersion) : '')),
      more);
    more.hidden = true;
    return it;
  }
  function fit() { // show "Show more" only when the 3-line clamp cuts text. Needs a visible panel to measure
    if (!panel.classList.contains('cmt-open')) return;
    list.querySelectorAll('.cmt-item').forEach(it => {
      const t = it.querySelector('.cmt-itext'), m = it.querySelector('.cmt-more'), open = it.classList.contains('cmt-exp');
      m.hidden = !open && t.scrollHeight <= t.clientHeight + 1;
      m.textContent = open ? 'Show less' : 'Show more';
    });
  }
  const linkOf = c => location.origin + location.pathname + location.search + '#comment=' + c.id;
  function openFromHash() { // a link ending in #comment=<id> opens that comment once. In shared mode the comment may arrive with the first sync
    const c = LINK && items.find(i => i.id === LINK);
    if (hashDone || !ready || !c) return;
    hashDone = true;
    jump(c);
  }
  function jump(c) {
    if (c.screen !== screen) { if (FRAME) pendingJump = c.id; showScreen(c.screen); }
    if (innerWidth <= 600) togglePanel(false);
    if (FRAME) scrollToAnchor(c);
    else { const p = pt(c); if (p) scrollTo(0, Math.max(0, p.y - innerHeight / 2)); }
    openId = c.id;
    editing = null;
    render();
    if (pop) pop.focus({ preventScroll: true });
  }
  function step(dir) {
    const rows = ordered();
    if (!rows.length) return;
    const i = rows.findIndex(c => c.id === openId);
    jump(rows[(i < 0 ? (dir > 0 ? 0 : rows.length - 1) : i + dir + rows.length) % rows.length]);
  }

  // ---- Export and import ----
  const rxText = o => { const p = REACTIONS.filter(e => o.reactions && o.reactions[e]).map(e => e + ' ' + o.reactions[e].length); return p.length ? ' [' + p.join(' · ') + ']' : ''; };
  function exportMarkdown() {
    nums = numbering();
    const ids = [...new Set(screens.map(s => s.id).concat(items.map(c => c.screen)))];
    const out = ['# ' + TITLE + ': review comments', ''];
    for (const id of ids) {
      const rows = items.filter(c => c.screen === id).sort((a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : a.id < b.id ? -1 : 1));
      if (!rows.length) continue;
      out.push('## ' + screenTitle(id));
      for (const c of rows) {
        out.push('- [' + (c.resolved ? 'x' : ' ') + '] **#' + nums[c.id] + '** ' + (c.version !== VERSION ? '(from ' + c.version + ') ' : '') + '[' + c.where + '] ' + c.author + ': ' + oneLine(c.text) + rxText(c));
        c.replies.forEach(r => out.push('  - ' + r.author + ': ' + oneLine(r.text) + rxText(r)));
      }
      out.push('');
    }
    return out.join('\n').trim() + '\n';
  }
  const exportJSON = () => ({ project: PROJECT, title: TITLE, version: VERSION, exportedAt: new Date().toISOString(), comments: JSON.parse(JSON.stringify(items)) });
  function importJSON(obj) { // merge by id: add new comments, add missing replies to known ones
    const inc = Array.isArray(obj) ? obj : obj && obj.comments;
    if (!Array.isArray(inc)) return 0;
    let n = 0;
    for (const c of inc.map(clean).filter(Boolean)) {
      const have = items.find(i => i.id === c.id);
      if (!have) { run({ t: 'add', c }); n++; continue; }
      for (const r of c.replies) if (!have.replies.some(x => x.id === r.id)) { run({ t: 'reply', cid: have.id, r }); n++; }
    }
    return n;
  }
  function importFile(input) {
    const f = input.files && input.files[0];
    if (!f) return;
    f.text().then(t => { const n = importJSON(JSON.parse(t)); note.textContent = 'Imported ' + n + ' new item' + (n === 1 ? '' : 's') + '.'; })
      .catch(() => { note.textContent = 'Import failed: the file is not valid comment JSON.'; })
      .then(() => { input.value = ''; });
  }
  function copyText(b, t) { // copy t, then show Copied on button b for 2 seconds
    const label = b.textContent;
    let ok = true;
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).catch(() => {
      const ta = h('textarea', { value: t });
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.append(ta);
      ta.select();
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
    }).then(() => { b.textContent = ok ? 'Copied' : 'Copy failed'; setTimeout(() => { b.textContent = label; }, 2000); });
  }
  function downloadJSON() {
    const a = h('a', { href: URL.createObjectURL(new Blob([JSON.stringify(exportJSON(), null, 2)], { type: 'application/json' })), download: PROJECT + '-comments.json' });
    root.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ---- What a click can anchor to ----
  // A pointer over an element anchors to that element. A pointer over empty space does not anchor to html, body or a container that fills half the viewport.
  // It anchors to the nearest element inside that container, and the pin sits on that element's edge. A pin on an element follows it on resize and scroll.
  const SOLID = new Set(['img', 'svg', 'video', 'canvas', 'iframe', 'input', 'textarea', 'select', 'button', 'a', 'table']);   // never empty space, however big
  const gap = (r, x, y) => Math.hypot(Math.max(r.left - x, 0, x - r.right), Math.max(r.top - y, 0, y - r.bottom));
  function overText(el, x, y) { // is the point on text that sits directly in el?
    for (const n of el.childNodes) {
      if (n.nodeType !== 3 || !n.nodeValue.trim()) continue;
      const rg = el.ownerDocument.createRange();
      rg.selectNodeContents(n);
      for (const r of rg.getClientRects()) if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return true;
    }
    return false;
  }
  function isEmpty(el, x, y) {
    const d = el.ownerDocument, w = d.defaultView;
    if (el === d.documentElement || el === d.body) return true;
    if (SOLID.has(el.localName)) return false;
    const r = el.getBoundingClientRect();
    const seen = Math.max(0, Math.min(r.right, w.innerWidth) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, w.innerHeight) - Math.max(r.top, 0));
    return seen >= 0.5 * w.innerWidth * w.innerHeight && !overText(el, x, y);
  }
  function pick(t, x, y) { // t: the element under the pointer. x, y: client coordinates in its document. Returns the element to outline and to anchor to
    let el = t;
    for (let i = 0; i < 20 && isEmpty(el, x, y); i++) {
      const kids = [...el.children].filter(k => { const r = k.getBoundingClientRect(); return !k.closest('.cmt-ui') && r.width > 0 && r.height > 0; });
      if (!kids.length) break;
      el = kids.reduce((a, k) => (gap(k.getBoundingClientRect(), x, y) < gap(a.getBoundingClientRect(), x, y) ? k : a));
    }
    return el;
  }
  // In frame mode the wrapper page around the iframe is not part of the app: it never gets an outline or a comment.
  const commentable = t => isEl(t) && t !== frameEl && !t.closest('.cmt-ui') && !(FRAME && t.ownerDocument === document);
  let hov = null;
  function hideHl() { hov = null; hl.style.display = 'none'; }
  function drawHover() {
    const v = hov;
    hov = null;
    if (!v) return;
    if (prefs.mode !== 'comment' || !commentable(v.t)) return hideHl();
    const o = offset(), r = pick(v.t, v.x, v.y).getBoundingClientRect();
    Object.assign(hl.style, { display: 'block', left: o.left + r.left + scrollX + 'px', top: o.top + r.top + scrollY + 'px', width: r.width + 'px', height: r.height + 'px' });
  }
  // The outline follows mousemove, not mouseover: over empty space the nearest element changes while the target stays the same.
  function onMove(e) { if (!hov) requestAnimationFrame(drawHover); hov = { t: e.target, x: e.clientX, y: e.clientY }; }
  const onOut = e => { if (!e.relatedTarget) hideHl(); };   // the pointer left the window or the iframe
  function onClick(e) {
    if (synth) return;   // a click that the prefill sends is the app's, not a comment
    const t = e.target;
    if (!isEl(t) || t.closest('.cmt-ui') || (FRAME && t.ownerDocument === document)) return;
    if (prefs.mode !== 'comment') { closePop(); return; }
    e.preventDefault(); e.stopPropagation();
    if (FRAME) e.stopImmediatePropagation();
    if (!mode) return; // storage mode not detected yet
    showHint = false; updateNote();
    const o = offset(), r0 = t.getBoundingClientRect();
    const cx = e.detail ? e.clientX : r0.left + r0.width / 2, cy = e.detail ? e.clientY : r0.top + r0.height / 2; // keyboard clicks have no position
    const el = pick(t, cx, cy), r = el.getBoundingClientRect();
    const frac = (v, size) => (size ? Math.min(1, Math.max(0, v / size)) : 0);
    composer({ path: pathOf(el), fx: frac(cx - r.left, r.width), fy: frac(cy - r.top, r.height), pageX: o.left + cx + scrollX, pageY: o.top + cy + scrollY, where: whereOf(el) });
  }
  function onSubmit(e) { if (prefs.mode === 'comment' && !e.target.closest('.cmt-ui')) e.preventDefault(); }
  function onKey(e) {
    if (e.key === 'Escape') { if (pop) closePop(); else togglePanel(false); return; }
    if (isEl(e.target) && e.target.closest('input,textarea,select,[contenteditable]')) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'c' || e.key === 'C') setMode(prefs.mode === 'comment' ? 'browse' : 'comment');
    else if (e.key === '[' || e.key === ']') step(e.key === ']' ? 1 : -1);
  }
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('mouseout', onOut, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('submit', onSubmit, true);
  document.addEventListener('keydown', onKey);
  const relayout = () => {
    if (FRAME && pinItems().map(c => c.id).join() !== pinKey) render(); // an anchor appeared or vanished in the app
    else placePins();
    placePop();
    if (FRAME) markHl();
  };
  addEventListener('resize', () => { relayout(); fit(); });
  addEventListener('load', relayout);
  addEventListener('hashchange', () => showScreen(hashScreen()));
  if (window.ResizeObserver) new ResizeObserver(relayout).observe(document.body);

  // ---- Start ----
  root.append(hl, pinsBox, panel, bar);
  if (FRAME) root.append(toastEl);
  document.documentElement.append(root);
  if (screens.length > 1) document.body.prepend(tabs);
  if (FRAME) {
    frameEl.before(screenNote);   // after the tab bar, before the app
    applyViewport();
    frameEl.addEventListener('load', onFrameLoad);
    setInterval(syncFrame, 500);
    render();
    Promise.resolve(window.cmtFrameReady).catch(() => {}).then(() => { gotoFrame(screens.length ? screen : 'path:/'); ready = true; openFromHash(); });   // after the optional auto-login
  } else if (screens.length) showScreen(screen); else render();
  updateName();
  openFromHash();
  syncMode();
  window.cmt = { exportMarkdown, exportJSON, importJSON, mode: () => mode || 'local', screen: () => screen, meta: () => meta, viewport: () => (FRAME ? curViewport() : 'desktop') };
  detect();
  if (FRAME) { loadMeta(); setInterval(loadMeta, 60000); addEventListener('focus', loadMeta); }
})();
