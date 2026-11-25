// Static frontend on port 8765; no live auth or API requests.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage(), origin = "http://127.0.0.1:8765";
  const checks = [], errors = [];
  const assert = (value, message) => { if (!value) throw Error(message); };
  const payload = {
    meta: { title: "Fixture evaluation", model: "primary:fixture-model", repo: "fixture/repo", fast_top_k: 5, search_preview_lines: 60,
      golden_set: { questions: 32, answerable: 27, should_decline: 5 }, limits: ["One repository and one run.", "Groundedness is a proxy."] },
    agent: { models: ["primary:fixture-model", "primary:provider/second-model"], n_errors: 0, answer_rate: .9259, decline_accuracy: 1, groundedness: 1, error_rate: 0, file_recall: .85, keyword_hit: .87,
      latency_p50_ms: 8912, latency_p95_ms: 10736, avg_tokens: 2836.78 },
    history: [{ run: 1, valid: false, answered: null, note: "Provider quota exhausted." }, { run: 2, answered: "25/27", declined: "5/5" }],
    redteam: { attacks_blocked: 12, attacks_total: 12, false_positives: 0, benign_total: 8 },
    retrieval: { vector: { "recall@1": .7037, "recall@3": .9259, "recall@5": .9629, mrr: .8611 },
      keyword: { "recall@5": .3704 }, hybrid: { "recall@5": .8333 } },
    retrieval_ablation: { n_tune: 14, n_test: 13, vector_only: { "recall@5": .9629, mrr: .871, "held_out_recall@5": .9231, held_out_mrr: .8333 } },
  };
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(/\/(workspace|evaluations|documentation)$/, async (route) => route.fulfill({ response: await route.fetch({ url: origin + "/" }) }));
  await tab.route(origin + "/config", (route) => route.fulfill({ json: { auth_required: false } }));
  await tab.route(origin + "/repos", (route) => route.fulfill({ json: [] }));
  await tab.route(origin + "/evals/latest", (route) => route.fulfill({ json: payload }));
  const ready = async (path) => {
    await tab.goto(origin + path);
    if (path === "/evaluations") await tab.getByRole("heading", { name: "Fixture evaluation", exact: true }).waitFor();
    else await tab.locator(path === "/workspace" ? "#app-shell" : "#documentation-page").waitFor({ state: "visible" });
    await tab.evaluate(() => document.fonts.ready);
  };
  try {
    await ready("/documentation");
    await tab.evaluate(() => localStorage.removeItem("repomind.theme"));
    await tab.emulateMedia({ colorScheme: "dark" }); await tab.reload();
    assert(await tab.evaluate(() => document.documentElement.dataset.theme === "dark"), "System dark preference ignored");
    await tab.evaluate(() => localStorage.setItem("repomind.theme", "light")); await tab.reload();
    assert(await tab.evaluate(() => document.documentElement.dataset.theme === "light"), "Saved light did not override system dark");
    const toggle = tab.locator(".public-header [data-theme-toggle]");
    await toggle.focus(); await toggle.press("Enter");
    assert(await toggle.getAttribute("aria-label") === "Switch to light mode", "Toggle label stale");
    assert(await toggle.getAttribute("aria-pressed") === "true", "Toggle pressed state stale");
    assert(await tab.evaluate(() => localStorage.getItem("repomind.theme") === "dark"), "Theme not persisted");
    assert(await toggle.evaluate((node) => getComputedStyle(node).outlineStyle !== "none"), "Keyboard focus ring missing");
    await toggle.press("Space");
    assert(await toggle.getAttribute("aria-label") === "Switch to dark mode", "Space did not toggle theme");
    await tab.emulateMedia({ reducedMotion: "reduce" });
    assert(await tab.locator(".public-header .theme-disc").evaluate((node) => getComputedStyle(node).transitionDuration === "0s"), "Reduced motion ignored");
    checks.push("system preference, saved override, Enter/Space, persistence, focus and reduced motion");
    await tab.emulateMedia({ reducedMotion: "no-preference" });

    for (const width of [1440, 390, 375]) for (const theme of ["light", "dark"]) {
      await tab.setViewportSize({ width, height: 900 });
      await tab.evaluate((theme) => localStorage.setItem("repomind.theme", theme), theme);
      for (const path of ["/workspace", "/evaluations", "/documentation"]) {
        await ready(path);
        const button = tab.locator("[data-theme-toggle]:visible");
        assert(await button.count() === 1, `Expected one toggle at ${width}/${theme}/${path}`);
        assert(await button.getAttribute("aria-pressed") === String(theme === "dark"), "Wrong initial pressed state");
        assert(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Overflow at ${width}/${theme}/${path}`);
        assert(await button.locator("svg").count() === 1, "Missing SVG icon");
        if (path === "/evaluations") {
          assert((await tab.locator(".eval-meta dt").allTextContents()).join(",") === "Repository,Model,Fast top K,Search preview lines", "Unexpected metadata rows");
          assert(await tab.locator(".eval-meta dd").nth(1).textContent() === "fixture-model, provider/second-model", "Models not formatted consistently");
        }
        if (path === "/documentation") {
          const flow = await tab.locator(".documentation-flow").evaluate((node) => {
            const boxes = [...node.querySelectorAll("li > span")];
            return { label: node.getAttribute("aria-label"), boxes: boxes.map((box) => {
              const rect = box.getBoundingClientRect(), css = getComputedStyle(box);
              return { top: rect.top, left: rect.left, radius: parseFloat(css.borderRadius), background: css.backgroundColor };
            }) };
          });
          assert(flow.boxes.length === 5 && flow.boxes.every((box) => box.radius > 0), "Missing rounded flow boxes");
          assert(flow.label.includes("answer or decline"), "Diagram text alternative missing");
          assert(flow.boxes[0].background !== flow.boxes[4].background, "Final flow box lacks accent");
          assert(flow.boxes.every((box, index) => index === 0 || (width >= 700
            ? box.left > flow.boxes[index - 1].left && Math.abs(box.top - flow.boxes[0].top) < 1
            : box.top > flow.boxes[index - 1].top && Math.abs(box.left - flow.boxes[0].left) < 1)), "Flow direction does not match viewport");
        }
        if (path === "/evaluations" && width < 900) {
          const region = tab.getByRole("region", { name: "Retrieval results", exact: true });
          await tab.waitForFunction(() => [...document.querySelectorAll(".eval-table-scroll")].find((node) => node.getAttribute("aria-label") === "Retrieval results").parentElement.dataset.overflow === "true");
          assert(await region.evaluate((node) => getComputedStyle(node.parentElement, "::after").backgroundImage.includes("linear-gradient")), "Scroll hint missing");
          await region.focus(); await region.press("ArrowRight");
          await tab.waitForFunction(() => [...document.querySelectorAll(".eval-table-scroll")].find((node) => node.getAttribute("aria-label") === "Retrieval results").scrollLeft > 0);
          assert(await region.evaluate((node) => Math.abs(node.querySelector("tbody th").getBoundingClientRect().left - node.getBoundingClientRect().left) < 2), "First column is not sticky");
          await region.evaluate((node) => { node.scrollLeft = node.scrollWidth; });
          await tab.waitForFunction(() => [...document.querySelectorAll(".eval-table-scroll")].find((node) => node.getAttribute("aria-label") === "Retrieval results").parentElement.dataset.overflow === "false");
          await region.evaluate((node) => { node.scrollLeft = 0; });
          await tab.evaluate(() => window.scrollTo(0, 0));
        }
      }
      checks.push({ width, theme, views: 3 });
    }
    await ready("/evaluations");
    const contrast = await tab.evaluate(() => {
      const css = getComputedStyle(document.documentElement);
      const luminance = (hex) => {
        const rgb = hex.trim().slice(1).match(/../g).map((value) => parseInt(value, 16) / 255)
          .map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
        return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
      };
      const ratio = (first, second) => {
        const values = [first, second].map((name) => luminance(css.getPropertyValue(name))).sort((a, b) => b - a);
        return (values[0] + .05) / (values[1] + .05);
      };
      return { text: ratio("--text", "--page"), muted: ratio("--muted", "--surface"), primary: ratio("--primary", "--surface"),
        tableBorders: ratio("--divider", "--surface"), hatch: ratio("--divider", "--chip"), limits: ratio("--accent", "--surface") };
    });
    assert(contrast.text >= 4.5 && contrast.muted >= 4.5 && contrast.primary >= 4.5, "Insufficient dark text contrast");
    assert(contrast.tableBorders >= 3 && contrast.hatch >= 3 && contrast.limits >= 3, "Insufficient dark boundary contrast");
    await tab.route(origin + "/config", (route) => route.fulfill({ json: { auth_required: true,
      supabase_url: "https://auth.example.test", supabase_anon_key: "sb_publishable_fixture" } }));
    await tab.route("https://auth.example.test/auth/v1/user", (route) => route.fulfill({ json: {
      id: "fixture", user_metadata: { user_name: "a-long-fixture-account-name" },
    } }));
    await tab.evaluate(() => sessionStorage.setItem("repomind.auth.session", JSON.stringify({ project_url: "https://auth.example.test",
      access_token: "fake-access", refresh_token: "fake-refresh", expires_at: Date.now() + 3600000 })));
    for (const width of [1440, 390, 375]) for (const theme of ["light", "dark"]) {
      await tab.setViewportSize({ width, height: 900 });
      await tab.evaluate((theme) => localStorage.setItem("repomind.theme", theme), theme);
      await ready("/workspace");
      assert(await tab.locator("[data-theme-toggle]:visible").count() === 1, "Account hid the theme toggle");
      assert(await tab.locator(width > 900 ? ".topbar [data-sign-out]" : ".mobile-header [data-sign-out]").isVisible(), "Toggle hid sign-out");
      assert(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Signed-in header overflow");
    }
    checks.push("signed-in account and theme control fit all three widths in both themes");
    assert(errors.length === 0, errors.join("\n"));
    return { passed: true, checks, darkContrast: contrast, scriptErrors: errors, serverFallback: "mocked" };
  } finally { await context.close(); }
})
