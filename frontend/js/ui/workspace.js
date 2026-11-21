// Repository state only. No answer, evidence, run statistics, or trace is invented.
export function workspaceView({ loading, error, selectedRepo, repoCount = 0 }) {
  if (loading) return {
    state: "loading", label: "Library", title: "Loading repositories…",
    description: "Retrieving your available repositories.",
  };
  if (error) return {
    state: "error", label: "Repository service", title: "Could not load repositories",
    description: error,
  };
  if (selectedRepo?.status === "ready") return {
    state: "ready", label: "Repository ready", title: "Ready to explore",
    description: "Ask a question about your selected repository. Answers will cite the retrieved code.",
  };
  return {
    state: "empty", label: "Workspace", title: "Select a repository to start exploring your codebase.",
    description: repoCount ? "Select a ready repository. Repositories still indexing will become available when ready."
      : "Add a public GitHub repository, then select it when indexing is ready.",
  };
}

export function renderWorkspaceState(repositoryState) {
  const view = workspaceView(repositoryState);
  for (const field of ["label", "title", "description"]) {
    document.querySelectorAll(`[data-workspace-${field}]`).forEach((node) => { node.textContent = view[field]; });
  }
  document.querySelectorAll("[data-workspace-state]").forEach((node) => { node.dataset.workspaceState = view.state; });
  for (const id of ["question", "mobile-question"]) document.getElementById(id).disabled = view.state !== "ready";
}
