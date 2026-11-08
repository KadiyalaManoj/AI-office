// Checks the local window.claude shim: model calls through /api/sample and a Firestore-like store in localStorage.
// Run: node tests/page/local_shim.test.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const code = fs.readFileSync(path.join(__dirname, "../../office/local-shim.js"), "utf8");
let fails = 0;
const ok = (c, m) => { if (!c) { fails++; console.log("FAIL", m); } };
const tick = () => new Promise((r) => setTimeout(r, 5));

function load(fetchImpl, saved) {
  const banner = { hidden: true, textContent: "" };
  const ls = { data: saved ? { "ai-office-local-db": saved } : {}, getItem(k) { return this.data[k] ?? null; }, setItem(k, v) { this.data[k] = v; } };
  const sandbox = { window: {}, localStorage: ls, document: { getElementById: (id) => (id === "banner" ? banner : null) },
    fetch: fetchImpl, setTimeout, JSON, Object, Promise, Map };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return { claude: sandbox.window.claude, banner, ls };
}

const reply = (status, body) => async () => ({ ok: status < 300, status, json: async () => body });

(async () => {
  // model calls
  let sent;
  let { claude, banner } = load(async (url, init) => { sent = { url, body: JSON.parse(init.body) }; return { ok: true, status: 200, json: async () => ({ text: "hi", json: { kind: "bug" }, truncated: false }) }; });
  const sample = await claude.use("sample");
  let streamed = null;
  const r = await sample("question", { modelTier: "quick", onText: (t) => { streamed = t.text; } });
  ok(r.text === "hi" && r.truncated === false && streamed === "hi", "sample returns text and streams once");
  ok(sent.url === "/api/sample" && sent.body.prompt === "question" && sent.body.tier === "quick" && sent.body.json === false, "sample request body");
  await sample([{ role: "user", content: "a" }], {});
  ok(Array.isArray(sent.body.messages) && sent.body.prompt === undefined, "chat turns are sent as messages");
  const j = await sample.json("plan", { modelTier: "default" });
  ok(j.kind === "bug" && sent.body.json === true, "sample.json returns the parsed object");

  // errors the page understands
  ({ claude, banner } = load(reply(502, { error: { code: "provider_error", message: "Ollama isn't running" } })));
  let e1 = null; try { await (await claude.use("sample"))("x", {}); } catch (e) { e1 = e; }
  ok(e1 && e1.code === "provider_error" && banner.hidden === false && banner.textContent.includes("Ollama"), "provider error shows a banner");
  ({ claude } = load(reply(502, { error: { code: "invalid_json", message: "x" } })));
  let e2 = null; try { await (await claude.use("sample")).json("x", {}); } catch (e) { e2 = e; }
  ok(e2 && e2.code === "invalid_json", "invalid json code is kept");
  ({ claude } = load(async () => { const e = new Error("abort"); e.name = "AbortError"; throw e; }));
  let e3 = null; try { await (await claude.use("sample"))("x", {}); } catch (e) { e3 = e; }
  ok(e3 && e3.code === "cancelled", "abort maps to cancelled");
  ({ claude, banner } = load(async () => { throw new Error("down"); }));
  let e4 = null; try { await (await claude.use("sample"))("x", {}); } catch (e) { e4 = e; }
  ok(e4 && e4.code === "unavailable" && banner.hidden === false, "unreachable server is reported");

  // a good answer clears the banner the shim itself raised
  let n = 0;
  ({ claude, banner } = load(async () => (n++ ? { ok: true, status: 200, json: async () => ({ text: "ok" }) } : { ok: false, status: 502, json: async () => ({ error: { code: "provider_error", message: "boom" } }) })));
  const s2 = await claude.use("sample");
  try { await s2("x", {}); } catch (_) {}
  await s2("x", {});
  ok(banner.hidden === true, "banner clears after a good answer");

  // outside actions: the stand-in connector runs jobs through the local server
  let e5 = null; try { await claude.use("other"); } catch (e) { e5 = e; }
  ok(e5 && e5.code === "unavailable", "unknown services are unavailable");
  let calls = [];
  ({ claude } = load(async (url, init) => {
    const body = JSON.parse(init.body); calls.push({ url, body });
    if (url === "/api/ship") return body.repo === "bad/repo"
      ? { ok: false, status: 400, json: async () => ({ error: { code: "integration_error", message: "not an allowed repository" } }) }
      : { ok: true, status: 200, json: async () => ({ prUrl: "https://github.com/me/repo/pull/3", branch: "office/x", files: ["a.py"] }) };
    return { ok: true, status: 200, json: async () => ({ to: "me@x.com" }) };
  }));
  const jdb = await claude.use("db"), mcp = await claude.use("mcp");
  const tool = (text) => mcp.callTool("Claude Code Remote", "fire_trigger", { trigger_id: "t", text });
  await jdb.collection("tasks").doc("t1").set({ outputs: [
    { agent: "nova", text: "spec" }, { agent: "byte", text: "File: a.py\n```python\nx=1\n```" }, null,
    { agent: "terra", text: "File: m.tf\n```hcl\n```" }] });
  await jdb.collection("ship").doc("s1").set({ taskId: "t1", title: "Calc", repo: "me/repo", status: "queued" });
  ok((await tool("New ship job: s1")).ok === true, "fire_trigger returns at once");
  await tick(); await tick();
  const s1 = (await jdb.collection("ship").doc("s1").get()).data();
  ok(s1.status === "done" && s1.prUrl === "https://github.com/me/repo/pull/3", "ship job completes with the PR link " + JSON.stringify(s1));
  const shipCall = calls.find((c) => c.url === "/api/ship").body;
  ok(shipCall.repo === "me/repo" && shipCall.title === "Calc" && shipCall.texts.length === 2 && !shipCall.texts.includes("spec"), "only the coders' replies are sent");

  await jdb.collection("ship").doc("s2").set({ taskId: "t1", title: "Calc", repo: "bad/repo", status: "queued" });
  await tool("New ship job: s2"); await tick(); await tick();
  const shipFail = (await jdb.collection("ship").doc("s2").get()).data();
  ok(shipFail.status === "failed" && shipFail.error.includes("not an allowed"), "refusals show the server's message");

  await jdb.collection("outbox").doc("m1").set({ type: "email", to: "me@x.com", subject: "S", body: "B", status: "queued" });
  await tool("New outbox job: m1"); await tick(); await tick();
  const m1 = (await jdb.collection("outbox").doc("m1").get()).data();
  ok(m1.status === "done" && m1.note === "Sent to me@x.com.", "email job completes " + JSON.stringify(m1));
  ok(calls.some((c) => c.url === "/api/email" && c.body.subject === "S"), "email request sent");

  await jdb.collection("outbox").doc("c1").set({ type: "calendar", status: "queued" });
  await tool("New outbox job: c1"); await tick(); await tick();
  const c1 = (await jdb.collection("outbox").doc("c1").get()).data();
  ok(c1.status === "failed" && c1.error.includes("Calendar"), "calendar says it is not available");

  await jdb.collection("builds").doc("b1").set({ status: "queued" });
  await tool("New build: b1"); await tick(); await tick();
  const b1 = (await jdb.collection("builds").doc("b1").get()).data();
  ok(b1.status === "failed" && b1.error.includes("claude.ai"), "builds are refused with a reason");
  const before = calls.length;
  await tool("Build b1: nudge");
  ok(calls.length === before, "other trigger messages are ignored");

  let slack = null; try { await mcp.callTool("Slack", "slack_send_message", {}); } catch (e) { slack = e; }
  ok(slack && slack.code === "server_not_connected", "Slack is reported as not connected");

  // the store
  ({ claude, ls } = load(reply(200, {})));
  const db = await claude.use("db");
  ok(!(await db.doc("settings/company").get()).exists, "missing doc does not exist");
  await db.doc("settings/company").set({ rate: 85 });
  const got = await db.doc("settings/company").get();
  ok(got.exists && got.data().rate === 85, "doc round trip");
  await db.collection("tasks").doc("a").set({ title: "A", created: 1 });
  await db.collection("tasks").doc("b").set({ title: "B", created: 2 });
  await db.collection("tasks").doc("c").set({ title: "C", created: 3 });
  ok(JSON.parse(ls.data["ai-office-local-db"]).tasks.b.title === "B", "writes are persisted to localStorage");

  const seen = [];
  db.collection("tasks").orderBy("created", "desc").limit(2).onSnapshot((snap) => {
    seen.push(snap.docChanges().map((c) => c.type + ":" + c.doc.id + ":" + c.doc.data().title));
  });
  await tick();
  ok(JSON.stringify(seen[0].sort()) === JSON.stringify(["added:b:B", "added:c:C"]), "query returns newest two, got " + JSON.stringify(seen[0]));
  await db.collection("tasks").doc("c").update({ title: "C2" });
  await tick();
  ok(seen.at(-1).includes("modified:c:C2"), "update emits modified");
  await db.collection("tasks").doc("c").delete();
  await tick();
  ok(seen.at(-1).some((x) => x.startsWith("removed:c")) && seen.at(-1).some((x) => x.startsWith("added:a")), "delete emits removed and refills the window");
  let missing = null; try { await db.collection("tasks").doc("zzz").update({ x: 1 }); } catch (e) { missing = e; }
  ok(missing && missing.code === "not_found", "update of a missing doc fails");

  // saved data survives a reload
  ({ claude } = load(reply(200, {}), ls.data["ai-office-local-db"]));
  const again = await (await claude.use("db")).doc("settings/company").get();
  ok(again.exists && again.data().rate === 85, "data survives a reload");

  console.log(fails ? fails + " failed" : "all shim checks passed");
  process.exit(fails ? 1 : 0);
})();
