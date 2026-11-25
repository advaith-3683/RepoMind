import { ApiError, repositoryRequest } from "../api.js";
import { validateEvent } from "../sse.js";

const KEY = "repomind.activeQueryId";
const validId = (id) => typeof id === "string" && /^[\w-]{1,200}$/.test(id);
const statuses = { completed: "Answer complete", needs_clarification: "Needs clarification", awaiting_approval: "Saved proposal (read-only)", error: "Could not answer" };

// Store only a query id. Answers, trace contents and credentials stay out of this key.
export function activeQueryId() {
  try { const id = sessionStorage.getItem(KEY); return validId(id) ? id : null; } catch { return null; }
}
export function rememberQuery(id) {
  try { if (validId(id)) sessionStorage.setItem(KEY, id); } catch { /* Live chat still works. */ }
}
export function forgetQuery() {
  try { sessionStorage.removeItem(KEY); } catch { /* Clear the in-memory view regardless. */ }
}

export async function fetchConversation(id, repoId, { signal, request = repositoryRequest } = {}) {
  const invalid = () => new ApiError("The saved conversation could not be read safely.", "invalid_response");
  if (!validId(id)) throw invalid();
  const trace = await request(`/traces/${encodeURIComponent(id)}`, { signal });
  if (!trace || trace.query_id !== id || typeof trace.question !== "string" || !Array.isArray(trace.steps)
      || (trace.repo_id !== undefined && trace.repo_id !== repoId)) throw invalid();
  // The current server includes repo_id. For older responses, verify membership via history.
  if (trace.repo_id === undefined) {
    const history = await request(`/history?${new URLSearchParams({ repo_id: repoId, limit: "100" })}`, { signal });
    if (!Array.isArray(history)) throw invalid();
    if (!history.some((item) => item?.id === id && item.repo_id === repoId)) {
      throw new ApiError("The saved conversation is not available in this repository's recent history.", "not_found", 404);
    }
  }
  const events = trace.steps.filter((event) => ["start", "step", "guardrail", "answer", "needs_clarification", "approval_required", "error", "done"].includes(event?.type))
    .map((event) => validateEvent(event.type, event));
  const start = events.find((event) => event.type === "start");
  if (!start || start.query_id !== id) throw invalid();
  const run = { repoId, queryId: id, question: trace.question, mode: start.mode,
    status: statuses[trace.query_status] || "Saved run (interrupted)",
    events: events.filter((event) => ["step", "guardrail", "error"].includes(event.type)),
    answer: events.find((event) => event.type === "answer"),
    clarification: events.find((event) => event.type === "needs_clarification"),
    savedProposal: events.find((event) => event.type === "approval_required"),
    error: events.find((event) => event.type === "error")?.message,
    usage: Object.fromEntries(["tokens_in", "tokens_out", "latency_ms", "cost_usd"]
      .filter((key) => typeof trace[key] === "number" && Number.isFinite(trace[key]) && trace[key] >= 0)
      .map((key) => [key, trace[key]])),
  };
  if (trace.query_status === "completed" && !run.answer) throw invalid();
  return run;
}
