import test, { beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { configureAuthentication, repositoryRequest } from "../js/api.js";
import { createApproval, githubResultUrl } from "../js/ui/approval.js";
import { citationParts } from "../js/ui/citation.js";

// Small DOM double: text is always literal and event handlers are exercised directly.
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.listeners = {}; this.attributes = {}; this.value = ""; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || "") + this.children.map((node) => node.textContent).join(""); }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  click() { if (!this.disabled) return this.listeners.click?.(); }
  remove() {}
  input(value) { this.value = value; this.listeners.input?.(); }
}
const flatten = (node) => [node, ...node.children.flatMap(flatten)];
const button = (node, label) => flatten(node).find((item) => item.tagName === "BUTTON" && item.textContent === label);
const json = (data, status = 200) => new Response(JSON.stringify(data), { status });
const payload = { title: "Title <script>literal</script>", body: "Body <b>literal</b>", path: "smartspend backend/auth.py",
  edits: [{ old: "before", new: "after" }, { old: "second", new: "replacement" }], diff: "--- a/file\n+++ b/file\n-before\n+after\n" };
let requests, reply, cards, notices, limits, provider;
beforeEach(() => {
  requests = []; cards = []; notices = []; limits = 0; provider = "fake-provider-token";
  globalThis.window = {};
  globalThis.document = { createElement: (tag) => new Node(tag) };
  globalThis.sessionStorage = { getItem: () => JSON.stringify({ provider_token: provider }) };
  configureAuthentication({ credentials: async () => ({ accessToken: "fake-access" }), recover: async () => null, expire() {} });
  reply = () => json({ id: "action", kind: "patch", status: "approved", payload: { ...payload, title: "Server final" }, diff: "+server-computed\n" });
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return reply(); };
});
afterEach(() => cards.forEach((card) => card.dispose()));
function proposal(kind = "patch") {
  const card = createApproval({ action_id: "action", kind, payload: kind === "patch" ? payload : { title: payload.title, body: payload.body } }, {
    notify: (text) => notices.push(text), onRateLimit: () => { limits += 1; },
  });
  cards.push(card); return card;
}

test("citation paths with spaces, parentheses and single-line references use verified entries only", () => {
  const citations = [{ path: "smartspend backend/auth.py", start: 111, end: 119 }, { path: "src/app/(auth)/page.tsx", start: 7, end: 7 }];
  const text = "Read [smartspend backend/auth.py:111-119], [src/app/(auth)/page.tsx:7] and [invented.py:9].";
  const parts = citationParts(text, citations);
  assert.equal(parts.map((part) => part.text).join(""), text);
  assert.deepEqual(parts.filter((part) => part.citation).map((part) => part.citation), citations);
  assert.ok(parts.some((part) => !part.citation && part.text.includes("[invented.py:9]")));
  assert.ok(citationParts("[smartspend backend/auth.py:112-119]", citations).every((part) => !part.citation));
  assert.deepEqual(citationParts(text, []), [{ text }]);
});

test("approval card shows literal payload, path and coloured server diff", () => {
  const root = proposal().render();
  assert.ok(root.textContent.includes(payload.title)); assert.ok(root.textContent.includes(payload.body));
  assert.ok(root.textContent.includes(payload.path)); assert.ok(root.textContent.includes(payload.diff));
  assert.ok(flatten(root).some((node) => node.className === "diff-added" && node.textContent.includes("+after")));
  assert.ok(flatten(root).some((node) => node.className === "diff-removed" && node.textContent.includes("-before")));
  assert.equal(flatten(root).filter((node) => node.className === "primary").length, 1);
  assert.equal(flatten(root).some((node) => ["SCRIPT", "B"].includes(node.tagName)), false);
});

test("approve sends only a decision, uses final server payload/diff, and never publishes automatically", async () => {
  const card = proposal(); await button(card.render(), "Approve").click();
  assert.deepEqual(JSON.parse(requests[0].body), { decision: "approve" });
  assert.equal(requests.length, 1); assert.equal(requests[0].url, "/actions/action/decision");
  assert.equal(requests[0].headers["X-GitHub-Token"], undefined);
  const root = card.render();
  assert.equal(root.dataset.actionStatus, "approved");
  assert.ok(root.textContent.includes("Server final")); assert.ok(root.textContent.includes("+server-computed"));
  assert.ok(!root.textContent.includes("+after"));
  assert.ok(button(root, "Open pull request")); assert.ok(!button(root, "Approve"));
});

test("edit preserves path and old text, sends each replacement and shows only recomputed diff", async () => {
  const card = proposal(); button(card.render(), "Edit").click();
  const root = card.render();
  assert.equal(requests.length, 0, "Entering edit mode must not decide or publish");
  assert.ok(button(root, "Approve edited version"));
  assert.ok(root.textContent.includes("This records your edited version as approved. Nothing is published to GitHub until you click Open pull request."));
  const inputs = flatten(root).filter((node) => ["INPUT", "TEXTAREA"].includes(node.tagName));
  inputs.forEach((input, index) => input.input(["New title", "New body", "new first", "new second"][index]));
  reply = () => json({ status: "edited", payload: { ...payload, title: "New title", body: "New body" }, diff: "+recomputed edited\n" });
  await button(card.render(), "Approve edited version").click();
  assert.deepEqual(JSON.parse(requests[0].body), { decision: "edit", edited_payload: {
    title: "New title", body: "New body", path: payload.path,
    edits: [{ old: "before", new: "new first" }, { old: "second", new: "new second" }],
  } });
  assert.equal(card.render().dataset.actionStatus, "edited");
  assert.ok(card.render().textContent.includes("+recomputed edited"));
  assert.equal(requests.length, 1, "Approving edits must only send the decision");
  assert.equal(requests[0].url, "/actions/action/decision");
  assert.ok(button(card.render(), "Open pull request"));
});

test("reject is terminal, clears server payload/diff and never offers publish", async () => {
  reply = () => json({ status: "rejected", payload: null, diff: null });
  const card = proposal(); await button(card.render(), "Reject").click();
  assert.deepEqual(JSON.parse(requests[0].body), { decision: "reject" });
  assert.equal(card.render().dataset.actionStatus, "rejected");
  assert.equal(flatten(card.render()).filter((node) => node.tagName === "BUTTON").length, 0);
  assert.ok(!card.render().textContent.includes(payload.title));
});

test("409 invalid_transition disables all decision buttons and shows the decided message", async () => {
  reply = () => json({ error: { code: "invalid_transition", message: "Original conflict" } }, 409);
  const card = proposal(); await button(card.render(), "Approve").click();
  const root = card.render(); assert.ok(root.textContent.includes("This action was already decided."));
  for (const label of ["Approve", "Edit", "Reject"]) assert.equal(button(root, label).disabled, true);
  assert.ok(!button(root, "Open pull request"));
});

for (const code of ["invalid_proposal", "invalid_decision"]) test(`400 ${code} preserves message and permits correction`, async () => {
  reply = () => json({ error: { code, message: "Correct this <b>literal</b>" } }, 400);
  const card = proposal(); await button(card.render(), "Approve").click();
  assert.ok(card.render().textContent.includes("Correct this <b>literal</b>"));
  assert.equal(button(card.render(), "Edit").disabled, false);
});

test("decision and publish in-flight locks prevent duplicate requests across both views", async () => {
  let finish; reply = () => new Promise((resolve) => { finish = resolve; });
  const card = proposal(), first = card.render(), stale = card.render();
  const pending = button(first, "Approve").click();
  await Promise.resolve(); await Promise.resolve();
  for (const label of ["Approve", "Edit", "Reject"]) assert.equal(button(card.render(), label).disabled, true);
  await button(stale, "Reject").click(); assert.equal(requests.length, 1);
  finish(json({ status: "approved", payload, diff: "+final" })); await pending;
  const other = card.render(); const publishing = button(card.render(), "Open pull request").click();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(button(card.render(), "Publishing…").disabled, true);
  await button(other, "Open pull request").click(); assert.equal(requests.length, 2);
  finish(json({ status: "published", result: { url: "https://github.com/o/r/pull/7" } })); await publishing;
});

for (const kind of ["patch", "issue"]) test(`${kind} publishes with provider header and displays success, number and link`, async () => {
  const card = proposal(kind); await button(card.render(), "Approve").click();
  reply = () => json({ status: "published", already: false, result: { kind, number: 7, url: `https://github.com/o/r/${kind === "patch" ? "pull" : "issues"}/7`, branch: kind === "patch" ? "repomind/fix-123" : undefined } });
  await button(card.render(), kind === "patch" ? "Open pull request" : "Create issue").click();
  const request = requests.at(-1), root = card.render();
  assert.equal(request.url, "/actions/action/publish"); assert.equal(request.method, "POST");
  assert.equal(request.headers["X-GitHub-Token"], provider); assert.equal(request.headers.Authorization, "Bearer fake-access");
  assert.equal(request.body, undefined); assert.equal(root.dataset.publishState, "published");
  assert.equal(card.statusText(), "Published to GitHub");
  assert.equal(flatten(root).find((node) => node.tagName === "H2").textContent, kind === "patch" ? "Pull request opened #7" : "Issue created #7");
  assert.ok(!root.textContent.includes("Already published.")); assert.ok(!root.textContent.includes(provider));
  assert.equal(button(root, "Open pull request"), undefined);
  assert.equal(button(root, "Create issue"), undefined);
  const link = flatten(root).find((node) => node.tagName === "A");
  assert.equal(link.textContent, kind === "patch" ? "View pull request on GitHub" : "View issue on GitHub");
  assert.equal(link.href, `https://github.com/o/r/${kind === "patch" ? "pull" : "issues"}/7`);
  assert.equal(link.className, "primary");
  assert.equal(link.target, "_blank"); assert.equal(link.rel, "noopener noreferrer");
  if (kind === "patch") {
    assert.ok(root.textContent.includes("repomind/fix-123")); assert.ok(root.textContent.includes("+server-computed"));
  }
});

test("already published wording is shown without inventing a missing result number", async () => {
  const card = proposal(); await button(card.render(), "Approve").click();
  reply = () => json({ status: "published", already: true, result: { url: "https://github.com/o/r/pull/7" } });
  await button(card.render(), "Open pull request").click();
  const root = card.render();
  assert.ok(flatten(root).some((node) => node.tagName === "P" && node.textContent === "Already published."));
  assert.equal(flatten(root).find((node) => node.tagName === "H2").textContent, "Pull request opened");
  assert.equal(button(root, "Open pull request"), undefined);
  assert.ok(root.textContent.includes("+server-computed"));
});

test("unsafe publish result URLs never become links", async () => {
  for (const url of ["javascript:alert(1)", "https://evil.test/o/r", "https://user:pass@github.com/o/r"]) {
    const card = proposal(); await button(card.render(), "Approve").click();
    reply = () => json({ status: "published", result: { url, number: 7 } });
    await button(card.render(), "Open pull request").click();
    const root = card.render();
    assert.equal(flatten(root).filter((node) => node.tagName === "A").length, 0);
    assert.ok(root.textContent.includes("GitHub returned an invalid result link."));
    assert.equal(button(root, "Open pull request"), undefined);
    reply = () => json({ status: "approved", payload, diff: "+server-computed\n" });
  }
});

test("missing, malformed or unavailable provider storage never calls publish", async () => {
  const card = proposal(); await button(card.render(), "Approve").click();
  for (const getter of [() => "null", () => "bad JSON", () => { throw Error("blocked"); }]) {
    sessionStorage.getItem = getter;
    await button(card.render(), "Open pull request").click();
    assert.equal(requests.length, 1);
    assert.ok(card.render().textContent.includes("Sign in with GitHub again to publish."));
    assert.equal(button(card.render(), "Open pull request").disabled, false);
  }
});

const failures = [
  ["github_auth", 403, "Sign in with GitHub again to publish."],
  ["github_no_permission", 403, "You need write access to open a PR on this repo."],
  ["github_conflict", 409, "The file changed on GitHub or the edit no longer applies. Re-index and ask again."],
  ["publish_in_progress", 409, "Publishing is already running. Wait a moment."],
  ["publish_not_ready", 409, "Approve this action first, then retry publishing."],
  ["missing_github_token", 400, "Sign in with GitHub again to publish."],
  ["rate_limited", 429, "Server rate limit"],
  ["github_error", 502, "Server unavailable"],
];
for (const [code, http, message] of failures) test(`publish ${code}: mapped message, fallback, retry and cooldown`, async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"] });
  const card = proposal(); await button(card.render(), "Approve").click();
  reply = () => json({ error: { code, message: ["rate_limited", "github_error"].includes(code) ? message : "Generic server error" } }, http);
  await button(card.render(), "Open pull request").click();
  const root = card.render(); assert.equal(root.dataset.publishState, "error"); assert.ok(root.textContent.includes(message));
  for (const label of ["Copy diff", "Download .patch"]) assert.equal(button(root, label).disabled, false);
  if (code === "rate_limited") {
    assert.deepEqual(notices, [message]); assert.equal(limits, 1);
    assert.equal(button(root, "Open pull request").disabled, true);
    t.mock.timers.tick(10001);
  }
  assert.equal(button(card.render(), "Open pull request").disabled, false);
  reply = () => json({ status: "published", result: { url: "https://github.com/o/r/pull/7" } });
  await button(card.render(), "Open pull request").click();
  assert.equal(card.render().dataset.publishState, "published");
});

test("provider token cannot be attached to other API endpoints", async () => {
  await repositoryRequest("/repos", { method: "POST", githubToken: provider, body: { url: "https://github.com/o/r" } });
  assert.equal(requests[0].headers["X-GitHub-Token"], undefined);
});

test("patch fallbacks copy and download the final server diff after a publish failure", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const previous = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  let copied, blob, revoked;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (text) => { copied = text; } } } });
  t.after(() => { if (previous) Object.defineProperty(globalThis, "navigator", previous); else delete globalThis.navigator; });
  document.body = new Node("body");
  t.mock.method(URL, "createObjectURL", (value) => { blob = value; return "blob:fixture"; });
  t.mock.method(URL, "revokeObjectURL", (value) => { revoked = value; });
  const card = proposal(); await button(card.render(), "Approve").click();
  reply = () => json({ error: { code: "github_no_permission", message: "No permission" } }, 403);
  await button(card.render(), "Open pull request").click();
  await button(card.render(), "Copy diff").click();
  assert.equal(copied, "+server-computed\n");
  button(card.render(), "Download .patch").click();
  assert.equal(await blob.text(), "+server-computed\n");
  assert.equal(document.body.children[0].download, "repomind.patch");
  t.mock.timers.tick(1000); assert.equal(revoked, "blob:fixture");
});

test("decision rate limit keeps the edit draft and enables correction after ten seconds", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"] });
  const card = proposal(); button(card.render(), "Edit").click();
  flatten(card.render()).find((node) => node.tagName === "INPUT").input("Keep my draft");
  reply = () => json({ error: { code: "rate_limited", message: "Wait before deciding." } }, 429);
  await button(card.render(), "Approve edited version").click();
  assert.equal(button(card.render(), "Approve edited version").disabled, true);
  assert.equal(limits, 1); assert.deepEqual(notices, ["Wait before deciding."]);
  t.mock.timers.tick(10001);
  assert.equal(button(card.render(), "Approve edited version").disabled, false);
  assert.equal(flatten(card.render()).find((node) => node.tagName === "INPUT").value, "Keep my draft");
});

test("GitHub result links reject executable protocols, credentials and other origins", () => {
  for (const url of ["javascript:alert(1)", "https://evil.test", "https://github.com.evil.test", "https://user:pass@github.com/o/r", "http://github.com/o/r", null]) assert.equal(githubResultUrl(url), null);
  assert.equal(githubResultUrl("https://github.com/o/r/pull/1"), "https://github.com/o/r/pull/1");
});
