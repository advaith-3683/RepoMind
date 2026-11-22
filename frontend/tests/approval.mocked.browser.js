// Run with Playwright browser_run_code_unsafe, against a static frontend on :8765.
// All API requests are intercepted. No OAuth or live GitHub traffic.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage();
  const origin = "http://127.0.0.1:8765", errors = [], checks = [], requests = [];
  const assert = (value, message) => { if (!value) throw Error(message); };
  const frame = (type, data) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
  const payload = { title: "Review <img src=x onerror=alert(1)>", body: "Body <b>literal</b>", path: "smartspend backend/auth.py",
    edits: [{ old: "before", new: "after" }], diff: "--- a/file\n+++ b/file\n-before\n+after\n+" + "long line ".repeat(70) };
  let variant = "approval", decisionError = false, publishError = false, published = false;
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(origin + "/config", (route) => route.fulfill({ json: { auth_required: false } }));
  await tab.route(origin + "/repos", (route) => route.fulfill({ json: [{ id: "repo", name: "fixture/repo", url: "https://github.com/fixture/repo", status: "ready" }] }));
  await tab.route(origin + "/ask", (route) => {
    const body = route.request().postDataJSON(); assert(body.mode === "agent", "Proposal test did not use agent mode");
    const terminal = variant === "answer" ? frame("answer", { text: "Use [smartspend backend/auth.py:111-119] and [src/app/(auth)/page.tsx:7]. Unverified [invented.py:9].", found: true, confidence: "high", groundedness: 1,
      citations: [{ path: payload.path, start: 111, end: 119 }, { path: "src/app/(auth)/page.tsx", start: 7, end: 7 }] })
      : variant === "approval" ? frame("approval_required", { action_id: "fixture-action", kind: "patch", payload })
        : frame("error", { code: variant, message: "Provider message <b>literal</b>", detail: "PRIVATE DEBUG <img>" });
    return route.fulfill({ contentType: "text/event-stream", body: frame("start", { query_id: "query", mode: "agent" }) + terminal
      + frame("done", { query_id: "query", mode: "agent", status: variant === "approval" ? "awaiting_approval" : variant === "answer" ? "completed" : "error", usage: {} }) });
  });
  await tab.route(origin + "/actions/*/decision", async (route) => {
    requests.push({ path: "decision", body: route.request().postDataJSON(), providerHeader: route.request().headers()["x-github-token"] });
    assert(!requests.at(-1).providerHeader, "Provider token sent with decision");
    if (decisionError) return route.fulfill({ status: 409, json: { error: { code: "invalid_transition", message: "Conflict" } } });
    const data = route.request().postDataJSON();
    return route.fulfill({ json: { status: data.decision === "reject" ? "rejected" : data.decision === "edit" ? "edited" : "approved",
      payload: data.decision === "reject" ? null : { ...payload, ...(data.edited_payload || {}), title: "Server final title" }, diff: data.decision === "reject" ? null : "+server recomputed\n" } });
  });
  await tab.route(origin + "/actions/*/publish", (route) => {
    assert(route.request().headers()["x-github-token"] === "fake-provider", "Wrong publish token");
    requests.push({ path: "publish" });
    if (publishError) return route.fulfill({ status: 403, json: { error: { code: "github_no_permission", message: "No permission" } } });
    published = true;
    return route.fulfill({ json: { status: "published", already: false, result: { kind: "patch", url: "https://github.com/fixture/repo/pull/7", branch: "repomind/fixture" } } });
  });
  const output = () => tab.locator(tab.viewportSize().width <= 900 ? ".mobile-content [data-chat-output]" : ".conversation [data-chat-output]");
  async function start() {
    await tab.goto(origin); await tab.locator('[data-workspace-state="ready"]').first().waitFor({ state: "attached" });
    const mobile = tab.viewportSize().width <= 900;
    await tab.locator(mobile ? '.mobile-composer [data-mode="agent"]' : '.composer [data-mode="agent"]').click();
    await tab.locator(mobile ? "#mobile-question" : "#question").fill("Propose a change");
    await tab.locator(mobile ? ".mobile-composer [data-send]" : ".composer [data-send]").click();
    await tab.waitForFunction(() => [...document.querySelectorAll("[data-stop]")].every((node) => node.hidden));
  }
  try {
    for (const width of [1440, 390, 375]) {
      await tab.setViewportSize({ width, height: 1000 }); variant = "answer"; await start();
      assert(await output().locator(".answer-text .citation-chip").count() === 2, "Missing verified inline chips");
      assert((await output().locator(".answer-text").textContent()).includes("[invented.py:9]"), "Unverified text was removed");
      variant = "approval"; await start();
      await output().getByRole("heading", { name: "Review before action" }).waitFor();
      assert(await output().locator("img, b").count() === 0, "Proposal became HTML");
      await tab.keyboard.press("Escape");
      assert(await output().getByRole("button", { name: "Approve", exact: true }).isVisible(), "Escape dismissed card");
      await output().getByRole("button", { name: "Edit", exact: true }).click();
      await output().getByLabel("Title", { exact: true }).fill("Edited title");
      await output().getByLabel("Body", { exact: true }).fill("Edited body");
      await output().getByLabel("Replacement text 1", { exact: true }).fill("edited replacement");
      await output().getByRole("button", { name: "Approve edited version" }).click();
      await output().getByRole("button", { name: "Open pull request" }).waitFor();
      assert(requests.at(-1).body.edited_payload.edits[0].new === "edited replacement", "Edit text not sent");
      assert(requests.at(-1).body.edited_payload.path === payload.path && requests.at(-1).body.edited_payload.edits[0].old === "before", "Original target changed");
      assert(!published && (await output().textContent()).includes("Server final title"), "Approval changed GitHub or ignored response");
      assert(await output().locator(".approval-diff").textContent() === "+server recomputed\n", "Stale diff displayed");
      const before = requests.length;
      await output().getByRole("button", { name: "Open pull request" }).click();
      await output().getByText("Sign in with GitHub again to publish.", { exact: true }).waitFor();
      assert(requests.length === before, "Missing token called publish");
      await tab.evaluate(() => sessionStorage.setItem("repomind.auth.session", JSON.stringify({ provider_token: "fake-provider" })));
      publishError = true;
      await output().getByRole("button", { name: "Open pull request" }).click();
      await output().getByText("You need write access to open a PR on this repo.", { exact: true }).waitFor();
      assert(await output().getByRole("button", { name: "Copy diff" }).isEnabled(), "Lost fallback");
      publishError = false;
      await output().getByRole("button", { name: "Open pull request" }).click();
      await output().getByRole("heading", { name: "Pull request opened" }).waitFor();
      assert(await output().getByRole("link").getAttribute("rel") === "noopener noreferrer", "Unsafe link");
      assert(!(await output().textContent()).includes("fake-provider"), "Token rendered");
      assert(await output().getByText("repomind/fixture", { exact: true }).isVisible(), "Branch missing");
      for (const theme of ["light", "dark"]) {
        await tab.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
        assert(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Overflow at ${width}/${theme}`);
        checks.push({ width, theme, state: "published" });
      }
      published = false;
    }
    decisionError = true; await start();
    await output().getByRole("button", { name: "Approve", exact: true }).click();
    await output().getByText("This action was already decided.", { exact: true }).waitFor();
    for (const label of ["Approve", "Edit", "Reject"]) assert(await output().getByRole("button", { name: label, exact: true }).isDisabled(), "409 did not lock decision");
    decisionError = false; await start();
    await output().getByRole("button", { name: "Reject", exact: true }).click();
    await output().getByRole("heading", { name: "Proposal rejected" }).waitFor();
    for (const code of ["rate_limited", "llm_unavailable"]) {
      variant = code; await start();
      await tab.locator("[data-toast]").getByText("Provider message <b>literal</b>", { exact: true }).waitFor();
      assert(!(await output().textContent()).includes("PRIVATE DEBUG"), "Detail leaked to answer");
      assert((await tab.locator("[data-trace-content]").first().textContent()).includes("PRIVATE DEBUG"), "Trace lost detail");
      if (code === "rate_limited") assert(await tab.locator(".mobile-composer [data-send]").isDisabled(), "Rate limit did not lock submit");
    }
    assert(errors.length === 0, errors.join("\n"));
    return { passed: true, checks, apiRequests: requests.length, scriptErrors: errors };
  } finally { await context.close(); }
})
