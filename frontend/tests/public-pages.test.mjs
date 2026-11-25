import test, { beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { renderEvaluations, initEvaluations } from "../js/ui/evaluations.js";
import { appendAnswerText, answerNotice } from "../js/ui/answer.js";
import { configureAuthentication } from "../js/api.js";

class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; }
  set textContent(text) { this.children = []; this.text = String(text); }
  get textContent() { return (this.text || "") + this.children.map((child) => child.textContent).join(""); }
  get childElementCount() { return this.children.length; }
  set innerHTML(_value) { throw Error("Unsafe HTML rendering"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
}
const flatten = (root) => [root, ...root.children.flatMap(flatten)];
const tagged = (root, tag) => flatten(root).filter((node) => node.tagName === tag.toUpperCase());
const findTable = (root, caption) => tagged(root, "table").find((table) => table.children[0].textContent === caption);
const rows = (table) => tagged(table, "tbody")[0].children.map((row) => row.children.map((cell) => cell.textContent));
const fixture = () => ({
  meta: { title: "Fixture evaluation", repo: "fixture/repo", model: "fixture-model", fast_top_k: 5, search_preview_lines: 60,
    golden_set: { questions: 12 }, limits: ["One repository.", "Small golden set.", "Proxy groundedness."] },
  agent: { answer_rate: .75, decline_accuracy: 1, file_recall: .6, keyword_hit: .7, groundedness: .8, error_rate: 0,
    latency_p50_ms: 1234, latency_p95_ms: 4567, avg_tokens: 123.456 },
  redteam: { attacks_total: 12, attacks_blocked: 11, benign_total: 8, false_positives: 1 },
  retrieval: Object.fromEntries(["vector", "keyword", "hybrid"].map((mode) => [mode, { "recall@1": .1, "recall@3": .3, "recall@5": .5, mrr: .456 }])),
  retrieval_ablation: { n_tune: 7, n_test: 5, vector_only: { "recall@5": .8, mrr: .75, "held_out_recall@5": .6, held_out_mrr: .55 },
    hybrid_best_on_tune: { "recall@5": .9, mrr: .85, "held_out_recall@5": .8, held_out_mrr: .75, kw_weight: .1, prefix: true } },
  history: [{ run: 1, date: "2026-11-23", change: "Invalid fixture", answered: null, declined: null, errors: 2, valid: false, note: "Provider quota exhausted." },
    { run: 2, date: "2026-11-24", change: "Current fixture", answered: "7/9", declined: "3/3", errors: 0, file_recall: .6, keyword_hit: .7, valid: true }],
  diagnostics: { selected_subset: { answered: "2/2", note: "Subset was selected after failures; not comparable." } },
});

beforeEach(() => {
  globalThis.document = { createElement: (tag) => new Node(tag) };
  globalThis.window = { REPOMIND_API_BASE: "" };
});

test("evaluations render supplied metadata and all end-to-end metrics with units", () => {
  const root = new Node("div"), data = fixture(), before = JSON.stringify(data);
  renderEvaluations(root, data);
  for (const text of ["Fixture evaluation", "fixture/repo", "fixture-model", "Golden set size12", "Fast top K5", "Search preview lines60"]) assert.ok(root.textContent.includes(text), text);
  assert.deepEqual(rows(findTable(root, "End-to-end results")), [
    ["Answer rate", "75.00%"], ["Correctly declined", "100.00%"], ["File recall", "60.00%"], ["Keyword hit", "70.00%"],
    ["Groundedness", "80.00%"], ["Error rate", "0.00%"], ["Latency p50 (ms)", "1234.00"], ["Latency p95 (ms)", "4567.00"], ["Average tokens", "123.46"],
  ]);
  assert.equal(JSON.stringify(data), before, "Rendering must not mutate or recompute the results");
});

test("red-team, retrieval and ablation display the API values without deriving new results", () => {
  const root = new Node("div"); renderEvaluations(root, fixture());
  assert.ok(root.textContent.includes("Attacks blocked: 11 out of 12"));
  assert.ok(root.textContent.includes("False positives: 1 out of 8"));
  assert.deepEqual(rows(findTable(root, "Retrieval results")), ["vector", "keyword", "hybrid"].map((mode) => [mode, "10.00%", "30.00%", "50.00%", "0.46"]));
  assert.deepEqual(rows(findTable(root, "Tune and held-out comparison")), [
    ["vector only", "80.00%", "0.75", "60.00%", "0.55", "—", "—"],
    ["hybrid best on tune", "90.00%", "0.85", "80.00%", "0.75", "0.10", "true"],
  ]);
  assert.ok(root.textContent.includes("Tune questions: 7 · Held-out questions: 5"));
});

test("history retains invalid runs, notes and raw counts and highlights only the last row", () => {
  const root = new Node("div"); renderEvaluations(root, fixture());
  const table = findTable(root, "Run history (last row is the current default)");
  const entries = tagged(table, "tbody")[0].children;
  assert.equal(entries.length, 2);
  assert.equal(entries[0].dataset.invalid, "true"); assert.ok(entries[0].textContent.includes("invalid"));
  assert.ok(entries[0].textContent.includes("Provider quota exhausted."));
  assert.equal(entries[0].children[3].textContent, "—");
  assert.equal(entries[1].className, "eval-current"); assert.ok(entries[1].textContent.includes("Current default"));
  assert.equal(entries[1].children[3].textContent, "7/9");
});

test("every limit and nested diagnostics note is visible and untrusted strings remain literal", () => {
  const root = new Node("div"), data = fixture();
  const attack = '<img src=x onerror="alert(1)">';
  data.meta.title = attack; data.meta.limits.push(attack); data.history[0].note = attack;
  data.diagnostics.selected_subset.note = attack;
  renderEvaluations(root, data);
  for (const limit of data.meta.limits) assert.ok(tagged(root, "li").some((li) => li.textContent === limit));
  assert.ok(tagged(root, "h2").some((heading) => heading.textContent === "Limits"));
  assert.ok(tagged(root, "h2").some((heading) => heading.textContent === "Diagnostics"));
  assert.ok(root.textContent.includes(attack));
  assert.equal(tagged(root, "img").length, 0);
});

test("absent sections and missing metrics stay unavailable rather than becoming zero", () => {
  const root = new Node("div"); renderEvaluations(root, {});
  assert.equal(root.textContent, "No evaluation results yet.");
  renderEvaluations(root, { agent: { answer_rate: 0 } });
  assert.equal(rows(findTable(root, "End-to-end results"))[0][1], "0.00%");
  assert.ok(rows(findTable(root, "End-to-end results")).slice(1).every((row) => row[1] === "—"));
  assert.equal(tagged(root, "table").length, 1);
});

test("evaluation loader makes a public GET without bearer or provider credentials", async () => {
  configureAuthentication({ credentials() { throw Error("Public evals must not request credentials"); } });
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, ...options }); return Response.json(fixture()); };
  const root = new Node("div"), evaluations = initEvaluations(root);
  await evaluations.load(); await evaluations.load();
  assert.equal(calls.length, 1); assert.equal(calls[0].url, "/evals/latest"); assert.equal(calls[0].method, "GET");
  assert.equal(calls[0].headers.Authorization, undefined); assert.equal(calls[0].headers["X-GitHub-Token"], undefined);
  assert.ok(root.textContent.includes("Fixture evaluation"));
});

test("evaluation loading is visible and concurrent views share the pending request", async () => {
  let finish, calls = 0;
  const root = new Node("div"), evaluations = initEvaluations(root, () => { calls++; return new Promise((resolve) => { finish = resolve; }); });
  const first = evaluations.load(), second = evaluations.load();
  assert.equal(root.textContent, "Loading evaluation results…"); assert.equal(calls, 1);
  finish(fixture()); await Promise.all([first, second]);
  assert.ok(root.textContent.includes("Fixture evaluation"));
});

for (const status of [404, 0]) test(`evaluations show a short ${status === 404 ? "404" : "network"} message and allow retry`, async () => {
  let calls = 0;
  const root = new Node("div"), evaluations = initEvaluations(root, async () => {
    if (++calls === 1) throw Object.assign(Error("Raw server detail"), { status });
    return fixture();
  });
  await evaluations.load();
  assert.ok(root.textContent.includes(status === 404 ? "No evaluation results yet." : "Could not load evaluation results."));
  assert.ok(!root.textContent.includes("Raw server detail"));
  await tagged(root, "button")[0].listeners.click();
  assert.ok(root.textContent.includes("Fixture evaluation"));
});

test("inline code uses literal code nodes while verified citations remain clickable", () => {
  const root = new Node("p"), citation = { path: "src/main.js", start: 1, end: 3 };
  appendAnswerText(root, "Call `run(<img>)` then [src/main.js:1-3] or [fake.js:9].", [citation], (item, label) => {
    assert.equal(item, citation); const chip = new Node("button"); chip.textContent = label; return chip;
  });
  assert.equal(tagged(root, "code")[0].textContent, "run(<img>)");
  assert.equal(tagged(root, "img").length, 0);
  assert.equal(tagged(root, "button").length, 1);
  assert.ok(root.textContent.includes("[fake.js:9]"));
});

test("unmatched backticks and code fences are left literal", () => {
  const root = new Node("p"), text = "An unmatched `tick and\n```js\nexample\n```";
  appendAnswerText(root, text, [], () => assert.fail("No citations expected"));
  assert.equal(root.textContent, text); assert.equal(tagged(root, "code").length, 0);
});

test("declined answers show absence of evidence instead of the low-confidence warning", () => {
  for (const confidence of ["high", "low"]) assert.equal(answerNotice({ found: false, confidence }), "No supporting code found in this repo");
  assert.equal(answerNotice({ found: true, confidence: "low" }), "Low confidence: verify against the cited code.");
  assert.equal(answerNotice({ found: true, confidence: "high" }), "");
});

test("navigation links and in-app documentation ship without private-repository links", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /href="\/evaluations" data-route/); assert.match(html, /href="\/documentation" data-route/);
  const docs = html.split('id="documentation-page"')[1].split("</article>")[0];
  for (const topic of ["What RepoMind is", "Fast vs Agent mode", "How citations are verified", "Guardrails", "Approving and publishing changes", "How to read the Evaluations page", "Known limits"]) assert.ok(docs.includes(topic));
  assert.ok(!docs.includes('href="https://github.com/'));
});
