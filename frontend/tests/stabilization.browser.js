// Browser regression suite. Run with Playwright MCP browser_run_code_unsafe,
// filename: frontend/tests/stabilization.browser.js, while frontend is served on 8765.
// Fixtures are isolated to a new browser context and never enter production code.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage();
  const origin = "http://127.0.0.1:8765";
  const project = "https://auth.example.test";
  const errors = [];
  const checks = [];
  const repo = (id, status = "ready") => ({ id, status, name: `fixture/${id}`, url: `https://github.com/fixture/${id}`, files_indexed: 8, chunks_indexed: 20 });
  let mode = "ready";
  let posts = 0;
  let refreshes = 0;
  let deletes = 0;
  let deleteStatus = 204;
  const deleted = new Set();
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(origin + "/config", (route) => route.fulfill({ json: {
    auth_required: true, supabase_url: project, supabase_anon_key: "sb_publishable_layout_test",
  } }));
  await tab.route(project + "/auth/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/user")) return route.fulfill({ json: { id: "layout-user", user_metadata: { user_name: "layout-user" } } });
    if (path.endsWith("/logout")) return route.fulfill({ status: 204 });
    if (path.endsWith("/token")) {
      refreshes += 1;
      return route.fulfill({ status: 401, json: {} });
    }
    return route.fulfill({ status: 404, json: {} });
  });
  await tab.route(origin + "/repos**", (route) => {
    const request = route.request();
    assert(request.headers().authorization === "Bearer fake-layout-access", "Wrong bearer token used");
    if (request.method() === "DELETE") {
      deletes += 1;
      assert(!request.headers()["x-github-token"], "Deletion sent the GitHub provider token");
      if (deleteStatus === 204) {
        deleted.add(decodeURIComponent(new URL(request.url()).pathname.split("/").pop()));
        return route.fulfill({ status: 204 });
      }
      return route.fulfill({ status: deleteStatus, json: { error: {
        code: deleteStatus === 409 ? "repo_not_ready" : deleteStatus === 403 ? "forbidden" : "not_found",
        message: "Server says <b>removal denied</b>.",
      } } });
    }
    if (request.method() === "POST") {
      posts += 1;
      assert(request.postDataJSON().url === "https://github.com/fixture/new", "Unexpected repository body");
      return route.fulfill({ status: 202, json: repo("new", "pending") });
    }
    if (mode === "network") return route.abort("connectionrefused");
    if (mode === "error") return route.fulfill({ status: 503, json: { error: { code: "unavailable", message: "Repository service temporarily unavailable." } } });
    if (mode === "unauthorized") return route.fulfill({ status: 401, json: { error: { code: "unauthorized", message: "Please sign in." } } });
    const path = new URL(request.url()).pathname;
    if (path !== "/repos") return route.fulfill({ json: repo(path.split("/").pop()) });
    return route.fulfill({ json: (mode === "empty" ? [] : mode === "indexing" ? [repo("pending", "indexing")] : [repo("repository"), repo("alternate")]).filter((item) => !deleted.has(item.id)) });
  });

  async function signIn() {
    await tab.evaluate(({ project }) => sessionStorage.setItem("repomind.auth.session", JSON.stringify({
      access_token: "fake-layout-access", refresh_token: "fake-layout-refresh", provider_token: "fake-layout-provider",
      expires_at: Date.now() + 3600000, project_url: project,
    })), { project });
    await tab.reload();
    await tab.locator("#app-shell").waitFor({ state: "visible" });
  }
  async function waitState(state) {
    await tab.waitForFunction((expected) => document.querySelector("[data-workspace-state]").dataset.workspaceState === expected, state);
  }
  async function dimensions() {
    return tab.evaluate(() => ({ width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight, composerBottom: document.querySelector(".composer").getBoundingClientRect().bottom,
      accountRightGap: innerWidth - document.querySelector(".header-account").getBoundingClientRect().right }));
  }

  try {
    for (const [width, height] of [[1440, 900], [1920, 1080], [390, 844], [375, 812], [1280, 600], [1024, 768]]) {
      await tab.setViewportSize({ width, height });
      await tab.goto(origin);
      await tab.evaluate(() => sessionStorage.clear());
      await tab.reload();
      await tab.getByRole("button", { name: "Continue with GitHub" }).waitFor();
      await tab.evaluate(() => document.fonts.ready);
      const landing = await dimensions();
      assert(landing.scrollWidth <= width, `Landing overflows horizontally at ${width}`);
      if (width >= 1440) assert(landing.scrollHeight <= height, `Landing scrolls at ${width}x${height}`);
      await tab.screenshot({ path: `.playwright-mcp/stabilization-after-landing-${width}.png`, fullPage: true });
      await signIn();
      await waitState("ready");
      const workspace = await dimensions();
      assert(workspace.scrollWidth <= width, `Workspace overflows horizontally at ${width}`);
      assert(await tab.locator(".answer, .citations, .feedback, .trace-steps").count() === 0, "Fabricated result containers remain");
      if (width > 900) {
        assert(workspace.scrollHeight <= height, `Desktop page scrolls at ${width}x${height}`);
        assert(workspace.composerBottom <= height && workspace.composerBottom > height - 80, "Composer is not anchored in the viewport");
        assert(Math.abs(workspace.accountRightGap - 16) < 1, "Account controls are not at the far right");
        const stress = await tab.evaluate(() => {
          const original = document.querySelector(".composer").getBoundingClientRect().bottom;
          for (const selector of [".conversation", ".trace", ".repositories"]) {
            const content = document.createElement("p");
            content.textContent = "Scroll containment regression content. ".repeat(1000);
            document.querySelector(selector).append(content);
          }
          return {
            contained: [".conversation", ".trace", ".repositories"].every((selector) => {
              const node = document.querySelector(selector);
              node.scrollTop = 200;
              return node.scrollHeight > node.clientHeight && node.scrollTop > 0;
            }),
            composerUnmoved: Math.abs(document.querySelector(".composer").getBoundingClientRect().bottom - original) < 1,
            pageFits: document.documentElement.scrollHeight <= innerHeight,
          };
        });
        assert(stress.contained && stress.composerUnmoved && stress.pageFits, "Long content escaped its scroll regions");
        await tab.reload();
        await waitState("ready");
      }
      await tab.screenshot({ path: `.playwright-mcp/stabilization-after-workspace-${width}.png`, fullPage: true });
      checks.push({ width, height, landing, workspace });
    }

    await tab.setViewportSize({ width: 1440, height: 900 });
    mode = "empty";
    await tab.reload(); await waitState("empty");
    assert(await tab.locator("#question").isDisabled(), "Question entry is enabled without a ready repository");
    assert((await tab.locator(".workspace [data-workspace-title]").textContent()).startsWith("Select a repository"), "Missing useful empty state");
    await tab.locator("#repo-url").fill("https://invalid.example/fixture/new");
    await tab.locator(".repositories .add-repo").click();
    assert(posts === 0, "Invalid URL reached the API");
    await tab.locator("#repo-url").fill("https://github.com/fixture/new");
    await tab.locator(".repositories .add-repo").click();
    await tab.locator(".repositories .status-pending").waitFor();
    assert(await tab.locator(".repositories .repo-select").isDisabled(), "Pending repository was selectable");
    await waitState("ready");
    assert(posts === 1, "Repository add did not execute once");

    mode = "ready";
    await tab.reload(); await waitState("ready");
    await tab.locator('.repositories [data-repo-id="alternate"]').click();
    await tab.setViewportSize({ width: 375, height: 812 });
    assert(await tab.locator("[data-selected-name]").textContent() === "fixture/alternate", "Mobile selection did not synchronize");
    await tab.reload(); await waitState("ready");
    assert(await tab.locator("[data-selected-name]").textContent() === "fixture/alternate", "Selection did not persist");

    for (const width of [1440, 375]) {
      await tab.setViewportSize({ width, height: 900 });
      deleted.clear();
      await tab.evaluate(() => localStorage.removeItem("repomind.selectedRepoId"));
      await tab.reload(); await waitState("ready");
      if (width < 900) await tab.locator("[data-open-repos]").click();
      const list = tab.locator(width > 900 ? ".repositories" : ".mobile-picker");
      const remove = list.getByRole("button", { name: "Remove fixture/repository", exact: true });
      const before = deletes;
      tab.once("dialog", async (dialog) => {
        assert(dialog.message() === "Remove fixture/repository? This deletes its index and history.", "Incorrect confirmation copy");
        await dialog.dismiss();
      });
      await remove.focus(); await tab.keyboard.press("Enter");
      assert(deletes === before, "Cancel sent a delete request");
      assert(await remove.isVisible(), "Cancel removed the repository");

      for (const status of [409, 403, 404]) {
        deleteStatus = status;
        tab.once("dialog", (dialog) => dialog.accept());
        await remove.focus(); await tab.keyboard.press("Space");
        const message = status === 409 ? "This repository is being indexed. Try again in a minute." : "Server says <b>removal denied</b>.";
        await list.locator(".repo-removal-message").filter({ hasText: message }).waitFor();
        assert(await remove.isVisible(), "Failed deletion removed the repository");
        assert(await list.locator(".repo-removal-message b").count() === 0, "Server error was interpreted as HTML");
      }

      deleteStatus = 204;
      // Removing an unselected repo keeps the selected workspace intact.
      tab.once("dialog", (dialog) => dialog.accept());
      await list.getByRole("button", { name: "Remove fixture/alternate", exact: true }).click();
      await list.getByRole("button", { name: "Remove fixture/alternate", exact: true }).waitFor({ state: "detached" });
      await waitState("ready");
      assert(await tab.locator("[data-selected-name]").textContent() === "fixture/repository", "Unselected removal cleared selection");
      deleted.clear();
      await tab.reload(); await waitState("ready");
      if (width < 900) await tab.locator("[data-open-repos]").click();
      tab.once("dialog", (dialog) => dialog.accept());
      await remove.focus(); await tab.keyboard.press("Enter");
      await remove.waitFor({ state: "detached" }); await waitState("empty");
      assert(await tab.evaluate(() => localStorage.getItem("repomind.selectedRepoId") === null), "Removed selection persisted");
      assert(await tab.locator("[data-selected-name]").textContent() === "Select a repository", "Selected repository name remained");
      assert(await tab.locator('[data-remove-repo-id="repository"]').count() === 0, "Desktop/mobile lists diverged");
      assert(await tab.locator('[data-remove-repo-id="alternate"]').count() === 2, "Remaining repository disappeared");
      assert(await list.getByRole("button", { name: "Remove fixture/alternate", exact: true }).evaluate((node) => node === document.activeElement), "Focus was lost after removal");
      assert(await tab.locator("#question").isDisabled(), "Removed repo still allowed questions");
    }
    deleted.clear();

    mode = "indexing";
    await tab.reload(); await waitState("empty");
    await waitState("ready");
    mode = "error";
    await tab.reload(); await waitState("error");
    assert(await tab.locator(".mobile-conversation [data-workspace-description]").textContent() === "Repository service temporarily unavailable.", "Actual API error was hidden");
    mode = "network";
    await tab.reload(); await waitState("error");
    assert((await tab.locator(".mobile-conversation [data-workspace-description]").textContent()).includes("Could not reach"), "Network error was hidden");
    mode = "ready";
    await tab.reload(); await waitState("ready");
    await tab.locator(".mobile-header [data-sign-out]").click();
    await tab.locator("#auth-screen").waitFor({ state: "visible" });
    assert(await tab.evaluate(() => sessionStorage.getItem("repomind.auth.session") === null), "Sign-out left a session behind");

    await signIn(); await waitState("ready");
    mode = "unauthorized";
    await tab.reload();
    await tab.locator("[data-auth-message]").filter({ hasText: "session ended" }).waitFor();
    assert(refreshes === 1, "Unauthorized requests did not use one refresh attempt");
    assert(await tab.locator("#app-shell").isHidden(), "Unauthenticated workspace remained visible");
    assert(errors.length === 0, "Unexpected browser JavaScript errors");
    return { passed: true, checks, repositoryAndAuthStates: "passed", scriptErrors: errors.length, verification: "mocked APIs, real browser rendering" };
  } finally { await context.close(); }
})
