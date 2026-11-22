// Auth fragments are consumed by oauth-callback.js before this module runs.
export function createRouter({ onView = () => {}, browser = window, page = document } = {}) {
  let phase = "loading";
  const ready = () => phase === "signed-in" || phase === "local";

  function render() {
    // History traversal respects the path; only protected signed-out visits redirect.
    if (phase !== "loading" && !ready() && browser.location.pathname === "/workspace") {
      browser.history.replaceState(null, "", "/");
    }
    const view = ready() && browser.location.pathname === "/workspace" ? "workspace" : "landing";
    page.title = view === "workspace" ? "RepoMind — Workspace" : "RepoMind — Understand your code";
    onView(view);
  }

  function navigate(path) {
    if (phase === "loading") return;
    if (path !== browser.location.pathname) browser.history.pushState(null, "", path);
    render();
  }

  browser.addEventListener("popstate", () => render());
  page.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[data-route]");
    if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
    const url = new URL(link.href, browser.location.origin);
    if (url.origin !== browser.location.origin || !["/", "/workspace"].includes(url.pathname) || url.hash || url.search) return;
    event.preventDefault();
    navigate(url.pathname);
  });

  return {
    navigate,
    setPhase(value) {
      const previous = phase;
      phase = value;
      // Initial/session/OAuth redirects replace the current entry, never add one.
      if (ready() && previous !== "signed-in" && previous !== "local" && browser.location.pathname === "/") {
        browser.history.replaceState(null, "", "/workspace");
      }
      render();
    },
  };
}
