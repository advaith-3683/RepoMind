// Reload regression against a static frontend on 8765; history, traces and auth are mocked.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage(), origin = "http://127.0.0.1:8765", project = "https://auth.example.test";
  const KEY = "repomind.activeQueryId", traces = new Map(), history = [], errors = [], checks = [];
  let asks = 0, traceReads = 0, unavailable = false;
  const assert = (value, message) => { if (!value) throw Error(message); };
  const repo = { id: "repo-a", name: "fixture/repository", url: "https://github.com/fixture/repository", status: "ready" };
  const usage = { steps: 1, tokens_in: 20, tokens_out: 8, latency_ms: 30, cost_usd: 0 };
  const answer = { type: "answer", text: "Use `run()` <img src=x onerror=alert(1)> [src/main.js:2-3]", found: true,
    confidence: "high", groundedness: 1, citations: [{ path: "src/main.js", start: 2, end: 3 }] };
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(origin + "/workspace", async (route) => route.fulfill({ response: await route.fetch({ url: origin + "/" }) }));
  await tab.route(origin + "/config", (route) => route.fulfill({ json: {
    auth_required: true, supabase_url: project, supabase_anon_key: "sb_publishable_fixture", github_scopes: "public_repo",
  } }));
  await tab.route(project + "/auth/v1/user", (route) => route.fulfill({ json: { id: "user-a", user_metadata: { user_name: "fixture" } } }));
  await tab.route(project + "/auth/v1/logout?scope=local", (route) => route.fulfill({ status: 204 }));
  await tab.route(origin + "/repos", (route) => route.fulfill({ json: [repo] }));
  await tab.route(origin + "/history?**", (route) => route.fulfill({ json: history }));
  await tab.route(origin + "/traces/*", (route) => {
    traceReads++;
    const data = traces.get(new URL(route.request().url()).pathname.split("/").at(-1));
    return data && !unavailable ? route.fulfill({ json: data })
      : route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Trace not found." } } });
  });
  await tab.route(origin + "/ask", (route) => {
    const body = route.request().postDataJSON(), id = `query-${++asks}`;
    const events = [{ type: "start", query_id: id, mode: body.mode },
      { type: "step", index: 1, tool: "search_code", thought: "Retrieved <b>source</b>", args: {}, observation_preview: "literal <script>", tokens_total: 20 }, answer];
    traces.set(id, { ...usage, query_id: id, repo_id: repo.id, question: body.question, answer: answer.text, query_status: "completed", steps: events });
    history.unshift({ id, repo_id: repo.id, question: body.question, answer: answer.text, status: "completed" });
    const frames = [...events, { type: "done", query_id: id, mode: body.mode, status: "completed", usage }];
    return route.fulfill({ contentType: "text/event-stream", body: frames.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join("") });
  });
  async function signIn() {
    await tab.evaluate((project) => sessionStorage.setItem("repomind.auth.session", JSON.stringify({ project_url: project,
      access_token: "fake-access", refresh_token: "fake-refresh", expires_at: Date.now() + 3600000 })), project);
    await tab.goto(origin + "/workspace"); await tab.locator("#question").waitFor({ state: "attached" });
    await tab.waitForFunction(() => document.documentElement.dataset.authState === "signed-in" && !document.querySelector("#question").disabled);
  }
  const output = (width) => tab.locator(width > 900 ? ".conversation [data-chat-output]" : ".mobile-content [data-chat-output]");
  async function send(width, question) {
    await tab.locator(width > 900 ? "#question" : "#mobile-question").fill(question);
    await tab.locator(width > 900 ? ".composer [data-send]" : ".mobile-composer [data-send]").click();
    await output(width).getByText("Answer complete", { exact: true }).waitFor();
    await tab.waitForFunction(() => [...document.querySelectorAll("[data-stop]")].every((node) => node.hidden));
  }
  try {
    await tab.goto(origin); await signIn();
    for (const width of [1440, 390, 375]) {
      await tab.setViewportSize({ width, height: 900 });
      const question = `Where does run start? (${width})`; await send(width, question);
      const id = await tab.evaluate((key) => sessionStorage.getItem(key), KEY), before = asks;
      assert(id === `query-${asks}`, "Stored conversation data instead of the query id");
      await tab.reload(); await output(width).getByText("Answer complete", { exact: true }).waitFor();
      assert(asks === before, "Reload issued another ask");
      assert(await output(width).getByText(question, { exact: true }).isVisible(), "Question not restored");
      assert(await output(width).locator("code").textContent() === "run()", "Safe inline code renderer bypassed");
      assert(await output(width).locator("img").count() === 0, "Restored answer became HTML");
      assert(await output(width).locator(".citation-chip").count() === 1, "Verified citations lost");
      const ids = await tab.evaluate(async () => {
        const { repositoryRequest } = await import("/js/api.js");
        return (await repositoryRequest("/history?repo_id=repo-a&limit=30")).map((row) => row.id);
      });
      assert(ids.includes(id), "Persisted run is missing from server history after reload");
      assert(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Workspace overflowed");
      await tab.locator("[data-new-chat]:visible").click();
      assert(await tab.evaluate((key) => sessionStorage.getItem(key) === null, KEY), "New chat retained the pointer");
      await tab.reload(); await tab.waitForFunction(() => !document.querySelector("#question").disabled);
      assert(await output(width).isHidden(), "New chat restored the previous answer on reload");
      checks.push(`reload, server history and New chat at ${width}px`);
    }
    await send(375, "Sign-out regression");
    await tab.locator(".mobile-header [data-sign-out]").click();
    await tab.waitForFunction(() => document.documentElement.dataset.authState === "signed-out");
    assert(await tab.evaluate((key) => sessionStorage.getItem(key) === null, KEY), "Sign-out retained the pointer");
    const readsBefore = traceReads; await signIn();
    assert(await output(375).isHidden() && traceReads === readsBefore, "Signing back in restored the previous session's conversation");
    checks.push("sign-out clears conversation state");
    await send(375, "Missing trace regression"); unavailable = true;
    await tab.reload(); await output(375).getByRole("button", { name: "Retry restoring conversation" }).waitFor();
    unavailable = false;
    await output(375).getByRole("button", { name: "Retry restoring conversation" }).click();
    await output(375).getByText("Answer complete", { exact: true }).waitFor();
    checks.push("missing trace retry");
    await tab.addInitScript((key) => {
      for (const name of ["getItem", "setItem", "removeItem"]) {
        const original = Storage.prototype[name];
        Storage.prototype[name] = function (item, ...args) {
          if (item === key) throw Error("Fixture storage blocked");
          return original.call(this, item, ...args);
        };
      }
    }, KEY);
    await tab.reload(); await tab.waitForFunction(() => !document.querySelector("#question").disabled);
    await send(375, "Blocked storage regression"); await tab.locator("[data-new-chat]:visible").click();
    assert(await output(375).isHidden(), "Storage failure prevented New chat");
    checks.push("storage failure does not break chat");
    assert(errors.length === 0, errors.join("\n"));
    return { passed: true, checks, asks, traceReads, scriptErrors: errors, persistence: "mocked backend", liveHistoryVerified: false };
  } catch (error) {
    error.message += ` (asks=${asks}, traces=${traceReads}, checks=${checks.join("; ")}, state=${await tab.locator(".conversation").textContent()}, errors=${errors.join("; ")})`;
    throw error;
  } finally { await context.close(); }
})
