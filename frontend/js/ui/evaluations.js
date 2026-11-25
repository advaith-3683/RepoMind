import { publicRequest } from "../api.js";

const missing = "—";
const value = (item) => item === undefined || item === null ? missing : String(item);
const decimal = (item) => typeof item === "number" && Number.isFinite(item) ? item.toFixed(2) : missing;
const percent = (item) => typeof item === "number" && Number.isFinite(item) ? `${(item * 100).toFixed(2)}%` : missing;

function node(tag, text) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = value(text);
  return result;
}

function section(root, title) {
  const result = node("section");
  result.append(node("h2", title));
  root.append(result);
  return result;
}

function table(root, caption, headers, rows) {
  const wrapper = node("div"); wrapper.className = "eval-table-scroll";
  wrapper.tabIndex = 0; wrapper.setAttribute("role", "region"); wrapper.setAttribute("aria-label", caption);
  const result = node("table"); result.append(node("caption", caption));
  const head = node("thead"), header = node("tr");
  headers.forEach((label) => { const cell = node("th", label); cell.setAttribute("scope", "col"); header.append(cell); });
  head.append(header);
  const body = node("tbody");
  rows.forEach(({ cells, current, invalid }) => {
    const row = node("tr");
    if (current) row.className = "eval-current";
    if (invalid) row.dataset.invalid = "true";
    cells.forEach((text, index) => {
      const cell = node(index === 0 ? "th" : "td", text);
      if (index === 0) cell.setAttribute("scope", "row");
      row.append(cell);
    });
    body.append(row);
  });
  result.append(head, body); wrapper.append(result); root.append(wrapper);
}

// Diagnostics can contain nested named subsets. Preserve their notes and supplied values.
function details(root, data) {
  if (Array.isArray(data)) {
    const list = node("ul");
    data.forEach((item) => { const entry = node("li"); details(entry, item); list.append(entry); });
    root.append(list);
  } else if (data && typeof data === "object") {
    const list = node("dl");
    Object.entries(data).forEach(([key, item]) => {
      list.append(node("dt", key.replaceAll("_", " ")));
      const description = node("dd"); details(description, item); list.append(description);
    });
    root.append(list);
  } else root.append(node("p", data));
}

export function renderEvaluations(root, data = {}) {
  root.replaceChildren();
  const meta = data.meta;
  if (meta) {
    const header = section(root, meta.title || "Run configuration");
    const list = node("dl"); list.className = "eval-meta";
    for (const [label, item] of [["Repository", meta.repo], ["Model", meta.model], ["Fast top K", meta.fast_top_k],
      ["Search preview lines", meta.search_preview_lines], ["Golden set size", meta.golden_set?.questions]]) {
      list.append(node("dt", label), node("dd", value(item)));
    }
    header.append(list);
  }
  if (data.agent) {
    const agent = data.agent;
    const metrics = [["Answer rate", "answer_rate", percent], ["Correctly declined", "decline_accuracy", percent],
      ["File recall", "file_recall", percent], ["Keyword hit", "keyword_hit", percent], ["Groundedness", "groundedness", percent],
      ["Error rate", "error_rate", percent], ["Latency p50 (ms)", "latency_p50_ms", decimal],
      ["Latency p95 (ms)", "latency_p95_ms", decimal], ["Average tokens", "avg_tokens", decimal]];
    table(section(root, "End-to-end"), "End-to-end results", ["Metric", "Value"],
      metrics.map(([label, key, format]) => ({ cells: [label, format(agent[key])] })));
  }
  if (data.redteam) {
    const team = data.redteam;
    const block = section(root, "Red-team");
    block.append(node("p", `Attacks blocked: ${value(team.attacks_blocked)} out of ${value(team.attacks_total)}`),
      node("p", `False positives: ${value(team.false_positives)} out of ${value(team.benign_total)} benign prompts`));
  }
  if (data.retrieval) {
    table(section(root, "Retrieval"), "Retrieval results", ["Mode", "Recall@1", "Recall@3", "Recall@5", "MRR"],
      ["vector", "keyword", "hybrid"].filter((mode) => data.retrieval[mode]).map((mode) => ({
        cells: [mode, ...["recall@1", "recall@3", "recall@5"].map((key) => percent(data.retrieval[mode][key])), decimal(data.retrieval[mode].mrr)],
      })));
  }
  if (data.retrieval_ablation) {
    const block = section(root, "Retrieval ablation"), ablation = data.retrieval_ablation;
    block.append(node("p", `Tune questions: ${value(ablation.n_tune)} · Held-out questions: ${value(ablation.n_test)}`));
    table(block, "Tune and held-out comparison", ["Configuration", "Tune recall@5", "Tune MRR", "Held-out recall@5", "Held-out MRR", "Keyword weight", "Prefix"],
      Object.entries(ablation).filter(([, item]) => item && typeof item === "object").map(([name, item]) => ({
        cells: [name.replaceAll("_", " "), percent(item["recall@5"]), decimal(item.mrr), percent(item["held_out_recall@5"]),
          decimal(item.held_out_mrr), decimal(item.kw_weight), value(item.prefix)],
      })));
  }
  if (Array.isArray(data.history) && data.history.length) {
    table(section(root, "Run history"), "Run history (last row is the current default)",
      ["Run", "Date", "Change", "Answered", "Declined", "Errors", "File recall", "Keyword hit", "Status", "Note"],
      data.history.map((run, index) => ({ current: index === data.history.length - 1, invalid: run.valid === false,
        cells: [value(run.run), value(run.date), value(run.change), value(run.answered), value(run.declined), value(run.errors),
          percent(run.file_recall), percent(run.keyword_hit),
          [run.valid === false ? "invalid" : "", index === data.history.length - 1 ? "Current default" : ""].filter(Boolean).join(" · ") || missing,
          value(run.note)],
      })));
  }
  if (Array.isArray(meta?.limits)) {
    const list = node("ul");
    meta.limits.filter((item) => typeof item === "string").forEach((item) => list.append(node("li", item)));
    section(root, "Limits").append(list);
  }
  if (data.diagnostics !== undefined) details(section(root, "Diagnostics"), data.diagnostics);
  if (!root.childElementCount) root.append(node("p", "No evaluation results yet."));
}

export function initEvaluations(root, request = publicRequest) {
  let loaded = false, pending;
  return {
    async load() {
      if (loaded) return;
      if (pending) return pending;
      root.replaceChildren(node("p", "Loading evaluation results…"));
      pending = (async () => {
        try {
          const data = await request("/evals/latest");
          renderEvaluations(root, data || {});
          loaded = true;
        } catch (error) {
          root.replaceChildren(node("p", error.status === 404 ? "No evaluation results yet." : "Could not load evaluation results. Please try again."));
          const retry = node("button", "Retry"); retry.type = "button"; retry.className = "repo-retry";
          retry.addEventListener("click", () => this.load()); root.append(retry);
        } finally { pending = null; }
      })();
      return pending;
    },
  };
}
