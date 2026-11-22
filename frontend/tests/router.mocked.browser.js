// Mocked auth and server fallback; no live OAuth, Supabase or backend requests.
(async (page) => {
  const context = await page.context().browser().newContext();
  const tab = await context.newPage();
  const origin = "http://127.0.0.1:8765", project = "https://auth.example.test";
  const errors = [], checks = [];
  const assert = (value, message) => { if (!value) throw Error(message); };
  await tab.addInitScript(() => {
    window.historyCalls = [];
    for (const method of ["pushState", "replaceState"]) {
      const original = history[method].bind(history);
      history[method] = (...args) => { window.historyCalls.push({ method, path: args[2] }); return original(...args); };
    }
  });
  tab.on("pageerror", (error) => errors.push(error.message));
  await tab.route(origin + "/workspace", async (route) => {
    // Simulate the separately owned server fallback using the same index.html.
    const response = await route.fetch({ url: origin + "/" });
    await route.fulfill({ response });
  });
  await tab.route(origin + "/config", (route) => route.fulfill({ json: {
    auth_required: true, supabase_url: project, supabase_anon_key: "sb_publishable_fixture", github_scopes: "public_repo",
  } }));
  await tab.route(project + "/auth/v1/user", (route) => route.fulfill({ json: { id: "user", user_metadata: { user_name: "fixture" } } }));
  await tab.route(project + "/auth/v1/logout?scope=local", (route) => route.fulfill({ status: 204 }));
  await tab.route(origin + "/repos", (route) => route.fulfill({ json: [] }));
  const settled = async (phase, path) => {
    await tab.waitForFunction(({ phase, path }) => document.documentElement.dataset.authState === phase && location.pathname === path, { phase, path }, { timeout: 10000 })
      .catch(async () => { throw Error(`Expected ${phase} at ${path}; observed ${await tab.evaluate(() => `${document.documentElement.dataset.authState} at ${location.pathname}`)} after ${checks.join(", ")}`); });
  };
  try {
    await tab.goto(origin + "/workspace"); await settled("signed-out", "/");
    assert(await tab.locator("#auth-screen").isVisible(), "Signed-out workspace guard failed");
    assert(await tab.title() === "RepoMind — Understand your code", "Wrong landing title");
    assert(await tab.evaluate(() => historyCalls.length === 1 && historyCalls[0].method === "replaceState" && historyCalls[0].path === "/"), "Signed-out redirect added history");
    checks.push("signed-out direct workspace visit");
    await tab.evaluate((project) => sessionStorage.setItem("repomind.oauth.pending", JSON.stringify({ project_url: project, created_at: Date.now() })), project);
    await tab.goto("about:blank"); // Simulate returning from an external provider, not a same-document hash change.
    await tab.goto(origin + "/#access_token=fake-access&refresh_token=fake-refresh&provider_token=fake-provider&expires_in=3600&token_type=bearer");
    await settled("signed-in", "/workspace");
    assert(await tab.locator("#app-shell").isVisible(), "Callback did not show app");
    assert(await tab.title() === "RepoMind — Workspace", "Wrong workspace title");
    assert(await tab.evaluate(() => location.hash === "" && JSON.parse(sessionStorage.getItem("repomind.auth.session")).provider_token === "fake-provider"), "Callback tokens not consumed before routing");
    assert(await tab.evaluate(() => historyCalls.length === 2 && historyCalls.every(call => call.method === "replaceState") && historyCalls[1].path === "/workspace"), "OAuth redirects must only replace history");
    checks.push("mock callback saved and stripped before workspace navigation");
    await tab.reload(); await settled("signed-in", "/workspace");
    // Seed a same-document landing entry, then traverse back to it while signed in.
    await tab.evaluate(() => { history.replaceState(null, "", "/"); history.pushState(null, "", "/workspace"); historyCalls.length = 0; });
    await tab.goBack();
    assert(await tab.evaluate(() => location.pathname === "/" && !document.querySelector("#auth-screen").hidden), "Back to / must render landing without redirecting");
    await tab.getByRole("button", { name: "Open workspace", exact: true }).waitFor();
    assert(await tab.title() === "RepoMind — Understand your code", "Back left a stale title");
    assert(await tab.evaluate(() => historyCalls.length === 0), "Signed-in popstate redirected");
    await tab.goForward(); await settled("signed-in", "/workspace");
    await tab.goBack();
    assert(await tab.evaluate(() => historyCalls.length === 0), "Back/Forward changed history");
    await tab.getByRole("button", { name: "Open workspace", exact: true }).click();
    await settled("signed-in", "/workspace");
    assert(await tab.evaluate(() => historyCalls.length === 1 && historyCalls[0].method === "pushState" && historyCalls[0].path === "/workspace"), "Open workspace must push its destination");
    checks.push("workspace reload, root nav and Back/Forward");
    await tab.locator(".topbar [data-sign-out]").click(); await settled("signed-out", "/");
    assert(await tab.title() === "RepoMind — Understand your code", "Sign-out title stale");
    await tab.goBack(); await settled("signed-out", "/");
    await tab.goForward(); await settled("signed-out", "/");
    assert(await tab.locator("#app-shell").isHidden(), "History exposed signed-out app");
    checks.push("sign-out and protected history");
    assert(errors.length === 0, errors.join("\n"));
    return { passed: true, checks, scriptErrors: errors, serverFallback: "mocked" };
  } finally { await context.close(); }
})
