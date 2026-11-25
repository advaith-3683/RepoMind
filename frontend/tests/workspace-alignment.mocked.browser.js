// Static frontend on port 8765; repository and SSE responses are mocked.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage(), origin = "http://127.0.0.1:8765";
  const checks = [], errors = [];
  const name = "owner-with-a-long-name/repository-with-a-deliberately-long-name-to-check-ellipsis";
  let variant = "short", query = 0;
  const assert = (value, message) => { if (!value) throw Error(message); };
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(origin + "/workspace", async (route) => route.fulfill({ response: await route.fetch({ url: origin + "/" }) }));
  await tab.route(origin + "/config", (route) => route.fulfill({ json: { auth_required: false } }));
  await tab.route(origin + "/repos", (route) => route.fulfill({ json: [{ id: "repo", name, status: "ready", url: "https://github.com/fixture/repo" }] }));
  await tab.route(origin + "/ask", (route) => {
    const id = `alignment-${++query}`, mode = route.request().postDataJSON().mode;
    const text = variant === "short" ? "Start in `run()` [src/entry.js:2-4]." :
      ("The execution flow is described by the retrieved source. Inspect `run()` and its caller [src/entry.js:2-4].\n\n").repeat(70)
      + "Unbroken identifier: " + "very_long_identifier_".repeat(25);
    const events = [{ type: "start", query_id: id, mode },
      { type: "step", index: 1, tool: "search_code", thought: "Retrieved fixture source", args: {}, observation_preview: "Fixture observation", tokens_total: 20 },
      { type: "answer", text, found: true, confidence: "high", groundedness: 1, citations: [{ path: "src/entry.js", start: 2, end: 4 }] },
      { type: "done", query_id: id, mode, status: "completed", usage: { steps: 1, tokens_in: 20, tokens_out: 8, latency_ms: 30, cost_usd: 0 } }];
    return route.fulfill({ contentType: "text/event-stream", body: events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join("") });
  });
  async function measure(width, state) {
    const geometry = await tab.evaluate(({ width, state }) => {
      const mobile = width <= 900;
      const rect = (node) => {
        const box = node.getBoundingClientRect();
        return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height };
      };
      const workspace = document.querySelector(".workspace"), header = document.querySelector(".context-header");
      const conversation = document.querySelector(mobile ? ".mobile-content" : ".conversation");
      const composer = document.querySelector(mobile ? ".mobile-composer" : ".composer");
      const output = document.querySelector(mobile ? ".mobile-content [data-chat-output]" : ".conversation [data-chat-output]");
      const card = state === "empty" ? document.querySelector(mobile ? ".mobile-conversation" : ".conversation .workspace-empty") : output;
      const breadcrumb = document.querySelector("[data-repo-context]");
      return { mobile, pageWidth: document.documentElement.scrollWidth, viewport: innerWidth, viewportHeight: innerHeight,
        header: rect(header), conversation: rect(conversation), composer: rect(composer), card: rect(card), trace: rect(document.querySelector(".trace")),
        column: rect(workspace), paddingBottom: parseFloat(getComputedStyle(workspace).paddingBottom),
        contentRight: conversation.getBoundingClientRect().left + conversation.clientWidth,
        overflow: conversation.scrollWidth > conversation.clientWidth + 1,
        align: getComputedStyle(header).alignItems, wrap: getComputedStyle(header).flexWrap,
        breadcrumb: { title: breadcrumb.title, text: breadcrumb.textContent, whiteSpace: getComputedStyle(breadcrumb).whiteSpace, overflow: getComputedStyle(breadcrumb).textOverflow } };
    }, { width, state });
    const near = (first, second) => Math.abs(first - second) <= 1;
    assert(geometry.pageWidth <= width && !geometry.overflow, `Horizontal overflow at ${width}/${state}`);
    if (!geometry.mobile) {
      assert(near(geometry.header.left, geometry.composer.left) && near(geometry.header.right, geometry.composer.right), `Composer inset at ${width}/${state}`);
      assert(near(geometry.header.left, geometry.conversation.left) && near(geometry.header.right, geometry.conversation.right), `Conversation inset at ${width}/${state}`);
      assert(near(geometry.header.left, geometry.card.left) && near(geometry.contentRight, geometry.card.right), `Card is width-capped at ${width}/${state}`);
      assert(geometry.align === "baseline" && geometry.wrap === "nowrap", "Header alignment is not a single baseline");
      assert(geometry.breadcrumb.title === `${name} / Ready` && geometry.breadcrumb.whiteSpace === "nowrap" && geometry.breadcrumb.overflow === "ellipsis", "Breadcrumb lacks safe truncation and full title");
      assert(near(geometry.composer.bottom, geometry.column.bottom - geometry.paddingBottom), "Composer is not pinned to the column bottom");
      assert(geometry.conversation.right <= geometry.trace.left && near(geometry.column.right, geometry.trace.left), "Middle column overlaps trace");
      assert(near(geometry.trace.width, width <= 1200 ? 280 : 368) && near(geometry.trace.right, width), "Trace column geometry changed");
    } else assert(geometry.composer.bottom <= geometry.viewportHeight + 1, "Mobile composer extends below viewport");
    return geometry;
  }
  try {
    await tab.goto(origin);
    const viewports = [1920, 1440, 1024, 390, 375].map((width) => ({ width, height: 900 }));
    viewports.push({ width: 1024, height: 600 });
    for (const { width, height } of viewports) for (const theme of ["light", "dark"]) {
      await tab.setViewportSize({ width, height });
      await tab.evaluate((theme) => { localStorage.setItem("repomind.theme", theme); sessionStorage.removeItem("repomind.activeQueryId"); }, theme);
      await tab.goto(origin + "/workspace");
      await tab.waitForFunction(() => !document.querySelector("#question").disabled);
      await tab.evaluate(() => document.fonts.ready);
      let empty;
      for (const state of ["empty", "short", "long"]) {
        if (state !== "empty") {
          await tab.locator("[data-new-chat]:visible").click(); variant = state;
          await tab.locator(width <= 900 ? "#mobile-question" : "#question").fill("Explain the execution flow.");
          await tab.locator(width <= 900 ? ".mobile-composer [data-send]" : ".composer [data-send]").click();
          await tab.locator(width <= 900 ? ".mobile-content [data-chat-output] .eyebrow" : ".conversation [data-chat-output] .eyebrow").getByText("Answer complete", { exact: true }).waitFor();
          await tab.waitForFunction(() => [...document.querySelectorAll("[data-stop]")].every((node) => node.hidden));
        }
        const geometry = await measure(width, state);
        if (state === "empty") empty = geometry;
        else {
          assert(Math.abs(geometry.composer.bottom - empty.composer.bottom) <= 1, "Answer moved composer bottom");
          if (width > 900) assert(JSON.stringify(geometry.trace) === JSON.stringify(empty.trace), "Answer changed trace bounds");
          const output = tab.locator(width <= 900 ? ".mobile-content [data-chat-output]" : ".conversation [data-chat-output]");
          assert(await output.locator(".citation-chip").count() > 0, "Citation fixture missing");
        }
        checks.push({ width, height, theme, state });
      }
    }
    assert(errors.length === 0, errors.join("\n"));
    return { passed: true, checks, scriptErrors: errors, serverFallback: "mocked" };
  } catch (error) {
    error.message += ` (checks=${checks.length}, query=${query}, variant=${variant}, url=${tab.url()}, phase=${await tab.evaluate(() => document.documentElement.dataset.authState)}, errors=${errors.join("; ")})`;
    throw error;
  } finally { await context.close(); }
})
