export class ApiError extends Error {
  constructor(message, code, status = 0) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

let authentication;

export function configureAuthentication(handler) {
  authentication = handler;
}

export async function publicRequest(path, options = {}) {
  return parseResponse(await backendFetch(path, options));
}

async function backendFetch(path, { method = "GET", body, signal, accessToken } = {}) {
  const base = (window.REPOMIND_API_BASE || "").replace(/\/$/, "");
  let response;
  try {
    response = await fetch(`${base}${path}`, {
      method, signal,
      cache: "no-store", redirect: "error", credentials: "omit",
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("Could not reach the repository service. Check your connection and retry.", "network_error");
  }
  return response;
}

async function parseResponse(response) {
  let data;
  try { data = await response.json(); } catch {
    throw new ApiError(`The repository service returned an invalid response (HTTP ${response.status}).`, "invalid_response", response.status);
  }
  if (!response.ok) {
    const validation = Array.isArray(data?.detail)
      ? data.detail.map((item) => item.msg).join("; ") : null;
    throw new ApiError(data?.error?.message || validation || `Repository request failed (HTTP ${response.status}).`,
      data?.error?.code || "request_failed", response.status);
  }
  return data;
}

export async function repositoryRequest(path, options = {}) {
  if (!authentication) throw new ApiError("Sign-in is still loading.", "unauthorized", 401);
  let credentials = await authentication.credentials();
  if (options.signal?.aborted) throw new DOMException("Request cancelled", "AbortError");
  let response = await backendFetch(path, { ...options, accessToken: credentials.accessToken });
  if (response.status === 401) {
    credentials = await authentication.recover(credentials);
    if (credentials) {
      if (options.signal?.aborted) throw new DOMException("Request cancelled", "AbortError");
      response = await backendFetch(path, { ...options, accessToken: credentials.accessToken });
    }
    if (!credentials || response.status === 401) {
      authentication.expire();
      throw new ApiError("Please sign in to continue.", "unauthorized", 401);
    }
  }
  return parseResponse(response);
}
