// Auth fragments are consumed by oauth-callback.js before this module runs.
export function createRouter({ onView = () => {}, browser = window, page = document } = {}) {
  let phase = "loading";
  // Back/Forward onto a page that is loaded fresh (e.g. "/" after the GitHub sign-in round trip) must show
  // that URL's view, not be treated as a first visit and bounced forward to /workspace.
  const traversal = browser.performance?.getEntriesByType?.("navigation")?.[0]?.type === "back_forward";
  const ready = () => phase === "signed-in" || phase === "local";
  const publicViews = { "/evaluations": "evaluations", "/documentation": "documentation" };

  function render() {
    // History traversal respects the path; only protected signed-out visits redirect.
    if (phase !== "loading" && !ready() && browser.location.pathname === "/workspace") {
      browser.history.replaceState(null, "", "/");
    }
    const view = publicViews[browser.location.pathname] || (ready() && browser.location.pathname === "/workspace" ? "workspace" : "landing");
    const titles = { workspace: "Workspace", landing: "Understand your code", evaluations: "Evaluations", documentation: "Documentation" };
    page.title = `RepoMind — ${titles[view]}`;
    onView(view);
  }

  function navigate(path) {
    if (phase === "loading" && !Object.hasOwn(publicViews, path)) return;
    if (path !== browser.location.pathname) browser.history.pushState(null, "", path);
    render();
  }

  browser.addEventListener("popstate", () => render());
  page.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[data-route]");
    if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
    const url = new URL(link.href, browser.location.origin);
    if (url.origin !== browser.location.origin || !["/", "/workspace", "/evaluations", "/documentation"].includes(url.pathname) || url.hash || url.search) return;
    event.preventDefault();
    navigate(url.pathname);
  });

  return {
    navigate,
    setPhase(value) {
      const previous = phase;
      phase = value;
      // Initial/session/OAuth redirects replace the current entry, never add one.
      if (ready() && !traversal && previous !== "signed-in" && previous !== "local" && browser.location.pathname === "/") {
        browser.history.replaceState(null, "", "/workspace");
      }
      render();
    },
  };
}
