import { ApiError, configureAuthentication, publicRequest } from "./api.js";

const SESSION = "repomind.auth.session";
const PENDING = "repomind.oauth.pending";
const CALLBACK = "repomind.oauth.callback";
const SIGN_IN_MESSAGE = "Your session ended. Please sign in again.";

function authError(message = SIGN_IN_MESSAGE) {
  return new ApiError(message, "unauthorized", 401);
}

function validSession(value) {
  return value && typeof value.access_token === "string" && value.access_token.length > 0
    && typeof value.refresh_token === "string" && value.refresh_token.length > 0
    && Number.isFinite(value.expires_at);
}

function publicKey(key) {
  if (typeof key !== "string") return false;
  if (key.startsWith("sb_publishable_")) return true;
  try {
    const payload = key.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(payload)).role === "anon";
  } catch { return false; }
}

export function createAuth() {
  let config;
  let session = null;
  let state = { phase: "loading", message: "Loading secure sign-in…", login: "" };
  let epoch = 0;
  let refreshPromise = null;
  let controller = new AbortController();
  const listeners = new Set();

  function publish(phase, message = "", login = "") {
    state = { phase, message, login };
    listeners.forEach((listener) => listener({ ...state }));
  }

  function clearStorage() {
    try {
      for (const key of [SESSION, PENDING, CALLBACK]) sessionStorage.removeItem(key);
    } catch { /* Memory is cleared even when browser storage is unavailable. */ }
  }

  function expire(message = SIGN_IN_MESSAGE) {
    epoch += 1;
    controller.abort();
    controller = new AbortController();
    refreshPromise = null;
    session = null;
    clearStorage();
    publish("signed-out", message);
  }

  function saveSession(value) {
    try { sessionStorage.setItem(SESSION, JSON.stringify(value)); } catch {
      throw authError("Session storage is unavailable. Enable storage for this site to sign in securely.");
    }
    session = value;
  }

  async function authFetch(path, { method = "GET", token, body } = {}) {
    const response = await fetch(`${config.supabase_url}/auth/v1${path}`, {
      method, redirect: "error", credentials: "omit", cache: "no-store", signal: controller.signal,
      headers: {
        apikey: config.supabase_anon_key, Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    return response;
  }

  async function refresh() {
    if (refreshPromise) return refreshPromise;
    if (!session?.refresh_token) throw authError();
    const version = epoch;
    const previous = session;
    const promise = (async () => {
      try {
        const response = await authFetch("/token?grant_type=refresh_token", {
          method: "POST", body: { refresh_token: previous.refresh_token },
        });
        if (!response.ok) throw authError();
        const data = await response.json();
        const lifetime = Number(data.expires_in);
        const next = {
          access_token: data.access_token, refresh_token: data.refresh_token,
          expires_at: Date.now() + lifetime * 1000,
          // Supabase refreshes its session, not the user's GitHub provider token.
          provider_token: previous.provider_token, project_url: config.supabase_url,
        };
        if (!validSession(next) || !(lifetime > 0) || version !== epoch) throw authError();
        saveSession(next);
        return { accessToken: next.access_token, refreshed: true, epoch: version };
      } catch {
        if (version === epoch) expire();
        throw authError();
      }
    })();
    refreshPromise = promise;
    try { return await promise; } finally {
      if (refreshPromise === promise) refreshPromise = null;
    }
  }

  async function credentials() {
    if (state.phase === "local") return { accessToken: null, refreshed: false, epoch };
    if (!session) throw authError();
    if (session.expires_at <= Date.now() + 60000) return refresh();
    return { accessToken: session.access_token, refreshed: false, epoch };
  }

  async function recover(previous) {
    if (previous.epoch !== epoch || !session) return null;
    if (session.access_token !== previous.accessToken) return credentials();
    if (previous.refreshed) return null;
    return refresh();
  }

  async function verifyUser() {
    let current = await credentials();
    let response = await authFetch("/user", { token: current.accessToken });
    if (response.status === 401) {
      current = await recover(current);
      if (!current) throw authError();
      response = await authFetch("/user", { token: current.accessToken });
    }
    if (!response.ok) throw authError("Could not verify your session. Please sign in again.");
    const user = await response.json();
    if (!user || typeof user.id !== "string" || !user.id) throw authError();
    const name = user.user_metadata?.user_name || user.user_metadata?.preferred_username || user.email || "GitHub account";
    return typeof name === "string" ? name.slice(0, 160) : "GitHub account";
  }

  async function initialize() {
    const version = ++epoch;
    controller.abort();
    controller = new AbortController();
    refreshPromise = null;
    session = null;
    publish("loading", "Loading secure sign-in…");
    let callback;
    let saved;
    let pending;
    let storageError = false;
    try {
      callback = JSON.parse(sessionStorage.getItem(CALLBACK) || "null");
      sessionStorage.removeItem(CALLBACK);
      pending = JSON.parse(sessionStorage.getItem(PENDING) || "null");
      saved = JSON.parse(sessionStorage.getItem(SESSION) || "null");
    } catch {
      clearStorage();
      storageError = true;
    }
    try {
      const data = await publicRequest("/config", { signal: controller.signal });
      if (version !== epoch) return;
      if (typeof data.auth_required !== "boolean") throw new Error();
      if (!data.auth_required) {
        clearStorage();
        config = data;
        publish("local");
        return;
      }
      const url = new URL(data.supabase_url);
      const secure = url.protocol === "https:" || (url.protocol === "http:" && ["127.0.0.1", "localhost"].includes(url.hostname));
      const key = data.supabase_anon_key;
      if (!secure || url.username || url.password || url.search || url.hash || url.pathname !== "/"
          || !publicKey(key)) throw new Error();
      config = { ...data, supabase_url: url.origin };
    } catch {
      if (version === epoch) publish("error", "Could not load sign-in configuration. Check the backend connection and retry.");
      return;
    }
    try {
      if (storageError) throw authError("Session storage is unavailable or invalid. Enable site storage, then sign in again.");
      if (document.documentElement.dataset.authCallbackError) {
        delete document.documentElement.dataset.authCallbackError;
        throw authError("Browser storage blocked the sign-in response. Enable site storage and sign in again.");
      }
      if (callback) {
        sessionStorage.removeItem(PENDING);
        if (callback.error) throw authError("GitHub sign-in was cancelled or unsuccessful. Please try again.");
        if (!pending || !Number.isFinite(pending.created_at) || !Number.isFinite(callback.received_at)
            || pending.project_url !== config.supabase_url || pending.created_at > Date.now()
            || callback.received_at > Date.now()
            || Date.now() - pending.created_at > 15 * 60 * 1000
            || Date.now() - callback.received_at > 60 * 1000) {
          throw authError("This sign-in response has expired or was not requested by this tab. Please sign in again.");
        }
        const lifetime = Number(callback.expires_in);
        const value = {
          access_token: callback.access_token, refresh_token: callback.refresh_token,
          expires_at: callback.received_at + lifetime * 1000,
          provider_token: typeof callback.provider_token === "string" ? callback.provider_token : null,
          project_url: config.supabase_url,
        };
        if (!validSession(value) || !(lifetime > 0) || callback.token_type?.toLowerCase() !== "bearer") {
          throw authError("The sign-in response was incomplete. Please try again.");
        }
        saveSession(value);
      } else if (validSession(saved) && saved.project_url === config.supabase_url) {
        session = saved;
      }
      if (!session) {
        clearStorage();
        publish("signed-out");
        return;
      }
      const login = await verifyUser();
      if (version === epoch && session) publish("signed-in", "", login);
    } catch (error) {
      if (version === epoch) expire(error instanceof ApiError ? error.message : "Could not verify your session. Please sign in again.");
    }
  }

  function signIn() {
    if (!config?.auth_required || state.phase === "loading" || state.phase === "redirecting") return;
    try {
      sessionStorage.setItem(PENDING, JSON.stringify({ project_url: config.supabase_url, created_at: Date.now() }));
      const target = new URL(`${config.supabase_url}/auth/v1/authorize`);
      target.searchParams.set("provider", "github");
      target.searchParams.set("scopes", config.github_scopes || "public_repo");
      target.searchParams.set("redirect_to", `${location.origin}/`);
      publish("redirecting", "Opening GitHub…");
      location.assign(target.href);
    } catch {
      expire("Could not begin sign-in. Enable session storage and try again.");
    }
  }

  async function signOut() {
    const token = session?.access_token;
    expire("");
    const version = epoch;
    if (!token || !config?.auth_required) return;
    try {
      const response = await authFetch("/logout?scope=local", { method: "POST", token });
      if (!response.ok && response.status !== 401) throw new Error();
    } catch {
      if (version === epoch) publish("signed-out", "Signed out on this device. The server session could not be revoked.");
    }
  }

  configureAuthentication({ credentials, recover, expire });
  return {
    initialize, signIn, signOut,
    subscribe(listener) { listeners.add(listener); listener({ ...state }); return () => listeners.delete(listener); },
  };
}
