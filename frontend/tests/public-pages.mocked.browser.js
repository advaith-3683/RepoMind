// Run against a static frontend on port 8765. All API and auth responses are mocked.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage();
  const origin = "http://127.0.0.1:8765", project = "https://auth.example.test";
  const errors = [], checks = [], requests = [];
  let result = "success";
  const assert = (value, message) => { if (!value) throw Error(message); };
  const payload = {
    meta: { title: "Fixture evaluation", repo: "fixture/repo", model: "fixture-model", fast_top_k: 5, search_preview_lines: 60,
      golden_set: { questions: 12, answerable: 9, should_decline: 3 }, limits: ["One repository.", "Small golden set.", "Proxy groundedness."] },
    agent: { n_errors: 0, answer_rate: .75, decline_accuracy: 1, file_recall: .6, keyword_hit: .7, groundedness: .8,
      error_rate: 0, latency_p50_ms: 1234, latency_p95_ms: 4567, avg_tokens: 123.456 },
    redteam: { attacks_total: 12, attacks_blocked: 11, benign_total: 8, false_positives: 1 },
    retrieval: { vector: { "recall@1": .1, "recall@3": .3, "recall@5": .5, mrr: .456 } },
    retrieval_ablation: { n_tune: 7, n_test: 5, vector_only: { "recall@5": .8, mrr: .75, "held_out_recall@5": .6, held_out_mrr: .55 } },
    history: [{ run: 1, date: "2026-11-23", change: "Invalid fixture", errors: 2, valid: false, note: "Provider quota exhausted." },
      { run: 2, date: "2026-11-24", change: "Current fixture", answered: "7/9", declined: "3/3", errors: 0, file_recall: .6, keyword_hit: .7 }],
    diagnostics: { subset: { note: "Selected after failures; not comparable." } },
  };
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(/\/(workspace|evaluations|documentation)$/, async (route) => route.fulfill({ response: await route.fetch({ url: origin + "/" }) }));
  await tab.route(origin + "/config", (route) => route.fulfill({ json: {
    auth_required: true, supabase_url: project, supabase_anon_key: "sb_publishable_fixture", github_scopes: "public_repo",
  } }));
  await tab.route(project + "/auth/v1/user", (route) => route.fulfill({ json: { id: "fixture", user_metadata: { user_name: "fixture" } } }));
  await tab.route(origin + "/repos", (route) => route.fulfill({ json: [] }));
  await tab.route(origin + "/evals/latest", (route) => {
    requests.push(route.request().headers());
    if (result === "network") return route.abort();
    if (result === "404") return route.fulfill({ status: 404, json: { error: { code: "not_found", message: "No results" } } });
    return route.fulfill({ json: payload });
  });
  const visible = async (path) => {
    await tab.waitForFunction((path) => location.pathname === path && !document.querySelector(path === "/evaluations" ? "#evaluations-page" : "#documentation-page").hidden, path);
  };
  try {
    for (const signedIn of [false, true]) {
      await tab.goto(origin + "/documentation");
      await tab.evaluate(({ signedIn, project }) => {
        sessionStorage.clear();
        if (signedIn) sessionStorage.setItem("repomind.auth.session", JSON.stringify({ project_url: project,
          access_token: "fake-access", refresh_token: "fake-refresh", expires_at: Date.now() + 3600000 }));
      }, { signedIn, project });
      await tab.reload(); await visible("/documentation");
      await tab.waitForFunction((phase) => document.documentElement.dataset.authState === phase, signedIn ? "signed-in" : "signed-out");
      assert(await tab.title() === "RepoMind — Documentation", "Documentation title stale");
      assert(await tab.locator("#documentation-page a[href^='https://github.com']").count() === 0, "Documentation links to private repository");
      await tab.locator(".public-header nav").getByRole("link", { name: "Evaluations" }).click(); await visible("/evaluations");
      await tab.getByRole("heading", { name: "Fixture evaluation" }).waitFor();
      assert(await tab.locator(".eval-current").textContent().then((text) => text.includes("Current default")), "Current default not marked");
      assert(await tab.locator('.eval-table-scroll [data-invalid="true"]').textContent().then((text) => text.includes("invalid") && text.includes("Provider quota exhausted.")), "Invalid run note missing");
      await tab.goBack(); await visible("/documentation");
      await tab.goForward(); await visible("/evaluations");
      assert(await tab.title() === "RepoMind — Evaluations", "Evaluations title stale after Forward");
      await tab.reload(); await visible("/evaluations"); await tab.getByRole("heading", { name: "Fixture evaluation" }).waitFor();
      checks.push(`${signedIn ? "signed-in" : "signed-out"} direct pages, navigation, Back/Forward and reload`);
    }
    for (const width of [1440, 390, 375]) {
      await tab.setViewportSize({ width, height: 900 });
      for (const path of ["/evaluations", "/documentation"]) {
        await tab.goto(origin + path); await visible(path);
        if (path === "/evaluations") await tab.getByRole("heading", { name: "Fixture evaluation" }).waitFor();
        assert(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Page overflow at ${width} on ${path}`);
        if (path === "/evaluations") {
          assert(await tab.locator(".eval-stat").count() === 4, "Missing summary cards");
          assert(await tab.locator(".eval-chart").count() === 2, "Missing charts");
          assert(await tab.evaluate(() => document.querySelector(".eval-limits").getBoundingClientRect().top < document.querySelector("table").getBoundingClientRect().top), "Limits follows detailed tables");
          const columns = await tab.locator(".eval-stats").evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(" ").length);
          assert(columns === (width > 900 ? 4 : 2), "Unexpected card layout");
        } else if (width <= 900) {
          assert(await tab.locator(".documentation-toc").isHidden(), "Desktop contents visible on mobile");
          assert(await tab.locator(".documentation-toc-mobile").evaluate((node) => !node.open), "Mobile contents should start collapsed");
          await tab.locator(".documentation-toc-mobile summary").click();
          await tab.locator(".documentation-toc-mobile").getByRole("link", { name: "Worked example", exact: true }).click();
          assert(await tab.evaluate(() => location.pathname === "/documentation" && location.hash === "#docs-example"), "Contents link changed page");
          assert(await tab.locator("#docs-example").isVisible(), "Example anchor is not visible");
        } else {
          assert(await tab.locator(".documentation-toc-mobile").isHidden(), "Mobile contents visible on desktop");
          const sticky = await tab.locator(".documentation-toc").evaluate((node) => {
            window.scrollTo(0, 600);
            return { position: getComputedStyle(node).position, top: node.getBoundingClientRect().top };
          });
          assert(sticky.position === "sticky" && sticky.top >= 23 && sticky.top <= 25, "Desktop contents is not sticky");
        }
      }
      checks.push(`layout at ${width}px`);
    }
    for (const state of ["404", "network"]) {
      result = state; await tab.goto(origin + "/evaluations");
      await tab.getByText(state === "404" ? "No evaluation results yet." : "Could not load evaluation results. Please try again.", { exact: true }).waitFor();
      result = "success"; await tab.getByRole("button", { name: "Retry", exact: true }).click();
      await tab.getByRole("heading", { name: "Fixture evaluation" }).waitFor();
      checks.push(`${state} and retry`);
    }
    assert(requests.every((headers) => !headers.authorization && !headers["x-github-token"]), "Public request included credentials");
    assert(errors.length === 0, errors.join("\n"));
    return { passed: true, checks, scriptErrors: errors, serverFallback: "mocked" };
  } finally { await context.close(); }
})
