import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRouter } from "../js/router.js";

function fixture(path = "/") {
  const events = {}, calls = [], views = [];
  let entries = [path], index = 0;
  const location = { origin: "https://repomind.example", pathname: path, hash: "" };
  const history = {};
  for (const method of ["pushState", "replaceState"]) history[method] = (_state, _title, url) => {
    calls.push({ method, url });
    location.pathname = url;
    if (method === "pushState") { entries = entries.slice(0, index + 1); entries.push(url); index++; }
    else entries[index] = url;
  };
  const browser = { location, history, addEventListener: (name, callback) => { events[name] = callback; } };
  const page = { title: "", addEventListener: (name, callback) => { events[name] = callback; } };
  const router = createRouter({ browser, page, onView: (view) => views.push(view) });
  function click(path, extra = {}) {
    let prevented = false;
    const link = { href: new URL(path, location.origin).href, hasAttribute: () => false };
    events.click({ button: 0, target: { closest: () => link }, preventDefault() { prevented = true; }, ...extra });
    return prevented;
  }
  function move(amount) {
    const next = index + amount;
    if (next < 0 || next >= entries.length) return;
    index = next; location.pathname = entries[index]; events.popstate();
  }
  return { router, location, page, calls, views, click, back: () => move(-1), forward: () => move(1) };
}

test("signed-out workspace visits redirect to the landing path and title", () => {
  const app = fixture("/workspace");
  app.router.setPhase("loading"); assert.equal(app.calls.length, 0);
  app.router.setPhase("signed-out");
  assert.equal(app.location.pathname, "/");
  assert.equal(app.page.title, "RepoMind — Understand your code");
  assert.equal(app.views.at(-1), "landing");
  assert.deepEqual(app.calls, [{ method: "replaceState", url: "/" }]);
});

test("restored signed-in sessions and local mode redirect landing visits to workspace", () => {
  for (const phase of ["signed-in", "local"]) {
    const app = fixture(); app.router.setPhase(phase);
    assert.equal(app.location.pathname, "/workspace");
    assert.equal(app.page.title, "RepoMind — Workspace");
    assert.equal(app.views.at(-1), "workspace");
  }
});

test("successful sign-in pushes workspace only after initialization finishes", () => {
  const app = fixture(); app.location.hash = "#access_token=fake-callback";
  app.router.setPhase("loading");
  assert.equal(app.calls.length, 0); assert.equal(app.location.hash, "#access_token=fake-callback");
  // The blocking callback script consumes the fragment; auth then validates the session.
  app.location.hash = "";
  app.router.setPhase("signed-out"); app.router.setPhase("redirecting"); app.router.setPhase("signed-in");
  assert.deepEqual(app.calls, [{ method: "pushState", url: "/workspace" }]);
  assert.equal(app.page.title, "RepoMind — Workspace");
});

test("Back and Forward reapply auth guards and titles without adding history entries", () => {
  const app = fixture(); app.router.setPhase("signed-out"); app.router.setPhase("signed-in");
  const pushes = () => app.calls.filter((call) => call.method === "pushState").length;
  const before = pushes();
  app.back();
  assert.equal(app.location.pathname, "/workspace"); assert.equal(app.views.at(-1), "workspace");
  assert.equal(app.page.title, "RepoMind — Workspace"); assert.equal(pushes(), before);
  app.router.setPhase("signed-out");
  assert.equal(app.location.pathname, "/"); assert.equal(app.views.at(-1), "landing");
  app.forward();
  assert.equal(app.location.pathname, "/"); assert.equal(app.page.title, "RepoMind — Understand your code");
  assert.equal(pushes(), before);
});

test("internal path links use pushState and apply the same auth guards", () => {
  const app = fixture(); app.router.setPhase("signed-out");
  assert.equal(app.click("/workspace"), true);
  assert.equal(app.location.pathname, "/");
  assert.deepEqual(app.calls, [{ method: "pushState", url: "/workspace" }, { method: "replaceState", url: "/" }]);
  app.router.setPhase("signed-in");
  assert.equal(app.click("/"), true); assert.equal(app.location.pathname, "/workspace");
});

test("modified clicks, external links and fragments retain native browser behavior", () => {
  const app = fixture(); app.router.setPhase("signed-out");
  for (const extra of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }, { defaultPrevented: true }]) {
    assert.equal(app.click("/workspace", extra), false);
  }
  for (const path of ["https://github.com/o/r", "/#auth-title", "/workspace?query=1", "/other"]) assert.equal(app.click(path), false);
  assert.equal(app.calls.length, 0);
});

test("entrypoint assets and navigation use root paths while OAuth still returns to root", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /src="\/js\/oauth-callback.js"/);
  assert.match(html, /src="\/js\/app.js"/);
  assert.match(html, /href="\/styles.css"/);
  assert.match(html, /href="\/workspace" data-route/);
  assert.ok(!html.includes('href="./"'));
  assert.ok(!html.includes("#/"));
  const auth = readFileSync(new URL("../js/auth.js", import.meta.url), "utf8");
  assert.ok(auth.includes('target.searchParams.set("redirect_to", `${location.origin}/`)'));
});
