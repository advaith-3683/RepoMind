import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { workspaceView } from "../js/ui/workspace.js";

test("no selected repository produces a useful empty workspace", () => {
  const view = workspaceView({ loading: false, selectedRepo: null, repoCount: 0 });
  assert.equal(view.state, "empty");
  assert.equal(view.title, "Select a repository to start exploring your codebase.");
  assert.match(view.description, /Add a public GitHub repository/);
});

test("only an actually ready repository produces a ready state", () => {
  for (const status of ["pending", "cloning", "indexing", "failed"]) {
    assert.equal(workspaceView({ selectedRepo: { status }, repoCount: 1 }).state, "empty");
  }
  const view = workspaceView({ selectedRepo: { status: "ready" }, repoCount: 1 });
  assert.equal(view.state, "ready");
  assert.match(view.description, /Ask a question about your selected repository/);
});

test("loading does not display a stale ready state", () => {
  const view = workspaceView({ loading: true, selectedRepo: { status: "ready" } });
  assert.equal(view.state, "loading");
  assert.equal(view.title, "Loading repositories…");
});

test("actual API errors remain visible even if a repository was previously selected", () => {
  const message = "Repository service temporarily unavailable.";
  const view = workspaceView({ error: message, selectedRepo: { status: "ready" } });
  assert.equal(view.state, "error");
  assert.equal(view.description, message);
});

test("indexing repositories are not described as connected AI sessions", () => {
  const view = workspaceView({ selectedRepo: null, repoCount: 2 });
  assert.equal(view.state, "empty");
  assert.match(view.description, /still indexing/);
});

test("repository state never invents AI-result fields", () => {
  for (const input of [{}, { loading: true }, { error: "offline" }, { selectedRepo: { status: "ready" } }]) {
    assert.deepEqual(Object.keys(workspaceView(input)).sort(), ["description", "label", "state", "title"]);
  }
});

test("the shipped HTML contains no fabricated completed results", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  for (const phrase of ["How does authentication protect user transactions?", "Authentication verifies the current session",
    "auth.py", "routes.py", "Three verified steps", "Citation checks passed", "1,248 tokens", "342 tokens", "$0.002",
    "Sources verified", "CONFIDENCE: HIGH", "Was this useful?"]) {
    assert.equal(html.includes(phrase), false, `Fabricated result remains: ${phrase}`);
  }
  assert.ok(!html.includes('class="answer"'));
});
