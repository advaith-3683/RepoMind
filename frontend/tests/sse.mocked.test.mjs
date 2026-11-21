// Mocked fetch and ReadableStream fixtures only; never calls a live backend.
import test, { beforeEach } from "node:test";
import assert from "node:assert/strict";
import { configureAuthentication } from "../js/api.js";
import { askQuestion, readEventStream } from "../js/sse.js";

const start = { type: "start", query_id: "query-test", mode: "fast" };
const answer = { type: "answer", text: "Real fixture text café 🙂", found: true, confidence: "high", groundedness: 1, citations: [] };
const done = { type: "done", query_id: "query-test", status: "completed", mode: "fast", usage: { steps: 1, tokens_in: 12, tokens_out: 6, latency_ms: 30, cost_usd: 0, llm_cache_hits: 0 } };
const frame = (event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
const encoder = new TextEncoder();
function response(chunks) {
  return new Response(new ReadableStream({ start(controller) { chunks.forEach((chunk) => controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk)); controller.close(); } }), { headers: { "content-type": "text/event-stream; charset=utf-8" } });
}
const text = [start, answer, done].map(frame).join("");
let requests, refreshes, expired;
beforeEach(() => {
  globalThis.window = { REPOMIND_API_BASE: "https://backend.example.test" };
  requests = []; refreshes = 0; expired = 0;
  configureAuthentication({
    credentials: async () => ({ accessToken: "fake-supabase-token" }),
    recover: async () => { refreshes += 1; return { accessToken: "fake-refreshed-token" }; },
    expire: () => { expired += 1; },
  });
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return response([text]); };
});

test("mocked SSE: one chunk containing several frames", async () => {
  const events = [];
  await readEventStream(response([text]), { onEvent: (event) => events.push(event) });
  assert.deepEqual(events, [start, answer, done]);
});

test("mocked SSE: every possible byte boundary, including UTF-8 characters", async () => {
  const bytes = encoder.encode(text);
  for (let index = 1; index < bytes.length; index++) {
    const events = [];
    await readEventStream(response([bytes.slice(0, index), bytes.slice(index)]), { onEvent: (event) => events.push(event) });
    assert.deepEqual(events, [start, answer, done], `Byte boundary ${index}`);
  }
});

test("mocked SSE: one byte at a time, CRLF and comments", async () => {
  const bytes = encoder.encode((": keep-alive\n\n" + text).replaceAll("\n", "\r\n"));
  const events = [];
  await readEventStream(response([...bytes].map((byte) => new Uint8Array([byte]))), { onEvent: (event) => events.push(event) });
  assert.deepEqual(events, [start, answer, done]);
});

test("mocked SSE: multiline data fields", async () => {
  const multiline = frame(answer).replace('"text":', '\n' + 'data: "text":');
  const events = [];
  await readEventStream(response([frame(start) + multiline + frame(done)]), { onEvent: (event) => events.push(event) });
  assert.equal(events[1].text, answer.text);
});

test("mocked SSE: malformed JSON gives a safe error without reflecting the data", async () => {
  await assert.rejects(readEventStream(response([frame(start) + 'event: answer\ndata: SECRET_INVALID_DATA\n\n'])), (error) => error.code === "invalid_stream" && !error.message.includes("SECRET"));
});

test("mocked SSE: unknown events are ignored without interpreting their payload", async () => {
  const events = [];
  await readEventStream(response([frame(start) + 'event: future_event\ndata: unknown payload\n\n' + frame(answer) + frame(done)]), { onEvent: (event) => events.push(event) });
  assert.deepEqual(events, [start, answer, done]);
});

test("mocked SSE: missing done and incomplete final frames fail", async () => {
  for (const input of [frame(start), frame(start) + frame(answer), text.slice(0, -1)]) {
    await assert.rejects(readEventStream(response([input])), { code: "incomplete_stream" });
  }
});

test("mocked SSE: bad event schemas and mismatched done cannot claim success", async () => {
  for (const input of [frame(answer), frame(start) + frame({ ...answer, citations: [{ path: "a", start: 0, end: 2 }] }),
    frame(start) + frame(answer) + frame({ ...done, query_id: "other" }),
    frame(start) + frame(done), frame(start) + frame(answer) + frame({ ...done, usage: { tokens_in: "invented" } })]) {
    await assert.rejects(readEventStream(response([input])), { code: "invalid_stream" });
  }
});

test("mocked SSE: oversized frames and unterminated lines are rejected", async () => {
  for (const input of ["data: " + "x".repeat(2_000_001), "data: " + "x".repeat(2_000_001) + "\n\n"]) {
    await assert.rejects(readEventStream(response([input])), { code: "invalid_stream" });
  }
});

test("mocked SSE: cancellation interrupts a pending read and releases its lock", async () => {
  let cancelled = false;
  const controller = new AbortController();
  const stream = new ReadableStream({ start(writer) { writer.enqueue(encoder.encode(frame(start))); }, cancel() { cancelled = true; } });
  const pending = readEventStream(new Response(stream, { headers: { "content-type": "text/event-stream" } }), {
    signal: controller.signal, onEvent() { queueMicrotask(() => controller.abort()); },
  });
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(cancelled, true); assert.equal(stream.locked, false);
});

test("mocked SSE: stream transport failure is normalized", async () => {
  let writer;
  const stream = new ReadableStream({ start(controller) { writer = controller; } });
  const pending = readEventStream(new Response(stream, { headers: { "content-type": "text/event-stream" } }));
  writer.error(new Error("PRIVATE transport details"));
  await assert.rejects(pending, (error) => error.code === "network_error" && !error.message.includes("PRIVATE"));
});

test("mocked ask: POST body, normal bearer auth and SSE accept header", async () => {
  const body = { repo_id: "real-id-placeholder", question: "Where is the entry point?", mode: "fast" };
  await askQuestion(body);
  assert.equal(requests[0].method, "POST");
  assert.equal(requests[0].url, "https://backend.example.test/ask");
  assert.equal(requests[0].headers.Authorization, "Bearer fake-supabase-token");
  assert.equal(requests[0].headers.Accept, "text/event-stream");
  assert.equal(requests[0].headers["X-GitHub-Token"], undefined);
  assert.deepEqual(JSON.parse(requests[0].body), body);
});

test("mocked ask: 401 refreshes and retries once; a second 401 signs out", async () => {
  let calls = 0;
  globalThis.fetch = async () => ++calls === 1 ? new Response("{}", { status: 401 }) : response([text]);
  await askQuestion({}); assert.equal(calls, 2); assert.equal(refreshes, 1);
  globalThis.fetch = async () => new Response("{}", { status: 401 });
  await assert.rejects(askQuestion({}), { status: 401 });
  assert.equal(refreshes, 2); assert.equal(expired, 1);
});

test("mocked ask: JSON errors (400, 409, 429, 503) preserve the server message", async () => {
  for (const status of [400, 409, 429, 503]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: status === 429 ? "rate_limited" : "fixture_error", message: "Server message" } }), { status });
    await assert.rejects(askQuestion({}), { status, message: "Server message" });
  }
});

test("mocked ask: network failure and non-SSE success responses fail safely", async () => {
  globalThis.fetch = async () => { throw Error("PRIVATE transport details"); };
  await assert.rejects(askQuestion({}), { code: "network_error" });
  globalThis.fetch = async () => new Response("<html>upstream</html>");
  await assert.rejects(askQuestion({}), { code: "invalid_stream" });
});

test("mocked ask: pre-aborted requests do not reach fetch", async () => {
  const controller = new AbortController(); controller.abort();
  await assert.rejects(askQuestion({}, { signal: controller.signal }), { name: "AbortError" });
  assert.equal(requests.length, 0);
});
