import test, { beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { renderEvaluations, initEvaluations } from "../js/ui/evaluations.js";
import { appendAnswerText, answerNotice } from "../js/ui/answer.js";
import { configureAuthentication } from "../js/api.js";

class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.style = {}; }
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
    golden_set: { questions: 12, answerable: 9, should_decline: 3 }, limits: ["One repository.", "Small golden set.", "Proxy groundedness."] },
  agent: { n_errors: 0, answer_rate: .75, decline_accuracy: 1, file_recall: .6, keyword_hit: .7, groundedness: .8, error_rate: 0,
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
    ["Answer rate", "75%"], ["Correctly declined", "100%"], ["File recall", "60%"], ["Keyword hit", "70%"],
    ["Groundedness", "80%"], ["Error rate", "0%"], ["Latency p50 (ms)", "1234"], ["Latency p95 (ms)", "4567"], ["Average tokens", "123"],
  ]);
  assert.equal(JSON.stringify(data), before, "Rendering must not mutate or recompute the results");
});

test("red-team, retrieval and ablation display the API values without deriving new results", () => {
  const root = new Node("div"); renderEvaluations(root, fixture());
  assert.ok(root.textContent.includes("Attacks blocked: 11 out of 12"));
  assert.ok(root.textContent.includes("False positives: 1 out of 8"));
  assert.deepEqual(rows(findTable(root, "Retrieval results")), ["vector", "keyword", "hybrid"].map((mode) => [mode, "10%", "30%", "50%", "0.46"]));
  assert.deepEqual(rows(findTable(root, "Tune and held-out comparison")), [
    ["vector only", "80%", "0.75", "60%", "0.55", "—", "—"],
    ["hybrid best on tune", "90%", "0.85", "80%", "0.75", "0.10", "true"],
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
  renderEvaluations(root, { agent: { n_errors: 0, answer_rate: 0 } });
  assert.equal(rows(findTable(root, "End-to-end results"))[0][1], "0%");
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

test("four summary cards use API counts, errors and attack totals, not rounded rates", () => {
  const root = new Node("div"), data = fixture();
  data.agent.answer_rate = .99; data.agent.decline_accuracy = .1; data.agent.n_errors = 4;
  renderEvaluations(root, data);
  const cards = flatten(root).filter((node) => node.className === "eval-stat");
  assert.equal(cards.length, 4);
  assert.deepEqual(cards.map((card) => tagged(card, "p")[0].textContent), ["7 / 9", "3 / 3", "4", "11 / 12"]);
  assert.ok(root.textContent.includes("Model: fixture-model · one repository, single run"));
  assert.ok(root.textContent.includes("9 answerable, 3 should decline"));
  const all = flatten(root);
  assert.ok(all.indexOf(cards[0]) < all.indexOf(tagged(root, "table")[0]));
});

test("summary cards preserve missing counts and mark a latest invalid run", () => {
  const root = new Node("div"), data = fixture();
  data.history = [{ run: 7, valid: false, answered: null, declined: null }];
  delete data.agent.n_errors;
  renderEvaluations(root, data);
  const cards = flatten(root).filter((node) => node.className === "eval-stat");
  assert.deepEqual(cards.slice(0, 3).map((card) => tagged(card, "p")[0].textContent), ["—", "—", "—"]);
  assert.ok(cards[0].textContent.includes("invalid run"));
  assert.ok(cards[1].textContent.includes("invalid run"));
});

test("history chart supplies textual counts and labels for invalid and current runs", () => {
  const root = new Node("div"), data = fixture();
  data.history[0].answered = "2/9";
  renderEvaluations(root, data);
  const chart = tagged(root, "figure").find((figure) => figure.children[0].textContent === "Answered by run");
  const bars = tagged(chart, "li");
  assert.equal(bars.length, 2);
  assert.ok(bars[0].textContent.includes("Run 1 · invalid"));
  assert.ok(bars[0].textContent.includes("2/9"));
  assert.ok(bars[1].textContent.includes("Run 2 · current default"));
  assert.equal(bars[0].dataset.invalid, "true"); assert.equal(bars[1].dataset.current, "true");
  const fills = flatten(chart).filter((node) => node.className === "eval-bar-fill");
  assert.equal(fills[0].style.width, `${2 / 9 * 100}%`);
  assert.equal(fills[1].style.width, `${7 / 9 * 100}%`);
  assert.ok(flatten(chart).filter((node) => node.className === "eval-bar-track").every((node) => node.attributes["aria-hidden"] === "true"));
});

test("retrieval chart shows all supplied recall@5 values with a shared 0–100% scale", () => {
  const root = new Node("div"), data = fixture();
  data.retrieval.vector["recall@5"] = .926;
  data.retrieval.keyword["recall@5"] = .374;
  data.retrieval.hybrid["recall@5"] = .815;
  renderEvaluations(root, data);
  const chart = tagged(root, "figure").find((figure) => figure.children[0].textContent === "Retrieval recall@5");
  assert.deepEqual(tagged(chart, "li").map((row) => row.children.slice(0, 2).map((item) => item.textContent)),
    [["vector", "93%"], ["keyword", "37%"], ["hybrid", "82%"]]);
  assert.deepEqual(flatten(chart).filter((node) => node.className === "eval-bar-fill").map((node) => node.style.width),
    [.926, .374, .815].map((ratio) => `${ratio * 100}%`));
});

test("malformed or missing chart data never becomes a CSS value or fabricated count", () => {
  const root = new Node("div"), data = fixture();
  data.history[0].answered = "99/0"; data.history[1].answered = "<img src=x>";
  data.retrieval.vector["recall@5"] = "10%; color: red";
  renderEvaluations(root, data);
  assert.equal(tagged(root, "img").length, 0);
  assert.ok(root.textContent.includes("<img src=x>"));
  const fills = flatten(root).filter((node) => node.className === "eval-bar-fill");
  assert.ok(fills.slice(0, 3).every((node) => node.style.width === "0%"));
});

test("Limits is a visible callout before every detailed table and appears only once", () => {
  const root = new Node("div"); renderEvaluations(root, fixture());
  const limits = flatten(root).filter((node) => node.className === "eval-limits");
  assert.equal(limits.length, 1); assert.equal(tagged(limits[0], "li").length, 3);
  const all = flatten(root);
  assert.ok(tagged(root, "table").every((table) => all.indexOf(limits[0]) < all.indexOf(table)));
});

test("documentation contains working section anchors, a text flow and an illustrative example", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const docs = html.split('id="documentation-page"')[1].split("</article>")[0];
  for (const key of ["overview", "modes", "citations", "guardrails", "publishing", "evaluations", "example", "limits"]) {
    assert.ok(docs.includes(`href="#docs-${key}"`)); assert.ok(docs.includes(`id="docs-${key}"`));
  }
  assert.match(docs, /<details class="documentation-toc-mobile"><summary>/);
  assert.ok(docs.includes("Question, then retrieval, then model, then citation check, then answer or decline"));
  assert.ok(docs.includes("Illustrative workflow, not a measured result"));
});

test("retrieval and ablation tables round every percentage while preserving two-decimal MRR", () => {
  const root = new Node("div"), data = fixture();
  data.retrieval.vector = { "recall@1": .7037, "recall@3": .9259, "recall@5": .9629, mrr: .8611 };
  data.retrieval_ablation.vector_only = { "recall@5": .9629, mrr: .871, "held_out_recall@5": .9231, held_out_mrr: .8333 };
  renderEvaluations(root, data);
  assert.deepEqual(rows(findTable(root, "Retrieval results"))[0], ["vector", "70%", "93%", "96%", "0.86"]);
  assert.deepEqual(rows(findTable(root, "Tune and held-out comparison"))[0], ["vector only", "96%", "0.87", "92%", "0.83", "—", "—"]);
  for (const caption of ["Retrieval results", "Tune and held-out comparison"]) {
    assert.ok(rows(findTable(root, caption)).flat().filter((text) => text.endsWith("%")).every((text) => /^\d+%$/.test(text)));
  }
});

test("every evaluation table retains a labelled keyboard-focusable scroll region", () => {
  const root = new Node("div"); renderEvaluations(root, fixture());
  const regions = flatten(root).filter((node) => node.className === "eval-table-scroll");
  assert.equal(regions.length, tagged(root, "table").length);
  assert.ok(regions.every((node) => node.attributes.role === "region" && node.attributes["aria-label"] && node.tabIndex === 0));
  assert.ok(regions.every((node) => typeof node.listeners.scroll === "function"));
});
