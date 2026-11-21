// LIVE API check, deliberately separate from all mocked tests.
// Use the manually signed-in browser page with the local frontend assets loaded.
// Never reads session storage, credentials, request headers, or OAuth URLs.
// Never seeds repositories, alters auth, or substitutes API responses.
(async (page) => {
  const origin = "https://repomind-gt4x.onrender.com";
  const phase = await page.locator("html").getAttribute("data-auth-state");
  if (phase !== "signed-in") return { verification: "LIVE", status: "BLOCKED", reason: "Manual sign-in is required in the live-test browser." };
  await page.setViewportSize({ width: 1440, height: 900 });
  const repository = page.locator(".repositories .repo-select").filter({ hasText: "advaith-3683/portfolio" });
  if (await repository.count() !== 1 || await repository.isDisabled()) {
    return { verification: "LIVE", status: "BLOCKED", reason: "The real portfolio repository is missing or not ready." };
  }
  await repository.click();
  const repoId = await repository.getAttribute("data-repo-id");
  const selected = await repository.getAttribute("aria-pressed") === "true";
  if (!selected) return { verification: "LIVE", status: "BLOCKED", reason: "Real repository selection failed." };
  const question = "What does this portfolio repository contain, and which file is its entry point? Cite the code you find.";
  await page.locator('.composer [data-mode="fast"]').click();
  await page.locator("#question").fill(question);
  const responsePromise = page.waitForResponse((response) => response.url() === `${origin}/ask` && response.request().method() === "POST");
  await page.locator(".composer [data-send]").click();
  const response = await responsePromise;
  const request = { method: "POST", url: `${origin}/ask`, body: { repo_id: repoId, question, mode: "fast" } };
  // Response bodies contain no request credentials. Redact credential-like values if an upstream error leaked one.
  let body;
  try { body = await response.text(); } catch {
    return { verification: "LIVE", status: "BLOCKED", selected, repository: "advaith-3683/portfolio", request,
      httpStatus: response.status(), responseBody: "Unavailable: the response transport failed before its body could be captured.",
      reason: "Live response transport failed. Backend left unchanged." };
  }
  body = body.replace(/Bearer\s+[^\s"<>]+/gi, "Bearer [REDACTED]")
    .replace(/(?:sk-|gsk_|AIza)[\w-]{12,}/g, "[REDACTED]")
    .replace(/\beyJ[\w-]+\.[\w-]+\.[\w-]+\b/g, "[REDACTED]");
  const events = body.split(/\r?\n\r?\n/).filter(Boolean).flatMap((frame) => {
    const data = frame.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
    try { return data ? [JSON.parse(data)] : []; } catch { return [{ type: "malformed" }]; }
  });
  const answer = events.find((event) => event.type === "answer");
  const done = events.find((event) => event.type === "done");
  if (!response.ok() || events.some((event) => ["error", "malformed"].includes(event.type)) || !done) {
    return { verification: "LIVE", status: "BLOCKED", selected, repository: "advaith-3683/portfolio", request,
      httpStatus: response.status(), responseBody: body, reason: "Live backend failed or returned an invalid/incomplete stream. Backend left unchanged." };
  }
  await page.waitForFunction(() => document.querySelector(".composer [data-stop]").hidden);
  const desktop = { width: 1440, answerVisible: await page.locator(".conversation .answer-text").isVisible(),
    citationChips: await page.locator(".conversation .citation-chip").count(),
    completed: await page.locator(".conversation .answer > .eyebrow").textContent() === "Answer complete" };
  await page.screenshot({ path: ".playwright-mcp/chat-live-1440.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 900 });
  const mobile = { width: 390, answerVisible: await page.locator(".mobile-content .answer-text").isVisible(),
    citationChips: await page.locator(".mobile-content .citation-chip").count(),
    composerVisible: await page.locator(".mobile-composer [data-send]").isVisible() };
  await page.screenshot({ path: ".playwright-mcp/chat-live-390.png", fullPage: true });
  return { verification: "LIVE", status: answer && done.status === "completed" && desktop.completed && desktop.answerVisible && mobile.answerVisible ? "PASSED" : "BLOCKED", selected,
    repository: "advaith-3683/portfolio", request, httpStatus: response.status(),
    eventTypes: events.map((event) => event.type), answerText: answer?.text, citations: answer?.citations,
    doneStatus: done.status, usage: done.usage, desktop, mobile };
})
