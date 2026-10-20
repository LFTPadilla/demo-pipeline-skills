// Review comments API for a single-file prototype, plus static assets.
// Config docs: https://developers.cloudflare.com/workers/static-assets/routing/worker-script/
//   (assets.run_worker_first takes route patterns, so only /api/* reaches this Worker).
// Binding: KV namespace COMMENTS. Secret: REVIEW_KEY.
//
// ceiling: one KV key per project holds all comments as one JSON array.
//   Each write reads the array, changes it and writes it back, so two writes in the same
//   second can overwrite each other (last write wins). KV allows 1 write per second per key
//   and can serve reads that are up to 60 s old from other locations.
//   This is fine for a few reviewers and 500 comments. Use a Durable Object or D1 beyond that.

const MAX = { text: 4000, author: 80, where: 200, path: 1000, screen: 64, version: 20, comments: 500, replies: 100, body: 200000, reactors: 50 };
const REACTIONS = ['👍', '✅', '👀', '❤️', '🎉', '❓'];   // the same fixed set as the layer and the proxy
const ID = /^[a-z0-9]{4,40}$/;
const PROJECT = /^[a-z0-9][a-z0-9-]{0,63}$/;
const COMMENT_FIELDS = ['project', 'type', 'id', 'n', 'screen', 'version', 'path', 'fx', 'fy', 'pageX', 'pageY', 'where', 'text', 'author', 'createdAt', 'resolved', 'replies', 'reactions'];
const REPLY_FIELDS = ['project', 'type', 'commentId', 'id', 'text', 'author', 'createdAt', 'reactions'];
const PATCH_FIELDS = ['project', 'id', 'replyId', 'text', 'resolved', 'author', 'react', 'on'];
const DELETE_PARAMS = ['project', 'id', 'replyId', 'author'];

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const fail = (status, message) => new HttpError(status, message);
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== '/api/comments') {
      if (url.pathname.startsWith('/api/')) return json({ error: 'not found' }, 404);
      return env.ASSETS ? env.ASSETS.fetch(request) : new Response('Not found', { status: 404 });
    }
    try {
      return await handle(request, url, env);
    } catch (e) {
      if (e instanceof HttpError) return json({ error: e.message }, e.status);
      return json({ error: 'server error' }, 500);
    }
  },
};

async function handle(request, url, env) {
  const method = request.method;
  if (!['GET', 'POST', 'PATCH', 'DELETE'].includes(method)) throw fail(405, 'method not allowed');
  if (method !== 'GET' || env.REVIEW_KEY) requireKey(request, env);

  let body = null;
  if (method === 'POST' || method === 'PATCH') body = await readBody(request);
  else exact([...url.searchParams.keys()], method === 'GET' ? ['project'] : DELETE_PARAMS);
  const project = body ? body.project : url.searchParams.get('project');
  if (typeof project !== 'string' || !PROJECT.test(project)) throw fail(400, 'invalid project');

  const key = 'p:' + project;
  const list = (await env.COMMENTS.get(key, 'json')) || [];
  const hw = (await env.COMMENTS.get('n:' + project, 'json')) || {};   // highest number ever used per screen and version
  backfill(list, hw);
  if (method === 'GET') return json({ comments: list });

  let status = 200;
  if (method === 'POST') { create(list, body, hw); status = 201; }
  else if (method === 'PATCH') edit(list, body);
  else remove(list, Object.fromEntries(url.searchParams));
  await env.COMMENTS.put(key, JSON.stringify(list));
  await env.COMMENTS.put('n:' + project, JSON.stringify(hw));
  return json({ ok: true }, status);
}

// Writes need the key. A missing REVIEW_KEY secret blocks all writes (fail closed).
function requireKey(request, env) {
  const given = request.headers.get('x-review-key') || '';
  const want = env.REVIEW_KEY || '';
  let diff = given.length ^ want.length;
  for (let i = 0; i < want.length; i++) diff |= (given.charCodeAt(i) || 0) ^ want.charCodeAt(i);
  if (!want || !given || diff !== 0) throw fail(401, 'unauthorized');
}

async function readBody(request) {
  const text = await request.text();
  if (text.length > MAX.body) throw fail(413, 'payload too large');
  let data;
  try { data = JSON.parse(text); } catch { throw fail(400, 'invalid JSON'); }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw fail(400, 'body must be an object');
  return data;
}

function exact(keys, allowed) {
  const extra = keys.filter((k) => !allowed.includes(k));
  if (extra.length) throw fail(400, 'unknown field: ' + extra[0]);
}
function text(v, name, max, required = true) {
  if (v === undefined && !required) return undefined;
  if (typeof v !== 'string') throw fail(400, name + ' must be a string');
  if (required && !v.trim()) throw fail(400, name + ' is required');
  if (v.length > max) throw fail(400, name + ' is longer than ' + max + ' characters');
  return v;
}
function num(v, name) {
  if (v === undefined) return 0;
  if (typeof v !== 'number' || !Number.isFinite(v)) throw fail(400, name + ' must be a number');
  return v;
}
function id(v, name) {
  if (typeof v !== 'string' || !ID.test(v)) throw fail(400, name + ' must be 4 to 40 lowercase letters or digits');
  return v;
}
function when(v) {
  if (v === undefined) return new Date().toISOString();
  if (typeof v !== 'string' || Number.isNaN(Date.parse(v))) throw fail(400, 'createdAt must be an ISO date');
  return new Date(v).toISOString();
}
// { emoji: [names] } from a client. Only the fixed set, at most 50 unique names of up to 80 characters.
function reactions(v) {
  if (v === undefined) return undefined;
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw fail(400, 'reactions must be an object');
  const out = {};
  for (const [e, names] of Object.entries(v)) {
    if (!REACTIONS.includes(e)) throw fail(400, 'reaction not allowed: ' + e);
    if (!Array.isArray(names) || names.length > MAX.reactors) throw fail(400, 'reactions must be a list of up to ' + MAX.reactors + ' names');
    const uniq = [...new Set(names.map((n) => text(n, 'reaction name', MAX.author)))];
    if (uniq.length) out[e] = uniq;
  }
  return Object.keys(out).length ? out : undefined;
}
// "on" is the wanted state, so a repeated request changes nothing.
function react(item, b) {
  const who = text(b.author, 'author', MAX.author);
  if (typeof b.react !== 'string' || !REACTIONS.includes(b.react)) throw fail(400, 'reaction not allowed');
  if (typeof b.on !== 'boolean') throw fail(400, 'on must be true or false');
  let names = (item.reactions || {})[b.react] || [];
  if (b.on && !names.includes(who)) {
    if (names.length >= MAX.reactors) throw fail(422, 'reaction limit reached');
    names = [...names, who];
  } else if (!b.on) names = names.filter((n) => n !== who);
  const rx = Object.fromEntries(Object.entries({ ...(item.reactions || {}), [b.react]: names }).filter(([, v]) => v.length));
  if (Object.keys(rx).length) item.reactions = rx; else delete item.reactions;
}
function reply(r) {
  if (!r || typeof r !== 'object') throw fail(400, 'reply must be an object');
  exact(Object.keys(r), REPLY_FIELDS.filter((k) => !['project', 'type', 'commentId'].includes(k)));
  const rx = reactions(r.reactions);
  return { id: id(r.id, 'reply id'), author: text(r.author, 'author', MAX.author), text: text(r.text, 'text', MAX.text), createdAt: when(r.createdAt), ...(rx ? { reactions: rx } : {}) };
}

function create(list, b, hw) {
  if (b.type === 'reply') {
    exact(Object.keys(b), REPLY_FIELDS);
    const c = find(list, id(b.commentId, 'commentId'));
    const r = reply({ id: b.id, author: b.author, text: b.text, createdAt: b.createdAt, reactions: b.reactions });
    if (c.replies.length >= MAX.replies) throw fail(422, 'reply limit reached');
    if (c.replies.some((x) => x.id === r.id)) throw fail(409, 'reply exists');
    c.replies.push(r);
    return;
  }
  if (b.type !== 'comment') throw fail(400, 'type must be comment or reply');
  exact(Object.keys(b), COMMENT_FIELDS);
  if (list.length >= MAX.comments) throw fail(422, 'comment limit reached');
  if (b.replies !== undefined && (!Array.isArray(b.replies) || b.replies.length > MAX.replies)) throw fail(400, 'invalid replies');
  if (b.n !== undefined) num(b.n, 'n'); // accepted and ignored: the server numbers comments
  const c = {
    id: id(b.id, 'id'),
    screen: text(b.screen ?? 'main', 'screen', MAX.screen),
    version: text(b.version ?? 'v1', 'version', MAX.version),
    path: text(b.path ?? '', 'path', MAX.path, false) ?? '',
    fx: num(b.fx, 'fx'), fy: num(b.fy, 'fy'), pageX: num(b.pageX, 'pageX'), pageY: num(b.pageY, 'pageY'),
    where: text(b.where ?? '', 'where', MAX.where, false) ?? '',
    text: text(b.text, 'text', MAX.text),
    author: text(b.author, 'author', MAX.author),
    createdAt: when(b.createdAt),
    resolved: b.resolved === true,
    replies: (b.replies || []).map(reply),
  };
  const rx = reactions(b.reactions);
  if (rx) c.reactions = rx;
  if (list.some((x) => x.id === c.id)) throw fail(409, 'comment exists');
  c.n = hw[group(c)] = nextN(list, c, hw);
  list.push(c);
}

// Pin numbers are stable: the server sets n once and never changes it. The next number is the highest one ever used
// for the same screen and version (hw), plus 1, so a delete leaves a gap and a number is never used twice.
// Old comments without n get the next free number once, in creation order.
const group = (c) => c.screen + '|' + c.version;
function nextN(list, c, hw) {
  return Math.max(hw[group(c)] || 0, list.reduce((m, x) => (group(x) === group(c) && x.n > m ? x.n : m), 0)) + 1;
}
function backfill(list, hw) {
  const byAge = (a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : a.id < b.id ? -1 : 1);
  [...list].sort(byAge).forEach((c) => { if (!c.n) c.n = hw[group(c)] = nextN(list, c, hw); });
}

function find(list, cid) {
  const c = list.find((x) => x.id === cid);
  if (!c) throw fail(404, 'comment not found');
  return c;
}
// Only the author can change or delete text. Authors are names that people type, so this stops mistakes, not attackers.
function target(list, b) {
  const c = find(list, id(b.id, 'id'));
  if (b.replyId === undefined) return { c, item: c };
  const r = c.replies.find((x) => x.id === b.replyId);
  if (!r) throw fail(404, 'reply not found');
  return { c, item: r };
}
function edit(list, b) {
  exact(Object.keys(b), PATCH_FIELDS);
  const { c, item } = target(list, b);
  if (b.react !== undefined) {
    if (b.text !== undefined || b.resolved !== undefined) throw fail(400, 'react cannot go with text or resolved');
    return react(item, b);
  }
  if (b.text === undefined && b.resolved === undefined) throw fail(400, 'nothing to change');
  if (b.text !== undefined) {
    if (item.author !== text(b.author, 'author', MAX.author)) throw fail(403, 'only the author can edit');
    item.text = text(b.text, 'text', MAX.text);
  }
  if (b.resolved !== undefined) {
    if (typeof b.resolved !== 'boolean' || item !== c) throw fail(400, 'resolved must be a boolean on a comment');
    c.resolved = b.resolved;
  }
}
function remove(list, q) {
  const { c, item } = target(list, q);
  if (item.author !== (q.author || '')) throw fail(403, 'only the author can delete');
  if (item === c) list.splice(list.indexOf(c), 1);
  else c.replies.splice(c.replies.indexOf(item), 1);
}
