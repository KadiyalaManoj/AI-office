/* Local stand-in for window.claude, loaded by `python -m agents.server` before the office page.
 *
 *   use("sample")  ->  model calls, answered by the local server through your chosen provider
 *   use("db")      ->  a small Firestore-like store kept in this browser's localStorage
 *   use("mcp")     ->  runs GitHub pull requests and email through the local server; builds are refused
 *   anything else  ->  unavailable (Slack and Calendar need claude.ai)
 */
(function () {
  "use strict";

  /* ---------- model calls ---------- */
  let shownBanner = false;

  function banner(message) {
    const el = document.getElementById("banner");
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
    shownBanner = true;
  }

  function clearBanner() {
    const el = document.getElementById("banner");
    if (el && shownBanner) el.hidden = true;
    shownBanner = false;
  }

  async function post(prompt, opts, wantJson) {
    const body = { tier: (opts && opts.modelTier) || "default", json: !!wantJson };
    if (typeof prompt === "string") body.prompt = prompt;
    else body.messages = prompt;
    let res;
    try {
      res = await fetch("/api/sample", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: opts && opts.signal,
      });
    } catch (e) {
      if (e && e.name === "AbortError") throw { code: "cancelled" };
      banner("The local office server isn't reachable. Is `python -m agents.server` still running?");
      throw { code: "unavailable" };
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const e = data.error || {};
      const code = e.code === "rate_limited" || e.code === "invalid_json" ? e.code : "provider_error";
      if (code === "provider_error" || code === "rate_limited") banner(e.message || "The model failed.");
      throw { code, message: e.message };
    }
    clearBanner();
    if (opts && typeof opts.onText === "function") opts.onText({ text: data.text || "" });
    return data;
  }

  async function sample(prompt, opts) {
    const data = await post(prompt, opts, false);
    return { text: data.text, truncated: !!data.truncated };
  }
  sample.json = async function (prompt, opts) {
    const data = await post(prompt, opts, true);
    return data.json;
  };

  /* ---------- saved tasks, proposals and settings ---------- */
  const KEY = "ai-office-local-db";
  let store = {};
  try { store = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (_) { store = {}; }
  const watchers = [];

  const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

  function persist() {
    try { localStorage.setItem(KEY, JSON.stringify(store)); }
    catch (_) { throw { code: "quota_exceeded" }; }
  }

  function rows(col, q) {
    let list = Object.keys(store[col] || {}).map((id) => ({ id, data: store[col][id] }));
    if (q && q.field) {
      const dir = q.dir === "desc" ? -1 : 1;
      list.sort((a, b) => (a.data[q.field] > b.data[q.field] ? 1 : a.data[q.field] < b.data[q.field] ? -1 : 0) * dir);
    }
    return q && q.max ? list.slice(0, q.max) : list;
  }

  const docSnap = (id, data) => ({ id, exists: data !== undefined, data: () => clone(data) });

  function emit(w) {
    const now = new Map(rows(w.col, w.q).map((r) => [r.id, JSON.stringify(r.data)]));
    const changes = [];
    now.forEach((json, id) => {
      if (!w.last.has(id)) changes.push({ type: "added", doc: docSnap(id, JSON.parse(json)) });
      else if (w.last.get(id) !== json) changes.push({ type: "modified", doc: docSnap(id, JSON.parse(json)) });
    });
    w.last.forEach((_, id) => {
      if (!now.has(id)) changes.push({ type: "removed", doc: docSnap(id, {}) });
    });
    w.last = now;
    if (changes.length || !w.started) {
      w.started = true;
      w.next({ metadata: { fromCache: false }, docChanges: () => changes });
    }
  }

  function notify(col) {
    setTimeout(() => watchers.filter((w) => w.col === col).forEach(emit), 0);
  }

  function docRef(col, id) {
    return {
      async get() { const d = (store[col] || {})[id]; return docSnap(id, d === undefined ? undefined : d); },
      async set(data) { (store[col] = store[col] || {})[id] = clone(data); persist(); notify(col); },
      async update(data) {
        const cur = (store[col] || {})[id];
        if (cur === undefined) throw { code: "not_found" };
        store[col][id] = Object.assign({}, cur, clone(data)); persist(); notify(col);
      },
      async delete() { if (store[col]) delete store[col][id]; persist(); notify(col); },
    };
  }

  function query(col, q) {
    return {
      orderBy: (field, dir) => query(col, Object.assign({}, q, { field, dir })),
      limit: (max) => query(col, Object.assign({}, q, { max })),
      onSnapshot(next, onError) {
        const w = { col, q, next, last: new Map(), started: false };
        watchers.push(w);
        setTimeout(() => { try { emit(w); } catch (e) { if (onError) onError(e); } }, 0);
        return () => { const i = watchers.indexOf(w); if (i >= 0) watchers.splice(i, 1); };
      },
    };
  }

  const db = {
    doc(path) { const [col, id] = path.split("/"); return docRef(col, id); },
    collection(col) { return Object.assign({ doc: (id) => docRef(col, id) }, query(col, {})); },
  };

  /* ---------- outside actions, done on this machine ---------- */
  // The page queues a job document and then "fires a runner" through the claude.ai connector. Here the
  // connector is this stand-in: it runs the job through the local server and writes the result back.
  const CODERS = ["byte", "pixel", "terra"];

  async function api(path, body) {
    let res;
    try {
      res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    } catch (_) {
      throw new Error("The local office server isn't reachable.");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((data.error && data.error.message) || "The request failed.");
    return data;
  }

  async function finishJob(col, id, work) {
    const ref = db.collection(col).doc(id);
    const snap = await ref.get();
    if (!snap.exists) return;
    const job = snap.data();
    await ref.update({ status: "working" });
    try {
      await ref.update(Object.assign({ status: "done" }, await work(job)));
    } catch (e) {
      await ref.update({ status: "failed", error: String((e && e.message) || e) });
    }
  }

  const jobs = {
    ship: (id) => finishJob("ship", id, async (job) => {
      const task = (store.tasks || {})[job.taskId];
      const texts = ((task && task.outputs) || []).filter((o) => o && CODERS.includes(o.agent)).map((o) => String(o.text));
      const r = await api("/api/ship", { repo: job.repo, title: job.title, texts });
      return { prUrl: r.prUrl, branch: r.branch, note: "" };
    }),
    outbox: (id) => finishJob("outbox", id, async (job) => {
      if (job.type === "email") {
        const r = await api("/api/email", { to: job.to, subject: job.subject, body: job.body });
        return { note: "Sent to " + r.to + "." };
      }
      throw new Error("Calendar invites aren't set up on localhost.");
    }),
    build: (id) => db.collection("builds").doc(id).update({
      status: "failed",
      error: "Building on your computer needs claude.ai. Use a Delivery task here instead.",
    }).catch(() => {}),
  };

  const mcp = {
    async callTool(server, tool, args) {
      if (server === "Claude Code Remote" && tool === "fire_trigger") {
        const text = String((args && args.text) || "");
        const m = /^New (ship|outbox) job: (\S+)/.exec(text) || /^New (build): (\S+)/.exec(text);
        if (m) jobs[m[1]](m[2]);
        return { ok: true };
      }
      throw { code: "server_not_connected" };      // Slack and every other connector
    },
  };

  /* ---------- the window.claude the page expects ---------- */
  window.claude = {
    async use(name) {
      if (name === "sample") return sample;
      if (name === "db") return db;
      if (name === "mcp") return mcp;
      throw { code: "unavailable" };
    },
    __local: { store: () => store },
  };
})();
