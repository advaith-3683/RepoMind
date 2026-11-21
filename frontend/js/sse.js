import { ApiError, authenticatedResponse } from "./api.js";

const TYPES = new Set(["start", "step", "guardrail", "answer", "needs_clarification", "approval_required", "error", "done"]);
const MODES = new Set(["fast", "agent"]);
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
const integer = (value) => Number.isInteger(value) && value >= 0;
const invalid = () => new ApiError("The answer service sent a malformed event. Please retry.", "invalid_stream");

export function validateEvent(type, data) {
  if (!record(data) || (data.type !== undefined && data.type !== type)) throw invalid();
  let valid = false;
  switch (type) {
    case "start": valid = typeof data.query_id === "string" && MODES.has(data.mode); break;
    case "step": valid = integer(data.index) && typeof data.thought === "string"
      && ["search_code", "read_file", "find_symbol", "list_dir"].includes(data.tool)
      && record(data.args) && typeof data.observation_preview === "string" && integer(data.tokens_total); break;
    case "guardrail": case "error": valid = typeof data.code === "string" && typeof data.message === "string"; break;
    case "answer": valid = typeof data.text === "string" && typeof data.found === "boolean"
      && ["high", "low"].includes(data.confidence) && number(data.groundedness) && data.groundedness <= 1
      && Array.isArray(data.citations) && data.citations.every((citation) => record(citation)
        && typeof citation.path === "string" && Number.isInteger(citation.start) && citation.start >= 1
        && Number.isInteger(citation.end) && citation.end >= citation.start); break;
    case "needs_clarification": valid = typeof data.message === "string"
      && (data.suggest_mode === undefined || data.suggest_mode === "agent"); break;
    case "approval_required": valid = typeof data.action_id === "string" && ["patch", "issue"].includes(data.kind)
      && record(data.payload) && typeof data.payload.title === "string"
      && (data.payload.path === undefined || typeof data.payload.path === "string"); break;
    case "done": valid = typeof data.query_id === "string" && MODES.has(data.mode)
      && ["completed", "needs_clarification", "awaiting_approval", "error"].includes(data.status)
      && record(data.usage) && Object.entries(data.usage).every(([key, value]) =>
        !["steps", "tokens_in", "tokens_out", "latency_ms", "cost_usd", "llm_cache_hits"].includes(key) || number(value)); break;
  }
  if (!valid) throw invalid();
  return { ...data, type };
}

// Read lines incrementally: both SSE frames and UTF-8 characters can span chunks.
export async function readEventStream(response, { signal, onEvent = () => {} } = {}) {
  if (!response.body || !response.headers.get("content-type")?.toLowerCase().startsWith("text/event-stream")) {
    throw new ApiError("The answer service did not return an event stream.", "invalid_stream", response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "", eventType = "", dataLines = [], frameSize = 0;
  let started = null, terminal = null, finished = false;
  const abort = () => { void reader.cancel().catch(() => {}); };
  const checkAbort = () => { if (signal?.aborted) throw new DOMException("Request cancelled", "AbortError"); };
  signal?.addEventListener("abort", abort, { once: true });

  function line(text) {
    frameSize += text.length;
    if (frameSize > 2_000_000) throw invalid();
    if (text === "") {
      const type = eventType;
      const json = dataLines.join("\n");
      eventType = ""; dataLines = []; frameSize = 0;
      if (!TYPES.has(type)) return; // Forward-compatible unknown events and keep-alive comments.
      let data;
      try { data = JSON.parse(json); } catch { throw invalid(); }
      const event = validateEvent(type, data);
      if (type === "start") {
        if (started) throw invalid();
        started = event;
      } else if (!started) throw invalid();
      if (["answer", "needs_clarification", "approval_required", "error"].includes(type)) {
        if (terminal) throw invalid();
        terminal = type;
      }
      if (type === "done") {
        const expected = { answer: "completed", needs_clarification: "needs_clarification", approval_required: "awaiting_approval", error: "error" };
        if (event.query_id !== started.query_id || event.mode !== started.mode || expected[terminal] !== event.status) throw invalid();
        finished = true;
      }
      onEvent(event);
      return;
    }
    if (text.startsWith(":")) return;
    const colon = text.indexOf(":");
    const field = colon < 0 ? text : text.slice(0, colon);
    const value = colon < 0 ? "" : text.slice(colon + 1).replace(/^ /, "");
    if (field === "event") eventType = value;
    if (field === "data") dataLines.push(value);
  }

  try {
    checkAbort();
    while (!finished) {
      let chunk;
      try { chunk = await reader.read(); } catch {
        checkAbort();
        throw new ApiError("The answer stream was interrupted. Check your connection and retry.", "network_error");
      }
      checkAbort();
      try { buffer += decoder.decode(chunk.value, { stream: !chunk.done }); } catch { throw invalid(); }
      let match;
      while (!finished && (match = /[\r\n]/.exec(buffer))) {
        const index = match.index;
        if (buffer[index] === "\r" && index === buffer.length - 1 && !chunk.done) break;
        const text = buffer.slice(0, index);
        buffer = buffer.slice(index + (buffer.slice(index, index + 2) === "\r\n" ? 2 : 1));
        line(text);
      }
      if (buffer.length + frameSize > 2_000_000) throw invalid();
      if (chunk.done && !finished) {
        throw new ApiError("The answer stream ended before completion. Please retry.", "incomplete_stream");
      }
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export async function askQuestion(body, { signal, onEvent } = {}) {
  const response = await authenticatedResponse("/ask", { method: "POST", body, signal, accept: "text/event-stream" });
  return readEventStream(response, { signal, onEvent });
}
