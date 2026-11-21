"use strict";

import { initRepositories } from "./ui/repos.js";
import { createAuth } from "./auth.js";
import { renderWorkspaceState } from "./ui/workspace.js";

// Question submission stays disconnected until real chat integration is implemented.
const composer = document.querySelector(".composer");
const question = document.querySelector("#question");
const notice = document.querySelector(".composer-notice");

function explainPreview(event) {
  event.preventDefault();
  notice.textContent = "Question answering is not connected yet.";
}

composer.addEventListener("submit", explainPreview);
question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    explainPreview(event);
  }
});

// Mobile interactions affect only this local visual preview.
const mobileComposer = document.querySelector(".mobile-composer");
const mobileQuestion = document.querySelector("#mobile-question");
const mobileNotice = document.querySelector(".mobile-composer-notice");
const mobileViewport = window.matchMedia("(max-width: 900px)");
const narrowViewport = window.matchMedia("(max-width: 380px)");

function updateMobileLayout() {
  const shellHidden = document.querySelector("#app-shell").hidden;
  document.querySelector(".skip-link").href = shellHidden ? "#auth-title" : mobileViewport.matches ? "#mobile-workspace" : "#workspace";
  mobileQuestion.placeholder = "Ask about this repository…";
}
mobileViewport.addEventListener("change", updateMobileLayout);
narrowViewport.addEventListener("change", updateMobileLayout);
updateMobileLayout();

mobileQuestion.addEventListener("input", () => {
  document.querySelector(".character-count").textContent = `${mobileQuestion.value.length} / 1000 characters`;
});
mobileComposer.addEventListener("submit", (event) => {
  event.preventDefault();
  mobileNotice.textContent = "Question answering is not connected yet.";
});
document.querySelectorAll("[data-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-mode]").forEach((option) => {
      option.setAttribute("aria-pressed", String(option === button));
      option.textContent = `${option.dataset.mode === "fast" ? "Fast" : "Agent"}${option === button ? " ✓" : ""}`;
    });
    document.querySelector(".mode-description").textContent = button.dataset.mode === "fast"
      ? "Fast: 1 retrieval + 1 LLM call"
      : "Agent: multi-step search, can propose changes";
  });
});

const repositories = initRepositories({ autoLoad: false, onChange: renderWorkspaceState });
const auth = createAuth();
let workspaceActive = false;
auth.subscribe(({ phase, message, login }) => {
  const ready = phase === "signed-in" || phase === "local";
  document.documentElement.dataset.authState = phase;
  document.querySelector("#app-shell").hidden = !ready;
  document.querySelector("#auth-screen").hidden = ready;
  document.querySelector("[data-auth-message]").textContent = message;
  document.querySelector("[data-sign-in]").disabled = phase !== "signed-out";
  document.querySelector("[data-sign-in]").textContent = phase === "redirecting" ? "Opening GitHub…" : "Continue with GitHub";
  document.querySelector("[data-auth-retry]").hidden = phase !== "error";
  document.querySelectorAll("[data-user-login]").forEach((node) => { node.textContent = login; });
  document.querySelectorAll("[data-account]").forEach((node) => { node.hidden = phase === "local"; });
  if (ready && !workspaceActive) {
    workspaceActive = true;
    repositories.start();
  } else if (!ready && workspaceActive) {
    workspaceActive = false;
    repositories.stop();
    question.value = "";
    mobileQuestion.value = "";
    document.querySelector(".character-count").textContent = "0 / 1000 characters";
    notice.textContent = "";
    mobileNotice.textContent = "";
    document.querySelector("[data-sign-in]").focus();
  }
  updateMobileLayout();
});
document.querySelector("[data-sign-in]").addEventListener("click", auth.signIn);
document.querySelector("[data-auth-retry]").addEventListener("click", auth.initialize);
document.querySelectorAll("[data-sign-out]").forEach((button) => button.addEventListener("click", auth.signOut));
window.addEventListener("pageshow", (event) => {
  if (event.persisted) auth.initialize();
});
auth.initialize();
