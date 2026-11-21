import { ApiError, repositoryRequest } from "../api.js";

const STORAGE_KEY = "repomind.selectedRepoId";
const ACTIVE = new Set(["pending", "cloning", "indexing"]);
const STATUSES = new Set([...ACTIVE, "ready", "failed"]);
const URL_PATTERN = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/?$/;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function validateRepo(repo) {
  if (!repo || typeof repo.id !== "string" || typeof repo.name !== "string"
      || typeof repo.url !== "string" || !STATUSES.has(repo.status)) {
    throw new ApiError("The repository service returned an unexpected repository object.", "invalid_response");
  }
  return repo;
}

export function initRepositories({ autoLoad = true, onChange = () => {} } = {}) {
  const lists = [...document.querySelectorAll("[data-repo-list]")];
  const forms = [...document.querySelectorAll("[data-repo-form]")];
  const messages = [...document.querySelectorAll("[data-repo-message]")];
  const picker = document.querySelector(".mobile-picker");
  let repos = [];
  let selectedId = null;
  let selectionCleared = false;
  let loading = true;
  let adding = false;
  let authRequired = false;
  let cooldownUntil = 0;
  let cooldownTimer;
  let controller = new AbortController();
  const polls = new Map();
  const pollErrors = new Map();
  const removals = new Set();
  const removalErrors = new Map();
  let listError = null;
  let savedId = null;
  let enabled = autoLoad;
  let generation = 0;
  try { savedId = localStorage.getItem(STORAGE_KEY); } catch { /* Storage may be unavailable. */ }

  function persist() {
    try {
      if (selectedId) localStorage.setItem(STORAGE_KEY, selectedId);
      else localStorage.removeItem(STORAGE_KEY);
    } catch { /* Selection still works for this page. */ }
  }

  function reconcileSelection() {
    if (selectionCleared && selectedId === null) return;
    if (!repos.some((repo) => repo.id === selectedId && repo.status === "ready")) {
      selectedId = repos.find((repo) => repo.id === savedId && repo.status === "ready")?.id
        || repos.find((repo) => repo.status === "ready")?.id || null;
    }
    savedId = selectedId;
    persist();
  }

  function clearPolls() {
    polls.forEach((timer) => clearTimeout(timer));
    polls.clear();
  }

  function showError(error) {
    if (error.status === 401) {
      authRequired = true;
      listError = error;
      clearPolls();
      controller.abort();
    }
    if (error.status === 429) {
      cooldownUntil = Date.now() + 10000;
      clearTimeout(cooldownTimer);
      cooldownTimer = setTimeout(render, 10000);
    }
  }

  function renderMessages() {
    messages.forEach((container) => {
      container.replaceChildren();
      const text = loading ? "Loading repositories…" : listError
        ? `${listError.message}${authRequired ? " Please sign in to access repositories." : ""}`
        : repos.length === 0 ? "No repositories yet. Add a public GitHub repository." : "";
      if (text) container.append(element("p", "repo-message", text));
      if (listError && !authRequired) {
        const retry = element("button", "repo-retry", "Retry loading");
        retry.type = "button";
        retry.disabled = loading || adding || Date.now() < cooldownUntil;
        retry.addEventListener("click", load);
        container.append(retry);
      }
    });
  }

  function render() {
    const selected = repos.find((repo) => repo.id === selectedId && repo.status === "ready");
    onChange({ loading, error: listError?.message || null, selectedRepo: selected || null, repoCount: repos.length });
    const name = selected?.name || (loading ? "Loading repositories…" : "Select a repository");
    document.querySelector("[data-selected-name]").textContent = name;
    document.querySelector("[data-selected-status]").textContent = selected ? "/ Ready" : "";
    const hasIndexCounts = Number.isInteger(selected?.files_indexed) && selected.files_indexed >= 0
      && Number.isInteger(selected?.chunks_indexed) && selected.chunks_indexed >= 0;
    document.querySelector("[data-selected-description]").textContent = selected
      ? hasIndexCounts ? `${selected.files_indexed} files · ${selected.chunks_indexed} chunks` : "Ready · Change repository"
      : authRequired ? "Authentication required" : "Select a ready repository";
    document.querySelector("[data-repo-context]").textContent = selected ? `${selected.name} / Ready` : "No repository selected";
    document.querySelector("[data-repo-composer]").textContent = selected ? `Repo · ${selected.name}` : "No repository selected";
    document.querySelectorAll("[data-repo-count]").forEach((node) => {
      node.textContent = loading ? "Loading repositories…" : listError && !repos.length
        ? "Repositories unavailable" : `${repos.length} connected`;
    });
    renderMessages();
    const focused = document.activeElement;
    const focusId = focused?.dataset.repoId;
    const focusRemoveId = focused?.dataset.removeRepoId;
    const focusList = lists.find((list) => list.contains(focused));
    lists.forEach((list) => {
      const expandedFailures = new Set([...list.querySelectorAll(".repo-failure[open]")]
        .map((details) => details.parentElement.querySelector("[data-repo-id]").dataset.repoId));
      list.setAttribute("aria-busy", String(loading));
      list.replaceChildren(...repos.map((repo) => {
        const item = element("li", `repo${repo.id === selectedId ? " selected" : ""}`);
        const button = element("button", "repo-select");
        button.type = "button";
        button.dataset.repoId = repo.id;
        button.disabled = repo.status !== "ready" || authRequired;
        button.setAttribute("aria-pressed", String(repo.id === selectedId));
        button.append(element("p", "repo-name", repo.name),
          element("span", `repo-status status-${repo.status}`, repo.status[0].toUpperCase() + repo.status.slice(1)));
        button.addEventListener("click", () => {
          selectedId = repo.id;
          selectionCleared = false;
          savedId = selectedId;
          persist();
          render();
          picker.open = false;
          if (window.matchMedia("(max-width: 900px)").matches) picker.querySelector("summary").focus();
        });
        const row = element("div", "repo-row");
        const removeButton = element("button", "repo-remove", "✕");
        removeButton.type = "button";
        removeButton.dataset.removeRepoId = repo.id;
        removeButton.setAttribute("aria-label", `Remove ${repo.name}`);
        removeButton.disabled = loading || authRequired || removals.has(repo.id) || Date.now() < cooldownUntil;
        removeButton.addEventListener("click", () => remove(repo));
        row.append(button, removeButton);
        item.append(row);
        const removalMessage = element("p", "repo-message repo-removal-message", removalErrors.get(repo.id) || "");
        removalMessage.setAttribute("role", "status");
        item.append(removalMessage);
        if (repo.status === "failed") {
          const details = element("details", "repo-failure");
          details.open = expandedFailures.has(repo.id);
          details.append(element("summary", "", "Indexing failed — details"),
            element("p", "", repo.error || "Indexing failed. Retry the connection."));
          const retry = element("button", "repo-retry", "Retry connection");
          retry.type = "button";
          retry.disabled = adding || authRequired || Date.now() < cooldownUntil;
          retry.addEventListener("click", () => add(repo.url));
          details.append(retry);
          item.append(details);
        }
        if (pollErrors.has(repo.id)) {
          item.append(element("p", "repo-message", pollErrors.get(repo.id).message));
          const retry = element("button", "repo-retry", "Retry status check");
          retry.type = "button";
          retry.disabled = authRequired || Date.now() < cooldownUntil;
          retry.addEventListener("click", () => {
            pollErrors.delete(repo.id);
            schedule(repo.id);
            render();
          });
          item.append(retry);
        }
        return item;
      }));
    });
    if (focusId && focusList) [...focusList.querySelectorAll("[data-repo-id]")]
      .find((node) => node.dataset.repoId === focusId)?.focus();
    if (focusRemoveId && focusList) {
      const target = [...focusList.querySelectorAll("[data-remove-repo-id]")]
        .find((node) => node.dataset.removeRepoId === focusRemoveId);
      if (target) target.focus();
      else (focusList.querySelector(".repo-remove:not(:disabled)")
        || forms[lists.indexOf(focusList)]?.querySelector("input"))?.focus();
    }
    forms.forEach((form) => {
      form.querySelector("button").disabled = loading || adding || authRequired || Date.now() < cooldownUntil;
      form.querySelector("input").disabled = loading || adding || authRequired;
      form.querySelector("button").textContent = adding ? "Adding…" : "+ Add repository";
    });
  }

  async function remove(repo) {
    if (!enabled || loading || authRequired || removals.has(repo.id) || Date.now() < cooldownUntil) return;
    if (!window.confirm(`Remove ${repo.name}? This deletes its index and history.`)) return;
    const focusList = lists.find((list) => list.contains(document.activeElement));
    const version = generation;
    removals.add(repo.id);
    removalErrors.delete(repo.id);
    render();
    try {
      await repositoryRequest(`/repos/${encodeURIComponent(repo.id)}`, { method: "DELETE", signal: controller.signal });
      if (version !== generation) return;
      repos = repos.filter((item) => item.id !== repo.id);
      clearTimeout(polls.get(repo.id));
      polls.delete(repo.id);
      pollErrors.delete(repo.id);
      if (selectedId === repo.id) {
        selectedId = null;
        savedId = null;
        selectionCleared = true;
        persist();
      }
    } catch (error) {
      if (version === generation && error.name !== "AbortError") {
        showError(error);
        removalErrors.set(repo.id, error.status === 409 && error.code === "repo_not_ready"
          ? "This repository is being indexed. Try again in a minute." : error.message);
      }
    } finally {
      if (version === generation) {
        removals.delete(repo.id);
        render();
        if (focusList && document.activeElement === document.body) {
          const target = [...focusList.querySelectorAll("[data-remove-repo-id]")]
            .find((node) => node.dataset.removeRepoId === repo.id);
          (target || focusList.querySelector(".repo-remove:not(:disabled)")
            || forms[lists.indexOf(focusList)]?.querySelector("input"))?.focus();
        }
      }
    }
  }

  function schedule(id) {
    if (!enabled || loading || polls.has(id) || authRequired || controller.signal.aborted || pollErrors.has(id)
        || !repos.some((repo) => repo.id === id && ACTIVE.has(repo.status))) return;
    const version = generation;
    polls.set(id, setTimeout(async () => {
      try {
        const repo = validateRepo(await repositoryRequest(`/repos/${encodeURIComponent(id)}`, { signal: controller.signal }));
        if (version !== generation) return;
        if (repo.id !== id) throw new ApiError("The status response does not match this repository.", "invalid_response");
        repos = repos.map((item) => item.id === id ? repo : item);
        reconcileSelection();
      } catch (error) {
        if (version === generation && error.name !== "AbortError") {
          pollErrors.set(id, error);
          showError(error);
        }
      } finally {
        if (version === generation) {
          polls.delete(id);
          render();
          schedule(id);
        }
      }
    }, 2000));
  }

  async function load() {
    if (!enabled || authRequired) return;
    const version = ++generation;
    clearPolls();
    controller.abort();
    controller = new AbortController();
    loading = true;
    listError = null;
    pollErrors.clear();
    removalErrors.clear();
    removals.clear();
    render();
    try {
      const data = await repositoryRequest("/repos", { signal: controller.signal });
      if (version !== generation) return;
      if (!Array.isArray(data)) throw new ApiError("The repository service returned an unexpected list.", "invalid_response");
      repos = data.map(validateRepo);
      reconcileSelection();
    } catch (error) {
      if (version === generation && error.name !== "AbortError") { listError = error; showError(error); }
    } finally {
      if (version === generation) {
        loading = false;
        render();
        repos.forEach((repo) => schedule(repo.id));
      }
    }
  }

  async function add(url, sourceForm) {
    if (!enabled || loading || adding || authRequired || Date.now() < cooldownUntil) return;
    const version = generation;
    if (!URL_PATTERN.test(url)) {
      if (sourceForm) {
        sourceForm.querySelector("[data-form-message]").textContent = "Enter a public GitHub URL: https://github.com/owner/repo";
        sourceForm.querySelector("input").setAttribute("aria-invalid", "true");
        sourceForm.querySelector("input").focus();
      }
      return;
    }
    adding = true;
    forms.forEach((form) => {
      form.querySelector("[data-form-message]").textContent = "";
      form.querySelector("input").removeAttribute("aria-invalid");
    });
    render();
    try {
      const repo = validateRepo(await repositoryRequest("/repos", {
        method: "POST", body: { url }, signal: controller.signal,
      }));
      if (version !== generation) return;
      repos = [repo, ...repos.filter((item) => item.id !== repo.id)];
      if (!ACTIVE.has(repo.status)) {
        clearTimeout(polls.get(repo.id));
        polls.delete(repo.id);
      }
      listError = null;
      pollErrors.delete(repo.id);
      reconcileSelection();
      if (sourceForm) sourceForm.reset();
      forms.forEach((form) => { form.querySelector("[data-form-message]").textContent = `${repo.name}: ${repo.status}.`; });
      schedule(repo.id);
    } catch (error) {
      if (version === generation && error.name !== "AbortError") {
        showError(error);
        forms.forEach((form) => { form.querySelector("[data-form-message]").textContent = error.message; });
      }
    } finally {
      if (version === generation) {
        adding = false;
        render();
      }
    }
  }

  forms.forEach((form) => form.addEventListener("submit", (event) => {
    event.preventDefault();
    add(form.elements.url.value.trim(), form);
  }));
  document.querySelector("[data-open-repos]").addEventListener("click", () => {
    picker.open = true;
    picker.scrollIntoView({ block: "start", behavior: "instant" });
    picker.querySelector("summary").focus();
  });
  window.addEventListener("pagehide", () => { clearPolls(); clearTimeout(cooldownTimer); controller.abort(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) load(); });
  if (autoLoad) load();
  return {
    start() { enabled = true; authRequired = false; load(); },
    stop() {
      enabled = false;
      generation += 1;
      clearPolls();
      clearTimeout(cooldownTimer);
      controller.abort();
      repos = [];
      selectedId = null;
      selectionCleared = false;
      loading = false;
      adding = false;
      authRequired = false;
      listError = null;
      cooldownUntil = 0;
      pollErrors.clear();
      removalErrors.clear();
      removals.clear();
      forms.forEach((form) => {
        form.reset();
        form.querySelector("[data-form-message]").textContent = "";
      });
      picker.open = false;
      render();
    },
  };
}
