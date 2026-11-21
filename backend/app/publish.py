"""Turn an approved proposal into a GitHub pull request or issue.

Safety rules: only ever a NEW branch + pull request (never a direct commit to the default branch);
nothing is touched until the edits are re-applied successfully to the file as it is on GitHub *now*;
if anything fails after the branch exists, the branch is deleted so a retry starts clean."""
import re

from .github import GitHubClient
from .guardrails.errors import GuardrailViolation
from .hitl.patching import PatchError, apply_edits

FOOTER = "_Proposed by RepoMind from automated code analysis. Reviewed and approved by {who} before this was opened._"


def branch_name(title: str, action_id) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "change"
    return f"repomind/{slug}-{str(action_id)[:8]}"


def publish_action(gh: GitHubClient, owner: str, repo: str, kind: str, payload: dict, action_id, approver: str | None) -> dict:
    body = f"{payload['body']}\n\n---\n{FOOTER.format(who=approver or 'a reviewer')}"

    if kind == "issue":
        issue = gh.create_issue(owner, repo, payload["title"], body)
        return {"kind": "issue", "url": issue["html_url"], "number": issue["number"]}
    if kind != "patch":
        raise GuardrailViolation("invalid_proposal", f"Unknown proposal kind '{kind}'.")

    info = gh.repo_info(owner, repo)
    if not info["can_push"]:
        raise GuardrailViolation(
            "github_no_permission",
            f"You don't have write access to {owner}/{repo}, so a pull request can't be opened from here. "
            "You can publish it as an issue instead, or fork the repo and apply the diff yourself.",
        )
    base = info["default_branch"]

    # Re-apply to the file as it is on GitHub right now (it may have changed since we indexed it).
    current, file_sha = gh.get_file(owner, repo, payload["path"], base)
    crlf = "\r\n" in current
    try:
        new = apply_edits(current.replace("\r\n", "\n"), payload["edits"])
    except PatchError as e:
        raise GuardrailViolation(
            "github_conflict",
            f"The file changed on GitHub since it was analysed, so this edit no longer applies ({e}). "
            "Re-index the repo and ask again.",
        )
    if crlf:
        new = new.replace("\n", "\r\n")
    if new == current:
        raise GuardrailViolation("github_conflict", "The edit would not change the file on GitHub (already applied?).")

    branch = branch_name(payload["title"], action_id)
    gh.create_branch(owner, repo, branch, gh.branch_sha(owner, repo, base))
    try:
        gh.put_file(owner, repo, payload["path"], new, payload["title"], branch, file_sha)
        pr = gh.create_pr(owner, repo, payload["title"], branch, base, body)
    except Exception:
        gh.delete_branch(owner, repo, branch)
        raise
    return {"kind": "patch", "url": pr["html_url"], "number": pr["number"], "branch": branch}
