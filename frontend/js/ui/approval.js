import { repositoryRequest } from "../api.js";
import { toast } from "./toast.js";

const signInAgain = "Sign in with GitHub again to publish.";
const publishErrors = {
  github_auth: signInAgain,
  missing_github_token: signInAgain,
  github_no_permission: "You need write access to open a PR on this repo.",
  github_conflict: "The file changed on GitHub or the edit no longer applies. Re-index and ask again.",
  publish_in_progress: "Publishing is already running. Wait a moment.",
  publish_not_ready: "Approve this action first, then retry publishing.",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function githubResultUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "github.com" && !url.username && !url.password
      ? url.href : null;
  } catch { return null; }
}

export function diffPreview(diff) {
  const pre = element("pre", "approval-diff");
  pre.setAttribute("aria-label", "Server-computed diff");
  diff.split("\n").forEach((text, index, lines) => {
    const style = text.startsWith("+") ? "diff-added" : text.startsWith("-") ? "diff-removed" : "";
    pre.append(element("span", style, text + (index < lines.length - 1 ? "\n" : "")));
  });
  return pre;
}

// One state is shared by the desktop and mobile cards; only explicit clicks mutate it.
export function createApproval(event, { onChange = () => {}, onRateLimit = () => {}, notify = toast } = {}) {
  let payload = event.payload, diff = typeof payload.diff === "string" ? payload.diff : "";
  let status = "pending", editing = false, busy = false, publishState = "idle";
  let draft, message = "", result, already = false, cooldownUntil = 0, cooldownTimer, disposed = false;
  const controller = new AbortController();
  const path = `/actions/${encodeURIComponent(event.action_id)}`;
  const changed = () => { if (!disposed) onChange(); };
  const blocked = () => busy || Date.now() < cooldownUntil;

  function failure(error, publishing) {
    if (publishing) publishState = "error";
    if (!publishing && error.code === "invalid_transition") {
      status = "decided"; editing = false;
      message = "This action was already decided.";
    } else message = (publishing && publishErrors[error.code]) || error.message;
    if (error.code === "rate_limited") {
      notify(message);
      cooldownUntil = Date.now() + 10000;
      onRateLimit();
      clearTimeout(cooldownTimer);
      cooldownTimer = setTimeout(changed, 10000);
    }
  }

  async function decide(decision) {
    if (status !== "pending" || blocked()) return;
    busy = true; message = ""; changed();
    const body = { decision };
    if (decision === "edit") body.edited_payload = {
      title: draft.title, body: draft.body,
      ...(event.kind === "patch" ? { path: payload.path, edits: draft.edits.map((edit) => ({ old: edit.old, new: edit.new })) } : {}),
    };
    try {
      const response = await repositoryRequest(`${path}/decision`, { method: "POST", body, signal: controller.signal });
      if (disposed) return;
      status = response.status; payload = response.payload;
      diff = typeof response.diff === "string" ? response.diff : "";
      editing = false;
    } catch (error) { if (!disposed) failure(error, false); }
    finally { busy = false; changed(); }
  }

  async function publish() {
    if (!["approved", "edited"].includes(status) || blocked() || publishState === "published") return;
    let token;
    try { token = JSON.parse(sessionStorage.getItem("repomind.auth.session") || "null")?.provider_token; } catch { /* Ask for sign-in below. */ }
    if (typeof token !== "string" || !token.trim()) {
      message = signInAgain; publishState = "error"; changed(); return;
    }
    busy = true; publishState = "publishing"; message = ""; changed();
    try {
      const response = await repositoryRequest(`${path}/publish`, { method: "POST", githubToken: token, signal: controller.signal });
      if (disposed) return;
      result = response.result; already = response.already === true; publishState = "published";
    } catch (error) { if (!disposed) failure(error, true); }
    finally { busy = false; changed(); }
  }

  function render() {
    const card = element("section", "pending-approval approval-card");
    card.dataset.actionStatus = status;
    card.dataset.publishState = publishState;
    card.setAttribute("aria-busy", String(busy));
    card.append(element("p", "eyebrow", `Human-in-the-loop / ${event.kind}`));
    const published = publishState === "published";
    const publishedNumber = Number.isInteger(result?.number) && result.number > 0 ? ` #${result.number}` : "";
    const heading = published ? (event.kind === "patch" ? "Pull request opened" : "Issue created") + publishedNumber
      : status === "rejected" ? "Proposal rejected" : status === "decided" ? "Decision unavailable"
        : status === "pending" ? (editing ? "Edit proposal" : "Review before action") : "Decision recorded successfully.";
    const title = element("h2", "approval-heading", heading);
    title.tabIndex = -1;
    card.append(title);
    if (payload) {
      const summary = element("div", "approval-summary");
      summary.append(element("h3", "", payload.title), element("p", "approval-body", payload.body));
      if (event.kind === "patch") summary.append(element("p", "mono", payload.path));
      card.append(summary);
    }
    if (editing) {
      const field = (label, value, update, mono = false) => {
        const wrapper = element("label", "approval-field", label);
        const input = element(label === "Title" ? "input" : "textarea", mono ? "mono" : "");
        input.value = value; input.disabled = blocked();
        if (input.tagName === "TEXTAREA") input.rows = 5;
        input.addEventListener("input", () => update(input.value));
        wrapper.append(input); card.append(wrapper);
      };
      field("Title", draft.title, (value) => { draft.title = value; });
      field("Body", draft.body, (value) => { draft.body = value; });
      if (event.kind === "patch") draft.edits.forEach((edit, index) => {
        card.append(element("pre", "approval-diff", `Original text ${index + 1}\n${edit.old}`));
        field(`Replacement text ${index + 1}`, edit.new, (value) => { edit.new = value; }, true);
      });
      card.append(element("p", "muted", "This records your edited version as approved. Nothing is published to GitHub until you click "
        + (event.kind === "patch" ? "Open pull request." : "Create issue.")));
    }
    if (event.kind === "patch" && diff) {
      card.append(element("p", "eyebrow", editing ? "Current server diff / recomputed after approval" : "Server-computed diff"), diffPreview(diff));
    }
    if (published) {
      if (already) card.append(element("p", "muted", "Already published."));
      const url = githubResultUrl(result?.url);
      if (url) {
        const link = element("a", "primary", event.kind === "patch" ? "View pull request on GitHub" : "View issue on GitHub");
        link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
        card.append(link);
      } else card.append(element("p", "chat-error", "GitHub returned an invalid result link."));
      if (result?.branch) card.append(element("p", "mono", result.branch));
    } else card.append(element("p", "muted", "Nothing has been applied. Approval only records your decision."));
    if (["approved", "edited"].includes(status) && !published) {
      card.append(element("p", "approval-confirmation", event.kind === "patch"
        ? "Publish this approved patch to GitHub as a new branch and pull request?"
        : "Publish this approved proposal as a GitHub issue?"));
    }
    if (message) {
      const error = element("p", "chat-error", message);
      error.setAttribute("role", "status"); card.append(error);
    }
    const actions = element("div", "approval-actions");
    const button = (label, action, primary = false, disabled = blocked()) => {
      const node = element("button", primary ? "primary" : "approval-secondary", label);
      node.type = "button"; node.disabled = disabled;
      node.addEventListener("click", action); actions.append(node);
    };
    if (["pending", "decided"].includes(status)) {
      const disabled = blocked() || status === "decided";
      button("Reject", () => decide("reject"), false, disabled);
      button(editing ? "Cancel edit" : "Edit", () => {
        editing = !editing;
        if (editing) draft = { title: payload.title, body: payload.body || "", edits: (payload.edits || []).map((edit) => ({ ...edit })) };
        changed();
      }, false, disabled);
      button(editing ? "Approve edited version" : "Approve", () => decide(editing ? "edit" : "approve"), true, disabled);
    }
    if (["approved", "edited"].includes(status) && !published) {
      button(publishState === "publishing" ? "Publishing…" : event.kind === "patch" ? "Open pull request" : "Create issue", publish, true);
    }
    if (event.kind === "patch" && diff) {
      button("Copy diff", async () => {
        try { await navigator.clipboard.writeText(diff); notify("Diff copied."); }
        catch { notify("Could not copy the diff. Download the patch instead."); }
      }, false, false);
      button("Download .patch", () => {
        const url = URL.createObjectURL(new Blob([diff], { type: "text/plain;charset=utf-8" }));
        const link = element("a"); link.href = url; link.download = "repomind.patch";
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }, false, false);
    }
    card.append(actions);
    return card;
  }
  return {
    render,
    statusText() {
      if (publishState === "published") return "Published to GitHub";
      if (publishState === "publishing") return "Publishing to GitHub…";
      return { pending: "Pending approval", approved: "Approved / ready to publish", edited: "Edited / ready to publish",
        rejected: "Proposal rejected", decided: "Action already decided" }[status];
    },
    dispose() { disposed = true; controller.abort(); clearTimeout(cooldownTimer); },
  };
}
