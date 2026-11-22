import { repositoryRequest, ApiError } from "../api.js";

// The server's verified list is the only source of chip targets.
export function citationParts(text, citations) {
  const parts = [];
  let cursor = 0;
  for (const match of text.matchAll(/\[([\w./\-@+()][\w./\-@+() ]*):(\d+)(?:-(\d+))?\]/g)) {
    const citation = citations.find((item) => item.path === match[1]
      && item.start === Number(match[2]) && item.end === Number(match[3] || match[2]));
    if (!citation) continue;
    if (match.index > cursor) parts.push({ text: text.slice(cursor, match.index) });
    parts.push({ text: match[0], citation });
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) });
  return parts;
}

export function initCitationPreview() {
  const dialog = document.querySelector("[data-citation-dialog]");
  const title = dialog.querySelector("h2");
  const content = dialog.querySelector("[data-citation-content]");
  let controller;
  let trigger;
  function close() { controller?.abort(); if (dialog.open) dialog.close(); }
  dialog.querySelector("button").addEventListener("click", close);
  dialog.addEventListener("close", () => { controller?.abort(); if (trigger?.isConnected) trigger.focus(); });
  return {
    close,
    async open(repoId, citation, button) {
      controller?.abort();
      const request = new AbortController();
      controller = request;
      trigger = button;
      title.textContent = `${citation.path}:${citation.start}-${citation.end}`;
      content.textContent = "Loading source evidence…";
      dialog.showModal();
      const start = Math.max(1, citation.start - 5);
      const end = Math.min(start + 199, citation.end + 5);
      const query = new URLSearchParams({ path: citation.path, start, end });
      try {
        const data = await repositoryRequest(`/repos/${encodeURIComponent(repoId)}/file?${query}`, { signal: request.signal });
        if (request.signal.aborted) return;
        if (!Number.isInteger(data.start) || !Array.isArray(data.lines) || !data.lines.every((line) => typeof line === "string")) {
          throw new ApiError("The source service returned an invalid file preview.", "invalid_response");
        }
        content.replaceChildren(...data.lines.map((text, index) => {
          const line = document.createElement("span");
          const number = data.start + index;
          line.className = number >= citation.start && number <= citation.end ? "source-line cited-line" : "source-line";
          line.textContent = `${number}  ${text}`;
          return line;
        }));
      } catch (error) {
        if (!request.signal.aborted) content.textContent = error.message;
      }
    },
  };
}
