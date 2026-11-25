import test, { beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { createAuth } from "../js/auth.js";
import { repositoryRequest } from "../js/api.js";

const SESSION = "repomind.auth.session";
const CALLBACK = "repomind.oauth.callback";
const PENDING = "repomind.oauth.pending";
const project = "https://auth.example.test";
const callbackScript = readFileSync(new URL("../js/oauth-callback.js", import.meta.url), "utf8");
let storage, config, requests, handler, navigation;
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const session = (extra = {}) => ({
  access_token: "supabase-access-test", refresh_token: "supabase-refresh-test",
  provider_token: "github-provider-test", expires_at: Date.now() + 3600000, project_url: project, ...extra,
});
const rotated = () => ({ access_token: "supabase-rotated-test", refresh_token: "rotated-refresh-test", expires_in: 3600 });

beforeEach(() => {
  storage = new Map(); requests = []; navigation = null;
  config = { auth_required: true, supabase_url: project, supabase_anon_key: "sb_publishable_test_only", github_scopes: "public_repo" };
  globalThis.sessionStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.window = { REPOMIND_API_BASE: "" };
  globalThis.document = { documentElement: { dataset: {} } };
  globalThis.location = { origin: "http://127.0.0.1:8000", pathname: "/", hash: "", search: "", assign: (url) => { navigation = url; } };
  globalThis.fetch = async (url, options = {}) => {
    const request = { url: new URL(url, location.origin), ...options };
    requests.push(request);
    if (options.signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    const override = await handler(request);
    if (override) return override;
    if (request.url.pathname === "/config") return json(config);
    if (request.url.pathname === "/auth/v1/user") return json({ id: "user-test", user_metadata: { user_name: "developer" } });
    if (request.url.pathname === "/auth/v1/token") return json(rotated());
    if (request.url.pathname === "/auth/v1/logout") return new Response(null, { status: 204 });
    if (request.url.pathname === "/repos") return json([]);
    throw Error("Unexpected test request");
  };
  handler = () => null;
});

function instance() {
  const auth = createAuth();
  let state;
  auth.subscribe((value) => { state = value; });
  return { auth, state: () => state };
}

function storeSession(extra) { storage.set(SESSION, JSON.stringify(session(extra))); }
function callback(extra = {}) {
  storage.set(PENDING, JSON.stringify({ project_url: project, created_at: Date.now() }));
  storage.set(CALLBACK, JSON.stringify({ ...session(), received_at: Date.now(), expires_in: "3600", token_type: "bearer", ...extra }));
}

test("signed-out initialization never requests protected data", async () => {
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-out");
  assert.deepEqual(requests.map((r) => r.url.pathname), ["/config"]);
});

test("auth_required=false permits local API use without a fabricated token", async () => {
  config.auth_required = false;
  const { auth, state } = instance(); await auth.initialize(); await repositoryRequest("/repos");
  assert.equal(state().phase, "local");
  assert.equal(requests.at(-1).headers.Authorization, undefined);
});

test("configuration failure is recoverable without reflecting raw server data", async () => {
  handler = (r) => r.url.pathname === "/config" ? json({ error: { message: "DO_NOT_REFLECT" } }, 503) : null;
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "error"); assert.ok(!state().message.includes("DO_NOT_REFLECT"));
  handler = () => null; await auth.initialize(); assert.equal(state().phase, "signed-out");
});

test("blocking callback script strips tokens before storing them", () => {
  let clean;
  const sandbox = { URLSearchParams, Date, sessionStorage, document,
    location: { ...location, hash: "#access_token=fake-access&refresh_token=fake-refresh&provider_token=fake-provider&expires_in=3600&token_type=bearer" },
    history: { replaceState: (_state, _title, path) => { clean = path; } } };
  vm.runInNewContext(callbackScript, sandbox);
  assert.equal(clean, "/"); assert.equal(JSON.parse(storage.get(CALLBACK)).access_token, "fake-access");
});

test("callback supplied in query parameters is stripped and rejected", () => {
  let clean;
  vm.runInNewContext(callbackScript, { URLSearchParams, Date, sessionStorage, document,
    location: { ...location, search: "?access_token=fake-query-token" },
    history: { replaceState: (_s, _t, path) => { clean = path; } } });
  assert.equal(clean, "/"); assert.equal(JSON.parse(storage.get(CALLBACK)).error, true);
  assert.ok(!storage.get(CALLBACK).includes("fake-query-token"));
});

test("OAuth callback validates the user and consumes pending callback records", async () => {
  callback(); const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-in"); assert.equal(state().login, "developer");
  assert.equal(storage.has(PENDING), false); assert.equal(storage.has(CALLBACK), false);
  const userCall = requests.find((r) => r.url.pathname === "/auth/v1/user");
  assert.equal(userCall.headers.apikey, config.supabase_anon_key);
});

test("unsolicited and stale OAuth callbacks fail closed", async () => {
  callback(); storage.delete(PENDING);
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-out"); assert.equal(storage.has(SESSION), false);
  callback(); storage.set(PENDING, JSON.stringify({ project_url: project, created_at: Date.now() - 3600000 }));
  await auth.initialize(); assert.equal(state().phase, "signed-out");
});

test("OAuth error text is not reflected in the UI", async () => {
  callback({ error: true, error_description: "DO_NOT_REFLECT" });
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-out"); assert.ok(!state().message.includes("DO_NOT_REFLECT"));
});

test("restored sessions use only the Supabase token for repository requests", async () => {
  storeSession(); const { auth, state } = instance(); await auth.initialize(); await repositoryRequest("/repos");
  assert.equal(state().phase, "signed-in");
  assert.equal(requests.at(-1).headers.Authorization, "Bearer supabase-access-test");
  assert.ok(!JSON.stringify(requests).includes("github-provider-test"));
});

test("repository deletion sends normal auth and accepts an empty 204 response", async () => {
  storeSession(); const { auth } = instance(); await auth.initialize();
  handler = (r) => r.url.pathname === "/repos/remove-me" ? new Response(null, { status: 204 }) : null;
  assert.equal(await repositoryRequest("/repos/remove-me", { method: "DELETE" }), null);
  const request = requests.at(-1);
  assert.equal(request.method, "DELETE");
  assert.equal(request.headers.Authorization, "Bearer supabase-access-test");
  assert.equal(request.headers["X-GitHub-Token"], undefined);
  assert.equal(request.body, undefined);
});

test("repository deletion preserves server error status, code and message", async () => {
  storeSession(); const { auth } = instance(); await auth.initialize();
  for (const [status, code] of [[409, "repo_not_ready"], [403, "forbidden"], [404, "not_found"]]) {
    handler = () => json({ error: { code, message: "Server removal message" } }, status);
    await assert.rejects(repositoryRequest("/repos/remove-me", { method: "DELETE" }), {
      status, code, message: "Server removal message",
    });
  }
});

test("expired sessions refresh, rotate tokens, and preserve the separate provider token", async () => {
  storeSession({ expires_at: Date.now() - 1 }); const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-in");
  assert.equal(requests.filter((r) => r.url.pathname === "/auth/v1/token").length, 1);
  const saved = JSON.parse(storage.get(SESSION));
  assert.equal(saved.access_token, "supabase-rotated-test");
  assert.equal(saved.refresh_token, "rotated-refresh-test");
  assert.equal(saved.provider_token, "github-provider-test");
});

test("concurrent unauthorized API requests share one refresh", async () => {
  storeSession(); const { auth } = instance(); await auth.initialize();
  handler = async (r) => {
    if (r.url.pathname === "/repos" && r.headers.Authorization === "Bearer supabase-access-test") return json({}, 401);
    if (r.url.pathname === "/auth/v1/token") { await new Promise((resolve) => setTimeout(resolve, 20)); return json(rotated()); }
  };
  await Promise.all([repositoryRequest("/repos"), repositoryRequest("/repos")]);
  assert.equal(requests.filter((r) => r.url.pathname === "/auth/v1/token").length, 1);
});

test("a second 401 signs out without a refresh loop", async () => {
  storeSession(); const { auth, state } = instance(); await auth.initialize();
  handler = (r) => r.url.pathname === "/repos" ? json({}, 401) : null;
  await assert.rejects(repositoryRequest("/repos"), { status: 401 });
  assert.equal(state().phase, "signed-out"); assert.equal(storage.has(SESSION), false);
  assert.equal(requests.filter((r) => r.url.pathname === "/auth/v1/token").length, 1);
});

test("a rejected refresh clears the session and provider token", async () => {
  storeSession({ expires_at: 1 });
  handler = (r) => r.url.pathname === "/auth/v1/token" ? json({}, 401) : null;
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-out"); assert.equal(storage.has(SESSION), false);
});

test("a late refresh cannot restore a session after sign-out", async () => {
  storeSession(); const { auth, state } = instance(); await auth.initialize();
  let complete, started;
  const ready = new Promise((resolve) => { started = resolve; });
  handler = (r) => {
    if (r.url.pathname === "/repos") return json({}, 401);
    if (r.url.pathname === "/auth/v1/token") return new Promise((resolve) => { complete = resolve; started(); });
  };
  const pending = repositoryRequest("/repos");
  const rejection = assert.rejects(pending);
  await ready; await auth.signOut(); complete(json(rotated())); await rejection;
  assert.equal(state().phase, "signed-out"); assert.equal(storage.has(SESSION), false);
});

test("blocked session storage prevents secure sign-in", async () => {
  sessionStorage.setItem = () => { throw Error("blocked"); };
  const { auth, state } = instance(); await auth.initialize(); auth.signIn();
  assert.equal(navigation, null); assert.equal(state().phase, "signed-out");
  assert.match(state().message, /storage/);
});

test("sessions belonging to another project are discarded", async () => {
  storeSession({ project_url: "https://another.example.test" });
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-out"); assert.equal(storage.has(SESSION), false);
});

test("sign-in uses configured OAuth endpoint, scopes, and a clean return URL", async () => {
  const { auth } = instance(); await auth.initialize(); auth.signIn();
  const target = new URL(navigation);
  assert.equal(target.origin, project); assert.equal(target.pathname, "/auth/v1/authorize");
  assert.equal(target.searchParams.get("provider"), "github");
  assert.equal(target.searchParams.get("scopes"), "public_repo");
  assert.equal(target.searchParams.get("redirect_to"), "http://127.0.0.1:8000/");
  assert.equal(target.searchParams.has("apikey"), false);
});

test("sign-in adds only public_repo when configured scopes cannot publish public repositories", async () => {
  for (const configured of [undefined, "", "read:user user:email", "public_repo", "repo", "repo,user:email"]) {
    config.github_scopes = configured;
    const { auth } = instance(); await auth.initialize(); auth.signIn();
    const scopes = new URL(navigation).searchParams.get("scopes").split(" ");
    const expected = (configured || "public_repo").split(/[ ,]+/).filter(Boolean);
    if (!expected.includes("repo") && !expected.includes("public_repo")) expected.push("public_repo");
    assert.deepEqual(scopes, expected);
  }
});

test("page-load initialization clears an abandoned sign-in and allows retry", async () => {
  const { auth, state } = instance(); await auth.initialize(); auth.signIn();
  assert.equal(state().phase, "redirecting");
  await auth.initialize();
  assert.equal(state().phase, "signed-out");
  navigation = null;
  auth.signIn();
  assert.ok(navigation);
});

test("pageshow resets the sign-in phase and re-enables the actual button for retry", async () => {
  const auth = createAuth();
  const nodes = new Map();
  const events = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {
      hidden: false, dataset: {}, addEventListener(name, listener) { this[name] = listener; },
    });
    return nodes.get(selector);
  };
  document.querySelector = node;
  document.querySelectorAll = () => [];
  window.matchMedia = () => ({ matches: false, addEventListener() {} });
  window.addEventListener = (name, listener) => events.set(name, listener);
  const script = readFileSync(new URL("../js/app.js", import.meta.url), "utf8")
    .replace(/^import .*;\r?\n/gm, "");
  vm.runInNewContext(script, {
    document, window, createAuth: () => auth, createRouter: () => ({ setPhase() {} }),
    initEvaluations: () => ({ load() {} }),
    initChat: () => ({ setRepository() {} }),
    initRepositories: () => ({ start() {}, stop() {} }), renderWorkspaceState() {},
  });
  await new Promise((resolve) => setImmediate(resolve));
  const button = node("[data-sign-in]");
  for (const persisted of [false, true]) {
    button.click();
    assert.equal(button.disabled, true);
    assert.equal(document.documentElement.dataset.authState, "redirecting");
    events.get("pageshow")({ persisted });
    if (persisted) await new Promise((resolve) => setImmediate(resolve));
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, "Continue with GitHub");
    assert.equal(document.documentElement.dataset.authState, "signed-out");
    navigation = null;
    button.click();
    assert.ok(navigation);
    auth.resetSignIn();
  }
});

test("sign-in reset preserves authenticated sessions and configuration errors", async () => {
  storeSession(); const { auth, state } = instance(); await auth.initialize();
  const saved = storage.get(SESSION);
  auth.resetSignIn();
  assert.equal(state().phase, "signed-in");
  assert.equal(storage.get(SESSION), saved);
  config.supabase_anon_key = "invalid";
  await auth.initialize(); auth.resetSignIn();
  assert.equal(state().phase, "error");
});

test("sign-out revokes only the current server session and clears app-owned storage", async () => {
  storeSession(); storage.set("unrelated", "preserved");
  const { auth, state } = instance(); await auth.initialize(); await auth.signOut();
  assert.equal(state().phase, "signed-out"); assert.equal(storage.has(SESSION), false);
  assert.equal(storage.get("unrelated"), "preserved");
  const logout = requests.find((r) => r.url.pathname === "/auth/v1/logout");
  assert.equal(logout.url.searchParams.get("scope"), "local");
});

test("secret API keys are rejected as public configuration", async () => {
  config.supabase_anon_key = "sb_secret_test_only";
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "error"); assert.equal(navigation, null);
});

test("API-base override applies to config and authenticated repository calls", async () => {
  window.REPOMIND_API_BASE = "https://backend.example.test/";
  storeSession(); const { auth } = instance(); await auth.initialize(); await repositoryRequest("/repos");
  assert.equal(requests[0].url.origin, "https://backend.example.test");
  assert.equal(requests.at(-1).url.origin, "https://backend.example.test");
});

test("a pre-expiry refresh is not repeated when that same request gets a 401", async () => {
  storeSession(); const { auth, state } = instance(); await auth.initialize();
  const realNow = Date.now;
  const timestamp = Date.now();
  Date.now = () => timestamp + 3590000;
  handler = (r) => r.url.pathname === "/repos" ? json({}, 401) : null;
  try {
    await assert.rejects(repositoryRequest("/repos"), { status: 401 });
    assert.equal(requests.filter((r) => r.url.pathname === "/auth/v1/token").length, 1);
    assert.equal(state().phase, "signed-out");
  } finally { Date.now = realNow; }
});

test("sign-out clears local authentication even when server revocation fails", async () => {
  storeSession(); const { auth, state } = instance(); await auth.initialize();
  handler = (r) => { if (r.url.pathname === "/auth/v1/logout") throw Error("offline"); };
  await auth.signOut();
  assert.equal(storage.has(SESSION), false); assert.equal(state().phase, "signed-out");
  assert.match(state().message, /Signed out on this device/);
});

test("an incomplete callback never becomes an authenticated session", async () => {
  callback({ refresh_token: null });
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "signed-out"); assert.equal(storage.has(SESSION), false);
  assert.equal(requests.some((r) => r.url.pathname === "/auth/v1/user"), false);
});

test("auth_required=false remains usable when session storage is blocked", async () => {
  config.auth_required = false;
  sessionStorage.getItem = () => { throw Error("blocked"); };
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "local");
});

test("insecure non-local auth URLs are rejected", async () => {
  config.supabase_url = "http://auth.example.test";
  const { auth, state } = instance(); await auth.initialize();
  assert.equal(state().phase, "error"); assert.equal(navigation, null);
});
