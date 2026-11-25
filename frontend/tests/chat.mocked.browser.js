// Mocked API/SSE browser regression. Run against the static frontend on port 8765.
// Uses a separate context; never uses a real session or calls a live API.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage();
  const origin = "http://127.0.0.1:8765";
  const errors = [], requests = [], checks = [];
  let variant = "answer", pending, sourceError = false;
  const repo = (id) => ({ id, name: `fixture/${id}`, url: `https://github.com/fixture/${id}`, status: "ready" });
  const assert = (condition, message) => { if (!condition) throw Error(message); };
  const frame = (type, data) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
  const usage = { steps: 1, tokens_in: 25, tokens_out: 11, latency_ms: 120, cost_usd: 0, llm_cache_hits: 1 };
  const answerText = "Fixture answer <img src=x onerror=alert(1)> [invented.py:7] uses `run()`.";
  const errorMessage = "Fixture provider unavailable <b>literal</b>.";
  const errorDetail = "Provider debug detail\n<img src=x onerror=alert(1)>";
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(origin + "/workspace", async (route) => route.fulfill({ response: await route.fetch({ url: origin + "/" }) }));
  await tab.route(origin + "/config", (route) => route.fulfill({ json: { auth_required: false } }));
  await tab.route(origin + "/repos", (route) => route.request().method() === "DELETE"
    ? route.fulfill({ status: 204 }) : route.fulfill({ json: [repo("one"), repo("two")] }));
  await tab.route(origin + "/repos/*", (route) => route.fulfill({ status: 204 }));
  await tab.route(origin + "/repos/*/file?**", (route) => {
    if (sourceError) return route.fulfill({ status: 502, json: { error: { code: "github_error", message: "Fixture source unavailable <b>literal</b>." } } });
    const url = new URL(route.request().url());
    assert(url.searchParams.get("path") === "src/main.js", "Wrong citation path");
    assert(url.searchParams.get("start") === "1" && url.searchParams.get("end") === "9", "Wrong source range");
    return route.fulfill({ json: { path: "src/main.js", start: 1, end: 4, total_lines: 4, lines: ["first", "<script>untrusted</script>", "third", "fourth"] } });
  });
  await tab.route(origin + "/ask", async (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    assert(route.request().method() === "POST", "Ask must be POST");
    if (variant === "pending") { await new Promise((resolve) => { pending = resolve; }); }
    if (variant === "network") return route.abort("connectionrefused");
    if (variant === "rate") return route.fulfill({ status: 429, json: { error: { code: "rate_limited", message: "Fixture limit: wait." } } });
    if (variant === "validation") return route.fulfill({ status: 400, json: { error: { code: "prompt_injection", message: "Fixture guardrail rejected this query." } } });
    const start = frame("start", { query_id: "fixture-query", mode: body.mode });
    const step = frame("step", { index: 1, thought: "Fixture retrieval", tool: "search_code", args: { query: body.question }, observation_preview: "Fixture observation <b>literal</b>", tokens_total: 25 });
    const guardrail = frame("guardrail", { code: "indirect_injection", message: "Fixture guardrail <b>literal</b>" });
    let terminal, status = "completed";
    if (variant === "clarification") {
      terminal = frame("needs_clarification", { message: "Fixture: narrow the question.", suggest_mode: "agent" }); status = "needs_clarification";
    } else if (variant === "approval") {
      terminal = frame("approval_required", { action_id: "fixture-action", kind: "patch", payload: { title: "Fixture change", path: "src/main.js" } }); status = "awaiting_approval";
    } else if (variant === "error" || variant === "error-no-detail") {
      terminal = frame("error", { code: "internal", message: errorMessage,
        ...(variant === "error" ? { detail: errorDetail } : {}) }); status = "error";
    } else if (variant === "malformed") terminal = "event: answer\ndata: {bad json}\n\n";
    else terminal = frame("answer", { text: answerText, found: variant !== "not-found", confidence: "low", groundedness: 0.5,
      citations: variant === "no-citations" || variant === "not-found" ? [] : [{ path: "src/main.js", start: 2, end: 4 }] });
    const done = variant === "cut" ? "" : frame("done", { query_id: "fixture-query", mode: body.mode, status, usage });
    return route.fulfill({ contentType: "text/event-stream", body: start + step + guardrail + terminal + done }).catch(() => {});
  });

  async function reset() {
    const fresh = tab.locator("[data-new-chat]:visible");
    if (await fresh.count() && await fresh.isEnabled()) await fresh.click();
    await tab.goto(origin); await tab.waitForFunction(() => document.querySelector("[data-workspace-state]").dataset.workspaceState === "ready");
  }
  async function send(text = "Where is the entry point?") {
    const mobile = tab.viewportSize().width <= 900;
    await tab.locator(mobile ? "#mobile-question" : "#question").fill(text);
    await tab.locator(mobile ? ".mobile-composer [data-send]" : ".composer [data-send]").click();
  }
  async function settled(text) {
    const output = tab.locator(tab.viewportSize().width <= 900 ? ".mobile-content [data-chat-output]" : ".conversation [data-chat-output]");
    await output.getByText(text, { exact: true }).first().waitFor();
    await tab.waitForFunction(() => [...document.querySelectorAll("[data-stop]")].every((node) => node.hidden));
    return output;
  }
  try {
    for (const width of [1440, 390]) {
      await tab.setViewportSize({ width, height: 900 });
      variant = "answer"; await reset();
      await send(); const output = await settled("Answer complete");
      assert(await output.locator(".answer-text").textContent() === answerText.replaceAll("`", ""), "Answer text changed");
      assert(await output.locator(".answer-text code").textContent() === "run()", "Inline code was not rendered");
      assert(await output.locator(".citation-chip").count() === 1, "Invented citation chips");
      assert(await output.locator("img").count() === 0, "Model text became HTML");
      assert(await output.getByText("Low confidence: verify against the cited code.").isVisible(), "Low confidence banner missing");
      assert(await output.getByText("Served from cache").isVisible(), "Cache notice missing");
      assert((await output.locator(".run-usage").textContent()).includes("25 tokens in"), "Real usage missing");
      assert(await tab.locator("[data-trace-content]").first().getByText("Fixture retrieval").count() === 1, "Trace step missing");
      assert(await tab.locator("[data-trace-content]").first().getByText("Fixture guardrail <b>literal</b>").count() === 1, "Guardrail missing");
      assert(await tab.locator(".trace-card b").count() === 0, "Trace became HTML");
      await output.locator(".citation-chip").click();
      await tab.locator(".cited-line").first().waitFor();
      assert(await tab.locator(".cited-line").count() === 3, "Wrong highlighted range");
      assert(await tab.locator("[data-citation-content] script").count() === 0, "Source became HTML");
      await tab.keyboard.press("Escape");
      assert(await output.locator(".citation-chip").evaluate((node) => node === document.activeElement), "Citation focus was lost");
      sourceError = true;
      await output.locator(".citation-chip").click();
      await tab.locator("[data-citation-content]").getByText("Fixture source unavailable <b>literal</b>.", { exact: true }).waitFor();
      assert(await tab.locator("[data-citation-content] b").count() === 0, "Source error became HTML");
      await tab.keyboard.press("Escape"); sourceError = false;
      const dimensions = await tab.evaluate(() => {
        const composer = document.querySelector(innerWidth <= 900 ? ".mobile-composer" : ".composer");
        const rect = composer.getBoundingClientRect();
        return { scrollWidth: document.documentElement.scrollWidth, width: innerWidth, bottom: rect.bottom, height: innerHeight };
      });
      assert(dimensions.scrollWidth <= width && dimensions.bottom <= dimensions.height, "Composer overflowed viewport");
      await tab.screenshot({ path: `.playwright-mcp/chat-mocked-${width}.png`, fullPage: true });
      checks.push({ width, dimensions });
      for (const value of ["error", "error-no-detail"]) {
        variant = value; await reset(); await send(); const failed = await settled(errorMessage);
        assert(await failed.locator(".chat-error").textContent() === errorMessage, "SSE error message missing");
        assert(!(await failed.textContent()).includes("Provider debug detail"), "Debug detail leaked into answer");
        const trace = tab.locator("[data-trace-content]").nth(width <= 900 ? 1 : 0);
        if (width <= 900) await tab.locator(".mobile-trace > summary").click();
        assert(await trace.getByText("internal", { exact: true }).isVisible(), "SSE error code missing from trace");
        assert(await trace.locator(".chat-error").textContent() === errorMessage, "SSE error message missing from trace");
        assert(await trace.locator(".trace-observation").count() === (value === "error" ? 2 : 1), "Optional error detail rendered incorrectly");
        if (value === "error") assert(await trace.getByText(errorDetail, { exact: true }).isVisible(), "Error detail missing from trace");
        assert(await tab.locator(".trace-card img, .trace-card b, .chat-error b").count() === 0, "SSE error became HTML");
      }
    }
    await tab.setViewportSize({ width: 1440, height: 900 });
    for (const [value, expected] of [["no-citations", "Answer complete"], ["not-found", "No supporting code found in this repo"],
      ["approval", "Pending approval"], ["error", errorMessage], ["malformed", "The answer service sent a malformed event. Please retry."],
      ["cut", "The answer stream ended before completion. Please retry."], ["network", "Could not reach the repository service. Check your connection and retry."],
      ["validation", "Fixture guardrail rejected this query."]]) {
      variant = value; await reset(); await send(); const output = await settled(expected);
      if (["no-citations", "not-found"].includes(value)) assert(await output.locator(".citation-chip").count() === 0, "Parsed inline citation text");
      if (value === "not-found") {
        assert(await output.getByRole("heading", { name: "Not found in this repo", exact: true }).count() === 0, "Duplicate declined-answer heading");
        assert(await output.getByText("No supporting code found in this repo", { exact: true }).isVisible(), "Missing absence-of-evidence notice");
        assert(await output.getByText("Low confidence: verify against the cited code.", { exact: true }).count() === 0, "Declined answer retained confidence warning");
      }
      if (value === "approval") {
        assert(await output.getByRole("button", { name: "Approve", exact: true }).isVisible(), "Approval control missing");
        assert(await output.getByText("Fixture change").isVisible(), "Proposal title missing");
      }
    }
    variant = "clarification"; await reset(); await send("Original submitted question"); await settled("Needs clarification");
    await tab.locator("#question").fill("Unsubmitted draft");
    variant = "answer";
    await tab.locator(".conversation [data-try-agent]").click(); await settled("Answer complete");
    assert(requests.at(-1).question === "Original submitted question" && requests.at(-1).mode === "agent", "Try Agent did not reuse original question");
    assert(await tab.locator("#question").inputValue() === "Unsubmitted draft", "Try Agent prefilled input");
    await tab.locator("[data-new-chat]:visible").click();
    await tab.reload(); await tab.locator('[data-workspace-state="ready"]').first().waitFor();
    assert(await tab.locator('.composer [data-mode="agent"]').getAttribute("aria-pressed") === "true", "Mode did not persist");

    variant = "pending"; await reset(); await send();
    await tab.waitForFunction(() => !document.querySelector(".composer [data-stop]").hidden);
    const before = requests.length;
    await tab.locator("#question").press("Enter");
    assert(await tab.locator(".composer [data-send]").isDisabled(), "Send allowed duplicates");
    assert(requests.length === before, "Duplicate submission reached API");
    await tab.locator(".composer [data-stop]").click(); await settled("Run stopped");
    pending?.();
    assert(await tab.locator(".composer [data-send]").isEnabled(), "Stop did not recover composer");

    variant = "answer"; await reset(); await send(); await settled("Answer complete");
    await tab.locator('.repositories [data-repo-id="two"]').click();
    assert(await tab.locator(".conversation [data-chat-output]").isHidden(), "Switching repository retained old answer");
    await send(); await settled("Answer complete");
    tab.once("dialog", (dialog) => dialog.accept());
    await tab.locator('.repositories [data-remove-repo-id="two"]').click();
    await tab.locator('[data-workspace-state="empty"]').first().waitFor();
    assert(await tab.locator(".conversation [data-chat-output]").isHidden(), "Deleting repository retained old answer");

    variant = "rate"; await reset(); await send(); await settled("Fixture limit: wait.");
    assert(await tab.locator(".composer [data-send]").isDisabled(), "Rate limit did not disable send");
    await tab.locator(".composer [data-send]").waitFor({ state: "visible" });
    await tab.waitForFunction(() => !document.querySelector(".composer [data-send]").disabled, null, { timeout: 12000 });
    assert(errors.length === 0, `Browser errors: ${errors.join(", ")}`);
    return { passed: true, verification: "MOCKED APIs and SSE only", checks, requests: requests.length, scriptErrors: errors };
  } catch (error) {
    error.message += ` (variant=${variant}, requests=${requests.length}, url=${tab.url()}, errors=${errors.join("; ")})`;
    throw error;
  } finally { pending?.(); await context.close(); }
})
