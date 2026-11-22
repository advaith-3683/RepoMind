// Auth fragments are consumed by oauth-callback.js before this module runs.
export function createRouter({ onView = () => {}, browser = window, page = document } = {}) {
  let phase = "loading";
  const ready = () => phase === "signed-in" || phase === "local";

  function render(method = "replaceState") {
    const view = ready() ? "workspace" : "landing";
    const path = ready() ? "/workspace" : "/";
    // Do not navigate while the callback/session is still being validated.
    if (phase !== "loading" && browser.location.pathname !== path) browser.history[method](null, "", path);
    page.title = view === "workspace" ? "RepoMind — Workspace" : "RepoMind — Understand your code";
    onView(view);
  }

  browser.addEventListener("popstate", () => render());
  page.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[data-route]");
    if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
    const url = new URL(link.href, browser.location.origin);
    if (url.origin !== browser.location.origin || !["/", "/workspace"].includes(url.pathname) || url.hash || url.search) return;
    event.preventDefault();
    if (phase === "loading") return;
    if (url.pathname !== browser.location.pathname) browser.history.pushState(null, "", url.pathname);
    render();
  });

  return {
    setPhase(value) {
      const previous = phase;
      phase = value;
      render(previous !== "loading" && ready() ? "pushState" : "replaceState");
    },
  };
}
