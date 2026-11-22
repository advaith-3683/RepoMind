"use strict";

import { initRepositories } from "./ui/repos.js";
import { createAuth } from "./auth.js";
import { initChat } from "./ui/chat.js";
import { renderWorkspaceState } from "./ui/workspace.js";
import { createRouter } from "./router.js";

const question = document.querySelector("#question");
const mobileQuestion = document.querySelector("#mobile-question");
const notice = document.querySelector(".composer-notice");
const mobileNotice = document.querySelector(".mobile-composer-notice");
const mobileViewport = window.matchMedia("(max-width: 900px)");
function updateMobileLayout() {
  const shellHidden = document.querySelector("#app-shell").hidden;
  document.querySelector(".skip-link").href = shellHidden ? "#auth-title" : mobileViewport.matches ? "#mobile-workspace" : "#workspace";
}
mobileViewport.addEventListener("change", updateMobileLayout);
updateMobileLayout();

const chat = initChat();
const repositories = initRepositories({ autoLoad: false, onChange(state) {
  renderWorkspaceState(state);
  chat.setRepository(state);
} });
const auth = createAuth();
let workspaceActive = false;
const router = createRouter({ onView(view) {
  document.querySelector("#app-shell").hidden = view !== "workspace";
  document.querySelector("#auth-screen").hidden = view !== "landing";
  updateMobileLayout();
} });
auth.subscribe(({ phase, message, login }) => {
  const ready = phase === "signed-in" || phase === "local";
  document.documentElement.dataset.authState = phase;
  document.querySelector("[data-auth-message]").textContent = message;
  document.querySelector("[data-sign-in]").disabled = phase !== "signed-out";
  document.querySelector("[data-sign-in]").textContent = phase === "redirecting" ? "Opening GitHub…" : "Continue with GitHub";
  document.querySelector("[data-auth-retry]").hidden = phase !== "error";
  document.querySelectorAll("[data-user-login]").forEach((node) => { node.textContent = login; });
  document.querySelectorAll("[data-account]").forEach((node) => { node.hidden = phase === "local"; });
  router.setPhase(phase);
  if (ready && !workspaceActive) {
    workspaceActive = true;
    repositories.start();
  } else if (!ready && workspaceActive) {
    workspaceActive = false;
    repositories.stop();
    question.value = "";
    mobileQuestion.value = "";
    document.querySelectorAll("[data-character-count]").forEach((node) => { node.textContent = "0 / 1000 characters"; });
    notice.textContent = "";
    mobileNotice.textContent = "";
    document.querySelector("[data-sign-in]").focus();
  }
});
document.querySelector("[data-sign-in]").addEventListener("click", auth.signIn);
document.querySelector("[data-auth-retry]").addEventListener("click", auth.initialize);
document.querySelectorAll("[data-sign-out]").forEach((button) => button.addEventListener("click", auth.signOut));
window.addEventListener("pageshow", (event) => {
  auth.resetSignIn();
  if (event.persisted) auth.initialize();
});
auth.initialize();
