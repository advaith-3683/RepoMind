import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../js/theme.js", import.meta.url), "utf8");
function fixture({ saved = null, dark = false, readFails = false, writeFails = false } = {}) {
  const events = {}, writes = [];
  const node = (tag) => ({ tag, attributes: {}, children: [], listeners: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    append(...items) { this.children.push(...items); },
    addEventListener(name, callback) { this.listeners[name] = callback; },
  });
  const buttons = [node("button"), node("button"), node("button")];
  const document = { documentElement: { dataset: {} }, querySelectorAll: () => buttons,
    createElementNS: (namespace, tag) => { assert.equal(namespace, "http://www.w3.org/2000/svg"); return node(tag); },
    addEventListener: (name, callback) => { events[name] = callback; },
  };
  let changed;
  const window = { matchMedia: (query) => {
    assert.equal(query, "(prefers-color-scheme: dark)");
    return { matches: dark, addEventListener: (_name, callback) => { changed = callback; } };
  } };
  const localStorage = {
    getItem(key) { assert.equal(key, "repomind.theme"); if (readFails) throw Error("Blocked"); return saved; },
    setItem(key, value) { if (writeFails) throw Error("Blocked"); writes.push({ key, value }); },
  };
  vm.runInNewContext(source, { document, window, localStorage });
  return { document, buttons, writes, initialize: () => events.DOMContentLoaded(), systemChange: (matches) => changed({ matches }) };
}

test("theme is chosen synchronously from saved preference before buttons initialize", () => {
  for (const saved of ["light", "dark"]) {
    const app = fixture({ saved, dark: saved === "light" });
    assert.equal(app.document.documentElement.dataset.theme, saved);
    assert.equal(app.buttons[0].children.length, 0);
  }
});

test("system preference is used when storage has no valid theme", () => {
  for (const saved of [null, "invalid", ""]) for (const dark of [true, false]) {
    const app = fixture({ saved, dark });
    assert.equal(app.document.documentElement.dataset.theme, dark ? "dark" : "light");
  }
});

test("click toggles theme, persists choice and synchronizes accessible state across headers", () => {
  const app = fixture(); app.initialize();
  for (const theme of ["dark", "light"]) {
    app.buttons[1].listeners.click();
    assert.equal(app.document.documentElement.dataset.theme, theme);
    for (const button of app.buttons) {
      assert.equal(button.attributes["aria-label"], theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
      assert.equal(button.attributes["aria-pressed"], String(theme === "dark"));
    }
    assert.deepEqual(app.writes.at(-1), { key: "repomind.theme", value: theme });
  }
});

test("initial button labels and pressed states reflect the applied dark theme", () => {
  const app = fixture({ dark: true }); app.initialize();
  assert.ok(app.buttons.every((button) => button.attributes["aria-label"] === "Switch to light mode" && button.attributes["aria-pressed"] === "true"));
});

test("blocked storage reads and writes do not prevent initialization or toggling", () => {
  const app = fixture({ dark: true, readFails: true, writeFails: true });
  assert.equal(app.document.documentElement.dataset.theme, "dark");
  assert.doesNotThrow(app.initialize);
  assert.doesNotThrow(() => app.buttons[0].listeners.click());
  assert.equal(app.document.documentElement.dataset.theme, "light");
  assert.equal(app.buttons[0].attributes["aria-pressed"], "false");
});

test("system changes apply until the user explicitly chooses a theme", () => {
  const app = fixture(); app.initialize(); app.systemChange(true);
  assert.equal(app.document.documentElement.dataset.theme, "dark");
  app.buttons[0].listeners.click(); app.systemChange(true);
  assert.equal(app.document.documentElement.dataset.theme, "light");
  const saved = fixture({ saved: "light" }); saved.initialize(); saved.systemChange(true);
  assert.equal(saved.document.documentElement.dataset.theme, "light");
});

test("each SVG uses a unique crescent mask and eight sun rays", () => {
  const app = fixture(); app.initialize();
  const flatten = (node) => [node, ...node.children.flatMap(flatten)];
  const masks = app.buttons.map((button) => flatten(button).find((node) => node.tag === "mask").attributes.id);
  assert.equal(new Set(masks).size, 3);
  app.buttons.forEach((button) => {
    assert.equal(flatten(button).filter((node) => node.tag === "line").length, 8);
    assert.equal(button.children[0].attributes["aria-hidden"], "true");
  });
});

test("blocking external theme script precedes the stylesheet and all toggles are native buttons", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.ok(html.indexOf('<script src="/js/theme.js"></script>') < html.indexOf('href="/styles.css"'));
  assert.equal((html.match(/<button class="theme-toggle" type="button" data-theme-toggle/g) || []).length, 3);
});
