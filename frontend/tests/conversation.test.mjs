import test, { beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { activeQueryId, rememberQuery, forgetQuery, fetchConversation } from "../js/ui/conversation.js";
import { initChat } from "../js/ui/chat.js";
import { configureAuthentication } from "../js/api.js";

const KEY = "repomind.activeQueryId";
const answer = { type: "answer", text: "Use `run()` <script>literal</script> [src/main.js:2-3]", found: true,
  confidence: "high", groundedness: 1, citations: [{ path: "src/main.js", start: 2, end: 3 }] };
const events = () => [{ type: "start", query_id: "query-a", mode: "fast" },
  { type: "step", index: 1, thought: "Retrieved <b>source</b>", tool: "search_code", args: { query: "run" }, observation_preview: "literal <img>", tokens_total: 20 }, answer];
const trace = () => ({ query_id: "query-a", repo_id: "repo-a", question: "Where does run start?", query_status: "completed",
  answer: answer.text, steps: events(), tokens_in: 20, tokens_out: 8, latency_ms: 30, cost_usd: 0 });
const done = { type: "done", query_id: "query-a", mode: "fast", status: "completed", usage: { tokens_in: 20, tokens_out: 8, latency_ms: 30, cost_usd: 0 } };
let storage, requests, reply, pageEvents;

class Node {
  constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.queries = {}; this.value = ""; }
  set textContent(text) { this.text = String(text); this.children = []; }
  get textContent() { return (this.text || "") + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_value) { throw Error("Unsafe HTML rendering"); }
  get childElementCount() { return this.children.length; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.text = ""; this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) { return this.queries[selector]; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { width: 700 }; }
  focus() {}
  close() { this.open = false; this.listeners.close?.(); }
  showModal() { this.open = true; }
}
const flatten = (node) => [node, ...node.children.flatMap(flatten)];
const waitFor = async (predicate) => {
  for (let index = 0; index < 30 && !predicate(); index++) await new Promise((resolve) => setImmediate(resolve));
  assert.ok(predicate(), "Expected asynchronous chat state did not arrive");
};

beforeEach(() => {
  storage = new Map(); requests = []; pageEvents = [];
  globalThis.sessionStorage = { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  configureAuthentication({ credentials: async () => ({ accessToken: "fake-access" }), recover: async () => null, expire() {} });
  reply = async (url) => {
    if (url === "/traces/query-a") return Response.json(trace());
    if (url === "/ask") {
      const body = [...events(), done].map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join("");
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    }
    throw Error(`Unexpected request ${url}`);
  };
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return reply(url, options); };
});
afterEach(() => pageEvents.forEach((events) => events.pagehide?.()));

function boot(ready = true) {
  const form = new Node("form"), output = new Node(), steps = new Node(), newChat = new Node("button");
  form.queries = { textarea: new Node("textarea"), "[data-send]": new Node("button"), "[data-stop]": new Node("button"), "[data-character-count]": new Node("p") };
  const dialog = new Node("dialog"); dialog.queries = { h2: new Node("h2"), button: new Node("button"), "[data-citation-content]": new Node("pre") };
  const single = { "[data-citation-dialog]": dialog, "#trace-title": new Node("h2"), "[data-trace-status]": new Node("p"), ".trace-summary-meta": new Node("p") };
  const lists = { "[data-chat-form]": [form], "[data-chat-output]": [output], "[data-trace-content]": [steps], "[data-workspace-state]": [new Node()], "[data-new-chat]": [newChat] };
  globalThis.document = { createElement: (tag) => new Node(tag), querySelector: (selector) => single[selector], querySelectorAll: (selector) => lists[selector] || [] };
  const listeners = {}; pageEvents.push(listeners);
  globalThis.window = { REPOMIND_API_BASE: "", addEventListener: (name, callback) => { listeners[name] = callback; } };
  const chat = initChat();
  const select = (id = "repo-a") => chat.setRepository({ loading: false, error: null, selectedRepo: { id, status: "ready" } });
  if (ready) select();
  return { chat, output, steps, form, select, newChat,
    send() { form.queries.textarea.value = "Where does run start?"; form.listeners.submit({ preventDefault() {} }); } };
}

test("only the active query id is stored, never answer text or usage", () => {
  rememberQuery("query-a"); assert.equal(activeQueryId(), "query-a");
  assert.deepEqual([...storage.entries()], [[KEY, "query-a"]]);
  forgetQuery(); assert.equal(activeQueryId(), null);
});

test("storage failure or a malformed saved id does not throw", () => {
  storage.set(KEY, "../query?token=secret"); assert.equal(activeQueryId(), null);
  for (const key of ["getItem", "setItem", "removeItem"]) sessionStorage[key] = () => { throw Error("Blocked"); };
  assert.doesNotThrow(() => { activeQueryId(); rememberQuery("query-a"); forgetQuery(); });
});

test("trace events restore the answer, citations and trace summary from the API", async () => {
  const run = await fetchConversation("query-a", "repo-a", { request: async (path) => { assert.equal(path, "/traces/query-a"); return trace(); } });
  assert.deepEqual(run.answer, answer); assert.equal(run.question, trace().question);
  assert.equal(run.events[0].thought, "Retrieved <b>source</b>"); assert.equal(run.usage.tokens_in, 20);
});

test("older traces without repo_id use history to check repository membership", async () => {
  const saved = trace(); delete saved.repo_id;
  const paths = [];
  const run = await fetchConversation("query-a", "repo-a", { request: async (path) => {
    paths.push(path); return path.startsWith("/traces/") ? saved : [{ id: "query-a", repo_id: "repo-a" }];
  } });
  assert.equal(run.queryId, "query-a"); assert.equal(paths[1], "/history?repo_id=repo-a&limit=100");
  await assert.rejects(fetchConversation("query-a", "other-repo", { request: async (path) => path.startsWith("/traces/") ? saved : [] }), /not available/);
});

test("mismatched or malformed stored traces fail closed", async () => {
  for (const saved of [{ ...trace(), query_id: "other" }, { ...trace(), repo_id: "other" }, { ...trace(), steps: [] },
    { ...trace(), steps: [events()[0], { ...answer, citations: [{ path: "x", start: -1, end: 3 }] }] }]) {
    await assert.rejects(fetchConversation("query-a", "repo-a", { request: async () => saved }));
  }
});

test("reload restores the current answer through the live safe renderer, without another ask", async () => {
  const first = boot(); first.send(); await waitFor(() => first.output.textContent.includes("Answer complete"));
  assert.equal(storage.get(KEY), "query-a"); assert.equal(storage.size, 1);
  const reloaded = boot(false); assert.equal(requests.length, 1);
  reloaded.select(); await waitFor(() => reloaded.output.textContent.includes("Answer complete"));
  assert.ok(reloaded.output.textContent.includes(trace().question));
  assert.ok(reloaded.output.textContent.includes("<script>literal</script>"));
  assert.ok(flatten(reloaded.output).some((node) => node.tagName === "CODE" && node.textContent === "run()"));
  assert.ok(flatten(reloaded.output).some((node) => node.className === "citation-chip"));
  assert.ok(!flatten(reloaded.output).some((node) => node.tagName === "SCRIPT"));
  assert.ok(reloaded.steps.textContent.includes("Retrieved <b>source</b>"));
  assert.deepEqual(requests.map((request) => request.url), ["/ask", "/traces/query-a"]);
});

test("New chat and the sign-out clear method remove the pointer and all visible conversation state", async () => {
  for (const action of ["new-chat", "sign-out"]) {
    rememberQuery("query-a"); const app = boot(); await waitFor(() => app.output.textContent.includes("Answer complete"));
    if (action === "new-chat") app.newChat.listeners.click(); else app.chat.clear();
    assert.equal(activeQueryId(), null); assert.equal(app.output.hidden, true); assert.equal(app.steps.textContent.includes("Retrieved"), false);
    const next = boot(); assert.equal(next.output.hidden, true);
  }
});

test("a late restore cannot overwrite New chat or a repository switch", async () => {
  for (const action of ["new-chat", "switch"]) {
    let finish; reply = () => new Promise((resolve) => { finish = resolve; });
    rememberQuery("query-a"); const app = boot(); await waitFor(() => !!finish);
    if (action === "new-chat") app.newChat.listeners.click(); else app.select("other-repo");
    finish(Response.json(trace())); await new Promise((resolve) => setImmediate(resolve));
    assert.equal(activeQueryId(), null); assert.equal(app.output.hidden, true);
  }
});

test("missing traces can be retried without losing the saved id or issuing another ask", async () => {
  rememberQuery("query-a"); reply = async () => Response.json({ error: { code: "not_found", message: "Trace not found." } }, { status: 404 });
  const app = boot(); await waitFor(() => app.output.textContent.includes("Retry restoring conversation"));
  assert.equal(activeQueryId(), "query-a");
  reply = async () => Response.json(trace());
  await flatten(app.output).find((node) => node.tagName === "BUTTON" && node.textContent === "Retry restoring conversation").listeners.click();
  assert.ok(app.output.textContent.includes("Answer complete")); assert.ok(requests.every((request) => request.method === "GET"));
});

test("blocked storage still permits live chat and New chat", async () => {
  sessionStorage.getItem = sessionStorage.setItem = sessionStorage.removeItem = () => { throw Error("Blocked"); };
  const app = boot(); app.send(); await waitFor(() => app.output.textContent.includes("Answer complete"));
  assert.doesNotThrow(() => app.newChat.listeners.click()); assert.equal(app.output.hidden, true);
});

test("saved proposals are displayed read-only without stale decision or publish controls", async () => {
  const saved = trace(); saved.query_status = "awaiting_approval";
  saved.steps = [events()[0], { type: "approval_required", action_id: "action-a", kind: "patch", payload: { title: "Saved <b>proposal</b>", body: "Review me", path: "src/main.js", diff: "+safe <img>" } }];
  rememberQuery("query-a"); reply = async () => Response.json(saved);
  const app = boot(); await waitFor(() => app.output.textContent.includes("Saved proposal (read-only)"));
  assert.ok(app.output.textContent.includes("Saved <b>proposal</b>"));
  assert.ok(app.output.textContent.includes("+safe <img>"));
  assert.equal(flatten(app.output).some((node) => ["Approve", "Open pull request", "Create issue"].includes(node.textContent)), false);
  assert.ok(requests.every((request) => request.method === "GET"));
});

test("repository removal clears the saved id, while auth revalidation preserves it", async () => {
  rememberQuery("query-a"); const app = boot(); await waitFor(() => app.output.textContent.includes("Answer complete"));
  const stopped = { loading: false, error: null, selectedRepo: null };
  app.chat.setRepository(stopped, { preserveActive: true });
  assert.equal(activeQueryId(), "query-a");
  app.select(); await waitFor(() => app.output.textContent.includes("Answer complete"));
  app.chat.setRepository(stopped);
  assert.equal(activeQueryId(), null); assert.equal(app.output.hidden, true);
});

test("New chat does not bypass the existing rate-limit cooldown", async () => {
  reply = async () => Response.json({ error: { code: "rate_limited", message: "Please wait." } }, { status: 429 });
  const app = boot();
  // Toast creation is outside this DOM double; provide its existing output node.
  const query = document.querySelector;
  const toast = new Node();
  document.querySelector = (selector) => selector === "[data-toast]" ? toast : query(selector);
  document.body = new Node("body");
  app.send(); await waitFor(() => requests.length === 1 && app.form.queries["[data-stop]"].hidden === true);
  app.newChat.listeners.click(); app.send();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
});
