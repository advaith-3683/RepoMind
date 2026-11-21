"""Minimal GitHub REST client (httpx). The user's GitHub token is passed per request, used only
in the Authorization header, and never stored or logged. Error messages never include it."""
import base64
from urllib.parse import quote

import httpx

from .guardrails.errors import GuardrailViolation

MAX_FILE_BYTES = 900_000   # the contents API does not return files over ~1 MB


class GitHubClient:
    def __init__(self, token: str, base_url: str = "https://api.github.com", client: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self._c = client or httpx.Client(timeout=20.0)
        self._h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "RepoMind",
        }

    # ---- plumbing ----
    def _send(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return self._c.request(method, f"{self.base}{path}", headers=self._h, **kw)
        except httpx.TransportError:
            raise GuardrailViolation("github_error", "Could not reach GitHub. Try again in a moment.")

    @staticmethod
    def _message(r: httpx.Response) -> str:
        try:
            return str(r.json().get("message", ""))[:200]
        except ValueError:
            return ""

    def _ok(self, r: httpx.Response, what: str, ok: tuple[int, ...] = (200, 201)) -> dict:
        if r.status_code in ok:
            try:
                return r.json() if r.content else {}
            except ValueError:
                raise GuardrailViolation("github_error", f"GitHub returned an unexpected response while trying to {what}.")
        if r.status_code == 401:
            raise GuardrailViolation("github_auth", "GitHub rejected your token. Sign in with GitHub again and retry.")
        if r.status_code in (403, 404):
            raise GuardrailViolation(
                "github_no_permission",
                f"GitHub says you can't {what}: you may lack write access, or the repository/file doesn't exist.",
            )
        if r.status_code in (409, 422):
            raise GuardrailViolation("github_conflict", f"GitHub could not {what}: {self._message(r) or 'conflict'}.")
        raise GuardrailViolation("github_error", f"GitHub returned HTTP {r.status_code} while trying to {what}.")

    # ---- reads ----
    def repo_info(self, owner: str, repo: str) -> dict:
        d = self._ok(self._send("GET", f"/repos/{owner}/{repo}"), "read the repository")
        return {"default_branch": d["default_branch"], "can_push": bool((d.get("permissions") or {}).get("push"))}

    def branch_sha(self, owner: str, repo: str, branch: str) -> str:
        d = self._ok(self._send("GET", f"/repos/{owner}/{repo}/git/ref/heads/{quote(branch, safe='/')}"),
                     "read the base branch")
        return d["object"]["sha"]

    def get_file(self, owner: str, repo: str, path: str, ref: str) -> tuple[str, str]:
        """Return (utf-8 text, blob sha)."""
        d = self._ok(self._send("GET", f"/repos/{owner}/{repo}/contents/{quote(path)}", params={"ref": ref}),
                     "read the file")
        if not isinstance(d, dict) or d.get("type") != "file":
            raise GuardrailViolation("github_conflict", f"'{path}' is not a file on {ref}.")
        if d.get("size", 0) > MAX_FILE_BYTES or d.get("encoding") != "base64" or "content" not in d:
            raise GuardrailViolation("github_conflict", f"'{path}' is too large to edit through the GitHub API.")
        try:
            return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]
        except (ValueError, UnicodeDecodeError):
            raise GuardrailViolation("github_conflict", f"'{path}' is not UTF-8 text.")

    # ---- writes ----
    def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> None:
        self._ok(self._send("POST", f"/repos/{owner}/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}),
                 "create a branch")

    def put_file(self, owner: str, repo: str, path: str, text: str, message: str, branch: str, file_sha: str) -> None:
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "sha": file_sha, "branch": branch}
        self._ok(self._send("PUT", f"/repos/{owner}/{repo}/contents/{quote(path)}", json=body), "commit the change")

    def create_pr(self, owner: str, repo: str, title: str, head: str, base: str, body: str) -> dict:
        return self._ok(self._send("POST", f"/repos/{owner}/{repo}/pulls",
                                   json={"title": title, "head": head, "base": base, "body": body}),
                        "open a pull request")

    def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict:
        return self._ok(self._send("POST", f"/repos/{owner}/{repo}/issues", json={"title": title, "body": body}),
                        "open an issue")

    def delete_branch(self, owner: str, repo: str, branch: str) -> None:
        """Best-effort cleanup; never raises."""
        try:
            self._send("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{quote(branch, safe='/')}")
        except Exception:  # noqa: BLE001
            pass
