import { askQuestion } from "../sse.js";
import { initCitationPreview } from "./citation.js";
import { appendAnswerText, answerNotice } from "./answer.js";
import { createApproval } from "./approval.js";
import { toast } from "./toast.js";

const MODE_KEY = "repomind.askMode";
const descriptions = { fast: "Fast: 1 retrieval + 1 LLM call", agent: "Agent: multi-step search, can propose changes" };
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function usageText(usage = {}) {
  return [["steps", "steps"], ["tokens_in", "tokens in"], ["tokens_out", "tokens out"],
    ["latency_ms", "ms"], ["cost_usd", "USD"]]
    .filter(([key]) => Number.isFinite(usage[key]))
    .map(([key, label]) => `${usage[key]} ${label}`).join(" · ");
}

export function initChat() {
  const forms = [...document.querySelectorAll("[data-chat-form]")];
  const outputs = [...document.querySelectorAll("[data-chat-output]")];
  const traces = [...document.querySelectorAll("[data-trace-content]")];
  const citationPreview = initCitationPreview();
  let repo = null, available = false, run = null, active = null, epoch = 0;
  let cooldownUntil = 0, cooldownTimer;
  let mode = "fast";
  try { if (localStorage.getItem(MODE_KEY) === "agent") mode = "agent"; } catch { /* Use default. */ }

  function textarea() {
    return forms.find((form) => form.getBoundingClientRect().width > 0)?.querySelector("textarea");
  }

  function controls() {
    forms.forEach((form) => {
      const input = form.querySelector("textarea");
      input.disabled = !available;
      form.querySelector("[data-send]").disabled = !available || !!active || !input.value.trim() || Date.now() < cooldownUntil;
      form.querySelector("[data-stop]").hidden = !active;
      form.querySelector("[data-character-count]").textContent = `${input.value.length} / 1000 characters`;
    });
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
      button.disabled = !!active;
    });
    document.querySelectorAll(".mode-description").forEach((node) => { node.textContent = descriptions[mode]; });
    document.querySelectorAll("[data-try-agent]").forEach((button) => {
      button.disabled = !!active || !available || Date.now() < cooldownUntil;
    });
  }

  function chooseMode(value) {
    mode = value;
    try { localStorage.setItem(MODE_KEY, mode); } catch { /* Mode still works in memory. */ }
    controls();
  }

  function rateLimit() {
    cooldownUntil = Date.now() + 10000;
    clearTimeout(cooldownTimer);
    cooldownTimer = setTimeout(controls, 10000);
    controls();
  }

  function render() {
    if (run?.approval) run.status = run.approval.statusText();
    document.querySelectorAll("[data-workspace-state]").forEach((node) => { node.hidden = !!run; });
    outputs.forEach((output) => {
      output.hidden = !run;
      output.replaceChildren();
      if (!run) return;
      output.append(element("p", "chat-question", run.question));
      const card = element("article", "answer");
      card.append(element("p", "eyebrow", run.status));
      if (run.answer) {
        const answer = run.answer;
        const notice = answerNotice(answer);
        if (notice) card.append(element("p", "chat-warning", notice));
        const text = element("p", "answer-text");
        const used = new Set();
        const chip = (citation, label) => {
          const button = element("button", "citation-chip", label);
          button.type = "button";
          button.addEventListener("click", () => citationPreview.open(run.repoId, citation, button));
          return button;
        };
        appendAnswerText(text, answer.text, answer.citations, (citation, label) => {
          used.add(citation);
          return chip(citation, label);
        });
        card.append(text);
        if (answer.citations.length) {
          const chips = element("div", "citations");
          answer.citations.forEach((citation) => {
            if (!used.has(citation)) chips.append(chip(citation, `${citation.path}:${citation.start}-${citation.end}`));
          });
          if (chips.childElementCount) card.append(chips);
        }
      }
      if (run.clarification) {
        card.append(element("p", "chat-clarification", run.clarification.message));
        if (run.clarification.suggest_mode === "agent") {
          const button = element("button", "repo-retry", "Try Agent mode ↗");
          button.type = "button"; button.dataset.tryAgent = "";
          button.addEventListener("click", () => {
            chooseMode("agent");
            textarea()?.focus();
            submit(run.question, "agent");
          });
          card.append(button);
        }
      }
      if (run.approval) {
        card.append(run.approval.render());
      }
      if (run.error) card.append(element("p", "chat-error", run.error));
      if (run.usage) {
        card.append(element("p", "run-usage", usageText(run.usage)));
        if (run.usage.llm_cache_hits > 0) card.append(element("p", "small muted", "Served from cache"));
      }
      output.append(card);
    });
    traces.forEach((container) => {
      const open = new Set([...container.querySelectorAll("details[open]")].map((node) => node.dataset.step));
      container.replaceChildren();
      if (!run) { container.append(element("p", "muted", "Agent steps will appear here when a real question is run.")); return; }
      container.append(element("p", "mono", `${run.mode} mode`));
      run.events.forEach((event, index) => {
        const card = element("section", event.type === "guardrail" ? "trace-card chat-warning" : "trace-card");
        if (event.type === "guardrail") card.append(element("h3", "", event.code), element("p", "", event.message));
        else if (event.type === "error") {
          card.append(element("h3", "", event.code), element("p", "chat-error", event.message));
          if (typeof event.detail === "string" && event.detail) card.append(element("pre", "trace-observation", event.detail));
        }
        else {
          card.append(element("h3", "", `${event.index} / ${event.tool}`), element("p", "", event.thought),
            element("pre", "trace-args", JSON.stringify(event.args, null, 2)));
          const details = element("details", "");
          details.dataset.step = String(index); details.open = open.has(String(index));
          details.append(element("summary", "", "Observation"), element("pre", "trace-observation", event.observation_preview));
          card.append(details, element("p", "small muted", `${event.tokens_total} tokens`));
        }
        container.append(card);
      });
      if (run.usage) container.append(element("p", "run-usage", usageText(run.usage)));
    });
    document.querySelector("#trace-title").textContent = run ? run.status : "No run yet";
    document.querySelector("[data-trace-status]").textContent = run ? run.status : "Waiting for a question.";
    document.querySelector(".trace-summary-meta").textContent = run ? usageText(run.usage) || run.status : "Waiting for a question";
    controls();
  }

  async function submit(question, selectedMode = mode) {
    if (!available || active || Date.now() < cooldownUntil || !question.trim()) return;
    const controller = new AbortController();
    const version = ++epoch;
    active = controller;
    citationPreview.close();
    run?.approval?.dispose();
    run = { repoId: repo.id, question, mode: selectedMode, status: "Connecting…", events: [] };
    render();
    try {
      await askQuestion({ repo_id: repo.id, question, mode: selectedMode }, {
        signal: controller.signal,
        onEvent(event) {
          if (version !== epoch) return;
          switch (event.type) {
            case "start": run.queryId = event.query_id; run.mode = event.mode; run.status = "Agent working…"; break;
            case "step": run.events.push(event); run.status = `${event.tool}…`; break;
            case "guardrail": run.events.push(event); break;
            case "answer": run.answer = event; run.status = "Finishing…"; break;
            case "needs_clarification": run.clarification = event; run.status = "Needs clarification"; textarea()?.focus(); break;
            case "approval_required": run.approval = createApproval(event, {
              onChange() {
                render();
                const card = [...document.querySelectorAll(".approval-card")].find((node) => node.getBoundingClientRect().width > 0);
                (card?.querySelector("input:not(:disabled)") || card?.querySelector("h2"))?.focus();
              },
              onRateLimit: rateLimit,
            }); run.status = "Pending approval"; break;
            case "error": run.error = event.message; run.events.push(event); run.status = "Could not answer";
              if (["rate_limited", "llm_unavailable"].includes(event.code)) toast(event.message);
              if (event.code === "rate_limited") rateLimit();
              break;
            case "done": run.usage = event.usage;
              run.status = { completed: "Answer complete", needs_clarification: "Needs clarification", awaiting_approval: "Pending approval", error: "Could not answer" }[event.status]; break;
          }
          render();
        },
      });
    } catch (error) {
      if (version !== epoch) return;
      run.status = error.name === "AbortError" ? "Run stopped" : "Could not answer";
      if (error.name !== "AbortError") run.error = error.message;
      if (error.status === 429) {
        rateLimit(); toast(error.message);
      }
      if (["prompt_injection", "empty_query", "query_too_long", "invalid_query"].includes(error.code)) textarea()?.focus();
    } finally {
      if (version === epoch) { active = null; render(); }
    }
  }

  forms.forEach((form) => {
    const input = form.querySelector("textarea");
    form.addEventListener("submit", (event) => { event.preventDefault(); submit(input.value); });
    input.addEventListener("input", () => {
      forms.forEach((other) => { if (other !== form) other.querySelector("textarea").value = input.value; });
      controls();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(input.value); }
    });
    form.querySelector("[data-stop]").addEventListener("click", () => { active?.abort(); input.focus(); });
  });
  document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => chooseMode(button.dataset.mode)));
  window.addEventListener("pagehide", () => { active?.abort(); run?.approval?.dispose(); citationPreview.close(); clearTimeout(cooldownTimer); });
  render();
  return {
    setRepository(state) {
      const next = state.selectedRepo;
      available = !state.loading && !state.error && next?.status === "ready";
      const changed = next?.id !== repo?.id;
      if (changed) {
        run?.approval?.dispose();
        epoch += 1; active?.abort(); active = null; run = null; citationPreview.close();
        forms.forEach((form) => { form.querySelector("textarea").value = ""; });
      }
      repo = next;
      if (changed) render();
      else controls();
    },
  };
}
