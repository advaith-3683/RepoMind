// Runs before styles and modules: authentication fragments must leave the URL first.
(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const query = new URLSearchParams(location.search);
  const names = ["access_token", "refresh_token", "provider_token", "provider_refresh_token", "error", "error_description", "error_code"];
  if (!names.some((key) => fragment.has(key) || query.has(key))) return;
  try {
    history.replaceState(null, "", location.pathname);
  } catch {
    location.replace(location.pathname);
    return;
  }
  try {
    const callback = { received_at: Date.now() };
    if (fragment.has("error") || fragment.has("error_code") || names.some((key) => query.has(key))) {
      callback.error = true;
    } else {
      for (const key of ["access_token", "refresh_token", "expires_in", "token_type", "provider_token"]) {
        callback[key] = fragment.get(key);
      }
    }
    sessionStorage.setItem("repomind.oauth.callback", JSON.stringify(callback));
  } catch {
    document.documentElement.dataset.authCallbackError = "storage";
  }
})();
