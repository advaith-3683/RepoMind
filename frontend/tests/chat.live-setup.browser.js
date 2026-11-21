// LIVE test setup: serve local frontend assets on the real production origin.
// Start `python -m http.server 8765 --bind 127.0.0.1 --directory frontend` first.
// All API/auth/repository responses remain REAL. The owner signs in manually.
// Explicit headers prevent conditional requests mixing cached production HTML
// with local modules after the OAuth redirect. No credentials are inspected.
(async (page) => {
  const origin = "https://repomind-gt4x.onrender.com";
  await page.unrouteAll();
  await page.route(origin + "/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/" || path === "/styles.css" || path.startsWith("/js/") || path.startsWith("/fonts/")) {
      const response = await route.fetch({ url: "http://127.0.0.1:8765" + path, headers: { Accept: "*/*" } });
      return route.fulfill({ status: response.status(), headers: {
        "Content-Type": response.headers()["content-type"] || "application/octet-stream", "Cache-Control": "no-store",
      }, body: await response.body() });
    }
    return route.continue();
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(origin);
  return { verification: "LIVE SETUP", note: "Local static assets, real Render API. Owner must sign in manually." };
})
